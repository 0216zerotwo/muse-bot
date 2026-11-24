"""Comprehensive unit & integration tests for `muse.context`, `muse.streaming`, `muse.turn_actor`, and `ChatTurnEngine` (PR-4)."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from companion.store import ChatStore
from muse.context import (
    CancelScope,
    ContextCompactor,
    MEMORY_DELIMITER,
    PromptAssembler,
    TurnExecutionContext,
    build_compaction_event,
    get_turn_context,
    run_in_turn_thread,
    split_user_groups,
    turn_context_scope,
)
from muse.db import MuseDB
from muse.engine import AgentEngine, ChatTurnEngine
from muse.llm.types import (
    EncryptedReasoningItem,
    LLMRequestError,
    MessageCarrierEvent,
    ReasoningDelta,
    RetryStatusEvent,
    StreamDone,
    TextDelta,
    ThinkingBlockComplete,
    ToolCallComplete,
    UsageInfo,
)
from muse.paths import muse_home_override
from muse.turn_actor import (
    CANCELLED_TOOL_RESULT,
    CLARIFY_PLACEHOLDER,
    ChatSink,
    ChatTurnActor,
    DraftSegmentStreamer,
    format_clarify_card,
    translate_retry_status,
)

HISTORY_ID_RE = re.compile(r"^history-[0-9a-f]{20}$")


class RecordingChatSink(ChatSink):
    """Real `ChatStore`-backed sink separating live WS frames from `.jsonl` store writes."""

    def __init__(self, store: ChatStore, *, main_chat_id: str = "") -> None:
        self.store = store
        self.main_chat_id = main_chat_id
        self.ws_frames: list[dict[str, Any]] = []
        self.active_turns: dict[str, str] = {}
        self.active_turn_history: list[tuple[str, Optional[str]]] = []
        self.sent_media: list[tuple[str, str, Optional[str], str]] = []
        self.goal_pushes: list[str] = []
        self.pushed_notifications: list[tuple[str, str, str]] = []
        self.job_sources: dict[str, dict[str, Any]] = {}
        self.pick_card_calls = 0

    async def emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
        body = {**event, "chat_id": chat_id}
        if persist:
            body = self.store.append(chat_id, body)
        self.ws_frames.append(body)
        return body

    def append_store(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.store.append(chat_id, {**event, "chat_id": chat_id})

    def remove_store_events(self, chat_id: str, predicate: Any) -> int:
        return self.store.remove_events(chat_id, predicate)

    def store_events(self, chat_id: str) -> list[dict[str, Any]]:
        return self.store.events(chat_id)

    def ensure_chat(self, chat_id: str) -> None:
        self.store.ensure(chat_id)

    def set_active_turn(self, chat_id: str, turn_id: Optional[str]) -> None:
        self.active_turn_history.append((chat_id, turn_id))
        if turn_id is None:
            self.active_turns.pop(chat_id, None)
        else:
            self.active_turns[chat_id] = turn_id

    def get_active_turn(self, chat_id: str) -> Optional[str]:
        return self.active_turns.get(chat_id)

    def is_main_chat(self, chat_id: str) -> bool:
        return bool(self.main_chat_id and chat_id == self.main_chat_id)

    def transform_cards(self, chat_id: str, text: str, *, include_pick_card: bool = False) -> str:
        if include_pick_card and self.is_main_chat(chat_id):
            self.pick_card_calls += 1
        return text

    async def send_media_file(
        self, chat_id: str, path: str, *, caption: str = "",
        turn_id: Optional[str] = None, reply_context: str = "",
    ) -> bool:
        self.sent_media.append((chat_id, path, turn_id, reply_context))
        body: dict[str, Any] = {
            "event": "message", "text": caption,
            "media_urls": [{"url": f"/api/media/{Path(path).name}", "name": Path(path).name, "kind": "image"}],
        }
        if turn_id:
            body.update(turn_id=turn_id, turn_phase="answer")
        await self.emit(chat_id, body, persist=True)
        return True

    async def push_goal_state(self, chat_id: str) -> None:
        self.goal_pushes.append(chat_id)

    def source_for_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self.job_sources.get(job_id)

    def schedule_push(self, title: str, preview: str, *, tag: str = "") -> None:
        self.pushed_notifications.append((title, preview, tag))


class ContextAndCompactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse-ctx-")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_turn_execution_context_propagates_to_thread_and_isolates_tasks(self) -> None:
        async def run() -> None:
            def read_in_worker() -> tuple[str, str, Optional[str]]:
                ctx = get_turn_context()
                assert ctx is not None
                return ctx.chat_id, ctx.facing_chat_id, ctx.turn_id

            async def task_body(chat: str, facing: str, turn: str) -> tuple[str, str, Optional[str]]:
                ctx = TurnExecutionContext(
                    chat_id=chat, facing_chat_id=facing, session_id="s1", turn_id=turn,
                    actor_kind="helper", helper_id="h1", enabled_toolsets=("web",),
                )
                with turn_context_scope(ctx):
                    a = await asyncio.to_thread(read_in_worker)
                    b = await run_in_turn_thread(read_in_worker)
                    self.assertEqual(a, b)
                    return a

            r1, r2 = await asyncio.gather(task_body("c-1", "main", "t-1"), task_body("c-2", "c-2", "t-2"))
            self.assertEqual(r1, ("c-1", "main", "t-1"))
            self.assertEqual(r2, ("c-2", "c-2", "t-2"))
            self.assertIsNone(get_turn_context())

        asyncio.run(run())

    def test_cancel_scope_kills_spawned_child_process_tree(self) -> None:
        async def run() -> None:
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                scope = CancelScope()
                scope.register_pid(proc.pid)
                self.assertIsNone(proc.poll())
                scope.cancel("user_stop")
                self.assertTrue(scope.cancelled)
                self.assertIsNotNone(proc.wait(timeout=3.0))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)

        asyncio.run(run())

    def test_prompt_assembler_freezes_soul_per_session_and_loads_memory_skills_goal(self) -> None:
        (self.home / "SOUL.md").write_text("我是小缪 v1", encoding="utf-8")
        mem_dir = self.home / "memories"
        mem_dir.mkdir(parents=True)
        (mem_dir / "USER.md").write_text(f"喜欢拿铁{MEMORY_DELIMITER}住杭州", encoding="utf-8")
        (mem_dir / "MEMORY.md").write_text(f"周报周五交{MEMORY_DELIMITER}偏好简洁", encoding="utf-8")
        skill_dir = self.home / "skills"
        (skill_dir / "learned-trip").mkdir(parents=True)
        (skill_dir / "learned-trip" / "SKILL.md").write_text(
            "---\nname: trip-plan\ndescription: 安排周末出行\n---\n步骤详情", encoding="utf-8",
        )
        (skill_dir / "legacy-dev").mkdir(parents=True)
        (skill_dir / "legacy-dev" / "SKILL.md").write_text(
            "---\nname: legacy-dev\ndescription: 应被折叠\n---\nbody", encoding="utf-8",
        )
        (skill_dir / ".bundled_manifest").write_text("legacy-dev:deadbeef\n", encoding="utf-8")

        with MuseDB(self.home / "muse.db") as db:
            sid1 = db.create_session("c1")
            db.upsert_goal("c1", status="active", objective="整理 Q4 复盘", state={"ui_summary": "进行中"})
            asm = PromptAssembler()
            p1 = asm.assemble(session_id=sid1, chat_id="c1", db=db, home=self.home, plan_context="第一步：收集数据")
            self.assertIn("我是小缪 v1", p1)
            self.assertIn(f"喜欢拿铁{MEMORY_DELIMITER}住杭州", p1)
            self.assertIn("trip-plan", p1)
            self.assertNotIn("legacy-dev", p1)
            self.assertNotIn("步骤详情", p1)
            self.assertIn("整理 Q4 复盘", p1)
            self.assertIn("第一步：收集数据", p1)

            (self.home / "SOUL.md").write_text("我是小缪 v2", encoding="utf-8")
            p1_again = asm.assemble(session_id=sid1, chat_id="c1", db=db, home=self.home)
            self.assertIn("我是小缪 v1", p1_again)
            self.assertNotIn("我是小缪 v2", p1_again)
            sid2 = db.renew_session("c1")
            p2 = asm.assemble(session_id=sid2, chat_id="c1", db=db, home=self.home)
            self.assertIn("我是小缪 v2", p2)

    def test_tier1_prunes_old_tool_outputs_and_old_inline_images_preserving_tool_pairs(self) -> None:
        msgs: list[dict[str, Any]] = []
        for i in range(6):
            msgs.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"turn {i}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,img{i}"}},
                ],
            })
            msgs.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"tc_{i}", "type": "function", "function": {"name": "web_extract", "arguments": "{}"}}],
            })
            body = ("A" * 40_000) if i == 0 else ("B" * 2_000)
            msgs.append({"role": "tool", "tool_call_id": f"tc_{i}", "tool_name": "web_extract", "content": body})
            msgs.append({"role": "assistant", "content": f"done {i}"})

        res = ContextCompactor(tier1_threshold=1_000).tier1_prune(msgs)
        self.assertTrue(res.compacted)
        self.assertEqual(res.pruned_tool_outputs, 2)
        self.assertEqual(res.evicted_images, 2)
        tool_msgs = [m for m in res.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], [f"tc_{i}" for i in range(6)])
        self.assertTrue(tool_msgs[0]["content"].startswith("[Pruned web_extract output"))
        self.assertTrue(tool_msgs[1]["content"].startswith("[Pruned web_extract output"))
        self.assertEqual(tool_msgs[2]["content"], "B" * 2_000)
        user_msgs = [m for m in res.messages if m["role"] == "user"]
        self.assertEqual(user_msgs[0]["content"][1]["type"], "text")
        self.assertEqual(user_msgs[1]["content"][1]["type"], "text")
        for idx in range(2, 6):
            self.assertEqual(user_msgs[idx]["content"][1]["type"], "image_url")

    def test_tier2_compaction_user_boundary_same_session_and_strict_wire_schema(self) -> None:
        async def run() -> None:
            with MuseDB(self.home / "muse.db") as db:
                sid = db.create_session("c1")
                for i in range(6):
                    db.append_message(sid, "c1", "user", f"问题 {i} " + ("x" * 200), turn_id=f"t{i}")
                    db.append_message(
                        sid, "c1", "assistant", "", turn_id=f"t{i}",
                        tool_calls=[{"id": f"call_{i}", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
                    )
                    db.append_message(sid, "c1", "tool", f"结果 {i}", turn_id=f"t{i}", tool_call_id=f"call_{i}", tool_name="search")
                    db.append_message(sid, "c1", "assistant", f"回答 {i}", turn_id=f"t{i}")

                rows = db.get_messages(sid, active_only=True)
                emitted: list[dict[str, Any]] = []

                async def fake_summarizer(older: list[dict[str, Any]], prev: str) -> str:
                    del prev
                    self.assertEqual(older[0]["role"], "user")
                    self.assertEqual(older[-1]["role"], "assistant")
                    return "- 已汇总前 2 轮搜索与结论"

                res = await ContextCompactor().tier2_compact(
                    rows, db=db, session_id=sid, chat_id="c1", turn_id="t5",
                    context_window=200, summarizer=fake_summarizer,
                    emit_event=lambda ev: asyncio.sleep(0, result=emitted.append(ev)),
                )
                self.assertTrue(res.compacted)
                self.assertEqual(res.summary_checkpoint, "- 已汇总前 2 轮搜索与结论")
                sess = db.get_active_session("c1")
                assert sess is not None
                self.assertEqual(sess["id"], sid)
                self.assertEqual(sess["summary_checkpoint"], "- 已汇总前 2 轮搜索与结论")
                active_after = db.get_messages(sid, active_only=True)
                self.assertEqual(len(split_user_groups(active_after)), 4)
                self.assertEqual(active_after[0]["role"], "user")
                self.assertEqual([e["phase"] for e in emitted], ["started", "succeeded"])
                self.assertEqual(emitted[0]["compaction_id"], emitted[1]["compaction_id"])
                for e in emitted:
                    self.assertEqual(e["event"], "context_compaction")
                    self.assertEqual(e["chat_id"], "c1")
                    self.assertEqual(e["turn_id"], "t5")
                    self.assertEqual(e["turn_phase"], "activity")
                    self.assertIsInstance(e["compaction_id"], str)
                    self.assertTrue(len(e["compaction_id"]) > 0)

                with self.assertRaises(ValueError):
                    build_compaction_event("c1", "", "started")
                with self.assertRaises(ValueError):
                    build_compaction_event("c1", "cid-1", "bogus")
                self.assertEqual(build_compaction_event("c1", "cid-1", "start")["phase"], "started")
                self.assertEqual(build_compaction_event("c1", "cid-1", "end")["phase"], "succeeded")

        asyncio.run(run())


class DraftStreamerAndActorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse-actor-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = ChatStore(self.root / "webui")
        self.chat_id = self.store.create()
        self.sink = RecordingChatSink(self.store, main_chat_id=self.chat_id)
        self.db = MuseDB(self.root / "muse.db")
        self.addCleanup(self.db.close)

    def test_draft_streamer_holdback_prefix_rewrite_media_and_pre_tool_persistence(self) -> None:
        async def run() -> None:
            streamer = DraftSegmentStreamer(self.sink, self.chat_id, "t1")
            await streamer.push_delta("先看结果\n```card\n{\"type\": \"choices\"")
            ws_deltas = "".join(f["text"] for f in self.sink.ws_frames if f["event"] == "delta")
            self.assertTrue(ws_deltas.endswith("```card\n"))
            self.assertNotIn('"choices"', ws_deltas)
            self.assertEqual(self.store.events(self.chat_id), [])

            await streamer.push_full("我先查一下天气。\nMED")
            rewrites = [f for f in self.sink.ws_frames if f["event"] == "stream_end" and f.get("resuming") and f.get("merge_next")]
            self.assertEqual(len(rewrites), 1)
            self.assertNotIn("MED", rewrites[0]["text"])

            pre_text, media = await streamer.close_segment(final_raw="我先查一下天气。\nMEDIA:/tmp/chart.png", for_tools=True)
            self.assertEqual(pre_text, "我先查一下天气。")
            self.assertEqual(media, ["/tmp/chart.png"])
            self.assertEqual(streamer.draft_id, 2)

            # ZERO live WS `message` duplicates; ChatStore has persisted `message(turn_phase="answer")` with `history-<20hex>`!
            self.assertEqual([f for f in self.sink.ws_frames if f["event"] == "message"], [])
            stored = self.store.events(self.chat_id)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["event"], "message")
            self.assertEqual(stored[0]["turn_phase"], "answer")
            self.assertEqual(stored[0]["text"], "我先查一下天气。")
            self.assertRegex(stored[0]["projection_id"], HISTORY_ID_RE)
            self.assertEqual([e for e in stored if e["event"] in ("delta", "stream_end")], [])

            await streamer.flush_media(media, reply_context=pre_text)
            self.assertEqual(len(self.sink.sent_media), 1)
            self.assertEqual(self.sink.sent_media[0][1], "/tmp/chart.png")

            await streamer.push_delta("查到了，今天晴 24℃。")
            await streamer.close_segment(for_tools=False)
            self.assertGreaterEqual(self.sink.pick_card_calls, 1)
            answers = [
                e["text"] for e in self.store.events(self.chat_id)
                if e["event"] == "message" and e.get("turn_phase") == "answer" and not e.get("media_urls")
            ]
            self.assertEqual(answers, ["我先查一下天气。", "查到了，今天晴 24℃。"])

        asyncio.run(run())

    def test_p0_idle_stop_emits_turn_end_and_goal_controls_stay_silent(self) -> None:
        async def run() -> None:
            goal_cmds: list[tuple[str, str]] = []

            async def dummy_llm(**_: Any):
                yield StreamDone()

            actor = ChatTurnActor(
                self.chat_id, db=self.db, sink=self.sink, stream_llm=dummy_llm,
                on_goal_control=lambda c, cmd: asyncio.sleep(0, result=goal_cmds.append((c, cmd))),
            )
            self.assertEqual(await actor.submit("/stop", "webui-system:1111"), "")
            self.assertEqual(self.store.events(self.chat_id), [])
            self.assertEqual(len(self.sink.ws_frames), 1)
            self.assertEqual(self.sink.ws_frames[0]["event"], "turn_end")
            self.assertEqual(self.sink.ws_frames[0]["turn_phase"], "complete")
            self.assertNotIn("turn_id", self.sink.ws_frames[0])

            for cmd in ("/goal stop", "/goal pause", "/goal resume", "/goal clear"):
                await actor.submit(cmd, "webui-system:2222")
            self.assertEqual([c for _, c in goal_cmds], ["/goal stop", "/goal pause", "/goal resume", "/goal clear"])
            self.assertEqual(self.store.events(self.chat_id), [])

        asyncio.run(run())

    def test_p0_active_stop_cancels_tool_and_writes_synthetic_cancelled_tool_results(self) -> None:
        async def run() -> None:
            tool_started = asyncio.Event()

            async def llm_gen(**_: Any):
                yield TextDelta("正在执行命令")
                yield ToolCallComplete(id="tc-1", name="terminal", arguments='{"cmd":"sleep 30"}')
                yield ToolCallComplete(id="tc-2", name="terminal", arguments='{"cmd":"echo hi"}')
                yield StreamDone(finish_reason="tool_calls")

            async def slow_tool(tc: dict[str, Any], ctx: TurnExecutionContext) -> str:
                del tc, ctx
                tool_started.set()
                await asyncio.sleep(30)
                return "done"

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=llm_gen, execute_tool=slow_tool)
            await actor.submit("跑个长任务", "t-stop")
            await asyncio.wait_for(tool_started.wait(), timeout=2.0)

            self.assertEqual(await actor.submit("/stop", "webui-system:stop-1"), "t-stop")
            await actor.wait_idle(timeout=2.0)

            sid = self.db.get_active_session(self.chat_id)["id"]
            tool_rows = [m for m in self.db.get_messages(sid, active_only=True) if m["role"] == "tool"]
            self.assertEqual([r["tool_call_id"] for r in tool_rows], ["tc-1", "tc-2"])
            self.assertTrue(all(r["content"] == CANCELLED_TOOL_RESULT for r in tool_rows))
            stored = self.store.events(self.chat_id)
            self.assertEqual([e["text"] for e in stored if e["event"] == "user_message"], ["跑个长任务"])
            ends = [e for e in stored if e["event"] == "turn_end"]
            self.assertEqual(len(ends), 1)
            self.assertEqual(ends[0]["turn_id"], "t-stop")
            self.assertEqual(ends[0]["outcome"], "cancelled")

        asyncio.run(run())

    def test_p1_clarify_all_four_invariants_and_retry_turn_b_preserves_turn_a(self) -> None:
        async def run() -> None:
            llm_calls: list[list[dict[str, Any]]] = []

            async def llm_gen(messages: list[dict[str, Any]], **_: Any):
                llm_calls.append(messages)
                if len(llm_calls) == 1:
                    yield TextDelta("我先确认一下目的地。")
                    yield ToolCallComplete(
                        id="clarify-1",
                        name="clarify",
                        arguments=json.dumps({
                            "question": "周末想去哪玩？",
                            "choices": ["杭州西湖 (Recommended)", "苏州园林（推荐）", "莫干山"],
                            "multi_select": False,
                        }, ensure_ascii=False),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                elif len(llm_calls) == 2:
                    yield TextDelta("第一版杭州西湖行程。")
                    yield StreamDone()
                else:
                    yield TextDelta("重新生成的杭州西湖精选行程！")
                    yield StreamDone()

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=llm_gen)
            await actor.submit("帮我规划周末行程", "turn_A")
            for _ in range(50):
                if actor._phase == "clarify_wait":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(actor._phase, "clarify_wait")

            # Invariant 1
            stored_mid = self.store.events(self.chat_id)
            card_msgs = [e for e in stored_mid if e["event"] == "message" and "```card" in e.get("text", "")]
            self.assertEqual(len(card_msgs), 1)
            self.assertEqual(card_msgs[0]["turn_id"], "turn_A")
            card_json = json.loads(card_msgs[0]["text"].split("```card\n", 1)[1].split("\n```", 1)[0])
            self.assertEqual(card_json["type"], "choices")
            self.assertEqual(card_json["options"][0], {"label": "杭州西湖", "send": "杭州西湖 (Recommended)", "primary": True})
            self.assertEqual(card_json["options"][1], {"label": "苏州园林", "send": "苏州园林（推荐）", "primary": True})
            self.assertEqual(card_json["options"][2], {"label": "莫干山", "send": "莫干山", "primary": False})
            self.assertTrue(any(e["event"] == "turn_end" and e["turn_id"] == "turn_A" for e in stored_mid))

            # Invariant 2
            self.assertIsNone(actor.active_turn_id)
            self.assertIsNone(self.sink.get_active_turn(self.chat_id))
            sid = self.db.get_active_session(self.chat_id)["id"]
            tool_rows_mid = [m for m in self.db.get_messages(sid, active_only=True) if m["role"] == "tool"]
            self.assertEqual(len(tool_rows_mid), 1)
            self.assertEqual(tool_rows_mid[0]["content"], CLARIFY_PLACEHOLDER)

            # Invariant 3
            await actor.submit("杭州西湖 (Recommended)", "turn_B")
            await actor.wait_idle(timeout=2.0)
            stored_after = self.store.events(self.chat_id)
            canonical_user_turns = [e["turn_id"] for e in stored_after if e["event"] == "user_message" and e.get("starts_turn")]
            self.assertEqual(canonical_user_turns, ["turn_A", "turn_B"])
            self.assertEqual([e["text"] for e in stored_after if e["event"] == "message" and e.get("turn_id") == "turn_B"], ["第一版杭州西湖行程。"])
            self.assertEqual([e["turn_id"] for e in stored_after if e["event"] == "turn_end"], ["turn_A", "turn_B"])

            # Invariant 4
            await actor.regenerate("turn_B")
            await actor.wait_idle(timeout=2.0)
            stored_retried = self.store.events(self.chat_id)
            self.assertTrue(any(e["event"] == "message" and e.get("turn_id") == "turn_A" and "```card" in e.get("text", "") for e in stored_retried))
            self.assertEqual([e["text"] for e in stored_retried if e["event"] == "message" and e.get("turn_id") == "turn_B"], ["重新生成的杭州西湖精选行程！"])
            self.assertTrue(any(m.get("role") == "tool" and m.get("content") == "杭州西湖 (Recommended)" for m in llm_calls[2]))

        asyncio.run(run())

    def test_p1_slash_confirm_undo_soft_deletes_db_and_refreshes_preview(self) -> None:
        async def run() -> None:
            replies = iter(["第一轮回答", "第二轮要撤回的回答"])

            async def llm_gen(**_: Any):
                yield TextDelta(next(replies))
                yield StreamDone()

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=llm_gen)
            await actor.submit("第一问", "t1")
            await actor.wait_idle(timeout=2.0)
            await actor.submit("第二问", "t2")
            await actor.wait_idle(timeout=2.0)
            self.assertEqual(self.store.meta(self.chat_id)["preview"], "第二轮要撤回的回答")

            await actor.submit("/undo", "t3")
            self.assertIsNotNone(actor._pending_confirm)
            await actor.submit("/approve", "t4")
            self.assertEqual(self.store.meta(self.chat_id)["preview"], "第一轮回答")
            sid = self.db.get_active_session(self.chat_id)["id"]
            self.assertEqual([m["content"] for m in self.db.get_messages(sid, active_only=True)], ["第一问", "第一轮回答"])

        asyncio.run(run())

    def test_p2_steer_during_tools_vs_queue_during_answer_and_internal(self) -> None:
        async def run() -> None:
            in_tool, release_tool = asyncio.Event(), asyncio.Event()
            in_answer, release_answer = asyncio.Event(), asyncio.Event()
            seen_prompts: list[list[dict[str, Any]]] = []
            call_num = 0

            async def llm_gen(messages: list[dict[str, Any]], **_: Any):
                nonlocal call_num
                call_num += 1
                seen_prompts.append(messages)
                if call_num == 1:
                    yield TextDelta("开始查资料。")
                    yield ToolCallComplete(id="c1", name="search", arguments="{}")
                    yield StreamDone(finish_reason="tool_calls")
                elif call_num == 2:
                    yield TextDelta("收到补充，正在总结")
                    in_answer.set()
                    await release_answer.wait()
                    yield TextDelta("完成。")
                    yield StreamDone()
                else:
                    yield TextDelta(f"排队回合-{call_num}")
                    yield StreamDone()

            async def exec_tool(tc: dict[str, Any], ctx: TurnExecutionContext) -> str:
                del tc, ctx
                in_tool.set()
                await release_tool.wait()
                return "tool-ok"

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=llm_gen, execute_tool=exec_tool)
            await actor.submit("查一下周末天气", "t1")
            await asyncio.wait_for(in_tool.wait(), timeout=2.0)

            await actor.submit("顺便查一下紫外线", "t-steer")
            notices = [f for f in self.sink.ws_frames if (f.get("source") or {}).get("kind") == "notice"]
            self.assertEqual(notices[-1]["text"], "收到，我边做边按你说的来。")
            self.assertNotIn("turn_id", notices[-1])
            release_tool.set()

            await asyncio.wait_for(in_answer.wait(), timeout=2.0)
            self.assertTrue(any(m.get("role") == "user" and m.get("content") == "顺便查一下紫外线" for m in seen_prompts[1]))

            await actor.submit("下一件事", "t-q1")
            await actor.submit_internal("【系统通知】审批通过")
            self.assertEqual(len(actor.inbox), 2)
            assert actor._streamer is not None
            self.assertIsNotNone(actor._streamer.stream_id)
            release_answer.set()
            await actor.wait_idle(timeout=2.0)

            ends = [e["turn_id"] for e in self.store.events(self.chat_id) if e["event"] == "turn_end"]
            self.assertEqual(len(ends), 3)
            self.assertEqual(ends[:2], ["t1", "t-q1"])

        asyncio.run(run())

    def test_p3_cron_proactive_isolation_never_serializes_turn_id_or_clobbers_draft(self) -> None:
        async def run() -> None:
            streaming, release = asyncio.Event(), asyncio.Event()

            async def llm_gen(**_: Any):
                yield TextDelta("正在回答你的问题")
                streaming.set()
                await release.wait()
                yield TextDelta("，已完成。")
                yield StreamDone()

            self.sink.job_sources["morning-brief"] = {"kind": "routine", "label": "早报"}
            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=llm_gen)
            await actor.submit("你好", "t1")
            await asyncio.wait_for(streaming.wait(), timeout=2.0)

            await actor.submit("今天杭州多云 22℃", job_id="morning-brief", attach_to_session=True)
            assert actor._streamer is not None
            self.assertIsNotNone(actor._streamer.stream_id)
            proactive_ev = next(f for f in self.sink.ws_frames if f["event"] == "message" and f.get("text") == "今天杭州多云 22℃")
            self.assertNotIn("turn_id", proactive_ev)
            self.assertEqual(proactive_ev["source"]["label"], "早报")

            release.set()
            await actor.wait_idle(timeout=2.0)
            sid = self.db.get_active_session(self.chat_id)["id"]
            self.assertTrue(any(m["role"] == "assistant" and m["content"] == "今天杭州多云 22℃" for m in self.db.get_messages(sid)))
            payload = self.store.thread_payload(self.chat_id)
            self.assertTrue(all("turn_id" not in e or isinstance(e["turn_id"], str) for e in payload["events"]))

        asyncio.run(run())

    def test_retry_status_failure_error_kinds_overflow_tier3_and_silent_renew_session(self) -> None:
        async def run() -> None:
            attempt_no = 0

            async def overflow_then_ok(messages: list[dict[str, Any]], **_: Any):
                nonlocal attempt_no
                attempt_no += 1
                if attempt_no == 1:
                    yield RetryStatusEvent(state="retrying", attempt=1, max_attempts=3, delay_seconds=1.5, error_kind="rate_limit")
                    yield RetryStatusEvent(state="recovered", attempt=2, max_attempts=3, error_kind="rate_limit")
                    raise LLMRequestError("prompt too long", error_kind="server", status_code=413, is_context_overflow=True)
                yield TextDelta(f"压缩后成功(msgs={len(messages)})")
                yield StreamDone(usage=UsageInfo(input_tokens=100, output_tokens=20))

            sid = self.db.create_session(self.chat_id)
            self.db.append_message(sid, self.chat_id, "user", "很早的问题", turn_id="t0")
            self.db.append_message(sid, self.chat_id, "assistant", "很早的回答", turn_id="t0")

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=overflow_then_ok, model_name="gpt-5")
            await actor.submit("新问题", "t1")
            await actor.wait_idle(timeout=2.0)

            retry_frames = [f for f in self.sink.ws_frames if f["event"] == "retry_status"]
            self.assertEqual([f["state"] for f in retry_frames], ["waiting", "recovered"])
            self.assertEqual(retry_frames[0]["retry_after_s"], 1.5)
            self.assertEqual(retry_frames[0]["error_kind"], "rate_limit")
            cmp_frames = [f for f in self.sink.ws_frames if f["event"] == "context_compaction"]
            self.assertEqual([f["phase"] for f in cmp_frames], ["started", "succeeded"])
            self.assertTrue(any("压缩后成功" in e.get("text", "") for e in self.store.events(self.chat_id)))

            for kind in ("billing", "rate_limit", "timeout", "connection", "server"):
                async def failing_llm(_kind: str = kind, **_: Any):
                    if False:
                        yield TextDelta("")
                    raise LLMRequestError(f"boom {_kind}", error_kind=_kind, attempts=3)  # type: ignore[arg-type]

                actor.stream_llm = failing_llm
                before_notices = len([f for f in self.sink.ws_frames if (f.get("source") or {}).get("kind") == "notice"])
                await actor.submit(f"test {kind}", f"t-fail-{kind}")
                await actor.wait_idle(timeout=2.0)
                end_ev = [e for e in self.store.events(self.chat_id) if e["event"] == "turn_end" and e.get("turn_id") == f"t-fail-{kind}"][-1]
                self.assertEqual(end_ev["outcome"], "failed")
                self.assertEqual(end_ev["failure_kind"], "model")
                self.assertEqual(end_ev["failure_error_kind"], kind)
                self.assertEqual(end_ev["failure_attempts"], 3)
                after_notices = len([f for f in self.sink.ws_frames if (f.get("source") or {}).get("kind") == "notice"])
                self.assertEqual(after_notices, before_notices + 1)

            before_count = len(self.sink.ws_frames)
            old_sid = self.db.get_active_session(self.chat_id)["id"]
            actor.schedule_soul_renewal(delay_s=0.02)
            await asyncio.sleep(0.08)
            self.assertNotEqual(self.db.get_active_session(self.chat_id)["id"], old_sid)
            self.assertEqual(len(self.sink.ws_frames), before_count)

        asyncio.run(run())

    def test_format_clarify_card_and_translate_retry_status_edge_cases(self) -> None:
        card = format_clarify_card("选一个", ["选项A (Recommended)", "选项B（推荐）", "选项C"], multi_select=True)
        parsed = json.loads(card.split("```card\n", 1)[1].split("\n```", 1)[0])
        self.assertTrue(parsed["multiple"])
        self.assertEqual([o["primary"] for o in parsed["options"]], [True, True, False])

        wire = translate_retry_status(RetryStatusEvent("retrying", 2, 4, 2.345, "timeout"), chat_id="c1", turn_id="t9")
        self.assertEqual(wire, {
            "event": "retry_status", "chat_id": "c1", "turn_id": "t9",
            "state": "waiting", "attempt": 2, "max_attempts": 4, "error_kind": "timeout", "retry_after_s": 2.35,
        })

    def test_clarify_timeout_and_stop_during_clarify_never_emit_duplicate_turn_end(self) -> None:
        async def run() -> None:
            async def clarify_llm(**_: Any):
                yield ToolCallComplete(
                    id="clar-to", name="clarify",
                    arguments=json.dumps({"question": "选哪个？", "choices": ["A", "B"]}, ensure_ascii=False),
                )
                yield StreamDone(finish_reason="tool_calls")

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=clarify_llm, clarify_timeout_s=0.05)
            await actor.submit("帮我选", "t-clar-1")
            await actor.wait_idle(timeout=2.0)
            ends = [e for e in self.store.events(self.chat_id) if e["event"] == "turn_end" and e.get("turn_id") == "t-clar-1"]
            self.assertEqual(len(ends), 1)
            self.assertIsNone(actor.active_turn_id)

            actor2 = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=clarify_llm, clarify_timeout_s=30.0)
            await actor2.submit("再帮我选", "t-clar-2")
            for _ in range(50):
                if actor2._phase == "clarify_wait":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(actor2._phase, "clarify_wait")
            await actor2.submit("/stop", "webui-system:stop-clar")
            await actor2.wait_idle(timeout=2.0)
            ends2 = [e for e in self.store.events(self.chat_id) if e["event"] == "turn_end" and e.get("turn_id") == "t-clar-2"]
            self.assertEqual(len(ends2), 1)
            sid = self.db.get_active_session(self.chat_id)["id"]
            cancelled_rows = [m for m in self.db.get_messages(sid, active_only=True) if m["role"] == "tool" and m["content"] == CANCELLED_TOOL_RESULT]
            self.assertEqual(len(cancelled_rows), 1)

        asyncio.run(run())

    def test_slash_confirm_new_cancel_manual_compact_and_partial_stream_failure_no_extra_notice(self) -> None:
        async def run() -> None:
            async def ok_llm(**_: Any):
                yield TextDelta("普通回复")
                yield StreamDone()

            async def fake_sum(older: list[dict[str, Any]], prev: str) -> str:
                del older, prev
                return "- 手动压缩摘要"

            actor = ChatTurnActor(self.chat_id, db=self.db, sink=self.sink, stream_llm=ok_llm, compactor=ContextCompactor(summarizer=fake_sum))
            await actor.submit("第一句", "t1")
            await actor.wait_idle(timeout=2.0)
            await actor.submit("第二句", "t2")
            await actor.wait_idle(timeout=2.0)

            await actor.submit("/undo", "t3")
            await actor.submit("/cancel", "t4")
            self.assertEqual(self.store.meta(self.chat_id)["preview"], "好，取消了。")
            sid_before = self.db.get_active_session(self.chat_id)["id"]
            self.assertEqual(len(self.db.get_messages(sid_before, active_only=True)), 4)

            await actor.submit("/compact", "t5")
            cmp_events = [e for e in self.store.events(self.chat_id) if e["event"] == "context_compaction"]
            self.assertEqual([e["phase"] for e in cmp_events], ["started", "succeeded"])
            self.assertEqual(self.db.get_active_session(self.chat_id)["summary_checkpoint"], "- 手动压缩摘要")

            await actor.submit("/new", "t6")
            await actor.submit("/approve", "t7")
            self.assertNotEqual(self.db.get_active_session(self.chat_id)["id"], sid_before)

            async def partial_fail_llm(**_: Any):
                yield TextDelta("已输出一半")
                raise LLMRequestError("connection reset", error_kind="connection", attempts=2)

            actor.stream_llm = partial_fail_llm
            notices_before = len([f for f in self.sink.ws_frames if (f.get("source") or {}).get("kind") == "notice"])
            await actor.submit("试一下断连", "t8")
            await actor.wait_idle(timeout=2.0)
            notices_after = len([f for f in self.sink.ws_frames if (f.get("source") or {}).get("kind") == "notice"])
            self.assertEqual(notices_after, notices_before)
            end_t8 = [e for e in self.store.events(self.chat_id) if e["event"] == "turn_end" and e.get("turn_id") == "t8"][-1]
            self.assertEqual(end_t8["failure_error_kind"], "connection")

            with self.assertRaises(ValueError) as cm:
                await actor.regenerate("t1")
            self.assertEqual(str(cm.exception), "只能重新生成最后一条回复")

        asyncio.run(run())

    def test_streamer_holds_back_json_and_ui_fences_and_emits_non_streamed_card(self) -> None:
        async def run() -> None:
            streamer = DraftSegmentStreamer(self.sink, self.chat_id, "t-fence")
            await streamer.push_delta("前文\n```ui\n<html><body>half")
            ws_text = "".join(f["text"] for f in self.sink.ws_frames if f["event"] == "delta")
            self.assertTrue(ws_text.endswith("```ui\n"))
            self.assertNotIn("<html>", ws_text)
            final_text, _ = await streamer.close_segment(final_raw="前文\n```ui\n<html><body>ok</body></html>\n```")
            self.assertIn("<html><body>ok</body></html>", final_text)

            streamer2 = DraftSegmentStreamer(self.sink, self.chat_id, "t-card-only")
            await streamer2.close_segment(final_raw='```card\n{"type":"choices","options":[]}\n```')
            ws_msgs = [f for f in self.sink.ws_frames if f["event"] == "message" and f.get("turn_id") == "t-card-only"]
            self.assertEqual(len(ws_msgs), 1)
            self.assertRegex(ws_msgs[0]["projection_id"], HISTORY_ID_RE)

        asyncio.run(run())


class RealAdapterAndEngineIntegrationTest(unittest.TestCase):
    """End-to-end integration tests wiring real `CompanionAdapter` (`ChatSink`), `ChatTurnEngine`, `MuseDB`, and `muse.llm`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse-e2e-")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _make_real_adapter(self) -> tuple[Any, list[dict[str, Any]]]:
        comp_tests = Path(__file__).resolve().parents[2] / "companion" / "tests"
        if str(comp_tests) not in sys.path:
            sys.path.insert(0, str(comp_tests))
        import _loader

        _loader.ensure_hermes_stubs()
        mod_name = "companion_pr4_adapter_test"
        comp_root = Path(__file__).resolve().parents[2] / "companion"
        if mod_name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                mod_name, comp_root / "__init__.py", submodule_search_locations=[str(comp_root)],
            )
            assert spec is not None
            sys.modules[mod_name] = importlib.util.module_from_spec(spec)
        adapter_mod = importlib.import_module(f"{mod_name}.adapter")
        from companion.media import MediaStore

        adapter = object.__new__(adapter_mod.CompanionAdapter)
        webui = self.home / "companion"
        adapter.store = ChatStore(webui)
        adapter.media = MediaStore(webui)
        adapter._subs = {}
        adapter._turns = {}
        adapter._chain_root = {}
        adapter._inband_active = set()
        adapter._pending_turns = {}
        adapter._turn_output = set()
        adapter._drafts = {}
        adapter._last_reply = {}
        adapter._turn_idle = {}
        adapter._internal_locks = {}
        ws_sent: list[dict[str, Any]] = []

        class FakeSocket:
            async def send_json(self, payload: dict[str, Any]) -> None:
                ws_sent.append(payload)

        chat_id = adapter.store.create()
        adapter.attach(chat_id, FakeSocket())
        return (adapter, chat_id), ws_sent

    def test_real_companion_adapter_as_chat_sink_with_chat_turn_engine(self) -> None:
        async def run() -> None:
            with muse_home_override(self.home), MuseDB(self.home / "muse.db") as db:
                (adapter, chat_id), ws_sent = self._make_real_adapter()
                self.assertIsInstance(adapter, ChatSink)

                calls = 0

                async def fake_stream(**_: Any):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        yield TextDelta("先查一下再问你。")
                        yield ToolCallComplete(
                            id="cl-1", name="clarify",
                            arguments=json.dumps({"question": "喝什么？", "choices": ["拿铁 (Recommended)", "美式"]}, ensure_ascii=False),
                        )
                        yield StreamDone(finish_reason="tool_calls")
                    elif calls == 2:
                        yield TextDelta("第一版拿铁推荐。")
                        yield StreamDone()
                    else:
                        yield TextDelta("重新生成的拿铁推荐！")
                        yield StreamDone()

                engine = ChatTurnEngine(adapter, db=db, stream_llm=fake_stream)
                self.assertIsInstance(engine, AgentEngine)
                adapter._engine = engine

                # Idle /stop emits turn_end immediately without polluting ChatStore
                await adapter.submit(chat_id, "/stop", "webui-system:idle-1")
                self.assertEqual(adapter.store.events(chat_id), [])
                self.assertEqual(ws_sent[-1]["event"], "turn_end")

                # Clarify across turn_A -> turn_B -> regenerate(turn_B) via real CompanionAdapter!
                with patch("companion_pr4_adapter_test.adapter.router.prefetch", lambda *a, **k: None):
                    await adapter.submit(chat_id, "推荐一杯咖啡", "turn_A")
                    actor = engine.get_actor(chat_id)
                    for _ in range(50):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNone(adapter.get_active_turn(chat_id))
                    # Pre-tool text persisted to .jsonl without live WS duplicate `message`
                    ws_pre_msgs = [f for f in ws_sent if f["event"] == "message" and f.get("text") == "先查一下再问你。"]
                    self.assertEqual(ws_pre_msgs, [])
                    self.assertTrue(any(e.get("text") == "先查一下再问你。" for e in adapter.store.events(chat_id)))

                    await adapter.submit(chat_id, "拿铁 (Recommended)", "turn_B")
                    await actor.wait_idle(timeout=2.0)
                    await adapter.regenerate(chat_id, "turn_B")
                    await actor.wait_idle(timeout=2.0)

                payload = adapter.store.thread_payload(chat_id)
                self.assertTrue(any(e.get("turn_id") == "turn_A" and "```card" in e.get("text", "") for e in payload["events"]))
                self.assertTrue(any(e.get("turn_id") == "turn_B" and e.get("text") == "重新生成的拿铁推荐！" for e in payload["events"]))

        asyncio.run(run())

    def test_chat_turn_engine_default_llm_transport_persists_signatures_and_usage(self) -> None:
        async def run() -> None:
            with muse_home_override(self.home), MuseDB(self.home / "muse.db") as db:
                store = ChatStore(self.home / "webui")
                chat_id = store.create()
                sink = RecordingChatSink(store, main_chat_id=chat_id)

                class FakeTransport:
                    async def stream(self, **kw: Any):
                        self.last_kw = kw
                        yield ReasoningDelta(text="思考中")
                        yield ThinkingBlockComplete(block_type="thinking", thinking="思考中", signature="sig-123")
                        yield EncryptedReasoningItem(item_type="reasoning", encrypted_content="enc-abc", issuer="openai-codex", model="gpt-5")
                        yield MessageCarrierEvent(extra_content={"google": {"thought_signature": "gsig-9"}})
                        yield TextDelta("最终答案")
                        yield StreamDone(
                            finish_reason="stop",
                            usage=UsageInfo(input_tokens=120, output_tokens=30, cache_read_tokens=40, reasoning_tokens=15),
                        )

                fake_transport = FakeTransport()
                fake_runtime = {
                    "provider": "m-main", "model": "claude-sonnet-4-5",
                    "base_url": "https://api.anthropic.com", "api_key": "sk-test",
                    "api_mode": "anthropic_messages", "extra_body": {}, "max_tokens": 2048,
                    "temperature": 0.3, "extra_headers": {}, "context_length": 200_000,
                }
                with (
                    patch("muse.llm.providers.resolve_runtime_provider", return_value=fake_runtime),
                    patch("muse.llm.auxiliary.create_transport", return_value=fake_transport),
                ):
                    engine = ChatTurnEngine(sink, db=db)
                    await engine.start()
                    await engine.submit_turn(chat_id, "测试端到端", "t-e2e")
                    await engine.get_actor(chat_id).wait_idle(timeout=2.0)
                    await engine.stop()

                sid = db.get_active_session(chat_id)["id"]
                asst_rows = [m for m in db.get_messages(sid, active_only=True) if m["role"] == "assistant"]
                self.assertEqual(len(asst_rows), 1)
                self.assertEqual(asst_rows[0]["content"], "最终答案")
                self.assertEqual(asst_rows[0]["extra"]["reasoning_details"][0]["signature"], "sig-123")
                self.assertEqual(asst_rows[0]["extra"]["codex_reasoning_items"][0]["encrypted_content"], "enc-abc")
                self.assertEqual(asst_rows[0]["extra"]["extra_content"]["google"]["thought_signature"], "gsig-9")
                usage_rows = db.query_usage_since(0.0)
                self.assertEqual(len(usage_rows), 1)
                self.assertEqual(usage_rows[0][0], "claude-sonnet-4-5")
                self.assertEqual(usage_rows[0][5], 160)  # input (120) + cache_read (40)
                self.assertEqual(usage_rows[0][6], 30)   # output
                self.assertEqual(usage_rows[0][7], 40)   # cache_read

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
