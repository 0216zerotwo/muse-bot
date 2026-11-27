"""Unit tests for Wave 1 Intel modules: FTS5 chat_search v2, neural TTS API, and contextual follow-ups."""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _loader import chat_store, load, temp_dir
from muse.db import MuseDB, close_all
from muse.paths import muse_home_override
from muse.runtime import close_all_shared_dbs


class ChatSearchV2Test(unittest.TestCase):
    def setUp(self) -> None:
        _, (self.search_mod,) = load("chat_search")
        self.folder = temp_dir("companion-search-v2-")
        self.store = chat_store(self.folder)
        self.main = self.store.create()
        self.side = self.store.create()
        self.helper = self.store.create()
        self.desk = self.store.create()
        self.cron = "cron:daily-brief"
        self.store.ensure(self.cron)
        self.store.set_title(self.side, "架构评审")
        idx = self.store._read_index()
        idx[self.helper]["helper"] = "研究员"
        idx[self.desk]["desk"] = True
        self.store._write_index(idx)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        close_all_shared_dbs()
        close_all()

    def _add(self, cid: str, event: str, text: str, at_ms: int, turn_id: str | None = None, **extra) -> None:
        self.store.append(
            cid,
            {
                "event": event,
                "chat_id": cid,
                "text": text,
                "turn_id": turn_id or f"t-{at_ms}",
                "created_at_ms": at_ms,
                **extra,
            },
        )

    def test_fts5_and_chatstore_merge_dedup_filters_and_match_offsets(self) -> None:
        home = temp_dir("companion-search-home-")
        with muse_home_override(home):
            db = MuseDB(home / "muse.db")
            try:
                s_main = db.get_or_create_active_session(self.main, source="companion")["id"]
                s_side = db.get_or_create_active_session(self.side, source="companion")["id"]
                s_helper = db.get_or_create_active_session(self.helper, source="companion")["id"]
                s_desk = db.get_or_create_active_session(self.desk, source="companion")["id"]
                s_cron = db.get_or_create_active_session(self.cron, source="cron")["id"]

                # Overlapping message in BOTH MuseDB and ChatStore -> must deduplicate by (chat_id, turn_id, role)
                db.append_message(
                    s_main, self.main, "user", "Perplexity 风格引用与 perplexity 检索对比", turn_id="t1",
                )
                self._add(self.main, "user_message", "Perplexity 风格引用与 perplexity 检索对比", 1000, turn_id="t1")

                # Message ONLY in MuseDB (tests FTS5 trigram + CJK LIKE path directly)
                db.append_message(
                    s_side, self.side, "assistant", "关于猫的饮食和架构设计，建议用 Perplexity 风格引用。", turn_id="t2",
                )

                # Message ONLY in ChatStore (tests pure ChatStore fallback)
                self._add(self.main, "message", "已记录：使用 Perplexity 风格引用，还有猫。", 3000, turn_id="t3")

                # Helper, desk, and cron messages
                db.append_message(s_helper, self.helper, "assistant", "帮手整理的 Perplexity 调研笔记", turn_id="t4")
                self._add(self.helper, "message", "帮手整理的 Perplexity 调研笔记", 4000, turn_id="t4")
                db.append_message(s_desk, self.desk, "assistant", "desk hidden Perplexity", turn_id="t5")
                self._add(self.desk, "message", "desk hidden Perplexity", 5000, turn_id="t5")
                db.append_message(s_cron, self.cron, "assistant", "cron hidden Perplexity", turn_id="t6")
                self._add(self.cron, "message", "cron hidden Perplexity", 6000, turn_id="t6")

                # Card markup inside MuseDB must not false-match
                db.append_message(s_main, self.main, "assistant", "看卡片\n```ui\n<div>SecretTokenXYZ</div>\n```", turn_id="t7")

                # 1) Default scope="all": includes main + side (3 unique hits), excludes helper/desk/cron, zero dupes!
                res_all = self.search_mod.search(self.store, "perplexity", self.main, db=db)
                self.assertEqual(len(res_all["hits"]), 3)
                hit_keys = [(h["chat_id"], h["turn_id"], h["role"]) for h in res_all["hits"]]
                self.assertEqual(len(hit_keys), len(set(hit_keys)))
                self.assertFalse(any(h["chat_id"] in (self.helper, self.desk, self.cron) for h in res_all["hits"]))

                # Verify full backward-compatible + v2 fields and multi-match offsets
                t1_hit = next(h for h in res_all["hits"] if h["turn_id"] == "t1")
                self.assertEqual(t1_hit["key"], f"websocket:{self.main}")
                self.assertEqual(t1_hit["title"], "主要聊天")
                self.assertEqual(t1_hit["chat_title"], "主要聊天")
                self.assertTrue(t1_hit["main"])
                self.assertEqual(t1_hit["helper"], "")
                self.assertEqual(t1_hit["snippet"], t1_hit["text"])
                self.assertEqual(t1_hit["created_at_ms"], t1_hit["at_ms"])
                self.assertEqual(len(t1_hit["matches"]), 2)
                for start, end in t1_hit["matches"]:
                    self.assertEqual(t1_hit["snippet"][start:end].lower(), "perplexity")

                # 2) Scope filtering: main / sides / helpers
                res_main = self.search_mod.search(self.store, "perplexity", self.main, scope="main", db=db)
                self.assertTrue(all(h["chat_id"] == self.main for h in res_main["hits"]))
                self.assertEqual(len(res_main["hits"]), 2)

                res_sides = self.search_mod.search(self.store, "perplexity", self.main, scope="sides", db=db)
                self.assertEqual([(h["chat_id"], h["title"]) for h in res_sides["hits"]], [(self.side, "架构评审")])

                res_helpers = self.search_mod.search(self.store, "perplexity", self.main, scope="helpers", db=db)
                self.assertEqual(len(res_helpers["hits"]), 1)
                self.assertEqual(res_helpers["hits"][0]["helper"], "研究员")

                # 3) Single-chat filter (including websocket: prefix) & role filter ("user" / "reply")
                res_chat = self.search_mod.search(
                    self.store, "perplexity", self.main, chat_id=f"websocket:{self.main}", role="reply", db=db,
                )
                self.assertEqual([(h["turn_id"], h["role"]) for h in res_chat["hits"]], [("t3", "assistant")])

                res_user = self.search_mod.search(self.store, "perplexity", self.main, role="user", db=db)
                self.assertEqual([(h["turn_id"], h["role"]) for h in res_user["hits"]], [("t1", "user")])

                # 4) 1-char CJK LIKE query via MuseDB + ChatStore
                res_cjk = self.search_mod.search(self.store, "猫", self.main, db=db)
                self.assertEqual({h["turn_id"] for h in res_cjk["hits"]}, {"t2", "t3"})
                for h in res_cjk["hits"]:
                    self.assertTrue(h["matches"])
                    s, e = h["matches"][0]
                    self.assertEqual(h["snippet"][s:e], "猫")

                # 5) Card UI markup excluded even from MuseDB hits
                self.assertEqual(self.search_mod.search(self.store, "SecretTokenXYZ", self.main, db=db)["hits"], [])

                # 6) Limit + more flag
                res_lim = self.search_mod.search(self.store, "perplexity", self.main, limit=1, db=db)
                self.assertEqual(len(res_lim["hits"]), 1)
                self.assertTrue(res_lim["more"])
            finally:
                db.close()


class TtsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        _, (self.tts_api,) = load("tts_api")

    def test_clean_text_for_speech_strips_cards_urls_code_and_caps_4000(self) -> None:
        raw = (
            "## 今日总结 [1]\n"
            "请看 [官方文档](https://example.com/docs?token=abc) 和 https://bare.example.com/path。\n"
            "```card\n{\"type\": \"buttons\", \"items\": []}\n```\n"
            "```ui\n<div>hidden</div>\n```\n"
            "```python\n"
            "def compute(x):\n"
            "    total = 0\n"
            "    for i in range(x):\n"
            "        total += i\n"
            "    return total\n"
            "```\n"
            "MEDIA:/tmp/chart.png\n"
            "**重点结论**：一切正常。\n"
        )
        cleaned = self.tts_api.clean_text_for_speech(raw)
        self.assertIn("今日总结", cleaned)
        self.assertIn("官方文档", cleaned)
        self.assertIn("请查看屏幕", cleaned)
        self.assertIn("重点结论：一切正常。", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("hidden", cleaned)
        self.assertNotIn("MEDIA:", cleaned)
        self.assertNotIn("[1]", cleaned)
        self.assertNotIn("def compute", cleaned)

        self.assertEqual(self.tts_api.clean_text_for_speech("```ui\n<div>only card</div>\n```"), "")
        long_clean = self.tts_api.clean_text_for_speech("这是一段测试文字。" * 1000)
        self.assertLessEqual(len(long_clean), 4000)

    def test_tts_available_and_synthesize_caching_and_errors(self) -> None:
        from companion.media import MediaStore, child_file

        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            adapter = SimpleNamespace(media=MediaStore(Path(tmp) / "companion"))

            # Empty / card-only text -> 400
            with self.assertRaises(self.tts_api.TtsError) as cm:
                self.tts_api.synthesize(adapter, {"text": "```card\n{}\n```"})
            self.assertEqual(cm.exception.status, 400)

            # tts_available checks edge_tts or OpenAI key
            with (
                mock.patch("muse.media.tts._has_edge_tts", return_value=False),
                mock.patch("muse.media._common.get_secret", return_value=""),
                mock.patch("muse.media._common.load_media_section", return_value={}),
            ):
                self.assertFalse(self.tts_api.tts_available())
            with (
                mock.patch("muse.media.tts._has_edge_tts", return_value=True),
                mock.patch("muse.media._common.load_media_section", return_value={}),
            ):
                self.assertTrue(self.tts_api.tts_available())

            calls: list[dict] = []

            def fake_tts(text: str, *, voice=None, model=None, speed=None, output_path=None, provider=None):
                calls.append({"text": text, "voice": voice, "provider": provider})
                Path(output_path).write_bytes(b"ID3-FAKE-MP3-AUDIO-DATA")
                return {"success": True, "file_path": output_path, "provider": provider or "edge"}

            with mock.patch("muse.media.tts.text_to_speech", side_effect=fake_tts):
                first = self.tts_api.synthesize(
                    adapter, {"text": "**你好**，这是一条朗读测试。", "provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"},
                )
                self.assertTrue(first["ok"])
                self.assertFalse(first["cached"])
                self.assertEqual(first["provider"], "edge")
                self.assertTrue(first["url"].startswith(self.tts_api.TTS_PREFIX + "/"))
                self.assertEqual(len(calls), 1)
                self.assertFalse(adapter.media.root.exists())

                # Cache file created under <MUSE_HOME>/cache/audio/tts_<hash>.mp3
                audio_files = list((Path(tmp) / "cache" / "audio").glob("tts_*.mp3"))
                self.assertEqual(len(audio_files), 1)
                self.assertEqual(audio_files[0].read_bytes(), b"ID3-FAKE-MP3-AUDIO-DATA")

                # Signed URL verifies under TTS_PREFIX and resolves inside audio_cache_dir() (zero-copy)
                sig, payload = first["url"].rsplit("/", 2)[-2:]
                rel = adapter.media.verify(self.tts_api.TTS_PREFIX, sig, payload)
                self.assertIsNotNone(rel)
                resolved = child_file(self.tts_api.audio_cache_dir(), rel)
                self.assertIsNotNone(resolved)
                self.assertEqual(Path(resolved).read_bytes(), b"ID3-FAKE-MP3-AUDIO-DATA")

                # Second call with same (provider, voice, cleaned text) hits cache without calling text_to_speech
                second = self.tts_api.synthesize(
                    adapter, {"text": "你好，这是一条朗读测试。", "provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"},
                )
                self.assertTrue(second["ok"])
                self.assertTrue(second["cached"])
                self.assertEqual(len(calls), 1)

            # Failure raises TtsError(503) with Chinese message
            with mock.patch(
                "muse.media.tts.text_to_speech",
                return_value={"success": False, "error": "edge-tts package is not installed"},
            ):
                with self.assertRaises(self.tts_api.TtsError) as fail_cm:
                    self.tts_api.synthesize(adapter, {"text": "全新的句子", "provider": "edge"})
                self.assertEqual(fail_cm.exception.status, 503)
                self.assertIn("语音朗读", fail_cm.exception.message)


class FollowupsTest(unittest.TestCase):
    def setUp(self) -> None:
        _, (self.followups,) = load("followups")
        self.followups.clear_cache()
        self.store = chat_store(temp_dir("companion-followups-"))
        self.chat_id = self.store.create()

    def tearDown(self) -> None:
        self.followups.clear_cache()

    def test_empty_chat_and_heuristic_categories(self) -> None:
        empty = asyncio.run(self.followups.get_followups(self.store, self.chat_id))
        self.assertEqual(empty, {"chat_id": self.chat_id, "turn_id": "", "suggestions": []})

        code_sugs = self.followups.heuristic_followups("帮我写排序", "```python\ndef sort_items(a):\n    return sorted(a)\n```")
        self.assertEqual(len(code_sugs), 3)
        self.assertTrue(any("测试" in s or "代码" in s for s in code_sugs))

        cite_sugs = self.followups.heuristic_followups(
            "调研一下方案",
            "研究报告结论如下 [1]。",
            sources=[{"id": 1, "title": "Paper", "url": "https://example.com"}],
        )
        self.assertEqual(len(cite_sugs), 3)
        self.assertTrue(any("来源" in s or "摘要" in s for s in cite_sugs))

        cmp_sugs = self.followups.heuristic_followups("对比方案 A 和方案 B 优缺点", "方案 A 更便宜，方案 B 更稳。")
        self.assertEqual(len(cmp_sugs), 3)
        self.assertTrue(any("方案" in s for s in cmp_sugs))

        step_sugs = self.followups.heuristic_followups("怎么部署？", "1. 安装依赖\n2. 初始化配置\n3. 启动服务")
        self.assertEqual(len(step_sugs), 3)
        self.assertTrue(any("第一步" in s or "清单" in s for s in step_sugs))

        sum_sugs = self.followups.heuristic_followups("帮我总结这篇文档", "以上是会议纪要核心提炼。")
        self.assertEqual(len(sum_sugs), 3)
        self.assertTrue(any("结论" in s or "口语" in s for s in sum_sugs))

    def test_quick_model_llm_timeout_fallback_and_lru_cache(self) -> None:
        self.store.append(
            self.chat_id,
            {"event": "user_message", "chat_id": self.chat_id, "text": "帮我调研 Rust 异步生态", "turn_id": "t-1"},
        )
        self.store.append(
            self.chat_id,
            {
                "event": "message",
                "chat_id": self.chat_id,
                "text": "根据调研 [1]，Tokio 是主流运行时。",
                "turn_id": "t-1",
                "sources": [{"id": 1, "title": "Tokio", "url": "https://tokio.rs"}],
            },
        )
        self.store.append(
            self.chat_id,
            {"event": "user_message", "chat_id": self.chat_id, "text": "给出一段示例代码", "turn_id": "t-2"},
        )
        self.store.append(
            self.chat_id,
            {"event": "message", "chat_id": self.chat_id, "text": "```rust\nasync fn main() {}\n```", "turn_id": "t-2"},
        )

        # 1) Configured quick_model -> calls async_call_llm with exact fast params and caches result
        captured_kw: dict = {}

        async def fake_llm(**kw):
            captured_kw.update(kw)
            msg = SimpleNamespace(content='["对比 Tokio 和 async-std", "加上超时取消示例", "整理生产最佳实践"]')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        with (
            mock.patch.object(
                self.followups,
                "_resolve_quick_model_target",
                return_value={"provider": "m-fast", "model": "gpt-4o-mini"},
            ),
            mock.patch("muse.llm.auxiliary.async_call_llm", side_effect=fake_llm) as mock_llm,
        ):
            r1 = asyncio.run(self.followups.get_followups(self.store, self.chat_id, turn_id="t-1"))
            self.assertEqual(r1["chat_id"], self.chat_id)
            self.assertEqual(r1["turn_id"], "t-1")
            self.assertEqual(r1["suggestions"], ["对比 Tokio 和 async-std", "加上超时取消示例", "整理生产最佳实践"])
            self.assertEqual(captured_kw["max_tokens"], 120)
            self.assertEqual(captured_kw["timeout"], 5)
            self.assertEqual(captured_kw["reasoning_config"], {"enabled": False})
            self.assertEqual(mock_llm.call_count, 1)

            # Second call for same (chat_id, t-1) hits LRU cache without calling LLM again
            r1_cached = asyncio.run(self.followups.get_followups(self.store, self.chat_id, turn_id="t-1"))
            self.assertEqual(r1_cached, r1)
            self.assertEqual(mock_llm.call_count, 1)

        # 2) Latest turn (t-2) with LLM timeout falls back cleanly to heuristic generator
        async def failing_llm(**_kw):
            raise TimeoutError("fast model timed out")

        with (
            mock.patch.object(
                self.followups,
                "_resolve_quick_model_target",
                return_value={"provider": "m-fast", "model": "gpt-4o-mini"},
            ),
            mock.patch("muse.llm.auxiliary.async_call_llm", side_effect=failing_llm),
        ):
            r2 = asyncio.run(self.followups.get_followups(self.store, self.chat_id))
            self.assertEqual(r2["turn_id"], "t-2")
            self.assertEqual(len(r2["suggestions"]), 3)
            self.assertTrue(any("测试" in s or "代码" in s for s in r2["suggestions"]))

        # 3) LRU cap enforcement (128 entries)
        for i in range(self.followups.CACHE_MAX_SIZE + 10):
            self.followups._cache_set((f"chat-{i}", "t"), [f"问题A{i}", f"问题B{i}", f"问题C{i}"])
        self.assertEqual(len(self.followups._CACHE), self.followups.CACHE_MAX_SIZE)
        self.assertIsNone(self.followups._cache_get(("chat-0", "t")))
        self.assertIsNotNone(self.followups._cache_get((f"chat-{self.followups.CACHE_MAX_SIZE + 9}", "t")))


class IntelHttpRoutesTest(unittest.TestCase):
    def test_search_tts_followups_and_capabilities_http_contracts(self) -> None:
        from starlette.testclient import TestClient

        from test_auth_devices import _companion_pkg

        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        server_mod = importlib.import_module(f"{pkg}.server")
        real_followups = importlib.import_module(f"{pkg}.modules.followups")

        real_followups.clear_cache()
        self.addCleanup(real_followups.clear_cache)

        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            folder = Path(tmp) / "companion-http"
            folder.mkdir(parents=True, exist_ok=True)
            web_dist = folder / "web-dist"
            web_dist.mkdir(parents=True, exist_ok=True)
            (web_dist / "companion.html").write_text("<!doctype html><title>Muse Bot</title>\n", encoding="utf-8")

            class _StubAdapter:
                def __init__(self) -> None:
                    self.tokens = auth_mod.TokenIssuer(None)
                    self.devices = devices_mod.Devices(folder)
                    self.media = media_mod.MediaStore(folder)
                    self.store = chat_store(folder)
                    self.web_dist = web_dist
                    self.socket_devices = {}
                    self._turns = {}

                def attach(self, chat_id, socket):
                    pass

                def detach_all(self, socket):
                    pass

            adapter = _StubAdapter()
            chat_id = adapter.store.create()
            adapter.store.set_title(chat_id, "研究讨论")
            adapter.store.append(
                chat_id,
                {"event": "user_message", "chat_id": chat_id, "text": "对比 SQLite FTS5 和向量检索", "turn_id": "turn-1", "created_at_ms": 1000},
            )
            adapter.store.append(
                chat_id,
                {
                    "event": "message",
                    "chat_id": chat_id,
                    "text": "SQLite FTS5 零依赖、延迟低，适合本地全文与中文 trigram 检索 [1]。",
                    "turn_id": "turn-1",
                    "created_at_ms": 2000,
                    "sources": [{"id": 1, "title": "SQLite FTS5", "url": "https://sqlite.org/fts5.html"}],
                },
            )

            app = server_mod.build_app(adapter)
            with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
                boot = client.get("/webui/bootstrap")
                self.assertEqual(boot.status_code, 200)
                boot_body = boot.json()
                self.assertIn("tts", boot_body["capabilities"])
                self.assertIsInstance(boot_body["capabilities"]["tts"], bool)
                headers = {"Authorization": f"Bearer {boot_body['api_token']}"}

                # 1) Search v2 HTTP query params + auth
                self.assertEqual(client.get("/api/webui/companion/search?q=FTS5").status_code, 401)
                s_resp = client.get(
                    f"/api/webui/companion/search?q=FTS5&chat_id={chat_id}&role=reply&scope=all&limit=5",
                    headers=headers,
                )
                self.assertEqual(s_resp.status_code, 200)
                s_body = s_resp.json()
                self.assertEqual(len(s_body["hits"]), 1)
                self.assertEqual(s_body["hits"][0]["turn_id"], "turn-1")
                self.assertTrue(s_body["hits"][0]["matches"])

                # 2) Follow-ups HTTP endpoint (auth, 404, 200)
                self.assertEqual(client.get(f"/api/sessions/websocket:{chat_id}/followups").status_code, 401)
                self.assertEqual(
                    client.get("/api/sessions/websocket:missing-chat/followups", headers=headers).status_code,
                    404,
                )
                with mock.patch.object(real_followups, "_resolve_quick_model_target", return_value=None):
                    f_resp = client.get(
                        f"/api/sessions/websocket:{chat_id}/followups?turn_id=turn-1",
                        headers=headers,
                    )
                self.assertEqual(f_resp.status_code, 200)
                f_body = f_resp.json()
                self.assertEqual(f_body["chat_id"], chat_id)
                self.assertEqual(f_body["turn_id"], "turn-1")
                self.assertEqual(len(f_body["suggestions"]), 3)

                # 3) TTS HTTP endpoint: origin check (403), auth (401), oversized (413), empty (400), 200 + signed media fetch
                self.assertEqual(
                    client.post(
                        "/api/webui/companion/tts",
                        json={"text": "hello"},
                        headers={**headers, "Origin": "https://evil.example.com"},
                    ).status_code,
                    403,
                )
                self.assertEqual(client.post("/api/webui/companion/tts", json={"text": "hello"}).status_code, 401)
                self.assertEqual(
                    client.post(
                        "/api/webui/companion/tts",
                        content=b"x" * (65 * 1024),
                        headers={**headers, "Content-Type": "application/json"},
                    ).status_code,
                    413,
                )
                self.assertEqual(
                    client.post("/api/webui/companion/tts", json={"text": "   "}, headers=headers).status_code,
                    400,
                )

                def fake_tts(text: str, *, voice=None, model=None, speed=None, output_path=None, provider=None):
                    Path(output_path).write_bytes(b"ID3-HTTP-TTS-MP3")
                    return {"success": True, "file_path": output_path, "provider": provider or "edge"}

                with mock.patch("muse.media.tts.text_to_speech", side_effect=fake_tts):
                    t1 = client.post(
                        "/api/webui/companion/tts",
                        json={"text": "测试神经语音朗读。", "provider": "edge"},
                        headers=headers,
                    )
                    self.assertEqual(t1.status_code, 200)
                    t1_body = t1.json()
                    self.assertTrue(t1_body["ok"])
                    self.assertFalse(t1_body["cached"])
                    media_resp = client.get(t1_body["url"])
                    self.assertEqual(media_resp.status_code, 200)
                    self.assertEqual(media_resp.content, b"ID3-HTTP-TTS-MP3")

                    t2 = client.post(
                        "/api/webui/companion/tts",
                        json={"text": "测试神经语音朗读。", "provider": "edge"},
                        headers=headers,
                    )
                    self.assertEqual(t2.status_code, 200)
                    self.assertTrue(t2.json()["cached"])


if __name__ == "__main__":
    unittest.main()
