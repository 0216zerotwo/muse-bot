"""Tests for chat branching (fork & edit_message), conversation export, memory pinning, and canvas creations."""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from _loader import chat_store, load, temp_dir
from muse.app import NativeCompanionAdapter
from muse.db import MuseDB, close_all
from muse.engine import ChatTurnEngine
from muse.llm.types import StreamDone, TextDelta
from muse.paths import bind_muse_home, reset_muse_home
from muse.runtime import close_all_shared_dbs
from muse.turn_actor import PendingClarify, PendingSlashConfirm
from test_auth_devices import _companion_pkg


class _FakeDiskMemoryStore:
    def __init__(self, mem_dir: Path):
        self.mem_dir = mem_dir
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, list[str]] = {"user": [], "memory": []}

    def load_from_disk(self) -> None:
        pass

    def entries_for(self, target: str) -> list[str]:
        return list(self.data.get(target, []))

    _entries_for = entries_for

    def add(self, target: str, content: str) -> dict[str, object]:
        self.data.setdefault(target, []).append(content)
        return {"success": True}

    def remove(self, target: str, line: str, matched_entry: str | None = None) -> dict[str, object]:
        items = self.data.setdefault(target, [])
        key = matched_entry or line
        if key not in items:
            return {"success": False, "error": "not found"}
        items.remove(key)
        return {"success": True}

    def apply_batch(self, target: str, ops: list[dict[str, object]]) -> dict[str, object]:
        items = self.data.setdefault(target, [])
        for op in ops:
            action = op.get("action")
            if action == "add":
                items.append(str(op["content"]))
            elif action == "remove":
                old = str(op.get("matched_entry") or op.get("old_text") or "")
                if old in items:
                    items.remove(old)
            elif action == "replace":
                old = str(op.get("matched_entry") or op.get("old_text") or "")
                new = str(op["content"])
                if old in items:
                    items[items.index(old)] = new
        return {"success": True}


def _ws_mutation(ws, action: str, payload: dict, req_id: str = "r1") -> dict:
    ws.send_json({"type": "webui_request", "request_id": req_id, "action": action, "payload": payload})
    while True:
        frame = ws.receive_json()
        if frame.get("event") == "webui_response" and frame.get("request_id") == req_id:
            return frame


class ChatBranchAndMuseDBSyncTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("companion-branch-")
        self._home_token = bind_muse_home(self.home)
        self.db = MuseDB(self.home / "muse.db")
        self.adapter = NativeCompanionAdapter(home=self.home, start_background_services=False)
        self.prompted_messages: list[list[dict]] = []

        async def fake_stream(*, messages, **_):
            self.prompted_messages.append([dict(m) for m in messages])
            user_msgs = [m for m in messages if m.get("role") == "user"]
            last_q = str(user_msgs[-1].get("content") or "") if user_msgs else ""
            yield TextDelta(f"新回答：{last_q}")
            yield StreamDone()

        self.engine = ChatTurnEngine(self.adapter, db=self.db, stream_llm=fake_stream)
        self.adapter._engine = self.engine
        pkg = _companion_pkg()
        self.server_mod = importlib.import_module(f"{pkg}.server")
        self.branch_mod = importlib.import_module(f"{pkg}.modules.chat_branch")
        self.state_mod = importlib.import_module(f"{pkg}.modules.companion_state")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            asyncio.run(self.engine.stop())
        except Exception:
            pass
        self.db.close()
        close_all_shared_dbs()
        close_all()
        reset_muse_home(self._home_token)

    def _seed_turn(self, chat_id: str, turn_id: str, question: str, answer: str, *, extra: dict | None = None):
        self.adapter.store.append(
            chat_id,
            {
                "event": "user_message",
                "chat_id": chat_id,
                "turn_id": turn_id,
                "text": question,
                "starts_turn": True,
                "turn_phase": "user",
            },
        )
        self.adapter.store.append(
            chat_id,
            {
                "event": "message",
                "chat_id": chat_id,
                "turn_id": turn_id,
                "text": "",
                "kind": "tool_hint",
                "turn_phase": "activity",
                "tool_events": [{"name": "web_search", "arguments": {"query": question}, "result": "ok"}],
            },
        )
        self.adapter.store.append(
            chat_id,
            {
                "event": "message",
                "chat_id": chat_id,
                "turn_id": turn_id,
                "text": answer,
                "turn_phase": "answer",
            },
        )
        self.adapter.store.append(
            chat_id,
            {"event": "turn_end", "chat_id": chat_id, "turn_id": turn_id, "turn_phase": "complete"},
        )
        sess = self.db.get_or_create_active_session(chat_id, source="companion", model="gpt-4o")
        sid = str(sess["id"])
        self.db.append_message(sid, chat_id, "user", question, turn_id=turn_id, extra=extra)
        self.db.append_message(sid, chat_id, "assistant", answer, turn_id=turn_id)
        return sid

    def test_fork_chat_truncates_at_turn_clones_musedb_and_clears_checkpoint_when_truncated(self):
        chat_id = self.adapter.store.create()
        self.adapter.store.set_title(chat_id, "周末西湖骑行路线规划与装备")
        sid = self._seed_turn(
            chat_id, "t1", "第一问", "第一答", extra={"legacy_key": f"state_db:old:{chat_id}:1"}
        )
        self._seed_turn(chat_id, "t2", "第二问", "第二答")
        self._seed_turn(chat_id, "t3", "第三问", "第三答")
        self.db.update_session(
            sid,
            summary_checkpoint="早期对话摘要",
            metadata={"soul_snapshot": "你是贴心伙伴"},
        )

        # 1) Fork up to turn t2 -> slices out t3, clears summary_checkpoint, preserves soul_snapshot & legacy_key safety
        forked = self.branch_mod.fork_chat(self.adapter, chat_id, turn_id="t2")
        new_id = forked["chat_id"]
        self.assertNotEqual(new_id, chat_id)
        self.assertEqual(forked["key"], f"websocket:{new_id}")

        listed = {row["key"]: row for row in forked["sessions"]}
        self.assertIn(f"websocket:{new_id}", listed)
        new_row = listed[f"websocket:{new_id}"]
        self.assertEqual(new_row["title"], "周末西湖骑行路线规划 · 分叉")
        self.assertEqual(new_row["forked_from"], {"chat_id": chat_id, "turn_id": "t2"})
        self.assertEqual(new_row["preview"], "第二答")

        fork_events = self.adapter.store.events(new_id)
        self.assertEqual([e["turn_id"] for e in fork_events if e.get("event") == "user_message"], ["t1", "t2"])
        self.assertEqual(fork_events[-1]["event"], "turn_end")
        self.assertEqual(fork_events[-1]["turn_id"], "t2")
        self.assertTrue(all(e["chat_id"] == new_id for e in fork_events))

        new_sess = self.db.get_active_session(new_id, source="companion")
        self.assertIsNotNone(new_sess)
        self.assertEqual(new_sess["model"], "gpt-4o")
        self.assertIsNone(new_sess["summary_checkpoint"])
        self.assertEqual(new_sess["metadata"].get("soul_snapshot"), "你是贴心伙伴")
        cloned_msgs = self.db.get_messages(str(new_sess["id"]), active_only=True)
        self.assertEqual([(m["turn_id"], m["role"], m["content"]) for m in cloned_msgs], [
            ("t1", "user", "第一问"),
            ("t1", "assistant", "第一答"),
            ("t2", "user", "第二问"),
            ("t2", "assistant", "第二答"),
        ])
        self.assertNotIn("legacy_key", cloned_msgs[0]["extra"])
        self.assertEqual(cloned_msgs[0]["extra"].get("forked_legacy_key"), f"state_db:old:{chat_id}:1")

        # 2) Full fork without turn_id -> copies all completed events AND preserves summary_checkpoint
        full_fork = self.branch_mod.fork_chat(self.adapter, chat_id, title="完整备份分支")
        full_id = full_fork["chat_id"]
        full_sess = self.db.get_active_session(full_id, source="companion")
        self.assertIsNotNone(full_sess)
        self.assertEqual(full_sess["summary_checkpoint"], "早期对话摘要")
        self.assertEqual(len(self.db.get_messages(str(full_sess["id"]), active_only=True)), 6)
        self.assertEqual(self.adapter.store.title(full_id), "完整备份分支")

        # 3) Missing turn_id raises ValueError("找不到这条消息")
        with self.assertRaisesRegex(ValueError, "找不到这条消息"):
            self.branch_mod.fork_chat(self.adapter, chat_id, turn_id="nonexistent-turn")

    def test_edit_and_resubmit_truncates_both_stores_cleans_pending_actor_and_runs_new_turn(self):
        chat_id = self.adapter.store.create()
        sid = self._seed_turn(chat_id, "t1", "旧问题一", "旧回答一")
        self._seed_turn(chat_id, "t2", "旧问题二", "旧回答二")
        self._seed_turn(chat_id, "t3", "旧问题三", "旧回答三")

        async def run_edit():
            actor = self.engine.get_actor(chat_id)
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            actor._pending_clarify = PendingClarify(
                clarify_id="c1",
                chat_id=chat_id,
                turn_id_a="t3",
                tool_call_id="call_1",
                tool_message_id=999999,
                question="选哪个？",
                choices=["A", "B"],
                multi_select=False,
                future=fut,
                created_at=time.monotonic(),
            )
            actor._pending_confirm = PendingSlashConfirm(
                confirm_id="cf1",
                chat_id=chat_id,
                command="undo",
                confirm_turn_id="t3",
                target_turn_id="t3",
                created_at=time.monotonic(),
            )

            result = await self.branch_mod.edit_and_resubmit(
                self.adapter,
                chat_id,
                turn_id="t2",
                content="改写后的问题二",
            )
            await actor.wait_idle(timeout=3.0)
            return result, actor, fut

        result, actor, clarify_fut = asyncio.run(run_edit())
        new_tid = result["turn_id"]
        self.assertEqual(result["chat_id"], chat_id)
        self.assertTrue(new_tid)
        self.assertNotEqual(new_tid, "t2")
        self.assertIsNone(actor._pending_clarify)
        self.assertIsNone(actor._pending_confirm)
        self.assertTrue(clarify_fut.cancelled())

        # ChatStore: t1 preserved, t2 & t3 completely gone, new_tid persisted with new answer
        events = self.adapter.store.events(chat_id)
        user_turns = [(e["turn_id"], e["text"]) for e in events if e.get("event") == "user_message"]
        self.assertEqual(user_turns, [("t1", "旧问题一"), (new_tid, "改写后的问题二")])
        answers = [(e["turn_id"], e["text"]) for e in events if e.get("event") == "message" and e.get("turn_phase") == "answer"]
        self.assertEqual(answers, [("t1", "旧回答一"), (new_tid, "新回答：改写后的问题二")])

        # MuseDB: t1 active, old t2/t3 deactivated, new_tid active, LLM never saw t2/t3!
        active_msgs = self.db.get_messages(sid, active_only=True)
        self.assertEqual([(m["turn_id"], m["role"], m["content"]) for m in active_msgs], [
            ("t1", "user", "旧问题一"),
            ("t1", "assistant", "旧回答一"),
            (new_tid, "user", "改写后的问题二"),
            (new_tid, "assistant", "新回答：改写后的问题二"),
        ])
        sent_contents = [str(m.get("content") or "") for m in self.prompted_messages[-1]]
        self.assertTrue(any("旧问题一" in c for c in sent_contents))
        self.assertFalse(any("旧问题二" in c or "旧问题三" in c for c in sent_contents))

    def test_edit_first_turn_clears_stale_summary_checkpoint(self):
        chat_id = self.adapter.store.create()
        sid = self._seed_turn(chat_id, "t1", "初始问题", "初始回答")
        self.db.update_session(sid, summary_checkpoint="不该保留的旧总结")

        async def run_first_edit():
            res = await self.branch_mod.edit_and_resubmit(
                self.adapter, chat_id, turn_id="t1", content="全新开场问题"
            )
            await self.engine.get_actor(chat_id).wait_idle(timeout=3.0)
            return res

        asyncio.run(run_first_edit())
        self.assertIsNone(self.db.get_session(sid)["summary_checkpoint"])

    def test_ws_main_chat_allows_fork_and_edit_while_rejecting_rename_delete_and_busy_409(self):
        main_chat = self.adapter.store.create()
        self.state_mod.update_companion_state({"chat_id": main_chat, "name": "小缪"})
        self._seed_turn(main_chat, "m1", "主对话第一句", "主对话回复一")
        self._seed_turn(main_chat, "m2", "主对话第二句", "主对话回复二")

        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            boot = client.get("/webui/bootstrap").json()
            ws_token = boot["token"]
            with client.websocket_connect(f"/ws?token={ws_token}") as ws:
                ready = ws.receive_json()
                self.assertEqual(ready["event"], "ready")

                # Main chat still forbids rename & delete (409)
                ren = _ws_mutation(ws, "companion.chats.change", {"id": main_chat, "action": "rename", "title": "新名"}, "r-ren")
                self.assertFalse(ren["ok"])
                self.assertEqual(ren["error"]["status"], 409)

                dele = _ws_mutation(ws, "companion.chats.change", {"id": main_chat, "action": "delete"}, "r-del")
                self.assertFalse(dele["ok"])
                self.assertEqual(dele["error"]["status"], 409)

                # Main chat ALLOWS fork via companion.chats.change
                fork_resp = _ws_mutation(
                    ws, "companion.chats.change", {"id": main_chat, "action": "fork", "turn_id": "m1"}, "r-fork"
                )
                self.assertTrue(fork_resp["ok"])
                forked_id = fork_resp["result"]["chat_id"]
                self.assertTrue(self.adapter.store.exists(forked_id))
                self.assertEqual(
                    [e["turn_id"] for e in self.adapter.store.events(forked_id) if e.get("event") == "user_message"],
                    ["m1"],
                )

                # Legacy WS frame `fork_chat` -> `chat_forked` also works
                ws.send_json({"type": "fork_chat", "source_chat_id": main_chat, "up_to_turn_id": "m2", "title": "WS分叉"})
                fork_frame = ws.receive_json()
                self.assertEqual(fork_frame["event"], "chat_forked")
                self.assertEqual(fork_frame["source_chat_id"], main_chat)
                self.assertEqual(self.adapter.store.title(fork_frame["chat_id"]), "WS分叉")

                # Busy check: active turn in adapter._turns returns 409
                self.adapter._turns[main_chat] = "m2"
                try:
                    busy = _ws_mutation(
                        ws,
                        "companion.chats.change",
                        {"id": main_chat, "action": "edit_message", "turn_id": "m2", "content": "趁忙修改"},
                        "r-busy",
                    )
                    self.assertFalse(busy["ok"])
                    self.assertEqual(busy["error"]["status"], 409)
                    self.assertIn("正在回复中", busy["error"]["message"])
                finally:
                    self.adapter._turns.pop(main_chat, None)

                # Validation errors: 400 (empty content / missing turn_id) and 404 (missing chat / missing turn)
                bad_empty = _ws_mutation(
                    ws,
                    "companion.chats.change",
                    {"id": main_chat, "action": "edit_message", "turn_id": "m2", "content": "   "},
                    "r-400",
                )
                self.assertFalse(bad_empty["ok"])
                self.assertEqual(bad_empty["error"]["status"], 400)

                not_found_turn = _ws_mutation(
                    ws,
                    "companion.chats.change",
                    {"id": main_chat, "action": "edit_message", "turn_id": "no-such-turn", "content": "测试"},
                    "r-404t",
                )
                self.assertFalse(not_found_turn["ok"])
                self.assertEqual(not_found_turn["error"]["status"], 404)

                not_found_chat = _ws_mutation(
                    ws,
                    "companion.chats.change",
                    {"id": "missing-chat-id", "action": "fork"},
                    "r-404c",
                )
                self.assertFalse(not_found_chat["ok"])
                self.assertEqual(not_found_chat["error"]["status"], 404)

                # Main chat ALLOWS edit_message when idle!
                edit_ok = _ws_mutation(
                    ws,
                    "companion.chats.change",
                    {"id": main_chat, "action": "edit_message", "turn_id": "m2", "content": "主对话改写第二句"},
                    "r-edit-ok",
                )
                self.assertTrue(edit_ok["ok"])
                self.assertEqual(edit_ok["result"]["chat_id"], main_chat)
                self.assertNotEqual(edit_ok["result"]["turn_id"], "m2")


class StubAdapterExportMemoryAndCreationsTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("companion-export-pin-")
        self._home_token = bind_muse_home(self.home)
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        self.server_mod = importlib.import_module(f"{pkg}.server")
        self.branch_mod = importlib.import_module(f"{pkg}.modules.chat_branch")
        self.creations_mod = importlib.import_module(f"{pkg}.modules.creations")
        self.memory_mod = importlib.import_module(f"{pkg}.modules.memory")
        self.state_mod = importlib.import_module(f"{pkg}.modules.companion_state")

        webui_dir = self.home / "companion"
        webui_dir.mkdir(parents=True, exist_ok=True)

        class StubAdapter:
            def __init__(self):
                self.tokens = auth_mod.TokenIssuer("secret-pass")
                self.devices = devices_mod.Devices(webui_dir)
                self.media = media_mod.MediaStore(webui_dir)
                self.store = chat_store(webui_dir)
                self.web_dist = webui_dir / "no-dist"
                self.socket_devices = {}
                self._turns = {}

            def attach(self, chat_id, socket):
                pass

            def detach_all(self, socket):
                pass

        self.adapter = StubAdapter()
        self.app = self.server_mod.build_app(self.adapter)
        self.addCleanup(reset_muse_home, self._home_token)

    def test_stub_adapter_edit_and_export_md_and_json_with_status_codes(self):
        self.state_mod.update_companion_state({"name": "阿缪"})
        chat_id = self.adapter.store.create()
        self.adapter.store.set_title(chat_id, "Python 异步与架构笔记")

        # Turn 1: user attachment + tool_hint + card JSON + real Python & JSON data code blocks + sources
        self.adapter.store.append(
            chat_id,
            {
                "event": "user_message",
                "chat_id": chat_id,
                "turn_id": "t1",
                "text": "帮我看看这段脚本和配置",
                "media_urls": [{"url": "/api/media/s/p?token=secret123", "name": "architecture.png", "kind": "image"}],
                "created_at_ms": 1700000001000,
            },
        )
        self.adapter.store.append(
            chat_id,
            {
                "event": "message",
                "chat_id": chat_id,
                "turn_id": "t1",
                "text": "",
                "kind": "tool_hint",
                "turn_phase": "activity",
                "tool_events": [{"name": "read_file", "result": "internal tool output"}],
                "created_at_ms": 1700000002000,
            },
        )
        reply_with_cards_and_code = (
            "下面是优化后的代码与配置示例：\n\n"
            "```python\nasync def main():\n    return 42\n```\n\n"
            "```json\n{\"timeout\": 30, \"retries\": 3}\n```\n\n"
            "```card\n{\"type\": \"choices\", \"options\": [{\"label\": \"继续\", \"send\": \"继续\"}]}\n```"
        )
        self.adapter.store.append(
            chat_id,
            {
                "event": "message",
                "chat_id": chat_id,
                "turn_id": "t1",
                "text": reply_with_cards_and_code,
                "turn_phase": "answer",
                "sources": [
                    {
                        "id": 1,
                        "title": "Python asyncio 官方文档",
                        "url": "https://docs.python.org/3/library/asyncio.html?api_key=leak999",
                        "domain": "docs.python.org",
                    }
                ],
                "created_at_ms": 1700000003000,
            },
        )
        self.adapter.store.append(chat_id, {"event": "turn_end", "chat_id": chat_id, "turn_id": "t1"})

        # Turn 2: to be edited on StubAdapter (which lacks adapter.submit)
        self.adapter.store.append(
            chat_id,
            {"event": "user_message", "chat_id": chat_id, "turn_id": "t2", "text": "旧追问"},
        )
        edit_res = asyncio.run(
            self.branch_mod.edit_and_resubmit(self.adapter, chat_id, turn_id="t2", content="Stub追问新内容")
        )
        self.assertEqual(
            [e["text"] for e in self.adapter.store.events(chat_id) if e.get("event") == "user_message"],
            ["帮我看看这段脚本和配置", "Stub追问新内容"],
        )
        self.assertTrue(edit_res["turn_id"])

        with TestClient(self.app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            # 401 without bearer token
            unauth = client.get(f"/api/sessions/websocket:{chat_id}/export")
            self.assertEqual(unauth.status_code, 401)

            boot = client.get("/webui/bootstrap").json()
            headers = {"Authorization": f"Bearer {boot['api_token']}"}

            # 404 on unknown session key
            missing = client.get("/api/sessions/websocket:does-not-exist/export", headers=headers)
            self.assertEqual(missing.status_code, 404)

            # 400 on invalid format
            bad_fmt = client.get(f"/api/sessions/websocket:{chat_id}/export?format=xml", headers=headers)
            self.assertEqual(bad_fmt.status_code, 400)

            # 200 default Markdown export
            md_resp = client.get(f"/api/sessions/websocket:{chat_id}/export", headers=headers)
            self.assertEqual(md_resp.status_code, 200)
            self.assertIn("text/markdown", md_resp.headers["content-type"])
            self.assertIn("Content-Disposition", md_resp.headers)
            self.assertEqual(md_resp.headers.get("Cache-Control"), "no-store")
            md = md_resp.text
            self.assertIn("# Python 异步与架构笔记", md)
            self.assertIn("### 我", md)
            self.assertIn("### 阿缪", md)
            self.assertIn("architecture.png", md)
            self.assertIn("```python\nasync def main():", md)
            self.assertIn('"timeout": 30', md)  # normal json block preserved!
            self.assertNotIn('"choices"', md)  # raw card JSON stripped!
            self.assertNotIn("internal tool output", md)  # tool_hint stripped!
            self.assertIn("[Python asyncio 官方文档](https://docs.python.org/3/library/asyncio.html?api_key=[REDACTED])", md)

            # 200 JSON export
            json_resp = client.get(f"/api/sessions/websocket:{chat_id}/export?format=json", headers=headers)
            self.assertEqual(json_resp.status_code, 200)
            payload = json_resp.json()
            self.assertEqual(payload["chat_id"], chat_id)
            self.assertEqual(payload["title"], "Python 异步与架构笔记")
            self.assertEqual(payload["markdown"], md)
            self.assertEqual(len(payload["turns"]), 2)
            self.assertEqual(payload["turns"][0]["turn_id"], "t1")
            self.assertEqual(len(payload["turns"][0]["sources"]), 1)

    def test_save_artifact_and_ws_creations_save_mutation(self):
        # SVG goes to 图片, code/markdown/mermaid go to 文档, URL secrets are scrubbed
        svg_rel = self.creations_mod.save_artifact(
            title="架构矢量图",
            content='<svg xmlns="http://www.w3.org/2000/svg"><text>https://api.example.com/x?token=sec123</text></svg>',
            fmt="svg",
            prompt="画一个架构图",
        )
        self.assertTrue(svg_rel.startswith("图片/"))
        svg_file = self.creations_mod.creations_root() / svg_rel
        self.assertIn("[REDACTED]", svg_file.read_text(encoding="utf-8"))

        for fmt, expected_ext in (
            ("html", ".html"),
            ("md", ".md"),
            ("mermaid", ".mmd"),
            ("mmd", ".mmd"),
            ("txt", ".txt"),
            ("json", ".json"),
            ("csv", ".csv"),
            ("py", ".py"),
            ("js", ".js"),
            ("ts", ".ts"),
            ("tsx", ".tsx"),
            ("css", ".css"),
            ("sh", ".sh"),
            ("yaml", ".yaml"),
        ):
            rel = self.creations_mod.save_artifact(title=f"产物-{fmt}", content=f"content-{fmt}", fmt=fmt)
            self.assertTrue(rel.startswith("文档/"), f"{fmt} should file under 文档/, got {rel}")
            self.assertTrue(rel.endswith(expected_ext), f"{fmt} should end with {expected_ext}, got {rel}")

        # Validation: unsupported format, empty content, > 2MB
        with self.assertRaises(ValueError):
            self.creations_mod.save_artifact(title="bad", content="abc", fmt="exe")
        with self.assertRaises(ValueError):
            self.creations_mod.save_artifact(title="empty", content="   ", fmt="md")
        with self.assertRaises(ValueError):
            self.creations_mod.save_artifact(title="huge", content="a" * (2 * 1024 * 1024 + 10), fmt="txt")

        with TestClient(self.app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            ws_token = client.get("/webui/bootstrap").json()["token"]
            with client.websocket_connect(f"/ws?token={ws_token}") as ws:
                _ = ws.receive_json()
                saved = _ws_mutation(
                    ws,
                    "companion.creations.save",
                    {"title": "交互看板", "content": "<!doctype html><h1>Board</h1>", "fmt": "html", "prompt": "生成看板"},
                    "r-save",
                )
                self.assertTrue(saved["ok"])
                saved_id = saved["result"]["saved_id"]
                self.assertTrue(saved_id.startswith("文档/"))
                matched = next(it for it in saved["result"]["items"] if it["id"] == saved_id)
                self.assertEqual(matched["origin"], "canvas")
                self.assertEqual(matched["title"], "交互看板")

                bad_save = _ws_mutation(
                    ws,
                    "companion.creations.save",
                    {"title": "坏格式", "content": "binary", "fmt": "dll"},
                    "r-bad-save",
                )
                self.assertFalse(bad_save["ok"])
                self.assertEqual(bad_save["error"]["status"], 400)

                # WS companion.memory.pin end-to-end (remember -> pin -> unpin -> 400 / 404 errors)
                rem = _ws_mutation(ws, "companion.memory.remember", {"text": "每天早上喝一杯温水"}, "r-rem")
                self.assertTrue(rem["ok"])
                pin_ok = _ws_mutation(
                    ws,
                    "companion.memory.pin",
                    {"file": "USER.md", "line": "每天早上喝一杯温水", "pinned": True},
                    "r-pin-ok",
                )
                self.assertTrue(pin_ok["ok"])
                self.assertTrue(pin_ok["result"]["pinned"])
                pinned_items = [
                    it
                    for sec in pin_ok["result"]["sections"]
                    for it in sec["items"]
                    if it["line"] == "每天早上喝一杯温水"
                ]
                self.assertEqual(len(pinned_items), 1)
                self.assertTrue(pinned_items[0]["pinned"])

                pin_400 = _ws_mutation(
                    ws,
                    "companion.memory.pin",
                    {"file": "USER.md", "line": ""},
                    "r-pin-400",
                )
                self.assertFalse(pin_400["ok"])
                self.assertEqual(pin_400["error"]["status"], 400)

                pin_404 = _ws_mutation(
                    ws,
                    "companion.memory.pin",
                    {"file": "USER.md", "line": "根本不存在的一条记忆", "pinned": True},
                    "r-pin-404",
                )
                self.assertFalse(pin_404["ok"])
                self.assertEqual(pin_404["error"]["status"], 404)


class MemoryPinProtectionTest(unittest.TestCase):
    def setUp(self):
        self.folder, (
            self.memory,
            self.tidy,
            self.fade,
            self.learn,
        ) = load("memory", "memory_tidy", "memory_fade", "memory_learn")
        self.mem_dir = self.folder / "memories"
        self.disk_store = _FakeDiskMemoryStore(self.mem_dir)

        import muse.tools.memory_store as mem_store

        self.enterContext(mock.patch.object(mem_store, "load_on_disk_store", side_effect=lambda *a, **kw: self.disk_store))
        self.enterContext(mock.patch.object(mem_store, "get_memory_dir", side_effect=lambda *a, **kw: self.mem_dir))

    def test_pin_toggle_persistence_forget_cleanup_and_protection_across_tidy_and_fade(self):
        pinned_plan = "【近况与计划】正在准备十二月的马拉松比赛训练计划"
        unpinned_plan = "【近况与计划】十月考虑过换显示器"
        dup_a = "【喜好与习惯】平时喜欢喝浅烘手冲咖啡"
        dup_b = "【喜好与习惯】平时喜欢喝手冲咖啡"
        pinned_note = "服务器备份脚本放在 /opt/backup.sh"

        for entry in (pinned_plan, unpinned_plan, dup_a, dup_b):
            self.disk_store.add("user", entry)
        self.disk_store.add("memory", pinned_note)

        # Initially nothing is pinned
        initial = self.memory.read_memory()
        for sec in initial["sections"]:
            for item in sec["items"]:
                self.assertFalse(item["pinned"])

        # Pin both USER.md and MEMORY.md items (toggle None -> True)
        res1 = self.memory.pin_memory("USER.md", pinned_plan)
        self.assertTrue(res1["pinned"])
        self.assertTrue(self.memory.is_pinned("USER.md", pinned_plan))
        self.assertTrue((self.folder / "memory_pins.json").is_file())

        self.memory.pin_memory("USER.md", dup_a, pinned=True)
        self.memory.pin_memory("MEMORY.md", pinned_note, pinned=True)

        after_pin = self.memory.read_memory()
        by_line = {it["line"]: it["pinned"] for sec in after_pin["sections"] for it in sec["items"]}
        self.assertTrue(by_line[pinned_plan])
        self.assertTrue(by_line[dup_a])
        self.assertFalse(by_line[unpinned_plan])
        self.assertTrue(by_line[pinned_note])

        # 1) memory_fade NEVER shortens or forgets pinned items even after 200 idle days!
        now = 1_800_000_000.0
        day = 86400
        meta = self.tidy._read_meta()
        for entry in (pinned_plan, unpinned_plan, dup_a, dup_b, pinned_note):
            meta["seen_at"][self.tidy.entry_key(entry)] = now - 200 * day
        shorten, forget = self.fade.plan(
            {"user": [pinned_plan, unpinned_plan, dup_a, dup_b], "memory": [pinned_note]},
            meta,
            now,
        )
        self.assertEqual([e for _, e, _ in shorten], [])
        self.assertEqual({e for _, e, _ in forget}, {unpinned_plan, dup_b})

        # 2) memory_tidy NEVER merges or drops pinned items even if LLM proposes it!
        malicious_proposal = {
            "merge": [{"ids": [3, 4], "text": "【喜好与习惯】平时喜欢喝手冲咖啡"}],
            "drop": [{"id": 1, "why": "过时"}, {"id": 2, "why": "过时"}],
        }
        with mock.patch.object(self.tidy, "_propose", return_value=malicious_proposal):
            tidy_res = self.tidy.tidy(force=True)
        self.assertEqual(tidy_res["changed"], 1)  # ONLY id=2 (unpinned_plan) is allowed to drop!
        remaining_user = self.disk_store.entries_for("user")
        self.assertIn(pinned_plan, remaining_user)
        self.assertIn(dup_a, remaining_user)
        self.assertNotIn(unpinned_plan, remaining_user)
        self.assertIn(pinned_note, self.disk_store.entries_for("memory"))

        # 3) Toggling pin off and forget_memory cleans memory_pins.json
        toggled_off = self.memory.pin_memory("USER.md", dup_a)
        self.assertFalse(toggled_off["pinned"])
        self.assertFalse(self.memory.is_pinned("USER.md", dup_a))

        self.memory.forget_memory("USER.md", pinned_plan)
        self.assertFalse(self.memory.is_pinned("USER.md", pinned_plan))
        pins_on_disk = json.loads((self.folder / "memory_pins.json").read_text(encoding="utf-8"))
        self.assertEqual(pins_on_disk["USER.md"], [])
        self.assertEqual(pins_on_disk["MEMORY.md"], [pinned_note])

        # Validation errors on pin_memory
        with self.assertRaises(ValueError):
            self.memory.pin_memory("INVALID.md", "abc", True)
        with self.assertRaises(ValueError):
            self.memory.pin_memory("USER.md", "不存在的记忆条目", True)


if __name__ == "__main__":
    unittest.main()
