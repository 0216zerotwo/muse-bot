"""Comprehensive unit & ASGI integration tests for Wave 3 backend upgrades (PR #38)."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from _loader import chat_store, load, temp_dir
from muse.app import NativeCompanionAdapter
from muse.db import MuseDB, close_all
from muse.engine import ChatTurnEngine
from muse.llm.types import StreamDone, TextDelta
from muse.paths import bind_muse_home, muse_home_override, reset_muse_home
from muse.runtime import close_all_shared_dbs
from test_auth_devices import _companion_pkg


def _ws_mutation(ws, action: str, payload: dict, req_id: str = "r1") -> dict:
    ws.send_json({"type": "webui_request", "request_id": req_id, "action": action, "payload": payload})
    while True:
        frame = ws.receive_json()
        if frame.get("event") == "webui_response" and frame.get("request_id") == req_id:
            return frame


class FollowupsAndTtsFixesTest(unittest.TestCase):
    def test_followups_regenerate_digest_key_and_invalidation(self) -> None:
        _, (followups,) = load("followups")
        followups.clear_cache()
        self.addCleanup(followups.clear_cache)

        store = chat_store(temp_dir("w3-followups-"))
        chat_a = store.create()
        chat_b = store.create()
        store.append(chat_a, {"event": "user_message", "chat_id": chat_a, "turn_id": "t1", "text": "写个函数"})
        store.append(chat_a, {"event": "message", "chat_id": chat_a, "turn_id": "t1", "text": "```python\ndef f(): return 1\n```"})
        store.append(chat_b, {"event": "user_message", "chat_id": chat_b, "turn_id": "t1", "text": "对比方案"})
        store.append(chat_b, {"event": "message", "chat_id": chat_b, "turn_id": "t1", "text": "方案 A 和方案 B 对比如下。"})

        llm_calls: list[str] = []

        async def fake_llm(**kw):
            prompt = str((kw.get("messages") or [{}])[-1].get("content") or "")
            llm_calls.append(prompt)
            if "方案" in prompt:
                content = '["选方案A还是B", "补充预算对比", "下一步怎么执行"]'
            elif "return 2" in prompt:
                content = '["为什么改成返回2", "补充并发测试", "性能如何"]'
            else:
                content = '["为什么返回1", "补充边界测试", "代码能否更短"]'
            msg = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        with (
            mock.patch.object(followups, "_resolve_quick_model_target", return_value={"provider": "m-fast", "model": "fast"}),
            mock.patch("muse.llm.auxiliary.async_call_llm", side_effect=fake_llm),
        ):
            r1 = asyncio.run(followups.get_followups(store, chat_a, turn_id="t1"))
            self.assertEqual(r1["suggestions"][0], "为什么返回1")
            self.assertEqual(len(llm_calls), 1)

            # Same text hits cache
            r1_again = asyncio.run(followups.get_followups(store, chat_a, turn_id="t1"))
            self.assertEqual(r1_again, r1)
            self.assertEqual(len(llm_calls), 1)

            # Regenerate reuses turn_id="t1" with new assistant text -> digest changes, must NOT return stale cache!
            store.remove_events(chat_a, lambda e: e.get("turn_id") == "t1" and e.get("event") != "user_message")
            store.append(chat_a, {"event": "message", "chat_id": chat_a, "turn_id": "t1", "text": "```python\ndef f(): return 2\n```"})
            r2 = asyncio.run(followups.get_followups(store, chat_a, turn_id="t1"))
            self.assertEqual(r2["suggestions"][0], "为什么改成返回2")
            self.assertEqual(len(llm_calls), 2)

            # Populate chat_b too, then invalidate_followups(chat_a) only drops chat_a
            rb1 = asyncio.run(followups.get_followups(store, chat_b, turn_id="t1"))
            self.assertEqual(len(llm_calls), 3)
            removed = followups.invalidate_followups(chat_a)
            self.assertGreaterEqual(removed, 2)
            rb2 = asyncio.run(followups.get_followups(store, chat_b, turn_id="t1"))
            self.assertEqual(rb2, rb1)
            self.assertEqual(len(llm_calls), 3)  # chat_b remained cached

            # chat_a recomputes after invalidation
            _ = asyncio.run(followups.get_followups(store, chat_a, turn_id="t1"))
            self.assertEqual(len(llm_calls), 4)

    def test_tts_singleflight_signed_route_security_and_cache_eviction(self) -> None:
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        server_mod = importlib.import_module(f"{pkg}.server")
        tts_mod = importlib.import_module(f"{pkg}.modules.tts_api")

        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            folder = Path(tmp) / "companion"
            folder.mkdir(parents=True, exist_ok=True)
            web_dist = folder / "no-dist"
            adapter = SimpleNamespace(
                tokens=auth_mod.TokenIssuer("tts-secret"),
                devices=devices_mod.Devices(folder),
                media=media_mod.MediaStore(folder),
                store=chat_store(folder),
                web_dist=web_dist,
                socket_devices={},
                _turns={},
                attach=lambda *a: None,
                detach_all=lambda *a: None,
            )

            # 1) Single-flight: 4 concurrent requests for identical text only invoke text_to_speech ONCE
            call_count = 0
            count_lock = threading.Lock()
            entered = threading.Event()
            release = threading.Event()

            def slow_tts(text: str, *, voice=None, model=None, speed=None, output_path=None, provider=None):
                nonlocal call_count
                with count_lock:
                    call_count += 1
                    entered.set()
                release.wait(timeout=3.0)
                Path(output_path).write_bytes(b"ID3-SINGLEFLIGHT-MP3")
                return {"success": True, "file_path": output_path, "provider": "edge"}

            with mock.patch("muse.media.tts.text_to_speech", side_effect=slow_tts):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futs = [
                        pool.submit(
                            contextvars.copy_context().run,
                            tts_mod.synthesize,
                            adapter,
                            {"text": "同一段并行朗读文本", "provider": "edge"},
                        )
                        for _ in range(4)
                    ]
                    self.assertTrue(entered.wait(timeout=2.0))
                    time.sleep(0.05)
                    release.set()
                    results = [f.result(timeout=5.0) for f in futs]

            self.assertEqual(call_count, 1)
            self.assertEqual(sum(1 for r in results if not r["cached"]), 1)
            self.assertEqual(sum(1 for r in results if r["cached"]), 3)
            self.assertEqual(len({r["url"] for r in results}), 1)
            self.assertFalse(adapter.media.root.exists())  # Zero copies into media/!

            # 2) Signed TTS file route serves audio/mpeg and rejects bad sig, traversal, and cross-prefix reuse
            app = server_mod.build_app(adapter)
            with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
                url = results[0]["url"]
                self.assertTrue(url.startswith(server_mod.TTS_PREFIX + "/"))
                resp = client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.content, b"ID3-SINGLEFLIGHT-MP3")
                self.assertIn("audio/mpeg", resp.headers.get("content-type", ""))

                # Tampered signature -> 404
                self.assertEqual(client.get(f"{server_mod.TTS_PREFIX}/bad-sig/bad-payload").status_code, 404)
                # Valid signature for traversal or hidden file -> 404
                trav_url = adapter.media.sign(tts_mod.TTS_PREFIX, "../secret.txt")
                self.assertEqual(client.get(trav_url).status_code, 404)
                hidden_url = adapter.media.sign(tts_mod.TTS_PREFIX, ".hidden.mp3")
                self.assertEqual(client.get(hidden_url).status_code, 404)
                # Cross-prefix token signed for /api/media rejected on /api/webui/companion/tts/file
                staged = adapter.media.stage_file(next(tts_mod.audio_cache_dir().glob("tts_*.mp3")))
                assert staged is not None
                sig, payload = staged["url"].rsplit("/", 2)[-2:]
                self.assertEqual(client.get(f"{server_mod.TTS_PREFIX}/{sig}/{payload}").status_code, 404)

            # 3) Audio cache eviction keeps at most MAX_AUDIO_CACHE_FILES newest entries
            cache_dir = tts_mod.audio_cache_dir()
            for f in cache_dir.glob("tts_*.mp3"):
                f.unlink(missing_ok=True)
            with mock.patch.object(tts_mod, "MAX_AUDIO_CACHE_FILES", 3):
                def quick_tts(text: str, *, output_path=None, provider=None, **_kw):
                    Path(output_path).write_bytes(text.encode("utf-8"))
                    return {"success": True, "file_path": output_path, "provider": "edge"}

                with mock.patch("muse.media.tts.text_to_speech", side_effect=quick_tts):
                    for idx in range(5):
                        tts_mod.synthesize(adapter, {"text": f"音频文件编号 {idx}", "provider": "edge"})
                        time.sleep(0.01)
                kept = list(cache_dir.glob("tts_*.mp3"))
                self.assertEqual(len(kept), 3)


class ChatStorePerfAndSearchTest(unittest.TestCase):
    def test_chat_store_skips_index_rewrite_on_tool_hint_and_caches_events(self) -> None:
        folder = temp_dir("w3-store-perf-")
        store = chat_store(folder)
        chat_id = store.create()

        write_calls = 0
        orig_write = store._write_index

        def counting_write(data):
            nonlocal write_calls
            write_calls += 1
            return orig_write(data)

        with mock.patch.object(store, "_write_index", side_effect=counting_write):
            store.append(chat_id, {"event": "user_message", "chat_id": chat_id, "turn_id": "t1", "text": "你好"})
            self.assertEqual(write_calls, 1)

            # 5 intermediate tool_hint / delta events must NOT rewrite chats.json!
            for i in range(5):
                store.append(
                    chat_id,
                    {"event": "message", "chat_id": chat_id, "turn_id": "t1", "text": "", "kind": "tool_hint", "n": i},
                )
            store.append(chat_id, {"event": "file_edit", "chat_id": chat_id, "turn_id": "t1", "path": "a.py"})
            self.assertEqual(write_calls, 1)
            self.assertEqual(store._read_index()[chat_id]["rev"], 7)

            # Assistant reply + turn_end flush chats.json
            store.append(chat_id, {"event": "message", "chat_id": chat_id, "turn_id": "t1", "text": "完成回复"})
            self.assertEqual(write_calls, 2)
            store.append(chat_id, {"event": "turn_end", "chat_id": chat_id, "turn_id": "t1"})
            self.assertEqual(write_calls, 3)

        # _events_cache avoids re-reading .jsonl when (mtime_ns, size) unchanged, and invalidates on external change
        _ = store.events(chat_id)
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("should hit _events_cache")) as _:
            cached_evs = store.events(chat_id)
            self.assertEqual(len(cached_evs), 9)
            payload = store.thread_payload(chat_id)
            self.assertEqual(len(payload["events"]), 9)

        # Mutating returned events must not corrupt internal cache
        cached_evs.append({"event": "bogus"})
        self.assertEqual(len(store.events(chat_id)), 9)

        # External append on disk changes (mtime_ns, size) -> cache reloads cleanly
        p = store._path(chat_id)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "turn_end", "chat_id": chat_id, "turn_id": "t2", "created_at_ms": 999999}) + "\n")
        self.assertEqual(len(store.events(chat_id)), 10)

    def test_chat_search_skips_jsonl_scan_for_musedb_synced_chats_but_scans_fallback(self) -> None:
        _, (search_mod,) = load("chat_search")
        home = temp_dir("w3-search-home-")
        store = chat_store(home / "companion")
        synced_cid = store.create()
        fallback_cid = store.create()
        store.set_title(synced_cid, "已同步会话")
        store.set_title(fallback_cid, "纯本地转录")

        with muse_home_override(home):
            db = MuseDB(home / "muse.db")
            try:
                sid = db.get_or_create_active_session(synced_cid, source="companion")["id"]
                db.append_message(sid, synced_cid, "user", "聊聊 SQLiteWAL 性能优化", turn_id="t1")
                db.append_message(sid, synced_cid, "assistant", "SQLiteWAL 并发读写性能很好", turn_id="t1")
                store.append(synced_cid, {"event": "user_message", "chat_id": synced_cid, "turn_id": "t1", "text": "聊聊 SQLiteWAL 性能优化", "created_at_ms": 1000})
                store.append(synced_cid, {"event": "message", "chat_id": synced_cid, "turn_id": "t1", "text": "SQLiteWAL 并发读写性能很好", "created_at_ms": 2000})

                # fallback_cid is ONLY in ChatStore (never in MuseDB)
                store.append(fallback_cid, {"event": "user_message", "chat_id": fallback_cid, "turn_id": "f1", "text": "纯本地的 SQLiteWAL 笔记", "created_at_ms": 3000})

                seen_event_reads: list[str] = []
                orig_events = store.events

                def spy_events(cid: str):
                    seen_event_reads.append(cid)
                    return orig_events(cid)

                with mock.patch.object(store, "events", side_effect=spy_events):
                    res = search_mod.search(store, "SQLiteWAL", db=db)

                self.assertNotIn(synced_cid, seen_event_reads)
                self.assertIn(fallback_cid, seen_event_reads)
                hit_cids = {h["chat_id"] for h in res["hits"]}
                self.assertEqual(hit_cids, {synced_cid, fallback_cid})
            finally:
                db.close()
                close_all_shared_dbs()
                close_all()


class BranchDeleteAndPerChatModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_dir("w3-branch-model-")
        self._home_token = bind_muse_home(self.home)
        self.db = MuseDB(self.home / "muse.db")
        self.adapter = NativeCompanionAdapter(home=self.home, start_background_services=False)
        self.seen_calls: list[dict] = []

        async def fake_stream(*, messages, model_override=None, **_):
            self.seen_calls.append({"messages": list(messages), "override": model_override})
            yield TextDelta("ok")
            yield StreamDone()

        self.engine = ChatTurnEngine(self.adapter, db=self.db, stream_llm=fake_stream)
        self.adapter._engine = self.engine
        pkg = _companion_pkg()
        self.server_mod = importlib.import_module(f"{pkg}.server")
        self.branch_mod = importlib.import_module(f"{pkg}.modules.chat_branch")
        self.models_mod = importlib.import_module(f"{pkg}.modules.models")
        self.state_mod = importlib.import_module(f"{pkg}.modules.companion_state")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            asyncio.run(self.engine.stop())
        except Exception:
            pass
        self.db.close()
        close_all_shared_dbs()
        close_all()
        reset_muse_home(self._home_token)

    def _seed_presets(self) -> None:
        data = self.models_mod.read_models()
        data["presets"] = {
            "Fast32K": {
                "provider": "svc-a",
                "model": "fast-32k",
                "reasoning_effort": "low",
                "max_tokens": 4096,
                "temperature": 0.2,
                "context_window_tokens": 32_000,
            },
            "Deep200K": {
                "provider": "svc-a",
                "model": "deep-200k",
                "reasoning_effort": "high",
                "max_tokens": 8192,
                "temperature": 0.1,
                "context_window_tokens": 200_000,
            },
        }
        data["default"] = "Fast32K"
        data["router"]["models"] = {
            "Fast32K": {"description": "", "tier": "cheap", "vision": False, "price_in": 1.0, "price_out": 2.0, "price_cached": 0.1},
            "Deep200K": {"description": "", "tier": "strong", "vision": True, "price_in": 10.0, "price_out": 30.0, "price_cached": 1.0},
        }
        self.models_mod._write_json(self.models_mod._models_path(), data)

    def test_delete_message_busy_409_orphan_turn_end_and_checkpoint_cleanup(self) -> None:
        chat_id = self.adapter.store.create()
        self.adapter.store.append(chat_id, {"event": "user_message", "chat_id": chat_id, "turn_id": "t1", "text": "第一句", "starts_turn": True})
        self.adapter.store.append(chat_id, {"event": "message", "chat_id": chat_id, "turn_id": "t1", "text": "第一答", "turn_phase": "answer"})
        self.adapter.store.append(chat_id, {"event": "turn_end", "chat_id": chat_id, "turn_id": "t1"})
        sid = self.db.get_or_create_active_session(chat_id, source="companion")["id"]
        self.db.append_message(sid, chat_id, "user", "第一句", turn_id="t1")
        self.db.append_message(sid, chat_id, "assistant", "第一答", turn_id="t1")
        self.db.update_session(sid, summary_checkpoint="历史压缩摘要")

        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            ws_token = client.get("/webui/bootstrap").json()["token"]
            with client.websocket_connect(f"/ws?token={ws_token}") as ws:
                _ = ws.receive_json()

                # Busy 409 check
                self.adapter._turns[chat_id] = "t1"
                try:
                    busy = _ws_mutation(
                        ws, "companion.chats.change",
                        {"id": chat_id, "action": "delete_message", "turn_id": "t1", "role": "reply"},
                        "del-busy",
                    )
                    self.assertFalse(busy["ok"])
                    self.assertEqual(busy["error"]["status"], 409)
                finally:
                    self.adapter._turns.pop(chat_id, None)

                # Delete reply then user -> orphan turn_end & summary_checkpoint both cleaned!
                d1 = _ws_mutation(ws, "companion.chats.change", {"id": chat_id, "action": "delete_message", "turn_id": "t1", "role": "reply"}, "del-1")
                self.assertTrue(d1["ok"])
                d2 = _ws_mutation(ws, "companion.chats.change", {"id": chat_id, "action": "delete_message", "turn_id": "t1", "role": "user"}, "del-2")
                self.assertTrue(d2["ok"])
                self.assertEqual(self.adapter.store.events(chat_id), [])
                self.assertEqual(self.db.get_messages(sid, active_only=True), [])
                self.assertIsNone(self.db.get_session(sid)["summary_checkpoint"])

    def test_fork_chat_across_new_session_rotation_and_store_backfill(self) -> None:
        chat_id = self.adapter.store.create()
        self.adapter.store.set_title(chat_id, "跨轮转测试")
        self.adapter.store.append(chat_id, {"event": "user_message", "chat_id": chat_id, "turn_id": "old1", "text": "旧问题1", "created_at_ms": 1000})
        self.adapter.store.append(chat_id, {"event": "message", "chat_id": chat_id, "turn_id": "old1", "text": "旧回答1", "created_at_ms": 2000})
        self.adapter.store.append(chat_id, {"event": "turn_end", "chat_id": chat_id, "turn_id": "old1", "created_at_ms": 2500})
        self.adapter.store.append(chat_id, {"event": "user_message", "chat_id": chat_id, "turn_id": "old2", "text": "旧问题2", "created_at_ms": 3000})
        self.adapter.store.append(chat_id, {"event": "message", "chat_id": chat_id, "turn_id": "old2", "text": "旧回答2", "created_at_ms": 4000})
        self.adapter.store.append(chat_id, {"event": "turn_end", "chat_id": chat_id, "turn_id": "old2", "created_at_ms": 4500})

        old_sid = self.db.get_or_create_active_session(chat_id, source="companion", model="gpt-4o")["id"]
        self.db.append_message(old_sid, chat_id, "user", "旧问题1", turn_id="old1")
        self.db.append_message(old_sid, chat_id, "assistant", "旧回答1", turn_id="old1")
        self.db.append_message(old_sid, chat_id, "user", "旧问题2", turn_id="old2")
        self.db.append_message(old_sid, chat_id, "assistant", "旧回答2", turn_id="old2")

        # Simulate /new rotating to a brand-new active session with turn new1
        new_sid = self.db.renew_session(chat_id, source="companion", model="gpt-4o")
        self.adapter.store.append(chat_id, {"event": "user_message", "chat_id": chat_id, "turn_id": "new1", "text": "新话题", "created_at_ms": 5000})
        self.adapter.store.append(chat_id, {"event": "message", "chat_id": chat_id, "turn_id": "new1", "text": "新回答", "created_at_ms": 6000})
        self.db.append_message(new_sid, chat_id, "user", "新话题", turn_id="new1")
        self.db.append_message(new_sid, chat_id, "assistant", "新回答", turn_id="new1")

        # Fork at old1 (pre-/new turn) -> must find old_sid and clone ONLY old1!
        forked = self.branch_mod.fork_chat(self.adapter, chat_id, turn_id="old1")
        forked_sess = self.db.get_active_session(forked["chat_id"], source="companion")
        self.assertIsNotNone(forked_sess)
        msgs = self.db.get_messages(str(forked_sess["id"]), active_only=True)
        self.assertEqual([(m["turn_id"], m["role"], m["content"]) for m in msgs], [
            ("old1", "user", "旧问题1"),
            ("old1", "assistant", "旧回答1"),
        ])

        # Pure ChatStore chat without MuseDB rows -> fork backfills user/assistant messages from sliced_events!
        legacy_chat = self.adapter.store.create()
        self.adapter.store.append(legacy_chat, {"event": "user_message", "chat_id": legacy_chat, "turn_id": "lg1", "text": "历史问题", "created_at_ms": 1000})
        self.adapter.store.append(legacy_chat, {"event": "message", "chat_id": legacy_chat, "turn_id": "lg1", "text": "历史回答", "created_at_ms": 2000})
        legacy_fork = self.branch_mod.fork_chat(self.adapter, legacy_chat, turn_id="lg1")
        lg_sess = self.db.get_active_session(legacy_fork["chat_id"], source="companion")
        self.assertIsNotNone(lg_sess)
        lg_msgs = self.db.get_messages(str(lg_sess["id"]), active_only=True)
        self.assertEqual([(m["role"], m["content"]) for m in lg_msgs], [("user", "历史问题"), ("assistant", "历史回答")])

    def test_per_chat_model_override_persistence_fork_inheritance_and_context_window_sync(self) -> None:
        self._seed_presets()
        main_chat = self.adapter.store.create()
        self.state_mod.update_companion_state({"chat_id": main_chat, "name": "伙伴"})
        side_chat = self.adapter.store.create()

        # resolve_session_override validation & output shape
        with self.assertRaises(ValueError):
            self.models_mod.resolve_session_override({"model_preset": "MissingPreset"})
        with self.assertRaises(ValueError):
            self.models_mod.resolve_session_override({"reasoning_effort": "ultra"})
        self.assertIsNone(self.models_mod.resolve_session_override({}))
        ov = self.models_mod.resolve_session_override({"model_preset": "Deep200K"})
        self.assertEqual(ov["preset"], "Deep200K")
        self.assertEqual(ov["model"], "deep-200k")
        self.assertEqual(ov["context_length"], 200_000)
        self.assertEqual(ov["reasoning_effort"], "high")
        self.assertTrue(ov["vision"])

        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            boot = client.get("/webui/bootstrap").json()
            headers = {"Authorization": f"Bearer {boot['api_token']}"}
            with client.websocket_connect(f"/ws?token={boot['token']}") as ws:
                _ = ws.receive_json()

                # Main chat ALSO allows set_model!
                res_main = _ws_mutation(
                    ws, "companion.chats.change",
                    {"id": main_chat, "action": "set_model", "model_preset": "Deep200K", "reasoning_effort": "medium"},
                    "m-main",
                )
                self.assertTrue(res_main["ok"], res_main)
                self.assertEqual(res_main["result"]["model_preset"], "Deep200K")
                self.assertEqual(res_main["result"]["reasoning_effort"], "medium")
                self.assertEqual(self.engine.get_actor(main_chat).context_window, 200_000)

                # Side chat switched to Fast32K -> actor.context_window immediately 32_000
                res_side = _ws_mutation(
                    ws, "companion.chats.change",
                    {"id": side_chat, "action": "set_model", "model_preset": "Fast32K", "reasoning_effort": "low"},
                    "m-side",
                )
                self.assertTrue(res_side["ok"], res_side)
                self.assertEqual(self.engine.get_actor(side_chat).context_window, 32_000)

                # Invalid preset / effort -> 400
                bad_p = _ws_mutation(ws, "companion.chats.change", {"id": side_chat, "action": "set_model", "model_preset": "NoSuch"}, "m-bad-p")
                self.assertFalse(bad_p["ok"])
                self.assertEqual(bad_p["error"]["status"], 400)
                bad_e = _ws_mutation(ws, "companion.chats.change", {"id": side_chat, "action": "set_model", "reasoning_effort": "invalid"}, "m-bad-e")
                self.assertFalse(bad_e["ok"])
                self.assertEqual(bad_e["error"]["status"], 400)

                # Verify sessions list exposes model_preset and reasoning_effort
                sessions_list = client.get("/api/sessions", headers=headers).json()["sessions"]
                side_row = next(s for s in sessions_list if s["key"] == f"websocket:{side_chat}")
                self.assertEqual(side_row["model_preset"], "Fast32K")
                self.assertEqual(side_row["reasoning_effort"], "low")

                # Fork inherits model_preset & reasoning_effort
                self.adapter.store.append(side_chat, {"event": "user_message", "chat_id": side_chat, "turn_id": "t1", "text": "hello"})
                forked = _ws_mutation(ws, "companion.chats.change", {"id": side_chat, "action": "fork"}, "m-fork")
                self.assertTrue(forked["ok"])
                forked_id = forked["result"]["chat_id"]
                forked_row = next(s for s in forked["result"]["sessions"] if s["key"] == f"websocket:{forked_id}")
                self.assertEqual(forked_row["model_preset"], "Fast32K")
                self.assertEqual(forked_row["reasoning_effort"], "low")

                # Simulate restart: drop in-memory actor, recreate via get_actor -> restores override & context_window!
                self.engine._actors.pop(side_chat, None)
                restored_actor = self.engine.get_actor(side_chat)
                self.assertEqual(restored_actor.context_window, 32_000)
                self.assertEqual((restored_actor._model_override or {}).get("preset"), "Fast32K")

                # Submit turn -> stream_llm receives overridden model & reasoning_effort
                async def run_turn():
                    await self.engine.submit_turn(side_chat, "测试首轮", "turn-override-1")
                    await restored_actor.wait_idle(timeout=3.0)

                asyncio.run(run_turn())
                self.assertEqual(self.seen_calls[-1]["override"]["model"], "fast-32k")
                self.assertEqual(self.seen_calls[-1]["override"]["reasoning_effort"], "low")

                # Clear override -> reverts to global (None, 128_000)
                cleared = _ws_mutation(
                    ws, "companion.chats.change",
                    {"id": side_chat, "action": "set_model", "model_preset": None, "reasoning_effort": None},
                    "m-clear",
                )
                self.assertTrue(cleared["ok"])
                self.assertIsNone(cleared["result"]["model_preset"])
                self.assertIsNone(self.engine.get_actor(side_chat)._model_override)
                self.assertEqual(self.engine.get_actor(side_chat).context_window, 128_000)

                # Manual in-memory override is preserved across _refresh_actor_settings when store has none
                manual_ov = {"provider": "svc-a", "model": "manual-model", "context_length": 64_000}
                self.engine.set_session_model_override(side_chat, manual_ov)
                self.engine._refresh_actor_settings(self.engine.get_actor(side_chat))
                self.assertEqual(self.engine.get_actor(side_chat)._model_override["model"], "manual-model")
                self.assertEqual(self.engine.get_actor(side_chat).context_window, 64_000)


class PinArchiveCreationsKnowledgeUsageContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_dir("w3-full-api-")
        self._home_token = bind_muse_home(self.home)
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        self.server_mod = importlib.import_module(f"{pkg}.server")
        self.creations_mod = importlib.import_module(f"{pkg}.modules.creations")
        self.knowledge_mod = importlib.import_module(f"{pkg}.modules.knowledge")
        self.models_mod = importlib.import_module(f"{pkg}.modules.models")
        self.state_mod = importlib.import_module(f"{pkg}.modules.companion_state")

        webui_dir = self.home / "companion"
        webui_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = SimpleNamespace(
            tokens=auth_mod.TokenIssuer("w3-secret"),
            devices=devices_mod.Devices(webui_dir),
            media=media_mod.MediaStore(webui_dir),
            store=chat_store(webui_dir),
            web_dist=webui_dir / "no-dist",
            socket_devices={},
            _turns={},
            deleted_chats=[],
            attach=lambda *a: None,
            detach_all=lambda *a: None,
        )
        self.adapter.delete_chat = lambda cid: self._record_delete(cid)
        self.addCleanup(self._cleanup)

    async def _record_delete(self, cid: str) -> None:
        self.adapter.deleted_chats.append(cid)

    def _cleanup(self) -> None:
        close_all_shared_dbs()
        close_all()
        reset_muse_home(self._home_token)

    def test_pin_archive_filter_and_prune_empty(self) -> None:
        main_id = self.adapter.store.create()
        self.state_mod.update_companion_state({"chat_id": main_id, "name": "伙伴"})

        c_nonempty = self.adapter.store.create()
        self.adapter.store.set_title(c_nonempty, "Python Async 笔记")
        self.adapter.store.append(c_nonempty, {"event": "user_message", "chat_id": c_nonempty, "turn_id": "t1", "text": "协程调度原理"})

        c_pinned_empty = self.adapter.store.create()
        self.adapter.store.set_title(c_pinned_empty, "置顶草稿")

        c_busy_empty = self.adapter.store.create()
        self.adapter._turns[c_busy_empty] = "active-turn"

        c_prunable_1 = self.adapter.store.create()
        c_prunable_2 = self.adapter.store.create()
        c_helper = self.adapter.store.create()
        self.adapter.store.update_meta(c_helper, helper="helper-1")

        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            boot = client.get("/webui/bootstrap").json()
            headers = {"Authorization": f"Bearer {boot['api_token']}"}
            with client.websocket_connect(f"/ws?token={boot['token']}") as ws:
                _ = ws.receive_json()

                # Pin c_pinned_empty & c_nonempty
                p1 = _ws_mutation(ws, "companion.chats.change", {"id": c_pinned_empty, "action": "pin", "pinned": True}, "pin1")
                self.assertTrue(p1["ok"])
                time.sleep(0.01)
                p2 = _ws_mutation(ws, "companion.chats.change", {"id": c_nonempty, "action": "pin", "pinned": True}, "pin2")
                self.assertTrue(p2["ok"])

                # Main chat cannot be archived -> 409
                arch_main = _ws_mutation(ws, "companion.chats.change", {"id": main_id, "action": "archive", "archived": True}, "arch-main")
                self.assertFalse(arch_main["ok"])
                self.assertEqual(arch_main["error"]["status"], 409)

                # Archive c_nonempty
                arch_ok = _ws_mutation(ws, "companion.chats.change", {"id": c_nonempty, "action": "archive", "archived": True}, "arch-ok")
                self.assertTrue(arch_ok["ok"])
                all_sessions = client.get("/api/sessions", headers=headers).json()["sessions"]
                self.assertTrue(any(s["key"] == f"websocket:{c_nonempty}" and s["archived"] for s in all_sessions))

                # ?include_archived=0 filters archived; ?q= filters case-insensitively
                no_arch = client.get("/api/sessions?include_archived=0", headers=headers).json()["sessions"]
                self.assertFalse(any(s["key"] == f"websocket:{c_nonempty}" for s in no_arch))
                q_hits = client.get("/api/sessions?q=async", headers=headers).json()["sessions"]
                self.assertEqual([s["key"] for s in q_hits], [f"websocket:{c_nonempty}"])

                # Unarchive & check pinned sorting (c_nonempty pinned later -> appears first)
                _ws_mutation(ws, "companion.chats.change", {"id": c_nonempty, "action": "archive", "archived": False}, "unarch")
                listed = client.get("/api/sessions", headers=headers).json()["sessions"]
                self.assertTrue(listed[0]["pinned"] and listed[1]["pinned"])
                self.assertEqual(listed[0]["key"], f"websocket:{c_nonempty}")

                # Prune empty side chats (protects main, pinned, busy, helper, non-empty)
                pruned = _ws_mutation(ws, "companion.chats.change", {"action": "prune_empty"}, "prune")
                self.assertTrue(pruned["ok"], pruned)
                self.assertEqual(pruned["result"]["removed_count"], 2)
                self.assertEqual(set(self.adapter.deleted_chats), {c_prunable_1, c_prunable_2})
                remaining_keys = {s["key"] for s in pruned["result"]["sessions"]}
                self.assertIn(f"websocket:{main_id}", remaining_keys)
                self.assertIn(f"websocket:{c_pinned_empty}", remaining_keys)
                self.assertIn(f"websocket:{c_busy_empty}", remaining_keys)
                self.assertIn(f"websocket:{c_nonempty}", remaining_keys)
                self.assertIn(f"websocket:{c_helper}", remaining_keys)
                self.assertNotIn(f"websocket:{c_prunable_1}", remaining_keys)
                self.assertNotIn(f"websocket:{c_prunable_2}", remaining_keys)

    def test_creations_inplace_update_versions_and_knowledge_read_replace(self) -> None:
        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            ws_token = client.get("/webui/bootstrap").json()["token"]
            with client.websocket_connect(f"/ws?token={ws_token}") as ws:
                _ = ws.receive_json()

                # 1) Create artifact -> saved_id returned, updated=False
                c1 = _ws_mutation(
                    ws, "companion.creations.save",
                    {"title": "架构说明", "content": "# V1\n\n第一版专属关键词AlphaVersion。", "format": "md", "prompt": "初版"},
                    "cr-1",
                )
                self.assertTrue(c1["ok"], c1)
                saved_id = c1["result"]["saved_id"]
                self.assertFalse(c1["result"]["updated"])

                # 2) Import into knowledge base and read full chunks
                imp1 = _ws_mutation(
                    ws, "companion.knowledge.change",
                    {"action": "import_creation", "creation_id": saved_id, "collection": "架构"},
                    "kb-imp-1",
                )
                self.assertTrue(imp1["ok"], imp1)
                doc_id_1 = imp1["result"]["added"]["id"]

                read_res = _ws_mutation(
                    ws, "companion.knowledge.change",
                    {"action": "read", "id": doc_id_1},
                    "kb-read-1",
                )
                self.assertTrue(read_res["ok"], read_res)
                self.assertEqual(read_res["result"]["document"]["id"], doc_id_1)
                self.assertEqual(len(read_res["result"]["chunks"]), 1)
                self.assertIn("AlphaVersion", read_res["result"]["chunks"][0]["content"])

                # 3) In-place update creation 12 times -> keeps same id, updated=True, caps .history at 10 backups!
                for ver in range(2, 14):
                    upd = _ws_mutation(
                        ws, "companion.creations.save",
                        {"id": saved_id, "title": "架构说明", "content": f"# V{ver}\n\n新版关键词BetaVersion{ver}。", "format": "md"},
                        f"cr-upd-{ver}",
                    )
                    self.assertTrue(upd["ok"], upd)
                    self.assertEqual(upd["result"]["saved_id"], saved_id)
                    self.assertTrue(upd["result"]["updated"])

                root = self.creations_mod.creations_root()
                idx_entries = self.creations_mod._read_index(root)["entries"][saved_id]
                self.assertEqual(len(idx_entries["versions"]), 10)
                self.assertTrue(idx_entries.get("updated_at"))
                history_files = list((root / ".history").iterdir())
                self.assertEqual(len(history_files), 10)

                # Bad creation id -> 404 / 400
                bad_upd = _ws_mutation(
                    ws, "companion.creations.save",
                    {"id": "../evil.md", "title": "x", "content": "x", "format": "md"},
                    "cr-bad",
                )
                self.assertFalse(bad_upd["ok"])
                self.assertEqual(bad_upd["error"]["status"], 404)

                # 4) Re-import updated creation into same collection -> replaces stale AlphaVersion chunks cleanly!
                imp2 = _ws_mutation(
                    ws, "companion.knowledge.change",
                    {"action": "import_creation", "creation_id": saved_id, "collection": "架构"},
                    "kb-imp-2",
                )
                self.assertTrue(imp2["ok"], imp2)
                self.assertEqual(self.knowledge_mod.search_knowledge("AlphaVersion", collection="架构"), [])
                beta_hits = self.knowledge_mod.search_knowledge("BetaVersion13", collection="架构")
                self.assertEqual(len(beta_hits), 1)
                self.assertEqual(self.knowledge_mod.list_knowledge(collection="架构")["stats"]["doc_count"], 1)

                # Read missing doc -> 404
                miss_read = _ws_mutation(ws, "companion.knowledge.change", {"action": "read", "id": "missing-id"}, "kb-read-404")
                self.assertFalse(miss_read["ok"])
                self.assertEqual(miss_read["error"]["status"], 404)

    def test_usage_14d_daily_cache_savings_and_session_context_endpoint(self) -> None:
        # Seed models with pricing + 32K preset
        mdata = self.models_mod.read_models()
        mdata["presets"] = {
            "Qwen32K": {
                "provider": "svc-q",
                "model": "qwen-max",
                "reasoning_effort": "medium",
                "context_window_tokens": 32_000,
            },
        }
        mdata["default"] = "Qwen32K"
        mdata["router"]["models"] = {
            "Qwen32K": {"description": "", "tier": "balanced", "vision": False, "price_in": 2.0, "price_out": 6.0, "price_cached": 0.2},
        }
        mdata["media"]["image"]["models"] = {
            "Flux": {
                "vendor": "custom",
                "backend": "openai",
                "provider": "svc-q",
                "model": "flux",
                "price": {"unit": "image", "rows": [{"spec": "1024x1024", "price": 0.04}]},
            },
        }
        mdata["media"]["image"]["active"] = "Flux"
        self.models_mod._write_json(self.models_mod._models_path(), mdata)

        # Create MuseDB usage across today and 3 days ago (plus edge case cached > input to verify fresh >= 0)
        db = MuseDB(self.home / "muse.db")
        try:
            chat_id = self.adapter.store.create()
            self.adapter.store.set_model_override(chat_id, model_preset="Qwen32K", reasoning_effort="medium")
            sid_today = db.get_or_create_active_session(chat_id, source="companion", model="qwen-max")["id"]
            now = time.time()
            db.record_usage(
                sid_today, "qwen-max", source="companion", chat_id=chat_id,
                input_tokens=400_000, cache_read_tokens=600_000, output_tokens=100_000,
                requests=3, seen_at=now,
            )
            db.record_usage(
                "old-sid", "qwen-max", source="companion", chat_id="other",
                input_tokens=100_000, cache_read_tokens=200_000, output_tokens=50_000,
                requests=2, seen_at=now - 3 * 86400,
            )
            db.append_message(sid_today, chat_id, "user", "请详细分析这段 Python 代码的执行流程。", turn_id="t1")
            db.append_message(sid_today, chat_id, "assistant", "好的，这段代码分为三个阶段执行。", turn_id="t1")
            db.update_session(sid_today, summary_checkpoint="前面的总结内容")
        finally:
            db.close()

        self.models_mod.record_media("image", count=2)
        self.state_mod.update_companion_state({"timezone": "Asia/Shanghai"})

        app = self.server_mod.build_app(self.adapter)
        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            # 1) Usage endpoint
            self.assertEqual(client.get("/api/webui/companion/usage").status_code, 401)
            headers = {"Authorization": f"Bearer {client.get('/webui/bootstrap').json()['api_token']}"}
            usage_resp = client.get("/api/webui/companion/usage", headers=headers)
            self.assertEqual(usage_resp.status_code, 200)
            usage = usage_resp.json()
            self.assertIn("today", usage)
            self.assertIn("week", usage)
            self.assertEqual(len(usage["daily"]), 14)
            self.assertEqual(usage["daily"], sorted(usage["daily"], key=lambda d: d["date"]))
            self.assertGreater(usage["cache_hit_rate"], 0.5)
            self.assertLessEqual(usage["cache_hit_rate"], 1.0)
            self.assertGreater(usage["cache_saved_cost"], 1.0)
            self.assertEqual(usage["daily"][-1]["requests"], 3)
            self.assertGreater(usage["daily"][-1]["media_cost"], 0.0)
            self.assertGreaterEqual(usage["daily"][-1]["cost"], usage["daily"][-1]["media_cost"])

            # 2) Session context endpoint (auth, 404, 200)
            self.assertEqual(client.get(f"/api/sessions/websocket:{chat_id}/context").status_code, 401)
            self.assertEqual(client.get("/api/sessions/websocket:missing-chat/context", headers=headers).status_code, 404)
            ctx_resp = client.get(f"/api/sessions/websocket:{chat_id}/context", headers=headers)
            self.assertEqual(ctx_resp.status_code, 200)
            ctx = ctx_resp.json()
            self.assertEqual(ctx["chat_id"], chat_id)
            self.assertEqual(ctx["context_window"], 32_000)
            self.assertEqual(ctx["tier1_threshold"], 40_000)
            self.assertEqual(ctx["tier2_threshold"], 24_000)
            self.assertTrue(ctx["has_summary_checkpoint"])
            self.assertEqual(ctx["model"], "qwen-max")
            self.assertEqual(ctx["model_preset"], "Qwen32K")
            self.assertEqual(ctx["reasoning_effort"], "medium")
            self.assertGreater(ctx["estimated_tokens"], 0)
            self.assertGreater(ctx["usage_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
