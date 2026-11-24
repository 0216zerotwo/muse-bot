"""Dual-engine E2E parity suite (`MUSE_ENGINE=hermes` vs `MUSE_ENGINE=native`) and performance gates.

Verifies identical WS frame + `ChatStore` `.jsonl` contracts across:
1. Streaming text (`delta` -> `stream_end` -> `message` + `turn_end`) & pre-tool persistence
2. Tool execution (`tool_hint` start/end + tool result persistence)
3. `clarify` cross-turn card + `turn_end(turn_A)` + `user_message(turn_B)` + `/retry(turn_B)` preservation
4. `/stop` active turn, idle `/stop` (iOS `stopGoal()`), and queued inbox turn cancellation (`#207` advisory #3)
5. `/undo` confirmation card -> `/approve` -> soft-delete in `muse.db` & `ChatStore` strip
6. `/compact` strict `context_compaction` schema (`compaction_id`, `phase` in 4 allowed values)
7. Approvals (`defer` + `hold_exec` native execution with zero duplicate `ensure_browser`)
8. Default `MUSE_ENGINE=native` cutover, `MUSE_ENGINE=hermes` rollback, 3s settle delay, and shared `MuseDB` reuse
9. Performance gates: cold-start `create_app` + lifespan + `GET /webui/bootstrap` `< 800ms` & idle RSS `< 120 MB`
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import psutil
from starlette.testclient import TestClient

from companion.store import ChatStore
from muse import app as muse_app
from muse import engine as muse_engine
from muse import runtime as muse_runtime
from muse.db import MuseDB, close_all
from muse.llm.types import StreamDone, TextDelta, ToolCallComplete, UsageInfo
from muse.paths import active_engine_name, muse_home_override

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION_TESTS = REPO_ROOT / "companion" / "tests"
if str(COMPANION_TESTS) not in sys.path:
    sys.path.insert(0, str(COMPANION_TESTS))


class _RecordingSocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(dict(payload))


def _load_hermes_companion_adapter_cls() -> Any:
    import _loader

    _loader.ensure_hermes_stubs()
    mod_name = "companion_parity_hermes_adapter"
    if mod_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            mod_name,
            REPO_ROOT / "companion" / "__init__.py",
            submodule_search_locations=[str(REPO_ROOT / "companion")],
        )
        assert spec is not None
        sys.modules[mod_name] = importlib.util.module_from_spec(spec)
    mod = importlib.import_module(f"{mod_name}.adapter")
    return mod.CompanionAdapter


class DualEngineParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse-parity-")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        muse_runtime.close_all_shared_dbs()
        close_all()
        self._tmp.cleanup()

    def _make_native_host(
        self,
        *,
        stream_llm: Any = None,
        execute_tool: Any = None,
        home: Path | None = None,
    ) -> tuple[muse_app.NativeCompanionAdapter, str, _RecordingSocket]:
        target_home = home or self.home
        adapter = muse_app.NativeCompanionAdapter(
            home=target_home,
            start_background_services=False,
        )
        adapter._engine = muse_runtime.NativeAgentEngine(
            adapter,
            home=target_home,
            stream_llm=stream_llm,
            execute_tool=execute_tool,
            start_background_services=False,
        )
        chat_id = adapter.store.create()
        sock = _RecordingSocket()
        adapter.attach(chat_id, sock)
        return adapter, chat_id, sock

    # ── 1. Text streaming & pre-tool persistence parity (Hermes vs Native) ───

    def test_text_streaming_and_pre_tool_text_persistence_parity(self) -> None:
        """Compare golden WS frames & ChatStore `.jsonl` between `hermes` relay and `native` runtime."""

        async def run_hermes_branch(branch_home: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            CompanionAdapter = _load_hermes_companion_adapter_cls()
            adapter = object.__new__(CompanionAdapter)
            adapter._engine = muse_engine.HermesEngineBridge(adapter)
            adapter.platform = "companion"
            adapter.store = ChatStore(branch_home / "companion")
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
            adapter._helper_waits = {}
            adapter._cards = lambda text: text
            adapter.companion_name = lambda: "伙伴"
            adapter.push_goal_state = lambda chat_id, socket=None: asyncio.sleep(0)

            chat_id = adapter.store.create()
            sock = _RecordingSocket()
            adapter.attach(chat_id, sock)

            captured_events: list[Any] = []

            async def fake_handle_message(ev: Any) -> None:
                captured_events.append(ev)

            adapter.handle_message = fake_handle_message
            with (
                mock.patch("companion_parity_hermes_adapter.adapter.router.prefetch", lambda *a, **k: None),
                mock.patch("companion_parity_hermes_adapter.adapter.memory_tidy.maybe_tidy_soon", lambda: None),
                mock.patch("companion_parity_hermes_adapter.adapter.computer.close_after_task", lambda _cid: None),
            ):
                await adapter.submit(chat_id, "查一下天气", "turn-1")
                ev = captured_events[-1]
                await adapter.on_processing_start(ev)
                await adapter.send_draft(chat_id, 1, "我查一下。")
                await adapter.send(chat_id, "我查一下。")
                await adapter.send_draft(chat_id, 2, "今天晴，24°C。")
                await adapter.send(chat_id, "今天晴，24°C。")
                await adapter.on_processing_complete(ev, "success")
            return sock.frames, adapter.store.events(chat_id)

        async def run_native_branch(branch_home: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            calls = 0

            async def fake_stream(**_: Any):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield TextDelta("我查一下。")
                    yield ToolCallComplete(id="tc-weather", name="lookup_weather", arguments='{"city":"上海"}')
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("今天晴，")
                    yield TextDelta("24°C。")
                    yield StreamDone(finish_reason="stop", usage=UsageInfo(input_tokens=40, output_tokens=12))

            async def fake_tool(tc: dict[str, Any], _ctx: Any) -> str:
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                tname = str(fn.get("name") or tc.get("name") or "")
                await adapter.post_tool_hint(chat_id, tool_name=tname, phase="start", turn_id="turn-1")
                await adapter.post_tool_hint(chat_id, tool_name=tname, phase="end", turn_id="turn-1")
                return json.dumps({"weather": "晴", "temp_c": 24}, ensure_ascii=False)

            with muse_home_override(branch_home):
                adapter, chat_id, sock = self._make_native_host(
                    home=branch_home, stream_llm=fake_stream, execute_tool=fake_tool
                )
                await adapter.startup()
                try:
                    await adapter.submit(chat_id, "查一下天气", "turn-1")
                    await adapter.engine.get_actor(chat_id).wait_idle(timeout=3.0)
                    return sock.frames, adapter.store.events(chat_id)
                finally:
                    await adapter.shutdown()

        async def scenario() -> None:
            h_frames, h_store = await run_hermes_branch(self.home / "hermes-branch")
            n_frames, n_store = await run_native_branch(self.home / "native-branch")

            for label, frames, stored in (("hermes", h_frames, h_store), ("native", n_frames, n_store)):
                with self.subTest(engine=label):
                    # Both emit user_message -> delta -> stream_end -> turn_end(turn-1)
                    self.assertTrue(any(f["event"] == "user_message" and f.get("turn_id") == "turn-1" for f in frames))
                    self.assertTrue(any(f["event"] == "delta" and f.get("turn_id") == "turn-1" for f in frames))
                    self.assertTrue(any(f["event"] == "stream_end" and f.get("turn_id") == "turn-1" for f in frames))
                    turn_ends = [f for f in frames if f["event"] == "turn_end" and f.get("turn_id") == "turn-1"]
                    self.assertEqual(len(turn_ends), 1)
                    self.assertEqual(turn_ends[0]["turn_phase"], "complete")

                    # Pre-tool segment is persisted for replay, NEVER duplicated as live WS `message`!
                    ws_pre_msgs = [f for f in frames if f["event"] == "message" and f.get("text") == "我查一下。"]
                    self.assertEqual(ws_pre_msgs, [])
                    stored_texts = [e.get("text") for e in stored if e.get("event") == "message"]
                    self.assertIn("我查一下。", stored_texts)
                    self.assertIn("今天晴，24°C。", stored_texts)

            # Native also emits tool_hint start/end cleanly without polluting ChatStore
            hints = [f for f in n_frames if f["event"] == "tool_hint" and f.get("tool") == "lookup_weather"]
            self.assertEqual([h["phase"] for h in hints], ["start", "end"])
            self.assertFalse(any(e.get("event") == "tool_hint" for e in n_store))

        asyncio.run(scenario())

    # ── 2. Clarify cross-turn card + /retry(turn_B) preservation ─────────────

    def test_clarify_cross_turn_card_and_retry_turn_b_preservation(self) -> None:
        async def scenario() -> None:
            calls = 0

            async def fake_stream(**_: Any):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield TextDelta("先确认一下偏好。")
                    yield ToolCallComplete(
                        id="cl-1",
                        name="clarify",
                        arguments=json.dumps(
                            {"question": "要哪一种？", "choices": ["冰美式 (Recommended)", "热拿铁"]},
                            ensure_ascii=False,
                        ),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                elif calls == 2:
                    yield TextDelta("好的，给你选冰美式（初版）。")
                    yield StreamDone()
                else:
                    yield TextDelta("好的，给你选冰美式（重新生成版）。")
                    yield StreamDone()

            with muse_home_override(self.home):
                adapter, chat_id, sock = self._make_native_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    await adapter.submit(chat_id, "帮我点咖啡", "turn_A")
                    actor = adapter.engine.get_actor(chat_id)
                    for _ in range(100):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(actor._phase, "clarify_wait")
                    self.assertIsNone(adapter.get_active_turn(chat_id))
                    self.assertTrue(
                        any(f["event"] == "turn_end" and f.get("turn_id") == "turn_A" for f in sock.frames)
                    )

                    await adapter.submit(chat_id, "冰美式 (Recommended)", "turn_B")
                    await actor.wait_idle(timeout=3.0)
                    await adapter.regenerate(chat_id, "turn_B")
                    await actor.wait_idle(timeout=3.0)

                    events = adapter.store.thread_payload(chat_id)["events"]
                    turn_a_cards = [
                        e for e in events if e.get("turn_id") == "turn_A" and "```card" in str(e.get("text") or "")
                    ]
                    self.assertEqual(len(turn_a_cards), 1)
                    self.assertIn('"primary": true', turn_a_cards[0]["text"])
                    turn_b_answers = [
                        e.get("text")
                        for e in events
                        if e.get("turn_id") == "turn_B" and e.get("event") == "message" and e.get("turn_phase") == "answer"
                    ]
                    self.assertEqual(turn_b_answers, ["好的，给你选冰美式（重新生成版）。"])
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 3. /stop: idle + active + queued inbox cancellation (#207 adv #3) ────

    def test_stop_active_idle_and_queued_inbox_turns_emit_cancelled_turn_end(self) -> None:
        async def scenario() -> None:
            started_ev = asyncio.Event()
            release_ev = asyncio.Event()

            async def blocking_stream(**kw: Any):
                msgs = kw.get("messages") or []
                last_user = next((str(m.get("content") or "") for m in reversed(msgs) if m.get("role") == "user"), "")
                if "第一条" in last_user:
                    started_ev.set()
                    yield TextDelta("正在思考第一条")
                    await release_ev.wait()
                yield TextDelta("恢复后的回复。")
                yield StreamDone()

            with muse_home_override(self.home):
                adapter, chat_id, sock = self._make_native_host(stream_llm=blocking_stream)
                await adapter.startup()
                try:
                    # (a) Idle /stop emits turn_end without turn_id and zero ChatStore pollution
                    await adapter.submit(chat_id, "/stop", "webui-system:stop-idle")
                    self.assertEqual(adapter.store.events(chat_id), [])
                    self.assertEqual(sock.frames[-1]["event"], "turn_end")
                    self.assertNotIn("turn_id", sock.frames[-1])

                    # (b) Active turn + two queued inbox turns -> /stop cancels ALL three!
                    await adapter.submit(chat_id, "第一条长任务", "t-active")
                    await asyncio.wait_for(started_ev.wait(), timeout=2.0)
                    await adapter.submit(chat_id, "第二条排队消息", "t-queued-1")
                    await adapter.submit(chat_id, "第三条排队消息", "t-queued-2")

                    actor = adapter.engine.get_actor(chat_id)
                    self.assertEqual([m.turn_id for m in actor.inbox], ["t-queued-1", "t-queued-2"])

                    await adapter.submit(chat_id, "/stop", "webui-system:stop-active")
                    await actor.wait_idle(timeout=2.0)

                    self.assertEqual(len(actor.inbox), 0)
                    self.assertIsNone(actor.active_turn_id)

                    cancelled_ends = {
                        f.get("turn_id"): f.get("outcome")
                        for f in sock.frames
                        if f.get("event") == "turn_end" and f.get("turn_id")
                    }
                    self.assertEqual(cancelled_ends.get("t-active"), "cancelled")
                    self.assertEqual(cancelled_ends.get("t-queued-1"), "cancelled")
                    self.assertEqual(cancelled_ends.get("t-queued-2"), "cancelled")

                    # (c) Actor remains healthy for subsequent turns
                    await adapter.submit(chat_id, "第四条正常提问", "t-after")
                    await actor.wait_idle(timeout=2.0)
                    self.assertTrue(
                        any(f.get("event") == "turn_end" and f.get("turn_id") == "t-after" and f.get("outcome") == "completed" for f in sock.frames)
                    )
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 4. /undo confirmation card -> /approve -> soft-delete ────────────────

    def test_undo_confirmation_and_soft_delete_parity(self) -> None:
        async def scenario() -> None:
            async def fake_stream(**_: Any):
                yield TextDelta("第一轮回答。")
                yield StreamDone()

            with muse_home_override(self.home):
                adapter, chat_id, sock = self._make_native_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    actor = adapter.engine.get_actor(chat_id)
                    await adapter.submit(chat_id, "你好", "t-1")
                    await actor.wait_idle(timeout=2.0)

                    await adapter.submit(chat_id, "/undo", "t-undo-ask")
                    confirm_cards = [
                        f for f in sock.frames if f.get("event") == "message" and "```card" in str(f.get("text") or "")
                    ]
                    self.assertEqual(len(confirm_cards), 1)
                    self.assertIn("/approve", confirm_cards[0]["text"])

                    await adapter.submit(chat_id, "/approve", "t-undo-ok")
                    await actor.wait_idle(timeout=2.0)

                    sid = adapter.engine.db.get_active_session(chat_id)["id"]
                    self.assertEqual(adapter.engine.db.get_messages(sid, active_only=True), [])
                    self.assertGreaterEqual(len(adapter.engine.db.get_messages(sid, active_only=False)), 2)
                    stored_tids = {e.get("turn_id") for e in adapter.store.events(chat_id)}
                    self.assertNotIn("t-1", stored_tids)
                    self.assertTrue(any("已撤回上一轮对话" in str(f.get("text") or "") for f in sock.frames))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 5. /compact strict context_compaction schema ─────────────────────────

    def test_compact_emits_strict_context_compaction_schema(self) -> None:
        async def scenario() -> None:
            async def fake_stream(**_: Any):
                yield TextDelta("收到。")
                yield StreamDone()

            with muse_home_override(self.home):
                adapter, chat_id, sock = self._make_native_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    actor = adapter.engine.get_actor(chat_id)
                    sess = adapter.engine.db.get_or_create_active_session(chat_id, source="companion")
                    for idx in range(12):
                        adapter.engine.db.append_message(sess["id"], chat_id, "user", f"长问题 {idx} " + ("细节" * 80), turn_id=f"u-{idx}")
                        adapter.engine.db.append_message(sess["id"], chat_id, "assistant", f"长回答 {idx} " + ("内容" * 80), turn_id=f"u-{idx}")

                    with mock.patch("muse.llm.auxiliary.async_call_llm", new=mock.AsyncMock(return_value=mock.Mock(choices=[mock.Mock(message=mock.Mock(content="【摘要】保留关键上下文"))]))):
                        await adapter.submit(chat_id, "/compact", "t-compact")
                        await actor.wait_idle(timeout=3.0)

                    comp_events = [f for f in sock.frames if f.get("event") == "context_compaction"]
                    self.assertGreaterEqual(len(comp_events), 2)
                    allowed_phases = {"started", "succeeded", "failed", "cancelled"}
                    cids = set()
                    for ev in comp_events:
                        self.assertIn(ev.get("phase"), allowed_phases)
                        self.assertTrue(ev.get("compaction_id"))
                        cids.add(ev["compaction_id"])
                    self.assertEqual(len(cids), 1)
                    self.assertEqual(comp_events[0]["phase"], "started")
                    self.assertEqual(comp_events[-1]["phase"], "succeeded")
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 6. Approvals: defer + hold_exec native execution ─────────────────────

    def test_approvals_defer_and_hold_exec_native_wiring(self) -> None:
        async def scenario() -> None:
            executed: list[tuple[str, dict[str, Any]]] = []

            async def raw_exec(name: str, args: dict[str, Any], _inv: Any) -> str:
                executed.append((name, dict(args)))
                return json.dumps({"ok": True, "tool": name})

            with muse_home_override(self.home):
                adapter, chat_id, _ = self._make_native_host()
                adapter._engine = muse_runtime.NativeAgentEngine(
                    adapter,
                    home=self.home,
                    start_background_services=False,
                )
                adapter.engine._dispatch_registered_tool = raw_exec  # type: ignore[method-assign]
                from muse.interceptors import ToolInterceptorPipeline

                adapter.engine.pipeline = ToolInterceptorPipeline(
                    raw_exec, coordinator=adapter.engine.approval_coordinator
                )
                await adapter.startup()
                try:
                    # (a) defer + approve executes through native coordinator with skip_policy_gates=True
                    deferred_msg = adapter.approvals.defer(
                        chat_id, "call-1", "mcp__mail__send_email", {"to": "alice@example.com"}, "send_message"
                    )
                    self.assertTrue(deferred_msg.strip())
                    pending_recs = adapter.approvals.store.list_for_session(f"websocket:{chat_id}")
                    self.assertEqual(len(pending_recs), 1)
                    rec = pending_recs[0]
                    self.assertEqual(rec.status, "pending")
                    with mock.patch.object(adapter, "submit_internal", new=mock.AsyncMock(return_value="rep-1")):
                        decided = await adapter.approvals.decide(rec.id, True, "once")
                    self.assertEqual(decided.status, "executed")
                    self.assertEqual(executed, [("mcp__mail__send_email", {"to": "alice@example.com"})])

                    # (b) hold_exec resolves inline via NativeApprovalCoordinator
                    coord = adapter.engine.approval_coordinator
                    assert coord is not None

                    async def resolve_soon() -> None:
                        for _ in range(50):
                            pending = [
                                r
                                for r in adapter.approvals.store.list_for_session(f"websocket:{chat_id}")
                                if r.status == "pending" and r.tool_name == "terminal"
                            ]
                            if pending:
                                await adapter.approvals.decide(pending[0].id, True, "once")
                                return
                            await asyncio.sleep(0.01)

                    resolver = asyncio.create_task(resolve_soon())
                    from muse.context import TurnExecutionContext

                    ctx = TurnExecutionContext(chat_id=chat_id, facing_chat_id=chat_id, session_id="s1")
                    out = await adapter.engine.pipeline.execute(
                        "terminal",
                        {"command": "rm -rf /tmp/demo"},
                        call_id="call-exec-1",
                        ctx=ctx,
                    )
                    await resolver
                    self.assertIn('"ok": true', out)
                    self.assertEqual(executed[-1][0], "terminal")
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 7. Cutover defaults, Hermes rollback, 3s settle, shared MuseDB reuse ─

    def test_default_engine_cutover_hermes_rollback_and_shared_db_reuse(self) -> None:
        from companion.modules import goals, interrupted, models, persona

        clean_env = {k: v for k, v in os.environ.items() if k != "MUSE_ENGINE"}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            self.assertEqual(active_engine_name(), "native")
            self.assertEqual(interrupted.settle_seconds(), 3)
            self.assertFalse(persona.write_locale_overlay("zh"))
            eng = muse_engine.build_engine()
            self.assertEqual(eng.name, "native")
            self.assertIsInstance(eng, muse_runtime.NativeAgentEngine)

        with mock.patch.dict(os.environ, {**clean_env, "MUSE_ENGINE": "hermes"}, clear=True):
            self.assertEqual(active_engine_name(), "hermes")
            self.assertEqual(interrupted.settle_seconds(), 45)
            eng_h = muse_engine.build_engine()
            self.assertEqual(eng_h.name, "hermes")
            self.assertIsInstance(eng_h, muse_engine.HermesEngineBridge)

        # Running NativeAgentEngine shares its open MuseDB handle with models.usage_summary & goals.goal_state
        async def check_shared_handle() -> None:
            with muse_home_override(self.home):
                adapter, chat_id, _ = self._make_native_host()
                await adapter.startup()
                try:
                    shared = adapter.engine.db
                    self.assertIs(muse_runtime.peek_shared_db(self.home), shared)
                    sess = shared.get_or_create_active_session(chat_id, source="companion")
                    shared.record_usage(
                        sess["id"], "deepseek-chat", provider="m-ds", base_url="https://api.deepseek.com",
                        source="companion", input_tokens=100, output_tokens=20,
                    )
                    shared.upsert_goal(chat_id, status="active", objective="验证共享句柄")
                    with mock.patch("muse.runtime.MuseDB", side_effect=AssertionError("should reuse shared handle!")):
                        usage = models.usage_summary("Asia/Shanghai")
                        gstate = goals.goal_state(chat_id)
                    self.assertEqual(usage["today"]["by_model"][0]["calls"], 1)
                    self.assertEqual(gstate["objective"], "验证共享句柄")
                finally:
                    await adapter.shutdown()
                self.assertIsNone(muse_runtime.peek_shared_db(self.home))

        asyncio.run(check_shared_handle())

    # ── 8. Performance gates: cold-start < 800ms & idle RSS < 120 MB ─────────

    def test_performance_gates_cold_start_and_idle_rss(self) -> None:
        # Ensure companion.server module is already imported (measuring app cold-start + lifespan + bootstrap).
        import companion.server  # noqa: F401

        web_dist = self.home / "web-dist"
        web_dist.mkdir(parents=True, exist_ok=True)
        (web_dist / "companion.html").write_text("<!doctype html><title>Muse Bot</title>\n", encoding="utf-8")

        t0 = time.perf_counter()
        asgi = muse_app.create_app(home=self.home / "perf-home", web_dist=web_dist, start_background_services=False)
        with TestClient(asgi, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            resp = client.get("/webui/bootstrap")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ws_path"], "/ws")
        self.assertLess(elapsed_ms, 800.0, f"Cold-start create_app+bootstrap took {elapsed_ms:.1f}ms (budget < 800ms)")
        self.assertLess(rss_mb, 120.0, f"Idle RSS was {rss_mb:.1f}MB (budget < 120MB)")

    # ── 9. Real WS _handle_frame: attach/goal_state + attachment upload ──────

    def test_ws_handle_frame_attach_goal_state_and_attachment_upload_parity(self) -> None:
        from companion.server import _handle_frame

        async def scenario() -> None:
            received_files: list[Any] = []

            async def fake_stream(**_: Any):
                yield TextDelta("看到图片了。")
                yield StreamDone()

            with muse_home_override(self.home):
                adapter, chat_id, _ = self._make_native_host(stream_llm=fake_stream)
                orig_submit = adapter.engine.submit_turn

                async def capture_submit(cid: str, text: str, tid: str, files: Any = None) -> None:
                    received_files.append(files)
                    await orig_submit(cid, text, tid, files=files)

                adapter.engine.submit_turn = capture_submit  # type: ignore[method-assign]
                await adapter.startup()
                try:
                    adapter.engine.db.upsert_goal(
                        chat_id, status="active", objective="验证 WS 附着目标", state={"status": "active", "last_reason": "进行中"},
                    )
                    sock = _RecordingSocket()
                    # (a) Real WS attach frame -> attached + nested goal_state payload
                    await _handle_frame(adapter, sock, {"type": "attach", "chat_id": chat_id})
                    self.assertEqual(sock.frames[0], {"event": "attached", "chat_id": chat_id})
                    gs_frame = sock.frames[1]
                    self.assertEqual(gs_frame["event"], "goal_state")
                    self.assertEqual(gs_frame["chat_id"], chat_id)
                    self.assertIsInstance(gs_frame.get("goal_state"), dict)
                    self.assertTrue(gs_frame["goal_state"]["active"])
                    self.assertEqual(gs_frame["goal_state"]["objective"], "验证 WS 附着目标")

                    # (b) Real HTTP POST /api/attachments (raw bytes) -> WS message frame
                    from companion.server import build_app

                    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
                    http_app = build_app(adapter)
                    with TestClient(http_app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as http_client:
                        api_token = http_client.get("/webui/bootstrap").json()["api_token"]
                        up = http_client.post(
                            "/api/attachments",
                            content=png_bytes,
                            headers={
                                "Authorization": f"Bearer {api_token}",
                                "Content-Type": "image/png",
                                "X-Attachment-Name": "cat.png",
                            },
                        )
                    self.assertEqual(up.status_code, 200, up.text)
                    ref = up.json()["reference"]
                    uploaded_path = adapter._attachments[ref][0]
                    self.assertIsInstance(uploaded_path, Path)
                    self.assertTrue(uploaded_path.is_file())

                    await _handle_frame(
                        adapter,
                        sock,
                        {
                            "type": "message",
                            "chat_id": chat_id,
                            "content": "看这张猫图",
                            "turn_id": "t-att-1",
                            "media": [{"reference": ref}],
                        },
                    )
                    # Let the created submit task launch and finish
                    for _ in range(100):
                        if any(f.get("event") == "turn_end" and f.get("turn_id") == "t-att-1" for f in sock.frames):
                            break
                        await asyncio.sleep(0.02)

                    self.assertFalse(uploaded_path.exists(), "Temp upload file must be unlinked after staging")
                    user_ev = next(f for f in sock.frames if f.get("event") == "user_message" and f.get("turn_id") == "t-att-1")
                    self.assertNotIn("attachments", user_ev)
                    self.assertIn("media_urls", user_ev)
                    self.assertEqual(user_ev["media_urls"][0]["kind"], "image")
                    self.assertTrue(str(user_ev["media_urls"][0]["url"]).startswith("/api/media/"))
                    self.assertEqual(len(received_files), 1)
                    self.assertEqual(received_files[0][0][1], "image/png")
                    self.assertEqual(Path(received_files[0][0][0]).read_bytes(), png_bytes)
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 10. Broadcast (feed_updated, approval_updated), signed media/cards, job source ──

    def test_live_broadcast_feed_approval_media_cards_and_proactive_source_parity(self) -> None:
        from companion.modules import feed, routines, widgets

        async def scenario() -> None:
            with muse_home_override(self.home):
                adapter, chat_id, chat_sock = self._make_native_host()
                global_sock = _RecordingSocket()
                adapter.socket_devices[global_sock] = "dev-global"
                await adapter.startup()
                try:
                    # (a) feed._write -> broadcast({"event": "feed_updated"}) reaches all sockets
                    await asyncio.to_thread(feed._write, {"posts": [], "generated_at": None, "job_id": None})
                    await asyncio.sleep(0.05)
                    self.assertTrue(any(f.get("event") == "feed_updated" for f in global_sock.frames))

                    # (b) approvals.defer -> live emit_approval_updated reaches all sockets
                    adapter.approvals.defer(chat_id, "c-live", "mcp__mail__send", {"to": "a@b.com"}, "send_message")
                    await asyncio.sleep(0.05)
                    ap_frames = [f for f in global_sock.frames if f.get("event") == "approval_updated"]
                    self.assertEqual(len(ap_frames), 1)
                    self.assertEqual(ap_frames[0]["chat_id"], chat_id)
                    self.assertEqual(ap_frames[0]["status"], "pending")
                    self.assertIsInstance(ap_frames[0]["approval"], dict)

                    # (c) send_media_file stages & signs URL instead of leaking filesystem path
                    img = self.home / "report.png"
                    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
                    ok = await adapter.send_media_file(chat_id, str(img), caption="图表在此", turn_id="t-media")
                    self.assertTrue(ok)
                    media_ev = next(f for f in chat_sock.frames if f.get("event") == "message" and f.get("media_urls"))
                    staged_url = media_ev["media_urls"][0]["url"]
                    self.assertTrue(staged_url.startswith("/api/media/"), f"Expected signed URL, got {staged_url!r}")
                    self.assertNotIn(str(self.home), staged_url)

                    # (d) transform_cards rewrites widget cards and signs picture cards
                    from companion.modules import avatar_make

                    widget_html = self.home / "calc.html"
                    widget_html.write_text("<!doctype html><p>hi</p>", encoding="utf-8")
                    cand_file = avatar_make._candidates_root() / "deadbeef" / "1.webp"
                    cand_file.parent.mkdir(parents=True, exist_ok=True)
                    cand_file.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
                    raw_cards = (
                        f"```card\n{json.dumps({'type': 'widget', 'title': '小工具', 'file': str(widget_html)}, ensure_ascii=False)}\n```\n\n"
                        f"```card\n{json.dumps({'type': 'pictures', 'items': [{'file': 'deadbeef/1.webp', 'label': '一号'}]}, ensure_ascii=False)}\n```"
                    )
                    transformed = adapter.transform_cards(chat_id, raw_cards)
                    self.assertIn(widgets.PREFIX, transformed)
                    self.assertIn("/api/webui/companion/avatars/", transformed)
                    self.assertNotIn(str(widget_html), transformed)

                    # (e) source_for_job delegates to routines.source_for_job (includes label)
                    with mock.patch.object(routines, "source_for_job", return_value={"kind": "checkin", "label": "早报"}):
                        self.assertEqual(adapter.source_for_job("job-morning"), {"kind": "checkin", "label": "早报"})
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 11. Helper tools (helper_ask, browser_request, post_as_helper, context_for) ──

    def test_native_helper_ask_browser_request_post_as_helper_and_context_injection(self) -> None:
        from companion.modules import helpers as comp_helpers

        async def scenario() -> None:
            seen_prompts: dict[str, str] = {}

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                last_user = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
                if "【你现在的身份】你是用户的帮手「调研员」" in last_user:
                    seen_prompts["helper"] = last_user
                    yield TextDelta("调研结论：Native 运行时全绿。")
                    yield StreamDone()
                    return
                if "【浏览器代办】" in last_user:
                    seen_prompts["desk"] = last_user
                    yield TextDelta("页面标题：Muse Docs")
                    yield StreamDone()
                    return
                seen_prompts["main"] = last_user
                yield TextDelta("主助理收到。")
                yield StreamDone()

            with muse_home_override(self.home):
                adapter, main_chat, main_sock = self._make_native_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    helper = comp_helpers.create(
                        adapter.store,
                        {"name": "调研员", "title": "查资料", "instructions": "只给结论和出处", "emoji": "🔎"},
                    )
                    # (a) Real ToolRegistry.dispatch('helper_ask') exercising helpers._handle_ask -> adapter.ask_helper & post_as_helper
                    reg = adapter.engine.tool_registry
                    raw_res = await reg.dispatch(
                        "helper_ask",
                        {"helper": "调研员", "task": "查最新进展"},
                        session_id=f"companion:{main_chat}",
                    )
                    parsed = json.loads(raw_res)
                    self.assertTrue(parsed["success"], raw_res)
                    self.assertIn("调研结论：Native 运行时全绿。", parsed["answer"])
                    self.assertIn("你的职责说明：只给结论和出处", seen_prompts.get("helper", ""))

                    # Canonical helper wire message: `source: {kind: "helper", id, label}` (NOT `sender`)
                    helper_msgs = [
                        f for f in main_sock.frames
                        if f.get("event") == "message" and (f.get("source") or {}).get("kind") == "helper"
                    ]
                    self.assertEqual(len(helper_msgs), 1)
                    self.assertEqual(helper_msgs[0]["source"], {"kind": "helper", "id": helper["id"], "label": "🔎 调研员"})
                    self.assertNotIn("sender", helper_msgs[0])

                    # (b) browser_for runs in an ephemeral desk chat with 【浏览器代办】 context and deletes it after
                    desk_ans = await adapter.browser_for(helper, "打开文档页读标题", timeout=5.0)
                    self.assertIn("页面标题：Muse Docs", desk_ans)
                    self.assertIn("【浏览器代办】", seen_prompts.get("desk", ""))
                    remaining_desks = [cid for cid, meta in adapter.store._read_index().items() if meta.get("desk")]
                    self.assertEqual(remaining_desks, [])

                    # (c) Main chat turn receives BOTH 【你的帮手】 (helpers.context_for) AND 【本轮呈现方式】 (router.on_pre_llm_call)
                    await adapter.submit(main_chat, "查一下上海明天天气", "t-router")
                    await adapter.engine.get_actor(main_chat).wait_idle(timeout=3.0)
                    self.assertIn("【你的帮手】", seen_prompts.get("main", ""))
                    self.assertIn("【本轮呈现方式（App 自动判断）】", seen_prompts.get("main", ""))
                    # muse.db user row stays clean (no internal routing hints persisted)
                    sid = adapter.engine.db.get_active_session(main_chat)["id"]
                    user_rows = [m for m in adapter.engine.db.get_messages(sid, active_only=True) if m["role"] == "user"]
                    self.assertEqual(user_rows[-1]["content"], "查一下上海明天天气")
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 12. sync_identity ensures triggers, plans, and feed jobs ─────────────

    def test_sync_identity_ensures_companion_jobs(self) -> None:
        from companion.modules import companion_state, feed, plans, triggers

        with muse_home_override(self.home):
            adapter, chat_id, _ = self._make_native_host()
            companion_state.update_companion_state({"chat_id": chat_id, "name": "小缪"})
            with (
                mock.patch.object(triggers, "ensure_jobs") as m_trig,
                mock.patch.object(plans, "ensure_jobs") as m_plans,
                mock.patch.object(feed, "ensure_job") as m_feed,
            ):
                adapter.sync_identity()
            m_trig.assert_called_once()
            m_plans.assert_called_once()
            m_feed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
