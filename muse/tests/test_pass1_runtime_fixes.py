"""Comprehensive Pass-1 Slice-2 regression suite (`#259`, `#260`, `#261`, PR #28).

Verifies all 10 runtime, adapter, turn_actor, multimodal, `/goal`, cron, `tool_search`,
`chats.change`, engine/helper routing, lifecycle, and CLI/bootstrap fixes end-to-end.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image
from pypdf import PdfWriter

from companion.modules import companion_state, helpers as comp_helpers, interrupted, models as comp_models
from companion.server import _handle_mutation
from muse import app as muse_app
from muse import cli as muse_cli
from muse import runtime as muse_runtime
from muse.config_store import save_config
from muse.context import PromptAssembler, TurnExecutionContext, turn_context_scope
from muse.db import MuseDB, close_all
from muse.engine import ChatTurnEngine
from muse.helpers_orch import resolve_helper_model_override
from muse.interceptors import NativeApprovalCoordinator, ToolInterceptorPipeline, ToolInvocation
from muse.llm.types import StreamDone, TextDelta, ToolCallComplete, UsageInfo
from muse.paths import get_repo_root, muse_home_override


class _RecordingSocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.frames.append(dict(payload))


class Pass1RuntimeFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse-pass1-rt-")
        self.home = Path(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        muse_runtime.close_all_shared_dbs()
        close_all()
        self._tmp.cleanup()

    def _make_host(
        self, *, stream_llm: Any = None, execute_tool: Any = None, start_bg: bool = False, home: Path | None = None
    ) -> tuple[muse_app.NativeCompanionAdapter, str, _RecordingSocket]:
        target_home = home or self.home
        adapter = muse_app.NativeCompanionAdapter(home=target_home, start_background_services=start_bg)
        adapter._engine = muse_runtime.NativeAgentEngine(
            adapter, home=target_home, stream_llm=stream_llm, execute_tool=execute_tool, start_background_services=start_bg
        )
        chat_id = adapter.store.create()
        sock = _RecordingSocket()
        adapter.attach(chat_id, sock)
        return adapter, chat_id, sock

    # ── 1. emit_activity wire contract + send_media_file projection_id ───────

    def test_01_emit_activity_wire_schema_and_send_media_projection_id(self) -> None:
        async def scenario() -> None:
            note = self.home / "hello.txt"
            note.write_text("hello activity trace", encoding="utf-8")
            step = 0

            async def fake_stream(**_: Any):
                nonlocal step
                step += 1
                if step == 1:
                    yield ToolCallComplete(id="call-rf-1", name="read_file", arguments=json.dumps({"path": str(note)}))
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("读取完成。")
                    yield StreamDone()

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None):
                adapter, chat_id, sock = self._make_host(stream_llm=fake_stream)
                self.assertIsNotNone(adapter.engine.approval_coordinator)
                self.assertIsNotNone(adapter.engine.approval_coordinator._emit_activity_fn)
                self.assertIsNotNone(adapter.engine.pipeline.interceptors[0]._emit_fn)  # type: ignore[attr-defined]
                await adapter.startup()
                try:
                    await adapter.submit(chat_id, "读一下文件", "turn-act-1")
                    await adapter.engine.get_actor(chat_id).wait_idle(timeout=3.0)

                    ws_hints = [f for f in sock.frames if f.get("kind") == "tool_hint"]
                    self.assertEqual(len(ws_hints), 2)
                    for idx, phase in enumerate(("start", "end")):
                        ev = ws_hints[idx]
                        self.assertEqual(ev["event"], "message")
                        self.assertEqual(ev["text"], "")
                        self.assertEqual(ev["turn_phase"], "activity")
                        self.assertEqual(ev["turn_id"], "turn-act-1")
                        self.assertEqual(len(ev["tool_events"]), 1)
                        te = ev["tool_events"][0]
                        self.assertEqual(te["version"], 1)
                        self.assertEqual(te["phase"], phase)
                        self.assertEqual(te["call_id"], "call-rf-1")
                        self.assertEqual(te["name"], "read_file")

                    # Persisted in ChatStore and passes thread_payload replay
                    thread = adapter.store.thread_payload(chat_id)
                    persisted_hints = [e for e in thread["events"] if e.get("kind") == "tool_hint"]
                    self.assertEqual([h["tool_events"][0]["phase"] for h in persisted_hints], ["start", "end"])

                    # send_media_file includes canonical projection_id on both success & fallback
                    img_path = self.home / "pic.png"
                    Image.new("RGB", (8, 8), color=(20, 40, 80)).save(img_path, format="PNG")
                    self.assertTrue(await adapter.send_media_file(chat_id, str(img_path), caption="图", turn_id="t-m1"))
                    self.assertFalse(await adapter.send_media_file(chat_id, str(self.home / "missing.png"), turn_id="t-m2"))
                    media_msgs = [f for f in sock.frames if f.get("turn_id") in ("t-m1", "t-m2") and f.get("event") == "message"]
                    self.assertEqual(len(media_msgs), 2)
                    for mm in media_msgs:
                        self.assertTrue(str(mm.get("projection_id") or "").startswith("history-"))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 2. Multimodal files -> content_parts + clarify turn_B attachments ────

    def test_02_multimodal_files_images_pdf_text_and_clarify_turn_b_files(self) -> None:
        async def scenario() -> None:
            img_a = self.home / "upload_a.png"
            Image.new("RGBA", (16, 16), color=(255, 0, 0, 128)).save(img_a, format="PNG")
            txt_a = self.home / "notes.txt"
            txt_a.write_text("deploy_env=staging\nport=8080\n", encoding="utf-8")
            pdf_a = self.home / "spec.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            with pdf_a.open("wb") as fh:
                writer.write(fh)

            img_b = self.home / "clarify_b.png"
            Image.new("RGB", (12, 12), color=(0, 200, 100)).save(img_b, format="PNG")

            captured_msgs: list[list[dict[str, Any]]] = []

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                captured_msgs.append(messages)
                if len(captured_msgs) == 1:
                    yield ToolCallComplete(
                        id="cl-mm-1",
                        name="clarify",
                        arguments=json.dumps({"question": "发到哪个环境？", "choices": ["prod", "staging"]}),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("已收到图片与环境确认。")
                    yield StreamDone()

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    await adapter.submit(
                        chat_id,
                        "请检查这些附件",
                        "turn_mm_A",
                        attachments=[
                            (img_a, "image/png", "upload_a.png"),
                            (txt_a, "text/plain", "notes.txt"),
                            (pdf_a, "application/pdf", "spec.pdf"),
                        ],
                    )
                    actor = adapter.engine.get_actor(chat_id)
                    for _ in range(100):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(actor._phase, "clarify_wait")

                    first_user = next(m for m in captured_msgs[0] if m.get("role") == "user")
                    parts = first_user["content"]
                    self.assertIsInstance(parts, list)
                    self.assertTrue(any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/") for p in parts))
                    joined_text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
                    self.assertIn("deploy_env=staging", joined_text)
                    self.assertIn("spec.pdf", joined_text)

                    # Answer clarify on turn_B WITH an attached image!
                    await adapter.submit(
                        chat_id,
                        "prod",
                        "turn_mm_B",
                        attachments=[(img_b, "image/png", "clarify_b.png")],
                    )
                    await actor.wait_idle(timeout=3.0)

                    self.assertEqual(len(captured_msgs), 2)
                    second_call = captured_msgs[1]
                    self.assertTrue(any(m.get("role") == "tool" and m.get("content") == "prod" for m in second_call))
                    clarify_att_msgs = [
                        m for m in second_call
                        if m.get("role") == "user"
                        and isinstance(m.get("content"), list)
                        and any("clarify" in str(p.get("text", "")).lower() for p in m["content"] if isinstance(p, dict))
                    ]
                    self.assertEqual(len(clarify_att_msgs), 1)
                    self.assertTrue(
                        any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/") for p in clarify_att_msgs[0]["content"])
                    )
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 3. Slash commands during clarify_wait + post-clarify /undo sync ──────

    def test_03_slash_commands_during_clarify_wait_and_post_clarify_undo(self) -> None:
        async def scenario() -> None:
            llm_calls = 0

            async def fake_stream(**_: Any):
                nonlocal llm_calls
                llm_calls += 1
                if llm_calls in (1, 2):
                    yield ToolCallComplete(
                        id=f"cl-{llm_calls}",
                        name="clarify",
                        arguments=json.dumps({"question": "选哪个？", "choices": ["prod", "dev"]}),
                    )
                    yield StreamDone(finish_reason="tool_calls")
                else:
                    yield TextDelta("已部署到 prod。")
                    yield StreamDone()

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None):
                adapter, chat_id, _ = self._make_host(stream_llm=fake_stream)
                await adapter.startup()
                try:
                    actor = adapter.engine.get_actor(chat_id)
                    # (a) /new during clarify_wait opens confirm card instead of feeding "/new" to LLM
                    await adapter.submit(chat_id, "第一次部署", "tA1")
                    for _ in range(100):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(llm_calls, 1)

                    await adapter.submit(chat_id, "/compact", "t-cmp-clarify")
                    self.assertEqual(llm_calls, 1)
                    self.assertEqual(actor._phase, "clarify_wait")

                    await adapter.submit(chat_id, "/new", "t-new-ask")
                    self.assertEqual(llm_calls, 1)
                    self.assertIsNotNone(actor._pending_confirm)
                    await adapter.submit(chat_id, "/approve", "t-new-ok")
                    await actor.wait_idle(timeout=2.0)
                    self.assertEqual(llm_calls, 1)
                    self.assertIsNone(actor._pending_clarify)
                    self.assertEqual(actor._phase, "idle")

                    # (b) Post-clarify /undo preserves turn_A in both ChatStore and muse.db!
                    await adapter.submit(chat_id, "第二次部署", "turn_A")
                    for _ in range(100):
                        if actor._phase == "clarify_wait":
                            break
                        await asyncio.sleep(0.01)
                    await adapter.submit(chat_id, "prod", "turn_B")
                    await actor.wait_idle(timeout=2.0)
                    self.assertEqual(llm_calls, 3)

                    await adapter.submit(chat_id, "/undo", "t-undo-ask")
                    await adapter.submit(chat_id, "/approve", "t-undo-ok")
                    await actor.wait_idle(timeout=2.0)

                    store_tids = {e.get("turn_id") for e in adapter.store.events(chat_id) if e.get("turn_id")}
                    self.assertIn("turn_A", store_tids)
                    self.assertNotIn("turn_B", store_tids)

                    sid = adapter.engine.db.get_active_session(chat_id)["id"]
                    db_rows = adapter.engine.db.get_messages(sid, active_only=True)
                    self.assertGreaterEqual(len(db_rows), 2)
                    self.assertEqual(db_rows[0]["content"], "第二次部署")
                    self.assertFalse(any(r.get("turn_id") == "turn_B" for r in db_rows))
                    self.assertFalse(any("已部署到 prod" in str(r.get("content") or "") for r in db_rows))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 4. /goal & /subgoal full autonomous loop + gates ─────────────────────

    def test_04_goal_and_subgoal_full_autonomous_loop_and_gates(self) -> None:
        async def scenario() -> None:
            gate_marker = self.home / "gate_pass.txt"
            turn_prompts: list[str] = []
            judge_calls = 0

            async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                user_msgs = [m for m in messages if m.get("role") == "user"]
                last_txt = str(user_msgs[-1].get("content") or "")
                turn_prompts.append(last_txt)
                if len(turn_prompts) == 1:
                    yield TextDelta("第 1 步：创建基础代码（门禁尚未通过）。")
                elif len(turn_prompts) == 2:
                    gate_marker.write_text("ok", encoding="utf-8")
                    yield TextDelta("第 2 步：修复门禁问题。")
                else:
                    yield TextDelta("第 3 步：完成全部子目标并总结。")
                yield StreamDone()

            async def fake_judge(_sys: str, _usr: str, _timeout: float, _max_tok: int) -> str:
                nonlocal judge_calls
                judge_calls += 1
                if judge_calls == 1:
                    return '{"verdict": "continue", "reason": "finish subgoal summary"}'
                return '{"verdict": "done", "reason": "all criteria met"}'

            with muse_home_override(self.home), mock.patch("companion.modules.router.prefetch", lambda *a, **k: None):
                adapter, chat_id, sock = self._make_host(stream_llm=fake_stream)
                adapter.engine.goal_controller._judge_llm_fn = fake_judge
                adapter.engine.goal_controller._gate_cwd = str(self.home)
                await adapter.startup()
                try:
                    actor = adapter.engine.get_actor(chat_id)
                    # Pause first so we can attach gate + subgoal deterministically, OR test /goal status before start
                    await adapter.submit(chat_id, "/goal status", "t-gs-0")
                    self.assertEqual(len(turn_prompts), 0)

                    # Set goal via controller, add gate & subgoal via chat commands (zero LLM turns!), then resume!
                    await adapter.engine.goal_controller.set_goal(chat_id, "完成基准测试套件")
                    await adapter.submit(chat_id, "/goal pause", "webui-system:pause-0")

                    gate_py = f'"{os.sys.executable}" -c "import pathlib, sys; sys.exit(0 if pathlib.Path(r\'{gate_marker}\').exists() else 1)"'
                    await adapter.submit(chat_id, f"/goal gate add {gate_py}", "t-gate-add")
                    await adapter.submit(chat_id, "/subgoal 输出汇总报告", "t-subgoal-add")
                    await adapter.submit(chat_id, "/goal status", "t-gs-1")
                    self.assertEqual(len(turn_prompts), 0)

                    st_mid = adapter.engine.goal_controller.load_state(chat_id)
                    self.assertIsNotNone(st_mid)
                    assert st_mid is not None
                    self.assertEqual(len(st_mid.gates), 1)
                    self.assertEqual(st_mid.subgoals, ["输出汇总报告"])

                    # Now kick off a fresh `/goal <objective>` directly from chat!
                    gate_marker.unlink(missing_ok=True)
                    await adapter.submit(chat_id, "/goal 交付完整基准测试", "t-goal-start")
                    # Re-attach gate before Turn 1 finishes? Even better: test `/goal <objective>` multi-turn judge continuation!
                    await actor.wait_idle(timeout=5.0)

                    st_done = adapter.engine.goal_controller.load_state(chat_id)
                    assert st_done is not None
                    self.assertEqual(st_done.status, "done")
                    self.assertGreaterEqual(len(turn_prompts), 2)
                    self.assertTrue(any("✓ 这件事办完了" in str(f.get("text") or "") for f in sock.frames))
                    goal_frames = [f["goal_state"] for f in sock.frames if f.get("event") == "goal_state"]
                    self.assertTrue(any(g.get("active") is True for g in goal_frames))
                    self.assertFalse(goal_frames[-1]["active"])
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 5. Cron enabled_toolsets, hidden sessions, cron approvals & usage ────

    def test_05_cron_enabled_toolsets_hidden_sessions_approvals_and_usage_source(self) -> None:
        async def scenario() -> None:
            seen_tool_counts: list[int] = []

            async def fake_cron_transport_stream(**kw: Any):
                tools = kw.get("tools") or []
                seen_tool_counts.append(len(tools))
                if len(seen_tool_counts) == 1:
                    yield ToolCallComplete(
                        id="tc-cron-risk", name="terminal", arguments=json.dumps({"command": "rm -rf /tmp/cron_test"})
                    )
                    yield StreamDone(finish_reason="tool_calls", usage=UsageInfo(input_tokens=100, output_tokens=50))
                else:
                    yield TextDelta("定时检查已完成。")
                    yield StreamDone(finish_reason="stop", usage=UsageInfo(input_tokens=80, output_tokens=30))

            class FakeTransport:
                async def stream(self, **kw: Any):
                    async for ev in fake_cron_transport_stream(**kw):
                        yield ev

            with muse_home_override(self.home):
                save_config({"model": {"provider": "openai", "default": "gpt-4o-mini"}, "providers": {"openai": {"api_key": "sk-test"}}})
                adapter, main_chat, _ = self._make_host()
                companion_state.update_companion_state({"chat_id": main_chat, "name": "伙伴"})
                await adapter.startup()
                try:
                    # (a) TaintAndPermissionsInterceptor blocks disallowed toolset directly
                    blocked = await adapter.engine.pipeline.execute(
                        "goal_add",
                        {"title": "Should not be created"},
                        call_id="c-block-ts",
                        ctx=TurnExecutionContext(
                            chat_id=main_chat, facing_chat_id=main_chat, session_id="s-ts", enabled_toolsets=("file",)
                        ),
                    )
                    self.assertIn("enabled_toolsets", blocked)

                    # (b) _run_cron_job plumbs enabled_toolsets=["file","terminal"], routes risky terminal via non-blocking defer to main_chat,
                    #     keeps cron:* out of ChatStore.list() & interrupted, and records source='cron' usage!
                    with mock.patch("muse.llm.auxiliary.create_transport", return_value=FakeTransport()):
                        ans = await asyncio.wait_for(
                            adapter.engine._run_cron_job(
                                {"id": "job_alpha", "enabled_toolsets": ["file", "terminal"], "deliver": f"companion:{main_chat}"},
                                "执行每日巡检",
                            ),
                            timeout=3.0,
                        )
                    self.assertIn("定时检查已完成", ans)
                    expected_file_and_term = len(
                        adapter.engine.tool_registry.get_definitions(enabled_toolsets=["file", "terminal"])
                    )
                    self.assertEqual(seen_tool_counts[0], expected_file_and_term)
                    self.assertLess(seen_tool_counts[0], 15)

                    # Never leaks websocket:cron:* into ChatStore.list() or interrupted.announce
                    session_keys = [row["key"] for row in adapter.store.list()]
                    self.assertFalse(any("cron:" in k for k in session_keys), session_keys)

                    # Risky cron command deferred to main_chat (never websocket:cron:job_alpha!)
                    cron_apvs = adapter.approvals.store.list_for_session("websocket:cron:job_alpha")
                    main_apvs = adapter.approvals.store.list_for_session(f"websocket:{main_chat}")
                    self.assertEqual(cron_apvs, [])
                    self.assertEqual(len(main_apvs), 1)
                    self.assertEqual(main_apvs[0].action, "run_command")

                    # Usage attributed to 'cron' in models.usage_summary()
                    summary = comp_models.usage_summary("Asia/Shanghai")
                    self.assertEqual(summary["today"]["by_source"].get("cron"), 100 + 50 + 80 + 30)
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 6. tool_search MCP discovery + per-session activation ────────────────

    def test_06_tool_search_mcp_discovery_and_session_activations(self) -> None:
        async def scenario() -> None:
            with muse_home_override(self.home):
                adapter, chat_id, _ = self._make_host()
                reg = adapter.engine.tool_registry
                for i in range(95):
                    reg.register(
                        f"extra_overflow_tool_{i}",
                        toolset="overflow_custom",
                        description=f"Overflow helper tool #{i}",
                        handler=lambda _a: "ok",
                        always_loaded=False,
                    )
                fake_mcp_tool = {
                    "type": "function",
                    "toolset": "mcp",
                    "function": {
                        "name": "mcp__github__create_issue",
                        "description": "Create a GitHub issue in a repository",
                        "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
                    },
                }
                fake_mgr = mock.Mock()
                fake_mgr.get_tool_definitions.return_value = [fake_mcp_tool]
                adapter.engine._mcp_manager = fake_mgr

                ctx_s1 = TurnExecutionContext(chat_id=chat_id, facing_chat_id=chat_id, session_id="sess-probe-1")
                inv = ToolInvocation(call_id="ts-1", tool_name="tool_search", arguments={"query": "github create_issue"}, ctx=ctx_s1)
                raw = await adapter.engine._dispatch_registered_tool("tool_search", {"query": "github create_issue"}, inv)
                parsed = json.loads(raw)
                self.assertTrue(parsed["success"])
                self.assertIn("mcp__github__create_issue", parsed["activated"])

                await reg.dispatch("tool_search", {"query": "extra_overflow_tool_90"}, session_id="sess-probe-1")
                with turn_context_scope(ctx_s1):
                    defs_s1 = adapter.engine._collect_tools_schema()
                names_s1 = {d["function"]["name"] for d in defs_s1}
                self.assertIn("mcp__github__create_issue", names_s1)
                self.assertIn("extra_overflow_tool_90", names_s1)

                await adapter.shutdown()

        asyncio.run(scenario())

    # ── 7. companion.chats.change DB/actor sync + regenerate pre-LLM text ────

    def test_07_chats_change_delete_sync_and_regenerate_pre_llm_context(self) -> None:
        async def scenario() -> None:
            router_seen_texts: list[str] = []

            def spy_pre_llm(**kw: Any) -> dict[str, str]:
                router_seen_texts.append(str(kw.get("user_message") or ""))
                return {"context": "【路由提示】测试"}

            async def fake_stream(**_: Any):
                yield TextDelta("回答内容。")
                yield StreamDone()

            with (
                muse_home_override(self.home),
                mock.patch("companion.modules.router.prefetch", lambda *a, **k: None),
                mock.patch("companion.modules.router.on_pre_llm_call", side_effect=spy_pre_llm),
            ):
                adapter, main_chat, _ = self._make_host(stream_llm=fake_stream)
                companion_state.update_companion_state({"chat_id": main_chat, "name": "伙伴"})
                side_chat = adapter.store.create()
                await adapter.startup()
                try:
                    await adapter.submit(side_chat, "推荐周末露营装备", "t-camp-1")
                    await adapter.engine.get_actor(side_chat).wait_idle(timeout=2.0)
                    await adapter.regenerate(side_chat, "t-camp-1")
                    await adapter.engine.get_actor(side_chat).wait_idle(timeout=2.0)

                    self.assertEqual(router_seen_texts, ["推荐周末露营装备", "推荐周末露营装备"])
                    self.assertNotIn("/retry", router_seen_texts)

                    # delete_message syncs muse.db
                    mut_sock = _RecordingSocket()
                    sid = adapter.engine.db.get_active_session(side_chat)["id"]
                    await _handle_mutation(
                        adapter, mut_sock,  # type: ignore[arg-type]
                        {"request_id": "req-1", "action": "companion.chats.change", "payload": {"id": side_chat, "action": "delete_message", "turn_id": "t-camp-1", "role": "reply"}},
                    )
                    await _handle_mutation(
                        adapter, mut_sock,  # type: ignore[arg-type]
                        {"request_id": "req-2", "action": "companion.chats.change", "payload": {"id": side_chat, "action": "delete_message", "turn_id": "t-camp-1", "role": "user"}},
                    )
                    self.assertEqual(adapter.engine.db.get_messages(sid, active_only=True), [])

                    # delete chat removes actor & deactivates session
                    self.assertIn(side_chat, adapter.engine._actors)
                    await _handle_mutation(
                        adapter, mut_sock,  # type: ignore[arg-type]
                        {"request_id": "req-3", "action": "companion.chats.change", "payload": {"id": side_chat, "action": "delete"}},
                    )
                    self.assertNotIn(side_chat, adapter.engine._actors)
                    self.assertIsNone(adapter.engine.db.get_active_session(side_chat))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 8. Engine reasoning/vision/fallbacks/max_turns + helper OAuth ────────

    def test_08_engine_reasoning_vision_fallbacks_max_turns_and_helper_oauth(self) -> None:
        async def scenario() -> None:
            attempted_models: list[tuple[str, Any]] = []
            blind_flags: list[bool] = []

            class FakeTransport:
                def __init__(self, provider: str):
                    self.provider = provider

                async def stream(self, **kw: Any):
                    model = kw.get("model", "")
                    attempted_models.append((model, kw.get("reasoning_config")))
                    if model == "blind-primary":
                        raise RuntimeError("503 primary overloaded")
                    yield TextDelta(f"answered by {model}")
                    yield StreamDone()

            async def spy_blind(messages: list[dict[str, Any]], *, chat_vision: bool, **_: Any) -> list[dict[str, Any]]:
                blind_flags.append(chat_vision)
                return messages

            with (
                muse_home_override(self.home),
                mock.patch("companion.modules.router.prefetch", lambda *a, **k: None),
                mock.patch("companion.modules.router.on_pre_llm_call", lambda **_: {"context": ""}),
                mock.patch.object(muse_app.NativeCompanionAdapter, "_name_chat", lambda self, cid: None),
            ):
                models_file = self.home / "companion" / "models.json"
                models_file.parent.mkdir(parents=True, exist_ok=True)
                models_data = {
                    "presets": {
                        "主力盲模型": {"provider": "deepseek", "model": "blind-primary", "reasoning_effort": "high"},
                        "备用模型": {"provider": "openrouter", "model": "backup-v2"},
                        "Codex帮手": {"provider": "openai-codex", "model": "gpt-5-codex", "auth": True},
                    },
                    "default": "主力盲模型",
                    "fallbacks": ["备用模型"],
                    "max_tool_iterations": 77,
                    "router": {
                        "enabled": True,
                        "fixed": None,
                        "models": {
                            "主力盲模型": {"description": "", "tier": "balanced", "vision": False, "price_in": 1.0, "price_out": 2.0},
                            "Codex帮手": {"description": "", "tier": "strong", "vision": True, "price_in": None, "price_out": None},
                        },
                    },
                    "media": {k: {"active": None, "models": {}} for k in ("image", "video", "tts", "stt")},
                }
                models_file.write_text(json.dumps(models_data, ensure_ascii=False), encoding="utf-8")
                save_config({
                    "providers": {
                        "deepseek": {"base_url": "https://api.deepseek.com", "api_key": "sk-ds"},
                        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or"},
                    }
                })
                comp_models.apply_to_hermes()

                adapter, chat_id, sock = self._make_host()
                await adapter.startup()
                try:
                    actor = adapter.engine.get_actor(chat_id)
                    self.assertEqual(actor.max_iterations, 77)

                    with (
                        mock.patch("muse.llm.auxiliary.create_transport", side_effect=lambda **kw: FakeTransport(kw.get("provider", ""))),
                        mock.patch("muse.llm.auxiliary.describe_images_if_blind", side_effect=spy_blind),
                    ):
                        await adapter.submit(chat_id, "测试主备切换与思考强度", "t-fb-1")
                        await actor.wait_idle(timeout=3.0)

                    self.assertEqual(attempted_models[0], ("blind-primary", {"enabled": True, "effort": "high"}))
                    self.assertEqual(attempted_models[1][0], "backup-v2")
                    self.assertIn(False, blind_flags)
                    self.assertTrue(any("answered by backup-v2" in str(f.get("text") or "") for f in sock.frames))

                    # Direct helper chat uses helper's configured model & OAuth override omits stale api_key!
                    helper = comp_helpers.create(
                        adapter.store, {"name": "代码帮手", "title": "写代码", "instructions": "简洁", "model": "Codex帮手"}
                    )
                    ov = resolve_helper_model_override(helper)
                    self.assertIsNotNone(ov)
                    assert ov is not None
                    self.assertNotIn("api_key", ov)
                    self.assertEqual(ov["vision"], True)

                    attempted_models.clear()
                    with (
                        mock.patch(
                            "muse.llm.providers.resolve_runtime_provider",
                            side_effect=lambda requested=None, **kw: {
                                "provider": requested or "openai-codex",
                                "model": kw.get("target_model") or "gpt-5-codex",
                                "base_url": "https://chatgpt.com/backend-api/codex",
                                "api_key": kw.get("api_key") or "fresh-rotated-jwt",
                                "api_mode": "codex_responses",
                            },
                        ) as spy_resolve,
                        mock.patch("muse.llm.auxiliary.create_transport", side_effect=lambda **kw: FakeTransport(kw.get("provider", ""))),
                    ):
                        h_chat = helper["chat_id"]
                        await adapter.submit(h_chat, "直接找帮手聊聊", "t-direct-helper")
                        await adapter.engine.get_actor(h_chat).wait_idle(timeout=3.0)

                    self.assertEqual(attempted_models[-1][0], "gpt-5-codex")
                    self.assertIsNone(spy_resolve.call_args.kwargs.get("api_key"))
                finally:
                    await adapter.shutdown()

        asyncio.run(scenario())

    # ── 9. Prompt MEDIA:, post-turn lifecycle, .env loading & hold scopes ────

    def test_09_prompt_media_guidance_post_turn_lifecycle_dotenv_and_approval_scopes(self) -> None:
        async def scenario() -> None:
            with muse_home_override(self.home):
                # (a) PromptAssembler includes MEDIA:/absolute/path/to/file guidance
                asm = PromptAssembler()
                with MuseDB(self.home / "muse.db") as db:
                    sid = db.create_session("c-prompt")
                    prompt = asm.assemble(session_id=sid, chat_id="c-prompt", db=db, home=self.home)
                self.assertIn("MEDIA:/absolute/path/to/file", prompt)

                # (b) create_app loads <MUSE_HOME>/.env with override=False and blocks PYTHONPATH
                (self.home / ".env").write_text(
                    "PASS1_DOTENV_PROBE_KEY=loaded_from_dotenv\nPYTHONPATH=/evil/inject\n",
                    encoding="utf-8",
                )
                old_pp = os.environ.get("PYTHONPATH")
                os.environ.pop("PASS1_DOTENV_PROBE_KEY", None)
                try:
                    muse_app.load_home_dotenv(self.home)
                    self.assertEqual(os.environ.get("PASS1_DOTENV_PROBE_KEY"), "loaded_from_dotenv")
                    self.assertEqual(os.environ.get("PYTHONPATH"), old_pp)
                finally:
                    os.environ.pop("PASS1_DOTENV_PROBE_KEY", None)

                # (c) Post-turn hooks: side-chat _name_chat, memory_tidy.maybe_tidy_soon, computer.close_after_task,
                #     startup pairing code log, and hold_and_await_exec scopes alignment
                async def fake_stream(**_: Any):
                    yield TextDelta("给你整理好了饮食计划。")
                    yield StreamDone()

                tidy_calls: list[bool] = []
                close_calls: list[str] = []
                with (
                    mock.patch("companion.modules.router.prefetch", lambda *a, **k: None),
                    mock.patch("companion.modules.memory_tidy.maybe_tidy_soon", side_effect=lambda: tidy_calls.append(True)),
                    mock.patch("companion.modules.computer.close_after_task", side_effect=lambda cid: close_calls.append(cid)),
                    mock.patch("companion.modules.interrupted.announce", new=mock.AsyncMock()),
                ):
                    adapter, main_chat, _ = self._make_host(stream_llm=fake_stream, start_bg=True)
                    companion_state.update_companion_state({"chat_id": main_chat, "name": "伙伴"})
                    side_chat = adapter.store.create()
                    with self.assertLogs("muse.app", level=logging.WARNING) as cm:
                        await adapter.startup()
                    self.assertTrue(any("pairing code" in line for line in cm.output))
                    try:
                        await adapter.submit(side_chat, "帮我记录本周饮食", "t-side-1")
                        await adapter.engine.get_actor(side_chat).wait_idle(timeout=2.0)
                        for _ in range(50):
                            if adapter.store.title(side_chat):
                                break
                            await asyncio.sleep(0.02)
                        self.assertTrue(bool(adapter.store.title(side_chat)))
                        self.assertGreaterEqual(len(tidy_calls), 1)
                        self.assertIn(side_chat, close_calls)

                        # Check hold_and_await_exec choices include "24h"
                        coord = NativeApprovalCoordinator(
                            exec_runner=lambda _c: "ok", approvals=adapter.approvals, adapter=adapter, default_exec_timeout_s=0.06
                        )
                        inv = ToolInvocation(
                            call_id="c-scope", tool_name="terminal", arguments={"command": "sudo ls"},
                            ctx=TurnExecutionContext(chat_id=main_chat, facing_chat_id=main_chat, session_id="s-sc"),
                        )
                        ok, _, rec = await coord.hold_and_await_exec(
                            inv, command="sudo ls", reason="管理员提权命令", facing_chat=main_chat, timeout_s=0.06
                        )
                        self.assertFalse(ok)
                        raw_saved = json.loads((adapter.webui_dir / "approvals" / f"{rec.id}.json").read_text(encoding="utf-8"))
                        self.assertEqual(raw_saved["extra"]["choices"], ["once", "24h", "session", "deny"])
                    finally:
                        await adapter.shutdown()

        asyncio.run(scenario())

    # ── 10. CLI _clean_legacy_artifacts v1 manifest + setup.py Py3.9 + no adapter.py ──

    def test_10_cli_clean_legacy_v1_manifest_setup_py39_bytes_and_no_legacy_adapter(self) -> None:
        repo_root = get_repo_root()
        self.assertFalse((repo_root / "companion" / "adapter.py").exists())

        # AST check: tools/setup.py must not pass Python 3.10+ `newline=` to Path.write_text
        setup_src = (repo_root / "tools" / "setup.py").read_text(encoding="utf-8")
        tree = ast.parse(setup_src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
                kw_names = {kw.arg for kw in node.keywords}
                self.assertNotIn("newline", kw_names, "Path.write_text(newline=...) breaks Python 3.9 bootstrap!")

        # Real v1.x line-delimited `<skill>:<md5>` categorized skills + path-traversal / `.` guard
        fake_repo = self.home / "fake_repo"
        v1_home = self.home / "hermes-home"
        (fake_repo / "hermes").mkdir(parents=True)
        (fake_repo / "hermes-env").mkdir(parents=True)
        (v1_home / "tools").mkdir(parents=True)

        # Unmodified categorized multi-file skill (POSIX MD5)
        s_airtable = v1_home / "skills" / "productivity" / "airtable"
        (s_airtable / "refs").mkdir(parents=True)
        (s_airtable / "SKILL.md").write_bytes(b"---\nname: airtable\ndescription: d\n---\nbody\n")
        (s_airtable / "refs" / "api.md").write_bytes(b"# API\n")
        h_posix = hashlib.md5()
        for p in sorted(f for f in s_airtable.rglob("*") if f.is_file()):
            h_posix.update(p.relative_to(s_airtable).as_posix().encode("utf-8"))
            h_posix.update(p.read_bytes())

        # Unmodified categorized multi-file skill (Windows `\` MD5)
        s_diag = v1_home / "skills" / "diagrams" / "arch"
        (s_diag / "sub").mkdir(parents=True)
        (s_diag / "SKILL.md").write_bytes(b"---\nname: arch\ndescription: d\n---\nbody\n")
        (s_diag / "sub" / "spec.txt").write_bytes(b"spec\n")
        h_win = hashlib.md5()
        for p in sorted(f for f in s_diag.rglob("*") if f.is_file()):
            h_win.update(p.relative_to(s_diag).as_posix().replace("/", "\\").encode("utf-8"))
            h_win.update(p.read_bytes())

        # Modified categorized skill + user custom skill
        s_mod = v1_home / "skills" / "coding" / "edited_bundled"
        s_mod.mkdir(parents=True)
        (s_mod / "SKILL.md").write_bytes(b"# User customized!\n")
        s_custom = v1_home / "skills" / "my_custom_skill"
        s_custom.mkdir(parents=True)
        (s_custom / "SKILL.md").write_bytes(b"# Pure user skill\n")

        manifest_lines = "\n".join([
            f"airtable:{h_posix.hexdigest()}",
            f"arch:{h_win.hexdigest()}",
            "edited_bundled:00000000000000000000000000000000",
            ".:d41d8cd98f00b204e9800998ecf8427e",
            "..:d41d8cd98f00b204e9800998ecf8427e",
        ]) + "\n"
        (v1_home / "skills" / ".bundled_manifest").write_text(manifest_lines, encoding="utf-8")

        with MuseDB(v1_home / "muse.db"):
            pass

        rep = muse_cli.run_doctor(home=v1_home, repo_root=fake_repo, clean_legacy=True)
        cl = rep["clean_legacy"]
        self.assertTrue(cl["executed"])
        self.assertFalse(s_airtable.exists())
        self.assertFalse((v1_home / "skills" / "productivity").exists(), "Empty category dir should be pruned")
        self.assertFalse(s_diag.exists())
        self.assertFalse((v1_home / "skills" / "diagrams").exists(), "Empty category dir should be pruned")
        self.assertTrue((v1_home / "skills").is_dir(), "skills root must never be deleted")
        self.assertTrue(s_mod.exists())
        self.assertTrue(s_custom.exists())
        self.assertIn("edited_bundled", cl["preserved_modified_skills"])


if __name__ == "__main__":
    unittest.main()
