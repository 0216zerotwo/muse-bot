"""Comprehensive tests for PR-5: 5-stage `ToolInterceptor` pipeline, 4-layer privacy hardening,
dual-track approvals (`defer` & `hold_exec`), deadlock-free nested async `HelperOrchestrator`,
and `GoalLoopController`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from muse.config_store import save_env_value
from muse.db import MuseDB
from muse.goal_loop import (
    GOAL_ALREADY_SEEN_KICK,
    NOTICE_GOAL_DONE,
    NOTICE_GOAL_PARKED,
    NOTICE_GOAL_PAUSED,
    GoalLoopController,
    goal_kick_prompt,
    parse_contract,
)
from muse.helpers_orch import HelperOrchestrator, resolve_helper_model_override
from muse.interceptors import (
    SECRETS_BLOCK_MESSAGE,
    SELF_CHANGE_MESSAGE,
    ActivityEmitter,
    BrowserOwnershipInterceptor,
    LogPrivacyFilter,
    NativeApprovalCoordinator,
    PostToolArtifactInterceptor,
    SecretsGuardInterceptor,
    TaintAndPermissionsInterceptor,
    ToolInterceptorPipeline,
    ToolInvocation,
    ToolResultBounder,
    TurnExecutionContext,
    check_tool_secrets,
    install_privacy_hardening,
    is_secret_path,
    sanitize_subprocess_env,
    scrub_url_secrets,
    turn_context,
)
from muse.paths import muse_home_override


class _TempHomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="muse-pr5-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self._home_cm = muse_home_override(self.tmp_dir)
        self._home_cm.__enter__()
        self.addCleanup(self._home_cm.__exit__, None, None, None)


class PrivacyHardeningTest(_TempHomeTestCase):
    def test_log_privacy_filter_redacts_user_words_and_scrubs_urls_across_loggers(self) -> None:
        flt = install_privacy_hardening()
        install_privacy_hardening()  # Idempotent second call

        for name in ("muse", "companion", "uvicorn.error", "httpx", "httpcore"):
            lg = logging.getLogger(name)
            self.assertEqual(len([f for f in lg.filters if isinstance(f, LogPrivacyFilter)]), 1)

        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)

        # 1) Positional args on `muse.*` / `companion.*` / `uvicorn.error`
        rec1 = logging.LogRecord(
            "muse.turn_actor", logging.INFO, __file__, 10,
            "inbound turn: chat=%s msg=%r url=%s",
            ("c-main", "我的银行卡密码和私人邮件内容", "https://api.example.com/v1?api_key=sk-live-secret999&foo=1"),
            None,
        )
        flt.filter(rec1)
        rendered1 = rec1.getMessage()
        self.assertIn("chat=c-main", rendered1)
        self.assertIn("msg='<14 字>'", rendered1)
        self.assertNotIn("银行卡", rendered1)
        self.assertNotIn("sk-live-secret999", rendered1)
        self.assertIn("api_key=[REDACTED]", rendered1)
        self.assertIn("foo=1", rendered1)

        # Double-filtering never mutates `<14 字>` into `<6 字>`
        flt.filter(rec1)
        self.assertIn("msg='<14 字>'", rec1.getMessage())

        # 2) Dict args & inline formatted strings on `uvicorn.error` / `companion.*`
        rec2 = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 20,
            "exec failed: command=%(command)r status=%(status)s",
            {"command": "curl https://internal.corp?token=tok_abc123", "status": "exit_1"},
            None,
        )
        flt.filter(rec2)
        rendered2 = rec2.getMessage()
        self.assertIn("command='<43 字>'", rendered2)
        self.assertNotIn("tok_abc123", rendered2)
        self.assertIn("status=exit_1", rendered2)

        rec3 = logging.LogRecord(
            "companion.server", logging.WARNING, __file__, 30,
            'bad request payload="secret user sentence" on https://x.test/hook?key=my_hook_secret',
            (),
            None,
        )
        flt.filter(rec3)
        rendered3 = rec3.getMessage()
        self.assertIn('payload="<20 字>"', rendered3)
        self.assertNotIn("secret user sentence", rendered3)
        self.assertNotIn("my_hook_secret", rendered3)
        self.assertIn("key=[REDACTED]", rendered3)

        # 3) Legacy Hermes log lines remain covered & single-redacted even with `companion.modules.log_privacy`
        from companion.modules import log_privacy

        log_privacy.install()
        rec4 = logging.LogRecord(
            "agent.tool_executor", logging.ERROR, __file__, 40,
            "Tool %s returned error (%.1fs): %s", ("terminal", 1.2, "sensitive stdout"), None
        )
        for lg_flt in logging.getLogger("agent.tool_executor").filters:
            lg_flt.filter(rec4)
        self.assertEqual(rec4.getMessage(), "Tool terminal returned error (1.2s): <16 字>")

        rec_gw = logging.LogRecord(
            "gateway.run", logging.INFO, __file__, 45,
            "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
            ("companion", "我", "c1", "1w预算，macbook air高配还是pro低配", None, ""),
            None,
        )
        for lg_flt in logging.getLogger("gateway.run").filters:
            lg_flt.filter(rec_gw)
        self.assertIn("msg='<25 字>'", rec_gw.getMessage())
        self.assertNotIn("macbook", rec_gw.getMessage())

        # 4) Regression checks for `%d`, `key='%s'`, `message text='%s'`, `content="%s"`, `prompt: %s`,
        #    dict `%(name)s`/`%(count)d`, and non-target prefix boundary (`museum` vs `muse.*`)
        rec_int = logging.LogRecord("muse.cli", logging.WARNING, __file__, 50, "command=%d items", (5,), None)
        flt.filter(rec_int)
        self.assertEqual(rec_int.getMessage(), "command=5 items")

        rec_quoted = logging.LogRecord(
            "companion.server", logging.WARNING, __file__, 55, "message='%s' at %s", ("secret", "now"), None
        )
        flt.filter(rec_quoted)
        self.assertEqual(rec_quoted.getMessage(), "message='<6 字>' at now")

        rec_text = logging.LogRecord(
            "companion.adapter", logging.INFO, __file__, 60, "message text='%s'", ("hello private world",), None
        )
        flt.filter(rec_text)
        self.assertEqual(rec_text.getMessage(), "message text='<19 字>'")

        rec_content = logging.LogRecord(
            "muse.streaming", logging.INFO, __file__, 65, 'content="%s"', ("my diary entry",), None
        )
        flt.filter(rec_content)
        self.assertEqual(rec_content.getMessage(), 'content="<14 字>"')

        rec_colon = logging.LogRecord(
            "muse.turn_actor", logging.INFO, __file__, 70, "prompt: %s", ("写一封私密家书",), None
        )
        flt.filter(rec_colon)
        self.assertEqual(rec_colon.getMessage(), "prompt: <7 字>")

        rec_dict_mixed = logging.LogRecord(
            "muse.tools", logging.WARNING, __file__, 75,
            "batch command=%(command)d text=%(text)s",
            {"command": 7, "text": "top secret note"},
            None,
        )
        flt.filter(rec_dict_mixed)
        self.assertEqual(rec_dict_mixed.getMessage(), "batch command=7 text=<15 字>")

        rec_museum = logging.LogRecord(
            "museum.guide", logging.INFO, __file__, 80, "message='%s'", ("public exhibit",), None
        )
        flt.filter(rec_museum)
        self.assertEqual(rec_museum.getMessage(), "message='public exhibit'")

    def test_subprocess_env_sanitizer_and_tool_result_bounder_prevent_secret_leaks(self) -> None:
        raw_env = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "ghp_userConfiguredToken1234567890",
            "COMPANION_AUTH_SECRET": "srv-master-auth-secret-999",
            "COMPANION_MCP_GMAIL_TOKEN": "mcp-gmail-oauth-secret",
            "COMPANION_PUSH_VAPID": "vapid-secret-xyz",
            "MUSE_MASTER_KEY": "master-vault-key-abc",
            "MUSE_INTERNAL_HOOK": "internal-only",
        }
        cleaned = sanitize_subprocess_env(raw_env)
        self.assertEqual(cleaned["PATH"], "/usr/bin")
        self.assertEqual(cleaned["GITHUB_TOKEN"], "ghp_userConfiguredToken1234567890")
        for stripped in (
            "COMPANION_AUTH_SECRET", "COMPANION_MCP_GMAIL_TOKEN",
            "COMPANION_PUSH_VAPID", "MUSE_MASTER_KEY", "MUSE_INTERNAL_HOOK",
        ):
            self.assertNotIn(stripped, cleaned)

        # Save a provider key in `<MUSE_HOME>/.env` and OAuth token in `auth.json`
        with mock.patch.dict(os.environ, {"COMPANION_AUTH_SECRET": "companion-super-secret-value"}, clear=False):
            save_env_value("CUSTOM_PROVIDER_API_KEY", " provider-secret-xyz-987654 ", home=self.tmp_dir)
            (self.tmp_dir / "auth.json").write_text(
                json.dumps({"providers": {"openai-codex": {"tokens": {"access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.codexSecretPayload"}}}}),
                encoding="utf-8",
            )
            bounder = ToolResultBounder(max_chars=600, home=self.tmp_dir)
            bounder.register_secret("vault-login-p@ssw0rd!")

            leaking_output = json.dumps({
                "env_leak": "provider-secret-xyz-987654",
                "srv_leak": "companion-super-secret-value",
                "oauth_leak": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.codexSecretPayload",
                "vault_fill": "vault-login-p@ssw0rd!",
                "password": "inline-json-password-123",
                "openai_key": "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "url": "https://api.service.io/data?api_key=raw_query_secret_1&ok=yes",
            }, ensure_ascii=False)
            bounded = bounder.bound(leaking_output, tool_name="browser_vault")
            for forbidden in (
                "provider-secret-xyz-987654",
                "companion-super-secret-value",
                "codexSecretPayload",
                "vault-login-p@ssw0rd!",
                "inline-json-password-123",
                "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                "raw_query_secret_1",
            ):
                self.assertNotIn(forbidden, bounded)
            self.assertIn("[REDACTED]", bounded)
            self.assertIn("ok=yes", bounded)

            # Oversized output truncation
            huge = "A" * 1000 + "B" * 1000
            clipped = bounder.bound(huge)
            self.assertLess(len(clipped), 700)
            self.assertIn("已省略", clipped)

    def test_vault_denylist_blocks_muse_and_musebot_env_names(self) -> None:
        from companion.modules import vault

        for forbidden in ("MUSE_HOME", "MUSE_ENGINE", "MUSE_SECRET", "MUSEBOT_HOST", "HERMES_HOME", "COMPANION_PORT"):
            self.assertFalse(vault.is_allowed_env_name(forbidden), forbidden)
        self.assertTrue(vault.is_allowed_env_name("NOTION_API_KEY"))
        self.assertEqual(scrub_url_secrets("https://a.io?token=abc&x=1"), "https://a.io?token=[REDACTED]&x=1")


class InterceptorPipelineAndApprovalsTest(_TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        from companion.approvals import Approvals
        from companion.modules import computer, creations, egress, models, permissions
        from companion.store import ChatStore

        self.comp_dir = self.tmp_dir / "companion"
        self.comp_dir.mkdir(parents=True, exist_ok=True)
        self.chat_store = ChatStore(self.comp_dir)
        self.activity_events: list[tuple[str, dict[str, Any]]] = []
        self.ws_approvals: list[dict[str, Any]] = []
        self.internal_turns: list[tuple[str, str]] = []

        class StubAdapter:
            def __init__(s) -> None:
                s.store = self.chat_store
                s.loop = None

            def companion_name(s) -> str:
                return "伙伴"

            async def emit_approval_updated(s, record: Any) -> None:
                self.ws_approvals.append({
                    "event": "approval_updated",
                    "approval_id": record.id,
                    "status": record.status,
                    "chat_id": record.session_key.split(":", 1)[-1],
                    "public": record.public(),
                })

            async def submit_internal(s, chat_id: str, text: str) -> str:
                self.internal_turns.append((chat_id, text))
                return "internal-turn-1"

        self.adapter = StubAdapter()
        self.approvals = Approvals(self.adapter, self.comp_dir)
        self.computer = computer
        self.creations = creations
        self.egress = egress
        self.models = models
        self.permissions = permissions

        # Reset shared module state cleanly per test
        self.egress._tainted.clear()
        self.egress._tainted_chats.clear()
        self.computer.release()
        self.computer._used_by.clear()
        self.computer.set_pending_browser_check(self.approvals.has_pending_browser)
        self.addCleanup(self.computer.release)

    def _build_pipeline(
        self,
        executor: Any,
        *,
        hold_wait_timeout_s: float = 0.3,
        exec_timeout_s: float = 0.5,
        is_helper_fn: Optional[Any] = None,
    ) -> tuple[ToolInterceptorPipeline, NativeApprovalCoordinator, BrowserOwnershipInterceptor]:
        async def emit_activity(chat_id: str, ev: dict[str, Any]) -> None:
            self.activity_events.append((chat_id, ev))

        coord = NativeApprovalCoordinator(
            self.approvals, emit_activity_fn=emit_activity, default_exec_timeout_s=exec_timeout_s
        )
        browser_stage = BrowserOwnershipInterceptor(
            computer_mod=self.computer,
            is_helper_chat_fn=is_helper_fn,
            has_pending_browser_fn=self.approvals.has_pending_browser,
            hold_wait_timeout_s=hold_wait_timeout_s,
            poll_interval_s=0.02,
        )
        pipeline = ToolInterceptorPipeline(
            executor,
            coordinator=coord,
            bounder=ToolResultBounder(home=self.tmp_dir),
            interceptors=[
                ActivityEmitter(emit_activity),
                SecretsGuardInterceptor(),
                browser_stage,
                TaintAndPermissionsInterceptor(
                    coord,
                    egress_mod=self.egress,
                    permissions_mod=self.permissions,
                    computer_mod=self.computer,
                    home_chat_fn=lambda: "main",
                    exec_timeout_s=exec_timeout_s,
                ),
                PostToolArtifactInterceptor(creations_mod=self.creations, models_mod=self.models),
            ],
        )
        return pipeline, coord, browser_stage

    def test_secrets_guard_covers_muse_and_hermes_paths_even_when_skip_policy_gates_true(self) -> None:
        # Dual-path file, DB, SSH, browser upload/file://, and self-change checks
        for tool, args, expected in (
            ("read_file", {"path": str(self.tmp_dir / ".env")}, SECRETS_BLOCK_MESSAGE),
            ("read_file", {"path": str(self.tmp_dir / "muse.db")}, SECRETS_BLOCK_MESSAGE),
            ("read_file", {"path": str(self.tmp_dir / "muse.db-wal")}, SECRETS_BLOCK_MESSAGE),
            ("read_file", {"path": "hermes-home/state.db"}, SECRETS_BLOCK_MESSAGE),
            ("terminal", {"command": "sqlite3 muse-home/muse.db 'select * from messages'"}, SECRETS_BLOCK_MESSAGE),
            ("read_file", {"path": "~/.ssh/id_ed25519"}, SECRETS_BLOCK_MESSAGE),
            ("read_file", {"path": "~/.codex/auth.json"}, SECRETS_BLOCK_MESSAGE),
            ("mcp__browser__browser_navigate", {"url": "file:///etc/passwd"}, SECRETS_BLOCK_MESSAGE),
            ("mcp__browser__browser_file_upload", {"paths": [str(self.tmp_dir / "auth.json")]}, SECRETS_BLOCK_MESSAGE),
            ("terminal", {"command": "musebot doctor --clean-legacy"}, SELF_CHANGE_MESSAGE),
            ("terminal", {"command": "hermes gateway restart"}, SELF_CHANGE_MESSAGE),
            ("write_file", {"path": str(self.tmp_dir / "config.yaml"), "content": "x: 1"}, SELF_CHANGE_MESSAGE),
        ):
            with self.subTest(tool=tool, args=args):
                self.assertEqual(check_tool_secrets(tool, args), expected)

        # Safe cases must NOT false-positive
        self.assertIsNone(check_tool_secrets("read_file", {"path": "deploy/.env.example"}))
        self.assertIsNone(check_tool_secrets("mcp__browser__browser_navigate", {"url": "https://github.com/hashicorp/vault"}))
        self.assertIsNone(check_tool_secrets("write_file", {"path": "NOTES.md", "content": "Check muse.db and .env"}))
        self.assertFalse(is_secret_path("https://example.com/muse.db"))
        self.assertTrue(is_secret_path(self.tmp_dir / "companion" / "media.key"))

        # Even `skip_policy_gates=True` still runs Stage 2 SecretsGuard!
        called = []
        pipeline, _, _ = self._build_pipeline(lambda name, args, inv: called.append(name) or "ok")
        res = asyncio.run(
            pipeline.execute(
                "read_file",
                {"path": str(self.tmp_dir / ".env")},
                ctx=TurnExecutionContext(chat_id="main"),
                skip_policy_gates=True,
            )
        )
        self.assertEqual(res, SECRETS_BLOCK_MESSAGE)
        self.assertEqual(called, [])

    def test_browser_ownership_blocks_helpers_yields_during_hold_and_preserves_pending_approvals(self) -> None:
        executed: list[str] = []

        async def scenario() -> None:
            ensured: list[bool] = []
            closed: list[bool] = []
            with (
                mock.patch.object(self.computer, "ensure_browser", side_effect=lambda: ensured.append(True) or True),
                mock.patch.object(self.computer, "running", return_value=True),
                mock.patch.object(self.computer, "close_browser", side_effect=lambda: closed.append(True) or True),
            ):
                pipeline, _, browser_stage = self._build_pipeline(
                    lambda name, args, inv: executed.append(name) or '{"ok": true}',
                    hold_wait_timeout_s=0.35,
                )

                # 1) Helper calling `mcp__browser__*` directly is guided to `browser_request`
                helper_ctx = TurnExecutionContext(
                    chat_id="helper-chat-1", facing_chat_id="main", actor_kind="helper", helper_id="h1"
                )
                blocked = await pipeline.execute(
                    "mcp__browser__browser_snapshot", {}, call_id="c-hlp", ctx=helper_ctx
                )
                self.assertIn("browser_request", blocked)
                self.assertEqual(executed, [])
                # ActivityEmitter routed start & end to `facing_chat_id` ("main") with zero SQLite queries
                self.assertEqual([c for c, _ in self.activity_events], ["main", "main"])
                self.assertEqual(self.activity_events[0][1]["phase"], "start")
                self.assertEqual(self.activity_events[1][1]["phase"], "end")
                self.assertIn("browser_request", self.activity_events[1][1]["result"])

                # 2) User holds browser (`_hold_owners` active) -> interceptor suspends asynchronously until released
                self.computer.take(owner=42)
                main_ctx = TurnExecutionContext(chat_id="main", facing_chat_id="main", actor_kind="companion")
                nav_task = asyncio.create_task(
                    pipeline.execute("mcp__browser__browser_navigate", {"url": "https://example.org"}, ctx=main_ctx)
                )
                await asyncio.sleep(0.06)
                self.assertFalse(nav_task.done())
                self.assertEqual(executed, [])
                self.computer.let_go(42)
                nav_res = await nav_task
                self.assertEqual(nav_res, '{"ok": true}')
                self.assertEqual(executed, ["mcp__browser__browser_navigate"])
                self.assertEqual(len(ensured), 1)

                # 3) If `browser_hand_over` stays active past timeout, returns graceful hold message
                self.computer.hand_over("登录账号", "main")
                timed_out = await pipeline.execute(
                    "mcp__browser__browser_snapshot", {}, ctx=main_ctx
                )
                self.assertEqual(timed_out, BrowserOwnershipInterceptor.USER_HOLD_MESSAGE)
                self.computer.release()

                # 4) Consequential browser click creates pending browser approval -> `close_after_task` keeps browser open!
                defer_msg = await pipeline.execute(
                    "mcp__browser__browser_click", {"element": "提交订单"}, call_id="c-sub", ctx=main_ctx
                )
                self.assertIn("[等待用户确认]", defer_msg)
                await browser_stage.close_after_task("main")
                self.assertEqual(closed, [])

                # Rejecting/clearing pending card allows `close_after_task` to close cleanly
                pending = self.approvals.store.list_for_session("websocket:main")[0]
                self.approvals.store.claim_pending(pending.id, "rejected")
                await browser_stage.close_after_task("main")
                self.assertEqual(len(closed), 1)

        asyncio.run(scenario())

    def test_dual_track_approvals_defer_replay_anti_loop_and_hold_exec_async_future(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        async def fake_executor(name: str, args: dict[str, Any], inv: ToolInvocation) -> str:
            calls.append((name, dict(args)))
            return json.dumps({"ran": name, "args": args}, ensure_ascii=False)

        async def scenario() -> None:
            self.adapter.loop = asyncio.get_running_loop()
            with mock.patch("companion.modules.push.schedule_push", lambda *a, **kw: None):
                pipeline, coord, _ = self._build_pipeline(fake_executor, exec_timeout_s=0.4)
                ctx = TurnExecutionContext(
                    chat_id="desk-9", facing_chat_id="main", session_id="s-desk", turn_id="t-1", actor_kind="desk"
                )

                # --- Track 1: Taint + `defer` + `execute_approved(skip_policy_gates=True)` without re-defer loop ---
                await pipeline.execute("mcp__gmail__read_mail", {"id": "m1"}, ctx=ctx)
                self.assertTrue(self.egress.chat_tainted("desk-9"))

                deferred = await pipeline.execute(
                    "mcp__browser__browser_navigate",
                    {"url": "https://tracker.example/collect?leak=1"},
                    call_id="c-egress",
                    ctx=ctx,
                )
                self.assertIn("[等待用户确认]", deferred)
                self.assertIn("确认卡片已放到用户正在看的聊天里", deferred)
                # Tool was NOT executed prior to approval
                self.assertEqual([n for n, _ in calls], ["mcp__gmail__read_mail"])

                records = self.approvals.store.list_for_session("websocket:main")
                self.assertEqual(len(records), 1)
                rec = records[0]
                self.assertEqual(rec.action, "send_private")

                # Replay via `execute_approved(rec, skip_policy_gates=True)` -> executes ONCE, zero re-defer!
                claimed = self.approvals.store.claim_pending(rec.id, "executed")
                assert claimed is not None
                self.approvals.grants.add(["send_private:tracker.example"], "24h")
                replay_out = await coord.execute_approved(claimed, skip_policy_gates=True)
                self.assertIn("mcp__browser__browser_navigate", replay_out)
                self.assertEqual(len(self.approvals.store.list_for_session("websocket:main")), 1)

                # Subsequent call to same host is covered by the 24h grant
                direct_out = await pipeline.execute(
                    "mcp__browser__browser_navigate",
                    {"url": "https://tracker.example/collect?leak=2"},
                    ctx=ctx,
                )
                self.assertIn("leak=2", direct_out)

                # --- Track 2: `hold_exec` async Future suspension (`once`, `24h`, `deny`, `timeout`) ---
                # 2a) Approve with `24h` scope -> suspends without blocking loop, wakes and executes
                exec_task = asyncio.create_task(
                    pipeline.execute("terminal", {"command": "rm -rf ./build_tmp"}, call_id="c-rm1", ctx=ctx)
                )
                await asyncio.sleep(0.05)
                self.assertFalse(exec_task.done())
                exec_records = [r for r in self.approvals.store.list_for_session("websocket:main") if r.action == "run_command"]
                self.assertEqual(len(exec_records), 1)
                rm_rec = exec_records[0]
                self.assertTrue(coord.has_held_exec(rm_rec.id))

                self.approvals.store.claim_pending(rm_rec.id, "executed")
                self.assertTrue(coord.resolve_held_exec(rm_rec.id, approve=True, scope="24h"))
                rm_out = await exec_task
                self.assertIn("rm -rf ./build_tmp", rm_out)

                # Second destructive delete in same session passes immediately via session grant!
                rm2_out = await pipeline.execute("terminal", {"command": "rm -rf ./dist_tmp"}, call_id="c-rm2", ctx=ctx)
                self.assertIn("rm -rf ./dist_tmp", rm2_out)

                # 2b) Deny risky `git reset --hard` -> blocks without executing
                deny_task = asyncio.create_task(
                    pipeline.execute("terminal", {"command": "git reset --hard HEAD~1"}, call_id="c-git", ctx=ctx)
                )
                await asyncio.sleep(0.05)
                git_rec = [r for r in self.approvals.store.list_for_session("websocket:main") if r.call_id == "c-git"][0]
                self.approvals.store.claim_pending(git_rec.id, "rejected")
                coord.resolve_held_exec(git_rec.id, approve=False)
                deny_out = await deny_task
                self.assertIn("拒绝", deny_out)
                self.assertFalse(any("git reset" in str(a) for _, a in calls))

                # 2c) Timeout on unapproved `sudo apt update` -> expires cleanly to FAILED
                timeout_out = await pipeline.execute("terminal", {"command": "sudo apt update"}, call_id="c-sudo", ctx=ctx)
                self.assertIn("超时", timeout_out)
                sudo_rec = [r for r in self.approvals.store.list_for_session("websocket:main") if r.call_id == "c-sudo"][0]
                self.assertEqual(sudo_rec.status, "failed")

        asyncio.run(scenario())

    def test_post_tool_artifact_archives_generated_media_and_records_usage(self) -> None:
        img_path = self.tmp_dir / "cat_sticker.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
        (self.comp_dir / "models.json").write_text(
            json.dumps({
                "media": {
                    "image": {"active": "画图预设", "models": {"画图预设": {"model": "gpt-image-1", "provider": "openai"}}},
                    "video": {"active": "视频预设", "models": {"视频预设": {"model": "wan-2.1", "provider": "dashscope"}}},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        pipeline, _, _ = self._build_pipeline(
            lambda name, args, inv: json.dumps({"success": True, "path": str(img_path)}, ensure_ascii=False)
        )
        ctx = TurnExecutionContext(chat_id="main")
        res = asyncio.run(pipeline.execute("image_generate", {"prompt": "戴围巾的小橘猫"}, ctx=ctx))
        self.assertIn("cat_sticker.png", res)
        asyncio.run(pipeline.execute("video_generate", {"prompt": "海边日落", "duration": 5, "resolution": "1080p"}, ctx=ctx))
        listed = self.creations.list_creations(lambda rel: f"/signed/{rel}")
        self.assertEqual(listed["counts"]["image"], 1)
        self.assertIn("戴围巾的小橘猫", listed["items"][0]["title"])
        usage_log = self.comp_dir / "media-usage.jsonl"
        self.assertTrue(usage_log.is_file())
        usage_text = usage_log.read_text(encoding="utf-8")
        self.assertIn('"kind": "image"', usage_text)
        self.assertIn('"kind": "video"', usage_text)
        self.assertIn('"resolution": "1080p"', usage_text)


class AsyncHelperOrchestratorStressTest(_TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        from companion.modules import egress, helpers
        from companion.store import ChatStore

        self.comp_dir = self.tmp_dir / "companion"
        self.comp_dir.mkdir(parents=True, exist_ok=True)
        self.store = ChatStore(self.comp_dir)
        self.helpers = helpers
        self.egress = egress
        self.egress._tainted.clear()
        self.egress._tainted_chats.clear()

        self._patch_webui = mock.patch("companion.modules.paths.get_webui_dir", return_value=self.comp_dir)
        self._patch_webui.start()
        self.addCleanup(self._patch_webui.stop)
        self.helpers._drop_jobs = lambda _cid: None

    def test_three_level_nested_async_helpers_plus_browser_desk_zero_deadlocks_and_cycle_guard(self) -> None:
        h1 = self.helpers.create(self.store, {"name": "管家", "title": "统筹", "instructions": "拆解任务", "emoji": "🧑‍💼"})
        h2 = self.helpers.create(self.store, {"name": "研究员", "title": "调研", "instructions": "查资料", "emoji": "🔎"})
        h3 = self.helpers.create(self.store, {"name": "比价", "title": "比价格", "instructions": "做对比", "emoji": "🛒"})
        h4 = self.helpers.create(self.store, {"name": "写手", "title": "写总结", "instructions": "写文案", "emoji": "✍️"})

        observed_facing: dict[str, str] = {}
        observed_overrides: dict[str, Any] = {}
        created_desks: list[str] = []
        posted_bubbles: list[tuple[str, str, str]] = []
        internal_Followups: list[tuple[str, str]] = []

        orch_holder: dict[str, HelperOrchestrator] = {}

        async def fake_turn_runner(*, chat_id: str, prompt: str, turn_id: str, ctx: TurnExecutionContext, model_override: Any = None) -> str:
            orch = orch_holder["orch"]
            observed_facing[ctx.actor_kind + ":" + ( self.helpers.helper_for_chat(chat_id)["name"] if self.helpers.helper_for_chat(chat_id) else "desk")] = ctx.facing_chat_id
            observed_overrides[chat_id] = model_override

            if chat_id == h1["chat_id"]:
                # Level 1 (管家) -> asks Level 2 (研究员)
                res = json.loads(await orch.handle_helper_ask({"helper": "研究员", "task": f"查参数:{prompt[-6:]}"}, ctx=ctx))
                self.assertTrue(res["success"], res)
                return f"管家汇总({res['answer']})"

            if chat_id == h2["chat_id"]:
                # Cycle attempt H2 -> H1 must fail immediately!
                cyc = json.loads(await orch.handle_helper_ask({"helper": "管家", "task": "反向回调"}, ctx=ctx))
                self.assertFalse(cyc["success"])
                self.assertIn("正在等你的结果", cyc["error"])
                # Level 2 (研究员) -> asks Level 3 (比价)
                res = json.loads(await orch.handle_helper_ask({"helper": "比价", "task": "去网页核价"}, ctx=ctx))
                self.assertTrue(res["success"], res)
                return f"研究结论({res['answer']})"

            if chat_id == h3["chat_id"]:
                # Transitive cycle H3 -> H1 must fail!
                cyc_root = json.loads(await orch.handle_helper_ask({"helper": "管家", "task": "跨层回调"}, ctx=ctx))
                self.assertFalse(cyc_root["success"])
                # 4th-level helper H3 -> H4 (`depth == 3 >= MAX_CHAIN`) must fail cleanly!
                too_deep = json.loads(await orch.handle_helper_ask({"helper": "写手", "task": "再转第四手"}, ctx=ctx))
                self.assertFalse(too_deep["success"])
                self.assertIn("已经转了好几手", too_deep["error"])
                # Level 3 (比价) -> delegates browser work via `browser_request` (ephemeral desk)
                desk_res = json.loads(await orch.handle_browser_request({"task": "读取商品页价格"}, ctx=ctx))
                self.assertTrue(desk_res["success"], desk_res)
                return f"比价完成[{desk_res['result']}]"

            # Ephemeral browser desk (`actor_kind == "desk"`)
            created_desks.append(chat_id)
            self.assertEqual(ctx.actor_kind, "desk")
            # Desk cannot call `helper_ask`
            desk_ask = json.loads(await orch.handle_helper_ask({"helper": "写手", "task": "x"}, ctx=ctx))
            self.assertFalse(desk_ask["success"])
            # Taint reading inside desk propagates all the way back to H3 -> H2 -> H1 -> main!
            self.egress.note("sess-desk", "browser_vault", chat_id=chat_id)
            await asyncio.sleep(0.01)
            return "¥3999 现货"

        async def post_as_helper(chat_id: str, helper: dict[str, Any], text: str) -> None:
            posted_bubbles.append((chat_id, helper["name"], text))

        async def submit_internal(chat_id: str, text: str) -> str:
            internal_Followups.append((chat_id, text))
            return "t-bg"

        class StubModels:
            @staticmethod
            def read_models() -> dict[str, Any]:
                return {"presets": {"fast-mini": {"model": "gpt-4.1-mini", "provider": "openai", "base_url": "https://api.openai.com/v1"}}}

            @staticmethod
            def runtime_provider(_name: str, preset: dict[str, Any]) -> str:
                return str(preset["provider"])

        self.helpers.update(self.store, h1["id"], {})  # valid store check
        with mock.patch.object(self.helpers, "read_models", StubModels.read_models, create=True):
            # Attach preset name directly in helpers.json for model_override resolution
            rows = self.helpers._read()
            rows[0]["model"] = "fast-mini"
            self.helpers._write(rows)

        orch = HelperOrchestrator(
            fake_turn_runner,
            store=self.store,
            helpers_mod=self.helpers,
            egress_mod=self.egress,
            models_mod=StubModels,
            main_chat_fn=lambda: "main",
            post_as_helper_fn=post_as_helper,
            submit_internal_fn=submit_internal,
        )
        orch_holder["orch"] = orch

        async def run_stress() -> None:
            # Launch 4 concurrent 3-level + browser_desk chains from two different user chats at once!
            with mock.patch("companion.modules.push.schedule_push", lambda *a, **kw: None):
                tasks = [
                    orch.handle_helper_ask(
                        {"helper": "管家", "task": f"并发任务-{i}"},
                        ctx=TurnExecutionContext(chat_id="main" if i % 2 == 0 else "side-chat", actor_kind="companion"),
                    )
                    for i in range(4)
                ]
                raw_outputs = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
                for raw in raw_outputs:
                    parsed = json.loads(raw)
                    self.assertTrue(parsed["success"], parsed)
                    self.assertIn("¥3999 现货", parsed["answer"])

                # Verify `facing_chat_id` walked 4 hops (`desk -> H3 -> H2 -> H1 -> main/side-chat`),
                # `model_override` resolved for H1, taint propagated back to `main`, and ALL ephemeral desks were deleted!
                self.assertIn(observed_facing["desk:desk"], {"main", "side-chat"})
                self.assertEqual(observed_overrides[h1["chat_id"]]["model"], "gpt-4.1-mini")
                self.assertIsNone(observed_overrides[h2["chat_id"]])
                self.assertTrue(self.egress.chat_tainted("main"))
                self.assertEqual(len(created_desks), 4)
                for desk_id in created_desks:
                    self.assertFalse(self.store.exists(desk_id))
                self.assertEqual(orch._asking, {})
                self.assertEqual(orch._asking_queue, {})

                # Verify `wait=False` (`hand_off`) background execution & `submit_internal` callback
                bg_res = json.loads(
                    await orch.handle_helper_ask(
                        {"helper": "写手", "task": "后台写周报", "wait": False},
                        ctx=TurnExecutionContext(chat_id="main", actor_kind="companion"),
                    )
                )
                self.assertTrue(bg_res["background"])
                await asyncio.gather(*list(orch._bg_tasks))
                self.assertTrue(any(cid == "main" and "写手" in txt for cid, txt in internal_Followups))

        asyncio.run(run_stress())

    def test_concurrent_fifo_waiters_preserve_cycle_detection_on_both_callers(self) -> None:
        boss_a = self.helpers.create(self.store, {"name": "主管甲", "title": "甲", "instructions": "做事"})
        boss_b = self.helpers.create(self.store, {"name": "主管乙", "title": "乙", "instructions": "做事"})
        worker_c = self.helpers.create(self.store, {"name": "执行丙", "title": "丙", "instructions": "做事"})

        a_inside = asyncio.Event()
        release_a = asyncio.Event()
        cycle_checks: dict[str, dict[str, Any]] = {}
        orch_ref: dict[str, HelperOrchestrator] = {}

        async def runner(*, chat_id: str, prompt: str, turn_id: str, ctx: TurnExecutionContext, model_override: Any = None) -> str:
            if chat_id == worker_c["chat_id"] and "任务A" in prompt:
                a_inside.set()
                await release_a.wait()
                return "甲完成"
            return "乙完成"

        orch = HelperOrchestrator(runner, store=self.store, helpers_mod=self.helpers, egress_mod=self.egress)
        orch_ref["orch"] = orch

        async def scenario() -> None:
            task_a = asyncio.create_task(
                orch.handle_helper_ask({"helper": "执行丙", "task": "任务A"}, ctx=TurnExecutionContext(chat_id=boss_a["chat_id"], actor_kind="helper"))
            )
            await a_inside.wait()
            task_b = asyncio.create_task(
                orch.handle_helper_ask({"helper": "执行丙", "task": "任务B"}, ctx=TurnExecutionContext(chat_id=boss_b["chat_id"], actor_kind="helper"))
            )
            await asyncio.sleep(0.03)
            # Both boss_a and boss_b are registered in `_asking_queue[worker_c]`!
            c_ctx = TurnExecutionContext(chat_id=worker_c["chat_id"], actor_kind="helper")
            cycle_checks["to_a"] = json.loads(await orch.handle_helper_ask({"helper": "主管甲", "task": "回调甲"}, ctx=c_ctx))
            cycle_checks["to_b"] = json.loads(await orch.handle_helper_ask({"helper": "主管乙", "task": "回调乙"}, ctx=c_ctx))
            release_a.set()
            res_a, res_b = await asyncio.gather(task_a, task_b)
            self.assertTrue(json.loads(res_a)["success"])
            self.assertTrue(json.loads(res_b)["success"])

        asyncio.run(scenario())
        self.assertFalse(cycle_checks["to_a"]["success"])
        self.assertFalse(cycle_checks["to_b"]["success"])
        self.assertIsNone(resolve_helper_model_override({"name": "无模型"}))


class GoalLoopControllerTest(_TempHomeTestCase):
    def test_goal_loop_gate_repair_async_wait_verdicts_and_muse_db_persistence(self) -> None:
        db = MuseDB(self.tmp_dir / "muse.db")
        self.addCleanup(db.close)

        notices: list[tuple[str, str]] = []
        banner_pushes: list[dict[str, Any]] = []
        gate_flag = self.tmp_dir / "gate_ok.txt"
        judge_replies = iter([
            '{"verdict": "continue", "reason": "step 1 complete, write report next"}',
            '{"verdict": "wait", "wait_for_seconds": 1, "reason": "waiting on background indexer"}',
            '{"verdict": "done", "reason": "all verification checks green"}',
        ])

        async def fake_judge(_sys: str, _usr: str, _timeout: float, _max_tok: int) -> str:
            return next(judge_replies)

        async def post_notice(chat_id: str, text: str) -> None:
            notices.append((chat_id, text))

        ctrl_ref: dict[str, GoalLoopController] = {}

        async def push_banner(chat_id: str) -> None:
            banner_pushes.append(ctrl_ref["ctrl"].banner_state(chat_id))

        ctrl = GoalLoopController(
            db=db,
            judge_llm_fn=fake_judge,
            post_notice_fn=post_notice,
            push_goal_state_fn=push_banner,
            gate_cwd=str(self.tmp_dir),
            poll_interval_s=0.05,
        )
        ctrl_ref["ctrl"] = ctrl

        gate_cmd = f'"{sys.executable}" -c "import pathlib, sys; sys.exit(0 if pathlib.Path(\'gate_ok.txt\').exists() else 2)"'

        async def scenario() -> None:
            raw_goal = "完成月度账单核对\nverify: gate_ok.txt 生成且总额一致\nconstraints: 不改动原始账单"
            state = await ctrl.set_goal("main", raw_goal, max_turns=6, gate=gate_cmd)
            self.assertEqual(state.goal, "完成月度账单核对")
            self.assertEqual(state.contract.verification, "gate_ok.txt 生成且总额一致")
            self.assertEqual(ctrl.banner_state("main"), {
                "active": True, "status": "active", "objective": "完成月度账单核对", "ui_summary": None
            })

            # Kick prompt deduplication check
            long_text = "请帮我详细整理项目架构并逐一核对模块边界 " * 30
            self.assertEqual(goal_kick_prompt(long_text, long_text), GOAL_ALREADY_SEEN_KICK)
            self.assertEqual(goal_kick_prompt("短目标", "短目标"), "短目标")

            prompts_seen: list[str] = []

            async def run_turn(chat_id: str, continuation_prompt: str) -> str:
                prompts_seen.append(continuation_prompt)
                if len(prompts_seen) == 1:
                    # First continuation was triggered by failing quality gate -> fix the gate!
                    self.assertIn("quality gate failed", continuation_prompt)
                    gate_flag.write_text("passed", encoding="utf-8")
                    return "已生成 gate_ok.txt，继续核对第一部分"
                if len(prompts_seen) == 2:
                    return "已启动后台索引，等待完成"
                return "全部账单核对完毕，结果一致。"

            final_decision = await ctrl.run_goal_loop("main", "初步扫描账单", run_turn)
            self.assertEqual(final_decision.verdict, "done")
            self.assertEqual(final_decision.status, "done")
            self.assertEqual(len(prompts_seen), 3)
            self.assertIn(("main", NOTICE_GOAL_PARKED), notices)
            self.assertIn(("main", NOTICE_GOAL_DONE), notices)
            self.assertEqual(ctrl.banner_state("main"), {"active": False})

            # Test `blocked` verdict + `/goal pause | resume | stop` controls
            await ctrl.set_goal("side-1", "登录需要验证码的内部系统")
            ctrl_blocked = GoalLoopController(
                db=db,
                judge_llm_fn=lambda *_: asyncio.sleep(0, result='{"verdict": "blocked", "reason": "需要用户输入短信验证码"}'),
                post_notice_fn=post_notice,
                push_goal_state_fn=push_banner,
            )
            blocked_dec = await ctrl_blocked.evaluate_after_turn("side-1", "页面提示请输入手机短信验证码")
            self.assertEqual(blocked_dec.verdict, "blocked")
            self.assertEqual(blocked_dec.notice, NOTICE_GOAL_PAUSED)
            banner = ctrl.banner_state("side-1")
            self.assertEqual(banner["status"], "blocked")
            self.assertEqual(banner["ui_summary"], "需要用户输入短信验证码")

            resumed = await ctrl.handle_control("side-1", "/goal resume")
            assert resumed is not None
            self.assertEqual(resumed.status, "active")
            self.assertEqual(ctrl.banner_state("side-1")["status"], "active")

            paused = await ctrl.handle_control("side-1", "/goal pause")
            assert paused is not None
            self.assertEqual(paused.status, "paused")
            self.assertEqual(ctrl.banner_state("side-1")["status"], "blocked")

            await ctrl.handle_control("side-1", "/goal stop")
            self.assertEqual(ctrl.banner_state("side-1"), {"active": False})

        asyncio.run(scenario())
        headline, contract = parse_contract("迁移数据库\nboundaries: muse/")
        self.assertEqual(headline, "迁移数据库")
        self.assertEqual(contract.boundaries, "muse/")


class Phase2EngineAndApprovalsIntegrationTest(_TempHomeTestCase):
    def test_chat_turn_engine_end_to_end_approvals_decide_nested_actors_and_activity_seam(self) -> None:
        from companion import activity
        from companion.approvals import Approvals
        from companion.modules import helpers
        from companion.store import ChatStore
        from muse.engine import ChatTurnEngine
        from muse.llm.types import StreamDone, TextDelta, ToolCallComplete
        from muse.streaming import ChatSink

        comp_dir = self.tmp_dir / "companion"
        comp_dir.mkdir(parents=True, exist_ok=True)
        store = ChatStore(comp_dir)
        db = MuseDB(self.tmp_dir / "muse.db")
        self.addCleanup(db.close)

        class TestSink(ChatSink):
            def __init__(s) -> None:
                s.store = store
                s.ws_frames: list[dict[str, Any]] = []
                s.active_turns: dict[str, str] = {}
                s.approvals: Any = None

            async def emit(s, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
                body = {**event, "chat_id": chat_id}
                if persist:
                    body = s.store.append(chat_id, body)
                s.ws_frames.append(body)
                return body

            def append_store(s, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
                return s.store.append(chat_id, {**event, "chat_id": chat_id})

            def remove_store_events(s, chat_id: str, predicate: Any) -> int:
                return s.store.remove_events(chat_id, predicate)

            def store_events(s, chat_id: str) -> list[dict[str, Any]]:
                return s.store.events(chat_id)

            def ensure_chat(s, chat_id: str) -> None:
                s.store.ensure(chat_id)

            def set_active_turn(s, chat_id: str, turn_id: Optional[str]) -> None:
                if turn_id is None:
                    s.active_turns.pop(chat_id, None)
                else:
                    s.active_turns[chat_id] = turn_id

            def get_active_turn(s, chat_id: str) -> Optional[str]:
                return s.active_turns.get(chat_id)

            async def emit_approval_updated(s, record: Any) -> None:
                s.ws_frames.append({"event": "approval_updated", "approval_id": record.id, "status": record.status})

            async def emit_activity(s, chat_id: str, tool_event: dict[str, Any]) -> None:
                s.ws_frames.append({"event": "tool_hint", "chat_id": chat_id, "tool_event": tool_event})

            async def submit_internal(s, chat_id: str, text: str) -> str:
                return await s._engine.submit_internal(chat_id, text)

        sink = TestSink()
        sink.approvals = Approvals(sink, comp_dir)  # type: ignore[arg-type]
        helpers._drop_jobs = lambda _cid: None
        helper_row = helpers.create(store, {"name": "调研员", "title": "查网页", "instructions": "用浏览器代办"})

        executed_tools: list[tuple[str, dict[str, Any], Optional[str]]] = []
        engine_ref: dict[str, ChatTurnEngine] = {}

        async def raw_executor(name: str, args: dict[str, Any], inv: ToolInvocation) -> str:
            # Verify `activity._chat_for_session` resolves directly from `TurnExecutionContext` in native mode
            resolved_chat = activity._chat_for_session(inv.session_id)
            executed_tools.append((name, dict(args), resolved_chat))
            eng = engine_ref["engine"]
            if name == "helper_ask":
                return await eng.helper_orchestrator.handle_helper_ask(args, ctx=inv.ctx)
            if name == "browser_request":
                return await eng.helper_orchestrator.handle_browser_request(args, ctx=inv.ctx)
            return json.dumps({"ok": True, "tool": name}, ensure_ascii=False)

        turn_step: dict[str, int] = {}

        async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
            last_content = str(messages[-1].get("content") or "") if messages else ""
            role = str(messages[-1].get("role") or "") if messages else ""
            if "【系统通知】用户在确认卡片上批准了" in last_content:
                yield TextDelta("好的，邮件已经替你发出了。")
                yield StreamDone()
                return
            if "交给你的事】查首页标题" in last_content and role == "user":
                yield ToolCallComplete(id="tc-br", name="browser_request", arguments='{"task": "打开首页读标题"}')
                yield StreamDone(finish_reason="tool_calls")
                return
            if "打开首页读标题" in last_content and role == "user":
                yield TextDelta("首页标题是 Muse Native 2.0")
                yield StreamDone()
                return
            if role == "tool" and '"result": "首页标题是 Muse Native 2.0"' in last_content:
                yield TextDelta("调研完毕：Muse Native 2.0")
                yield StreamDone()
                return
            if "直接跟调研员说话" in last_content and role == "user":
                yield ToolCallComplete(id="tc-direct-read", name="read_file", arguments='{"path": "README.md"}')
                yield ToolCallComplete(id="tc-direct-br", name="mcp__browser__browser_snapshot", arguments="{}")
                yield StreamDone(finish_reason="tool_calls")
                return
            if role == "tool" and "browser_request" in last_content and any("直接跟调研员说话" in str(m.get("content") or "") for m in messages):
                yield TextDelta("直接对话完成。")
                yield StreamDone()
                return
            if "执行重启测试" in last_content and role == "user":
                yield ToolCallComplete(id="tc-sudo-stop", name="terminal", arguments='{"command": "sudo reboot"}')
                yield StreamDone(finish_reason="tool_calls")
                return
            step = turn_step.get("main", 0) + 1
            turn_step["main"] = step
            if step == 1:
                yield ToolCallComplete(id="tc-hlp", name="helper_ask", arguments='{"helper": "调研员", "task": "查首页标题"}')
                yield StreamDone(finish_reason="tool_calls")
            elif step == 2:
                yield ToolCallComplete(
                    id="tc-mail", name="mcp__gmail__send_mail",
                    arguments='{"to": "boss@example.com", "subject": "调研报告"}',
                )
                yield StreamDone(finish_reason="tool_calls")
            elif step == 3:
                yield ToolCallComplete(id="tc-rm", name="terminal", arguments='{"command": "rm -rf ./old_cache"}')
                yield StreamDone(finish_reason="tool_calls")
            elif step == 4:
                # Second destructive delete in same session should pass without prompting when `scope="session"`
                yield ToolCallComplete(id="tc-rm-2", name="terminal", arguments='{"command": "rm -rf ./old_cache_2"}')
                yield StreamDone(finish_reason="tool_calls")
            else:
                yield TextDelta("调研和清理已完成，邮件等你确认。")
                yield StreamDone()

        async def scenario() -> None:
            with (
                mock.patch.dict(os.environ, {"MUSE_ENGINE": "native"}, clear=False),
                mock.patch("companion.modules.push.schedule_push", lambda *a, **kw: None),
            ):
                engine = ChatTurnEngine(sink, db=db, stream_llm=fake_stream, raw_tool_executor=raw_executor)
                sink._engine = engine  # type: ignore[attr-defined]
                engine_ref["engine"] = engine
                await engine.start()
                try:
                    main_chat = store.create()
                    await engine.submit_turn(main_chat, "帮我调研并发邮件", "t-main-1")

                    # Wait until Track 2 `hold_exec` (`rm -rf ./old_cache`) suspends on its asyncio.Future
                    rm_rec = None
                    for _ in range(100):
                        pending = [
                            r for r in sink.approvals.store.list_for_session(f"websocket:{main_chat}")
                            if r.action == "run_command" and r.status == "pending"
                        ]
                        if pending:
                            rm_rec = pending[0]
                            break
                        await asyncio.sleep(0.02)
                    self.assertIsNotNone(rm_rec)
                    assert rm_rec is not None

                    # Approve Track 2 (`hold_exec`) with `scope="session"` via real `Approvals.decide()`!
                    decided_rm = await sink.approvals.decide(rm_rec.id, True, "session")
                    assert decided_rm is not None
                    self.assertEqual(decided_rm.status, "executed")
                    self.assertEqual(decided_rm.scope, "session")
                    self.assertEqual(sink.approvals.grants.list(), [])  # session grant stays in-memory only

                    # Step 4 (`rm -rf ./old_cache_2`) runs automatically under the session grant without hanging!
                    await engine.get_actor(main_chat).wait_idle(timeout=3.0)
                    run_cmd_recs = [
                        r for r in sink.approvals.store.list_for_session(f"websocket:{main_chat}")
                        if r.action == "run_command"
                    ]
                    self.assertEqual(len(run_cmd_recs), 1)

                    # Ephemeral desk actor must be cleaned up from `engine._actors`
                    self.assertEqual(set(engine._actors.keys()), {main_chat, helper_row["chat_id"]})

                    # Regression #1 (#217): Direct user turn on `helper_row["chat_id"]` after delegation
                    # MUST emit `tool_hint` to `helper_row["chat_id"]` (not `main_chat`!) while keeping `actor_kind="helper"`
                    ws_before_direct = len(sink.ws_frames)
                    await engine.submit_turn(helper_row["chat_id"], "直接跟调研员说话", "t-direct-hlp")
                    await engine.get_actor(helper_row["chat_id"]).wait_idle(timeout=3.0)
                    direct_hints = [
                        f for f in sink.ws_frames[ws_before_direct:]
                        if f.get("event") == "tool_hint"
                    ]
                    self.assertGreaterEqual(len(direct_hints), 4)
                    self.assertTrue(all(f["chat_id"] == helper_row["chat_id"] for f in direct_hints))

                    # Regression #2 (#217): `/stop` while `hold_exec` is suspended marks record `failed` (no zombie `pending` card)
                    stop_chat = store.create()
                    await engine.submit_turn(stop_chat, "执行重启测试", "t-stop-exec")
                    sudo_rec = None
                    for _ in range(100):
                        pending_sudo = [
                            r for r in sink.approvals.store.list_for_session(f"websocket:{stop_chat}")
                            if r.action == "run_command" and r.status == "pending"
                        ]
                        if pending_sudo:
                            sudo_rec = pending_sudo[0]
                            break
                        await asyncio.sleep(0.02)
                    self.assertIsNotNone(sudo_rec)
                    assert sudo_rec is not None

                    await engine.stop_turn(stop_chat)
                    await asyncio.sleep(0.05)
                    reloaded_sudo = sink.approvals.store.get(sudo_rec.id)
                    assert reloaded_sudo is not None
                    self.assertEqual(reloaded_sudo.status, "failed")
                    self.assertTrue(
                        any(
                            f.get("event") == "approval_updated"
                            and f.get("approval_id") == sudo_rec.id
                            and f.get("status") == "failed"
                            for f in sink.ws_frames
                        )
                    )

                    # Now approve Track 1 (`defer` mail send) via real `Approvals.decide()`!
                    mail_recs = [
                        r for r in sink.approvals.store.list_for_session(f"websocket:{main_chat}")
                        if r.action == "send_email" and r.status == "pending"
                    ]
                    self.assertEqual(len(mail_recs), 1)
                    decided_mail = await sink.approvals.decide(mail_recs[0].id, True, "24h")
                    assert decided_mail is not None
                    self.assertEqual(decided_mail.status, "executed")
                    await engine.get_actor(main_chat).wait_idle(timeout=3.0)

                    # Verify tool execution order, context-resolved chat_id, and internal follow-up reply!
                    tool_names = [t[0] for t in executed_tools]
                    self.assertIn("helper_ask", tool_names)
                    self.assertIn("browser_request", tool_names)
                    self.assertIn("terminal", tool_names)
                    self.assertIn("mcp__gmail__send_mail", tool_names)
                    self.assertEqual(executed_tools[0][2], main_chat)
                    events = store.events(main_chat)
                    self.assertTrue(any("邮件已经替你发出了" in str(e.get("text") or "") for e in events))

                    # Verify URL secret scrubbing in `turn_end.failure_message`
                    async def failing_stream(**_: Any):
                        if False:
                            yield TextDelta("")
                        raise RuntimeError("Upstream failed at https://api.vendor.io/v1/chat?api_key=sk-secret999")

                    err_chat = store.create()
                    err_engine = ChatTurnEngine(sink, db=db, stream_llm=failing_stream)
                    await err_engine.submit_turn(err_chat, "触发错误", "t-err")
                    await err_engine.get_actor(err_chat).wait_idle(timeout=2.0)
                    err_end = [e for e in store.events(err_chat) if e.get("event") == "turn_end"][-1]
                    self.assertNotIn("sk-secret999", err_end.get("failure_message", ""))
                    self.assertIn("api_key=[REDACTED]", err_end.get("failure_message", ""))
                finally:
                    await engine.stop()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
