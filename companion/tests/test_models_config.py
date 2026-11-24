"""Model presets, Chinese provider keys, health-check protocol fallbacks, SQLite usage, atomic writes, and WS mutation resilience."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from starlette.testclient import TestClient

from _loader import chat_store, load, temp_dir
from test_auth_devices import _companion_pkg


class ModelsConfigAndHealthTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.models, self.fs_atomic, self.goals) = load("models", "fs_atomic", "goals")

    def test_chinese_and_mixed_preset_names_produce_unique_provider_keys(self):
        names = ["通义千问", "火山豆包", "模型", "智谱清言", "GPT-4o 主力", "GPT-4o 备用", "DeepSeek V3"]
        keys = [self.models._provider_key(name) for name in names]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertTrue(key.startswith("m-"))
            self.assertGreater(len(key), 2)
        self.assertEqual(self.models._provider_key("DeepSeek V3"), "m-deepseek-v3")
        self.assertEqual(self.models._provider_key("Claude Sonnet 4.6"), "m-claude-sonnet-4-6")
        self.assertEqual(self.models._provider_key("GPT-5.4"), "m-gpt-5-4")
        self.assertEqual(self.models._provider_key("Gemini 2.5 Pro"), "m-gemini-2-5-pro")
        # Deterministic across calls.
        self.assertEqual(self.models._provider_key("通义千问"), self.models._provider_key("通义千问"))

        services = {"svc-qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY"}}
        data = self.models.read_models()
        data["presets"] = {
            "通义千问": {"provider": "svc-qwen", "model": "qwen-max", "max_tokens": 4096, "temperature": 0.2},
            "火山豆包": {"provider": "svc-qwen", "model": "doubao-pro", "max_tokens": 8192, "temperature": 0.5},
        }
        data["default"] = "通义千问"
        data["fallbacks"] = ["火山豆包"]
        wanted = self.models.hermes_settings(data, services)
        self.assertEqual(len(wanted["providers"]), 2)
        self.assertEqual({entry["model"] for entry in wanted["providers"].values()}, {"qwen-max", "doubao-pro"})
        self.assertEqual(len(wanted["fallback_providers"]), 1)

    def test_check_models_supports_openai_reasoning_fallback_and_anthropic_messages(self):
        services = {
            "svc-openai": {"base_url": "https://api.openai.com/v1", "api_key": "sk-oai"},
            "svc-claude": {"base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant"},
        }
        self.models._service = lambda provider: services[provider]
        self.models._services = lambda: services
        data = self.models.read_models()
        data["presets"] = {
            "OpenAI o3": {"provider": "svc-openai", "model": "o3-mini"},
            "Claude Sonnet": {"provider": "svc-claude", "model": "claude-sonnet-4-20250514"},
        }
        self.models._write_json(self.models._models_path(), data)

        calls: list[tuple[str, dict, dict]] = []

        async def fake_post(client_self, url, *, headers=None, json=None, **kw):
            calls.append((url, dict(headers or {}), dict(json or {})))
            req = httpx.Request("POST", url)
            if "api.openai.com" in url and "max_tokens" in (json or {}):
                return httpx.Response(
                    400,
                    json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}},
                    request=req,
                )
            return httpx.Response(200, json={"id": "ok"}, request=req)

        with mock.patch.object(httpx.AsyncClient, "post", fake_post):
            payload = asyncio.run(self.models.check_models(["OpenAI o3", "Claude Sonnet"]))

        health = payload["health"]
        self.assertTrue(health["OpenAI o3"]["ok"])
        self.assertTrue(health["Claude Sonnet"]["ok"])

        # OpenAI retried with max_completion_tokens; Anthropic hit /v1/messages with x-api-key + anthropic-version.
        oai_calls = [c for c in calls if "api.openai.com" in c[0]]
        self.assertEqual(len(oai_calls), 2)
        self.assertIn("max_tokens", oai_calls[0][2])
        self.assertEqual(oai_calls[1][2].get("max_completion_tokens"), 16)

        ant_calls = [c for c in calls if "api.anthropic.com" in c[0]]
        self.assertEqual(len(ant_calls), 1)
        self.assertEqual(ant_calls[0][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(ant_calls[0][1].get("x-api-key"), "sk-ant")
        self.assertEqual(ant_calls[0][1].get("anthropic-version"), "2023-06-01")

    def test_usage_summary_and_goals_handle_missing_state_db_and_close_sqlite_handles(self):
        from muse.paths import muse_home_override

        home_with_space = temp_dir("companion home 中文 ")
        with muse_home_override(home_with_space):
            # 1) Missing state.db on fresh install -> returns empty today/week without raising.
            empty = self.models.usage_summary("Asia/Shanghai")
            self.assertEqual(empty["today"]["by_model"], [])
            self.assertEqual(empty["week"]["by_model"], [])
            self.assertIsNone(self.goals._session_id("main"))

            # 2) Existing state.db in path with spaces & CJK -> read-only URI works and closes handle cleanly.
            db_path = home_with_space / "state.db"
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, started_at REAL)")
                conn.execute(
                    "CREATE TABLE session_model_usage ("
                    "session_id TEXT, model TEXT, billing_base_url TEXT, task TEXT, api_call_count INT, "
                    "input_tokens INT, cache_read_tokens INT, cache_write_tokens INT, output_tokens INT, last_seen REAL)"
                )
                now = time.time()
                conn.execute("INSERT INTO sessions VALUES ('s1', 'companion', 'main', ?)", (now,))
                conn.execute(
                    "INSERT INTO session_model_usage VALUES ('s1', 'deepseek-chat', 'https://api.deepseek.com', '', 2, 1000, 200, 0, 300, ?)",
                    (now,),
                )
                conn.commit()

            summary = self.models.usage_summary("Asia/Shanghai")
            self.assertEqual(len(summary["today"]["by_model"]), 1)
            self.assertEqual(summary["today"]["by_model"][0]["calls"], 2)
            self.assertEqual(self.goals._session_id("main"), "s1")

            # On Windows, unlinking an open SQLite file fails with PermissionError; succeeding proves handle was closed!
            os.unlink(db_path)
            self.assertFalse(db_path.exists())

    def test_fs_atomic_retries_permission_error_and_leaves_no_tmp_files(self):
        target = self.folder / "nested" / "state.json"
        real_replace = os.replace
        attempts = {"count": 0}

        def flaky_replace(src, dst):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise PermissionError("simulated Windows lock")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky_replace):
            self.fs_atomic.atomic_write_json(target, {"ok": True}, sort_keys=True, trailing_newline=True)

        self.assertEqual(attempts["count"], 3)
        self.assertTrue(target.is_file())
        self.assertEqual(list(target.parent.glob("*.tmp")), [])


class MutationResilienceTest(unittest.TestCase):
    def test_unexpected_mutation_error_returns_500_without_dropping_websocket(self):
        folder = temp_dir("companion-mut-")
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        server_mod = importlib.import_module(f"{pkg}.server")
        store = chat_store(folder)

        class StubAdapter:
            def __init__(self):
                self.tokens = auth_mod.TokenIssuer("s")
                self.devices = devices_mod.Devices(folder)
                self.media = media_mod.MediaStore(folder)
                self.store = store
                self.web_dist = folder / "no-dist"
                self.socket_devices = {}
                self._turns = {}

            def attach(self, chat_id, socket):
                pass

            def detach_all(self, socket):
                pass

        adapter = StubAdapter()
        app = server_mod.build_app(adapter)

        async def boom(_adapter, _payload):
            raise RuntimeError("disk glitch")

        tok = adapter.tokens.issue("local", purpose="ws")
        with mock.patch.dict(server_mod.MUTATIONS, {"test.boom": boom}):
            with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
                with client.websocket_connect(f"ws://127.0.0.1:8766/ws?token={tok}") as ws:
                    self.assertEqual(ws.receive_json()["event"], "ready")
                    ws.send_json({"type": "webui_request", "request_id": "r1", "action": "test.boom", "payload": {}})
                    err = ws.receive_json()
                    self.assertEqual(err["event"], "webui_response")
                    self.assertEqual(err["request_id"], "r1")
                    self.assertFalse(err["ok"])
                    self.assertEqual(err["error"]["status"], 500)

                    # Socket stays alive for subsequent requests!
                    ws.send_json({"type": "new_chat"})
                    attached = ws.receive_json()
                    self.assertEqual(attached["event"], "attached")


if __name__ == "__main__":
    unittest.main()
