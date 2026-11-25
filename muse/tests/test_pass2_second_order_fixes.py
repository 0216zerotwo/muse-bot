"""Pass-2 second-order concurrency, `/goal`, post-`clarify`, and multimodal regression suite (PR #29).

Covers all 13 findings from Pass-2 adversarial probes (`#276`, `#277`, `#278`).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib
import io
import json
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from PIL import Image

from companion import activity as comp_activity
from companion import approvals as comp_approvals
from companion.modules import companion_state, helpers as comp_helpers, models as comp_models
from muse import app as muse_app
from muse import runtime as muse_runtime
from muse.config_store import save_config
from muse.context import ContextCompactor, build_content_parts_sync
from muse.db import MuseDB, close_all
from muse.goal_loop import GoalLoopController
from muse.llm.auxiliary import describe_images_if_blind
from muse.llm.chat_completions import _sanitize_chat_messages
from muse.llm.types import StreamDone, TextDelta, ToolCallComplete, UsageInfo
from muse.paths import muse_home_override
from muse.turn_actor import CANCELLED_TOOL_RESULT


class _DummySocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(dict(frame))


class Pass2SecondOrderFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        muse_runtime.close_all_shared_dbs()
        close_all()
        self._tmp.cleanup()

    def _make_host(
        self,
        *,
        stream_llm: Any = None,
        execute_tool: Any = None,
    ) -> tuple[muse_app.NativeCompanionAdapter, str, _DummySocket]:
        adapter = muse_app.NativeCompanionAdapter(home=self.home, start_background_services=False)
        adapter._engine = muse_runtime.NativeAgentEngine(
            adapter,
            home=self.home,
            stream_llm=stream_llm,
            execute_tool=execute_tool,
            start_background_services=False,
        )
        chat_id = adapter.store.create()
        companion_state.update_companion_state({"chat_id": chat_id})
        sock = _DummySocket()
        adapter.attach(chat_id, sock)
        return adapter, chat_id, sock

    # ── 1. Blind text models flatten all-text content lists (#276-1) ─────────

    def test_01_blind_text_models_flatten_all_text_content_lists(self) -> None:
        async def scenario() -> None:
            txt_file = self.home / "notes.txt"
            txt_file.write_text("alpha=1\nbeta=2\n", encoding="utf-8")
            img_file = self.home / "photo.png"
            Image.new("RGB", (12, 12), color=(10, 20, 30)).save(img_file, format="PNG")

            # Case A: .txt attachment produces text-only parts -> flattened by _sanitize_chat_messages
            txt_parts = build_content_parts_sync("请总结", [(str(txt_file), "text/plain")])
            self.assertIsInstance(txt_parts, list)
            sanitized_txt = _sanitize_chat_messages(
                [{"role": "user", "content": txt_parts}],
                keep_gemini_sig=False, is_kimi=False, keep_reasoning_content=False,
                mistral_mapper=None, tool_forward={},
            )
            self.assertIsInstance(sanitized_txt[0]["content"], str)
            self.assertIn("请总结", sanitized_txt[0]["content"])
            self.assertIn("alpha=1", sanitized_txt[0]["content"])

            # Case B: describe_images_if_blind replaces image with text and flattens to str
            mm_parts = build_content_parts_sync("看图", [(str(img_file), "image/png")])
            blind_msgs = await describe_images_if_blind(
                [{"role": "user", "content": mm_parts}],
                chat_vision=False,
                config={},
            )
            self.assertIsInstance(blind_msgs[0]["content"], str)
            self.assertIn("看图", blind_msgs[0]["content"])

            # Case C: Tier-1 image eviction leaves text-only list -> flattened by _sanitize_chat_messages
            compactor = ContextCompactor(tier1_max_inline_images=1)
            copy_mm = build_content_parts_sync("第二张图", [(str(img_file), "image/png")])
            old_and_new = [
                {"role": "user", "content": mm_parts},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": copy_mm},
                {"role": "assistant", "content": "ok2"},
            ]
            pruned = compactor.tier1_prune(old_and_new).messages
            sanitized_pruned = _sanitize_chat_messages(
                pruned,
                keep_gemini_sig=False, is_kimi=False, keep_reasoning_content=False,
                mistral_mapper=None, tool_forward={},
            )
            self.assertIsInstance(sanitized_pruned[0]["content"], str)

            # Vision messages that still hold image_url keep multimodal list format
            sanitized_vision = _sanitize_chat_messages(
                [{"role": "user", "content": mm_parts}],
                keep_gemini_sig=False, is_kimi=False, keep_reasoning_content=False,
                mistral_mapper=None, tool_forward={},
            )
            self.assertIsInstance(sanitized_vision[0]["content"], list)

        asyncio.run(scenario())

    # ── 2. Primary context overflow runs Tier-3 compaction before fallback (#276-2) ──

    def test_02_primary_context_overflow_triggers_tier3_compaction_before_fallback(self) -> None:
        async def scenario() -> None:
            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                save_config(
                    {
                        "model": {"provider": "primary-p", "default": "claude-primary", "base_url": "https://primary.example/v1"},
                        "fallback_providers": [
                            {"provider": "backup-p", "model": "backup-small", "base_url": "https://backup.example/v1"},
                        ],
                    },
                    home=self.home,
                )
                calls: list[tuple[str, int]] = []

                class _FakeTransport:
                    def __init__(self, base_url: str) -> None:
                        self.base_url = base_url

                    async def stream(self, *, model: str, messages: list[dict[str, Any]], **_: Any):
                        calls.append((model, len(messages)))
                        if model == "backup-small":
                            raise RuntimeError("HTTP 401 unauthorized backup")
                        if len(messages) >= 18:
                            raise RuntimeError("HTTP 400: This model's maximum context length is 200000 tokens")
                        yield TextDelta("压缩后主模型成功回复。")
                        yield StreamDone()

                with mock.patch("muse.llm.auxiliary.create_transport", side_effect=lambda **kw: _FakeTransport(kw["base_url"])), mock.patch(
                    "muse.llm.providers.resolve_runtime_provider",
                    side_effect=lambda prov, target_model=None, **kw: {
                        "provider": prov or "primary-p",
                        "model": target_model or "claude-primary",
                        "api_mode": "chat_completions",
                        "base_url": kw.get("base_url") or "https://primary.example/v1",
                        "api_key": "sk-test",
                        "extra_headers": {},
                        "extra_body": {},
                    },
                ):
                    adapter, chat_id, sock = self._make_host()
                    await adapter.startup()
                    try:
                        sid = adapter._engine._ensure_db().get_or_create_active_session(chat_id, source="companion")["id"]
                        db = adapter._engine._ensure_db()
                        for idx in range(8):
                            db.append_message(sid, chat_id, "user", f"历史消息 {idx} " + ("x" * 200), turn_id=f"h-{idx}")
                            db.append_message(sid, chat_id, "assistant", f"历史回复 {idx} " + ("y" * 200), turn_id=f"h-{idx}")

                        await adapter.submit(chat_id, "触发超长压缩", turn_id="t-ov-1")
                        await adapter._engine.get_actor(chat_id).wait_idle(timeout=5.0)

                        # Backup was NEVER called before Tier-3 emergency compaction rescued primary!
                        self.assertEqual([m for m, _ in calls], ["claude-primary", "claude-primary"])
                        self.assertLess(calls[1][1], calls[0][1])
                        comp_events = [f for f in sock.frames if f.get("event") == "context_compaction"]
                        self.assertTrue(any(e.get("phase") == "succeeded" for e in comp_events))
                    finally:
                        await adapter.shutdown()

        asyncio.run(scenario())

    # ── 3. Post-clarify /undo with turn_B attachments (#276-3) ───────────────

    def test_03_post_clarify_undo_with_turn_b_attachments_restores_clarify_row(self) -> None:
        db = MuseDB(self.home / "undo_att.db")
        try:
            sess = db.get_or_create_active_session("c-undo", source="companion")
            sid = sess["id"]
            db.append_message(sid, "c-undo", "user", "部署服务", turn_id="turn_A")
            db.append_message(
                sid, "c-undo", "assistant", "", turn_id="turn_A",
                tool_calls=[{"id": "tc-cl", "type": "function", "function": {"name": "clarify", "arguments": "{}"}}],
            )
            cid = db.append_message(
                sid, "c-undo", "tool", "Here is the proof", turn_id="turn_B",
                tool_call_id="tc-cl", tool_name="clarify", extra={"clarify_id": "c1", "awaiting_user": False},
            )
            db.append_message(
                sid, "c-undo", "user", "[User attached files with clarify response]", turn_id="turn_B",
                extra={"clarify_attachment": True, "files": [["/tmp/a.png", "image/png"]]},
            )
            db.append_message(sid, "c-undo", "assistant", "收到证明，已完成。", turn_id="turn_B")

            changed = db.undo_last_exchange(sid)
            self.assertGreaterEqual(changed, 3)
            active = db.get_messages(sid, active_only=True)
            self.assertEqual([(r["role"], r["turn_id"]) for r in active], [("user", "turn_A"), ("assistant", "turn_A"), ("tool", "turn_A")])
            self.assertEqual(active[2]["id"], cid)
            self.assertNotEqual(active[2]["content"], "Here is the proof")
        finally:
            db.close()

    # ── 4. Fallback usage attributed to actual answering model (#276-4) ──────

    def test_04_fallback_usage_attributed_to_actual_answering_model_under_override(self) -> None:
        async def scenario() -> None:
            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                save_config(
                    {
                        "model": {"provider": "m-claude", "default": "claude-sonnet-4-6", "base_url": "https://api.anthropic.com"},
                        "fallback_providers": [
                            {"provider": "m-deepseek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1"},
                        ],
                    },
                    home=self.home,
                )

                class _FallbackTransport:
                    def __init__(self, provider: str) -> None:
                        self.provider = provider

                    async def stream(self, *, model: str, **_: Any):
                        if model == "claude-sonnet-4-6":
                            raise RuntimeError("HTTP 500 anthropic down")
                        yield TextDelta("由备用模型回复")
                        yield StreamDone(usage=UsageInfo(input_tokens=70, output_tokens=41))

                with mock.patch("muse.llm.auxiliary.create_transport", side_effect=lambda **kw: _FallbackTransport(kw["provider"])), mock.patch(
                    "muse.llm.providers.resolve_runtime_provider",
                    side_effect=lambda prov, target_model=None, **kw: {
                        "provider": prov,
                        "model": target_model,
                        "api_mode": "chat_completions",
                        "base_url": kw.get("base_url") or "",
                        "api_key": "sk-test",
                        "extra_headers": {},
                        "extra_body": {},
                    },
                ):
                    adapter, chat_id, _ = self._make_host()
                    await adapter.startup()
                    try:
                        actor = adapter._engine.get_actor(chat_id)
                        actor.set_model_override(
                            {"provider": "m-claude", "model": "claude-sonnet-4-6", "base_url": "https://api.anthropic.com"}
                        )
                        await adapter.submit(chat_id, "测试回退计费归属", turn_id="t-fb-usage")
                        await actor.wait_idle(timeout=5.0)

                        summary = comp_models.usage_summary()
                        by_model = {
                            m["model"]: (m["input_tokens"] + m["output_tokens"])
                            for m in summary["today"]["by_model"]
                        }
                        self.assertEqual(by_model.get("deepseek-chat"), 111)
                        self.assertNotIn("claude-sonnet-4-6", by_model)
                    finally:
                        await adapter.shutdown()

        asyncio.run(scenario())

    # ── 5. EXIF Orientation=6 JPEG <= 5MB/2048px rotated (#276-5) ────────────

    def test_05_exif_orientation_6_jpeg_transposed_and_reencoded(self) -> None:
        jpg_path = self.home / "sideways.jpg"
        raw_img = Image.new("RGB", (40, 20), color=(120, 60, 30))
        exif = raw_img.getexif()
        exif[274] = 6  # Rotate 90 CW -> (20, 40)
        raw_img.save(jpg_path, format="JPEG", exif=exif.tobytes())

        parts = build_content_parts_sync("旋转测试", [(str(jpg_path), "image/jpeg")])
        self.assertIsNotNone(parts)
        img_parts = [p for p in (parts or []) if p.get("type") == "image_url"]
        self.assertEqual(len(img_parts), 1)
        url = img_parts[0]["image_url"]["url"]
        _, b64 = url.split(",", 1)
        with Image.open(io.BytesIO(base64.b64decode(b64))) as decoded:
            self.assertEqual(decoded.size, (20, 40))
            self.assertIn(decoded.getexif().get(274, 1), (None, 1))

    # ── 6. deliver_proactive mid-tool-batch preserves adjacency (#277-1) ─────

    def test_06_deliver_proactive_mid_tool_batch_buffers_db_row_preserving_tool_adjacency(self) -> None:
        async def scenario() -> None:
            step = 0

            async def fake_stream(**_: Any):
                nonlocal step
                step += 1
                if step == 1:
                    yield ToolCallComplete(id="tc-1", name="tool_one", arguments="{}")
                    yield ToolCallComplete(id="tc-2", name="tool_two", arguments="{}")
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("双工具执行完毕。")
                    yield StreamDone()

            adapter_ref: list[muse_app.NativeCompanionAdapter] = []
            chat_ref: list[str] = []

            async def exec_tool(tc: dict[str, Any], _ctx: Any) -> str:
                fn = (tc.get("function") or {}).get("name") or tc.get("name")
                if fn == "tool_one":
                    # Proactive cron delivery lands right between tc-1 and tc-2!
                    await adapter_ref[0]._engine.deliver_proactive(
                        chat_ref[0], "定时提醒：喝水", job_id="job-water", attach_to_session=True
                    )
                return json.dumps({"ran": fn})

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                adapter, chat_id, sock = self._make_host(stream_llm=fake_stream, execute_tool=exec_tool)
                adapter_ref.append(adapter)
                chat_ref.append(chat_id)
                await adapter.startup()
                try:
                    await adapter.submit(chat_id, "跑两个工具", turn_id="t-chaos")
                    await adapter._engine.get_actor(chat_id).wait_idle(timeout=5.0)

                    # WS emitted proactive message immediately
                    self.assertTrue(any("定时提醒：喝水" in str(f.get("text") or "") for f in sock.frames))
                    db = adapter._engine._ensure_db()
                    sid = db.get_active_session(chat_id, source="companion")["id"]
                    self.assertEqual(db.heal_orphan_tool_calls(sid), 0)
                    rows = db.get_messages(sid, active_only=True)
                    roles = [(r["role"], r.get("tool_call_id"), bool((r.get("extra") or {}).get("proactive"))) for r in rows]
                    self.assertEqual(
                        roles,
                        [
                            ("user", None, False),
                            ("assistant", None, False),
                            ("tool", "tc-1", False),
                            ("tool", "tc-2", False),
                            ("assistant", None, True),
                            ("assistant", None, False),
                        ],
                    )
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 7 & 8. /stop, /new->/approve, and TOCTOU during slow_judge (#277-2,3,4) ──

    def test_07_stop_new_reset_and_toctou_during_slow_goal_judge(self) -> None:
        async def scenario() -> None:
            judge_entered = asyncio.Event()
            release_judge = asyncio.Event()
            llm_prompts: list[str] = []

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                user_txts = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
                llm_prompts.append(user_txts[-1] if user_txts else "")
                yield TextDelta("推进了一步。")
                yield StreamDone()

            async def slow_judge(self_ctrl: Any, state: Any, last_response: str, cid: str):
                judge_entered.set()
                await release_judge.wait()
                return "continue", "继续下一步", False, None, False

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ), mock.patch.object(GoalLoopController, "_call_judge", slow_judge):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    actor = adapter._engine.get_actor(chat_id)

                    # Part A: /stop during slow_judge pauses goal and cancels _runner_task
                    await adapter.submit(chat_id, "/goal 完成基准压测", turn_id="g-stop-1")
                    await asyncio.wait_for(judge_entered.wait(), timeout=5.0)
                    self.assertEqual(actor._phase, "goal_eval")
                    await adapter.submit(chat_id, "/stop", turn_id="g-stop-cmd")
                    release_judge.set()
                    await actor.wait_idle(timeout=5.0)
                    st = adapter._engine.goal_controller.load_state(chat_id)
                    self.assertIsNotNone(st)
                    self.assertEqual(st.status, "paused")
                    self.assertEqual(len(llm_prompts), 1)

                    # Part B: TOCTOU — concurrent `/goal stop` during evaluate_after_turn never resurrects goal
                    judge_entered.clear()
                    release_judge.clear()
                    await adapter._engine.goal_controller.set_goal(chat_id, "TOCTOU测试目标")
                    eval_task = asyncio.create_task(
                        adapter._engine.goal_controller.evaluate_after_turn(chat_id, "阶段进展")
                    )
                    await asyncio.wait_for(judge_entered.wait(), timeout=5.0)
                    await adapter._engine.goal_controller.handle_control(chat_id, "/goal stop")
                    release_judge.set()
                    decision = await eval_task
                    self.assertFalse(decision.should_continue)
                    st_toctou = adapter._engine.goal_controller.load_state(chat_id)
                    self.assertTrue(st_toctou is None or st_toctou.status == "cleared")

                    # Part C: /new -> /approve during slow_judge cancels eval, clears goal, and rotates session
                    judge_entered.clear()
                    release_judge.clear()
                    llm_prompts.clear()
                    sid_before = actor._active_session()["id"]
                    await adapter.submit(chat_id, "/goal 不应跨会话残留的目标", turn_id="g-new-1")
                    await asyncio.wait_for(judge_entered.wait(), timeout=5.0)
                    await adapter.submit(chat_id, "/new", turn_id="g-new-cmd")
                    await adapter.submit(chat_id, "/approve", turn_id="g-new-apv")
                    release_judge.set()
                    await actor.wait_idle(timeout=5.0)

                    self.assertNotEqual(actor._active_session()["id"], sid_before)
                    st_new = adapter._engine.goal_controller.load_state(chat_id)
                    self.assertTrue(st_new is None or st_new.status == "cleared")
                    self.assertNotIn("/new", llm_prompts)
                    self.assertNotIn("/approve", llm_prompts)
                    self.assertEqual(len(llm_prompts), 1)
                finally:
                    release_judge.set()
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 9. Non-blocking wait_for_barrier (#277-5) ────────────────────────────

    def test_08_wait_for_barrier_releases_actor_idle_for_immediate_user_messages(self) -> None:
        async def scenario() -> None:
            replies: list[str] = []

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                last_u = next((str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), "")
                replies.append(last_u)
                yield TextDelta(f"回复:{last_u[:20]}")
                yield StreamDone()

            verdicts = iter(
                [
                    ("wait", "等待长任务 30 秒", False, {"seconds": 30}, False),
                    ("done", "全部完成", False, None, False),
                ]
            )

            async def fake_judge(self_ctrl: Any, state: Any, last_response: str, cid: str):
                return next(verdicts, ("done", "ok", False, None, False))

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ), mock.patch.object(GoalLoopController, "_call_judge", fake_judge):
                adapter, chat_id, sock = self._make_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    actor = adapter._engine.get_actor(chat_id)
                    await adapter.submit(chat_id, "/goal 监控后台编译", turn_id="t-bar-1")
                    await actor.wait_idle(timeout=5.0)

                    # Actor is IMMEDIATELY idle while _goal_barrier_task sleeps on 30s barrier!
                    self.assertEqual(actor._phase, "idle")
                    self.assertIsNotNone(actor._goal_barrier_task)

                    sock.frames.clear()
                    await adapter.submit(chat_id, "顺便问一下进度", turn_id="t-bar-user")
                    await actor.wait_idle(timeout=5.0)

                    # User message ran immediately without busy queue notice!
                    self.assertTrue(any("顺便问一下进度" in r for r in replies))
                    busy_notices = [f for f in sock.frames if "手上这件做完" in str(f.get("text") or "")]
                    self.assertEqual(busy_notices, [])
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 10. Track-1 defer approval mid-/goal pauses goal (#277-6) ────────────

    def test_09_track1_defer_approval_mid_goal_auto_pauses_without_looping(self) -> None:
        async def scenario() -> None:
            llm_calls = 0

            async def fake_stream(**_: Any):
                nonlocal llm_calls
                llm_calls += 1
                if llm_calls % 2 == 1:
                    yield ToolCallComplete(
                        id=f"tc-apv-{llm_calls}",
                        name="mcp__mail__send_email",
                        arguments=json.dumps({"to": "ops@example.com", "subject": "Report"}),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("邮件已准备好，请确认发送。")
                    yield StreamDone()

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    await adapter.submit(chat_id, "/goal 汇总并发送周报邮件", turn_id="t-apv-goal")
                    await adapter._engine.get_actor(chat_id).wait_idle(timeout=5.0)

                    # Ran exactly 1 turn (2 LLM calls: tool_call + final reply), created 1 approval, and paused goal!
                    self.assertEqual(llm_calls, 2)
                    pending = [
                        r for r in adapter.approvals.store.list_for_session(f"websocket:{chat_id}")
                        if r.status == "pending"
                    ]
                    self.assertEqual(len(pending), 1)
                    st = adapter._engine.goal_controller.load_state(chat_id)
                    self.assertIsNotNone(st)
                    self.assertEqual(st.status, "paused")
                    self.assertEqual(st.paused_reason, "等待操作确认")
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 11. Post-clarify _inject_pre_llm_context & partial delete_turn (#277-7) ──

    def test_10_post_clarify_pre_llm_context_and_partial_delete_turn_edges(self) -> None:
        async def scenario() -> None:
            seen_wire: list[list[dict[str, Any]]] = []
            call_idx = 0

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                nonlocal call_idx
                call_idx += 1
                seen_wire.append(messages)
                if call_idx in (1, 4):
                    yield ToolCallComplete(
                        id=f"cl-{call_idx}",
                        name="clarify",
                        arguments=json.dumps({"question": "选哪个环境？", "choices": ["prod", "dev"]}),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta(f"答复#{call_idx}")
                    yield StreamDone()

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream)
                comp_helpers.create(adapter.store, {"name": "研究员", "duty": "核查环境配置"})
                await adapter.startup()
                try:
                    actor = adapter._engine.get_actor(chat_id)
                    db = adapter._engine._ensure_db()
                    sid = db.get_or_create_active_session(chat_id, source="companion")["id"]

                    # Turn A -> clarify -> Turn B mentioning @研究员
                    await adapter.submit(chat_id, "开始部署", turn_id="dA")
                    for _ in range(40):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.02)
                    await adapter.submit(chat_id, "prod @研究员", turn_id="dB")
                    await actor.wait_idle(timeout=5.0)

                    # Helper context injected onto Turn B's clarify `tool` wire message, NEVER Turn A's user message!
                    turn_b_wire = seen_wire[1]
                    user_a_msg = next(m for m in turn_b_wire if m.get("role") == "user")
                    tool_b_msg = next(m for m in turn_b_wire if m.get("role") == "tool")
                    self.assertEqual(user_a_msg["content"], "开始部署")
                    self.assertIn("研究员", str(tool_b_msg["content"]))

                    # Regenerate('dB') ALSO keeps Turn A user message clean and injects onto Turn B tool row!
                    await adapter.regenerate(chat_id, "dB")
                    await actor.wait_idle(timeout=5.0)
                    regen_wire = seen_wire[2]
                    self.assertEqual(next(m for m in regen_wire if m.get("role") == "user")["content"], "开始部署")
                    self.assertIn("研究员", str(next(m for m in regen_wire if m.get("role") == "tool")["content"]))

                    # delete_turn(dB, role="reply") keeps user's clarify answer in muse.db while dropping reply!
                    adapter.store.remove_events(
                        chat_id,
                        lambda e: e.get("turn_id") == "dB" and e.get("event") != "user_message" and e.get("event") != "turn_end",
                    )
                    await adapter.delete_turn(chat_id, "dB", role="reply")
                    rows_after_reply_del = db.get_messages(sid, active_only=True)
                    self.assertEqual(
                        [(r["role"], r["turn_id"], r["content"]) for r in rows_after_reply_del],
                        [("user", "dA", "开始部署"), ("assistant", "dA", ""), ("tool", "dB", "prod @研究员")],
                    )

                    # New clarify exchange duA -> duB, then delete_turn(duB, role="user") restores clarify & drops duB reply!
                    await adapter.submit(chat_id, "第二次部署", turn_id="duA")
                    for _ in range(40):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.02)
                    await adapter.submit(chat_id, "staging", turn_id="duB")
                    await actor.wait_idle(timeout=5.0)

                    adapter.store.remove_events(
                        chat_id,
                        lambda e: e.get("turn_id") == "duB" and e.get("event") == "user_message",
                    )
                    await adapter.delete_turn(chat_id, "duB", role="user")
                    rows_after_user_del = db.get_messages(sid, active_only=True)
                    du_rows = [(r["role"], r["turn_id"], r["content"]) for r in rows_after_user_del[-3:]]
                    self.assertEqual(
                        du_rows,
                        [("user", "duA", "第二次部署"), ("assistant", "duA", ""), ("tool", "duA", CANCELLED_TOOL_RESULT)],
                    )
                    self.assertFalse(any(e.get("turn_id") == "duB" for e in adapter.store.events(chat_id)))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 12. Static type-checking & comment hygiene (#278-1) ──────────────────

    def test_11_static_type_checking_and_comment_hygiene(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for pkg_mod, rel in (
            ("companion", "companion/activity.py"),
            ("companion", "companion/approvals.py"),
            ("companion", "companion/server.py"),
        ):
            src = (repo_root / rel).read_text(encoding="utf-8")
            self.assertNotIn("from .adapter import", src)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    target = f"{pkg_mod}.{node.module}" if node.level == 1 else node.module
                    mod = importlib.import_module(target)
                    for alias in node.names:
                        if not hasattr(mod, alias.name):
                            importlib.import_module(f"{target}.{alias.name}")

        local_ns = {"CompanionAdapter": muse_app.NativeCompanionAdapter}
        hints = typing.get_type_hints(comp_activity.bind, localns=local_ns)
        self.assertIs(hints["adapter"], muse_app.NativeCompanionAdapter)
        apv_cls = getattr(comp_approvals, "Approvals", None) or getattr(comp_approvals, "ApprovalService")
        apv_hints = typing.get_type_hints(apv_cls.__init__, localns=local_ns)
        self.assertIs(apv_hints["adapter"], muse_app.NativeCompanionAdapter)

        vite_txt = (repo_root / "web/vite.config.ts").read_text(encoding="utf-8")
        self.assertNotIn("companion/adapter.py", vite_txt)

    # ── 13. Pass-3 proactive flush ordering without & with Steer (#288) ──────

    def test_12_proactive_flush_ordering_without_and_with_steer(self) -> None:
        from muse.llm.anthropic_messages import convert_messages_to_anthropic

        async def scenario() -> None:
            iter_wire: list[list[dict[str, Any]]] = []
            call_num = 0

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                nonlocal call_num
                call_num += 1
                iter_wire.append([dict(m) for m in messages])
                if call_num in (1, 3):
                    yield ToolCallComplete(id=f"tc-p3-{call_num}", name="slow_tool", arguments="{}")
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta(f"done-{call_num}")
                    yield StreamDone()

            adapter_ref: list[muse_app.NativeCompanionAdapter] = []
            chat_ref: list[str] = []
            with_steer = False

            async def exec_tool(_tc: dict[str, Any], _ctx: Any) -> str:
                await adapter_ref[0]._engine.deliver_proactive(
                    chat_ref[0],
                    "Cron提醒：有新日程",
                    job_id="job-p3",
                    attach_to_session=True,
                )
                if with_steer:
                    await adapter_ref[0].submit(chat_ref[0], "Steer: 顺便查一下天气", turn_id="t-steer-p3")
                return json.dumps({"ok": True})

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None), mock.patch(
                "companion.modules.router.on_pre_llm_call", lambda **_: {}
            ):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream, execute_tool=exec_tool)
                adapter_ref.append(adapter)
                chat_ref.append(chat_id)
                await adapter.startup()
                try:
                    actor = adapter._engine.get_actor(chat_id)
                    db = adapter._engine._ensure_db()

                    # (a) WITHOUT Steer: Iter 2 wire MUST NOT end in role="assistant", while final DB has [tool, assistant(proactive), assistant(final)]
                    await adapter.submit(chat_id, "执行工具A", turn_id="t-p3-no-steer")
                    await actor.wait_idle(timeout=5.0)

                    iter2_no_steer = iter_wire[1]
                    self.assertNotEqual(iter2_no_steer[-1]["role"], "assistant")
                    self.assertEqual(iter2_no_steer[-1]["role"], "tool")
                    _, anth_conv_a = convert_messages_to_anthropic(iter2_no_steer, model="claude-opus-4-6")
                    self.assertEqual(anth_conv_a[-1]["role"], "user")

                    sid = db.get_active_session(chat_id, source="companion")["id"]
                    rows_a = db.get_messages(sid, active_only=True)
                    tail_a = [(r["role"], bool((r.get("extra") or {}).get("proactive")), r["content"]) for r in rows_a[-3:]]
                    self.assertEqual(
                        tail_a,
                        [
                            ("tool", False, '{"ok": true}'),
                            ("assistant", True, "Cron提醒：有新日程"),
                            ("assistant", False, "done-2"),
                        ],
                    )

                    # (b) WITH Steer: Iter 2 wire & DB MUST be [..., tool, assistant(proactive), user(steer)] ending in role="user"
                    with_steer = True
                    await adapter.submit(chat_id, "执行工具B", turn_id="t-p3-with-steer")
                    await actor.wait_idle(timeout=5.0)

                    iter2_steer = iter_wire[3]
                    self.assertEqual(
                        [(m["role"], m["content"]) for m in iter2_steer[-3:]],
                        [
                            ("tool", '{"ok": true}'),
                            ("assistant", "Cron提醒：有新日程"),
                            ("user", "Steer: 顺便查一下天气"),
                        ],
                    )
                    self.assertEqual(iter2_steer[-1]["role"], "user")
                    _, anth_conv_b = convert_messages_to_anthropic(iter2_steer, model="claude-opus-4-6")
                    self.assertEqual(anth_conv_b[-1]["role"], "user")

                    rows_b = db.get_messages(sid, active_only=True)
                    tail_b = [
                        (r["role"], bool((r.get("extra") or {}).get("proactive")), r["content"])
                        for r in rows_b[-4:]
                    ]
                    self.assertEqual(
                        tail_b,
                        [
                            ("tool", False, '{"ok": true}'),
                            ("assistant", True, "Cron提醒：有新日程"),
                            ("user", False, "Steer: 顺便查一下天气"),
                            ("assistant", False, "done-4"),
                        ],
                    )
                    self.assertEqual(db.heal_orphan_tool_calls(sid), 0)
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
