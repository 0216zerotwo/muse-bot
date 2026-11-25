"""Comprehensive Pass-1 Slice-1 regression suite for Protocols, MCP HTTP, .env, Media/STT, and MuseDB."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from muse.config_store import invalidate_caches, save_config
from muse.db import MuseDB, close_all, sync_from_legacy_state_db
from muse.paths import bind_muse_home, reset_muse_home


def _make_jwt(payload: dict[str, Any]) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


def _sse_response(lines: list[str], status_code: int = 200) -> httpx.Response:
    raw = ("\n".join(lines) + "\n\n").encode("utf-8")
    return httpx.Response(
        status_code,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        content=raw,
    )


class Pass1ProtocolsFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="muse pass1 proto 中文 ")
        self.home = Path(self._tmp.name)
        self._home_token = bind_muse_home(self.home)
        invalidate_caches()

    def tearDown(self) -> None:
        from muse.media import catalog

        close_all()
        invalidate_caches()
        catalog.reset_deepinfra_cache()
        reset_muse_home(self._home_token)
        self._tmp.cleanup()

    def _write_dotenv_only(self, pairs: dict[str, str]) -> None:
        lines = [f"{k}={v}" for k, v in pairs.items()]
        (self.home / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        invalidate_caches()

    def test_1_streamable_http_mcp_live_server_on_mcp_v2(self) -> None:
        import uvicorn
        from mcp.server import MCPServer
        from muse.mcp.manager import AsyncMCPManager

        srv = MCPServer("pass1-http-mcp")

        @srv.tool()
        def add_and_tag(a: int, b: int) -> str:
            """Add two integers and tag result."""
            return f"sum={a + b}"

        async def _run() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            app = srv.streamable_http_app()
            cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
            server = uvicorn.Server(cfg)
            serve_task = asyncio.create_task(server.serve())
            deadline = time.monotonic() + 10.0
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError("Timed out starting local Streamable HTTP MCP server")
                await asyncio.sleep(0.02)

            mgr = AsyncMCPManager()
            url = f"http://127.0.0.1:{port}/mcp"
            server_cfg = {
                "url": url,
                "headers": {"X-Pass1-Probe": "ok"},
                "connect_timeout": 10.0,
                "read_timeout": 30.0,
            }
            try:
                probed = await mgr.probe_server("tiny_http", server_cfg)
                self.assertEqual(probed, ["add_and_tag"])

                res = await mgr.reload_mcp({"tiny_http": server_cfg})
                self.assertTrue(res["ok"], f"reload_mcp failed: {res}")
                self.assertEqual(res["added"], ["tiny_http"])

                defs = mgr.get_tool_definitions()
                self.assertEqual([d["function"]["name"] for d in defs], ["mcp__tiny_http__add_and_tag"])

                out = await mgr.call_tool("mcp__tiny_http__add_and_tag", {"a": 19, "b": 23})
                self.assertEqual(out, "sum=42")
            finally:
                await mgr.shutdown()
                server.should_exit = True
                await serve_task

        asyncio.run(_run())

    def test_2_dotenv_key_discovery_across_companion_web_and_catalog(self) -> None:
        from companion.modules import connections, models, vault
        from muse.media import catalog
        from muse.tools.web import web_search_handler

        scrub_keys = (
            "DEEPSEEK_API_KEY",
            "COMPANION_KEY_OPENROUTER",
            "OPENROUTER_API_KEY",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "COMPANION_MCP_MY_CUSTOM_TOKEN",
            "CUSTOM_VAULT_TOKEN",
            "TAVILY_API_KEY",
            "BRAVE_API_KEY",
            "DEEPINFRA_API_KEY",
        )
        clean_env = {k: v for k, v in os.environ.items() if k not in scrub_keys}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            self._write_dotenv_only(
                {
                    "DEEPSEEK_API_KEY": "sk-ds-dotenv-only",
                    "COMPANION_KEY_OPENROUTER": "sk-or-dotenv-only",
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_dotenv_only",
                    "COMPANION_MCP_MY_CUSTOM_TOKEN": "sec_custom_dotenv",
                    "CUSTOM_VAULT_TOKEN": "vault_secret_dotenv",
                    "TAVILY_API_KEY": "tvly-dotenv-only",
                    "DEEPINFRA_API_KEY": "di-dotenv-only",
                }
            )
            save_config(
                {
                    "providers": {
                        "svc-deepseek": {
                            "base_url": "https://api.deepseek.com",
                            "key_env": "DEEPSEEK_API_KEY",
                        },
                        "svc-openrouter": {
                            "base_url": "https://openrouter.ai/api/v1",
                            "key_env": "COMPANION_KEY_OPENROUTER",
                        },
                    }
                }
            )
            # 1) models.saved_key_urls()
            self.assertEqual(
                models.saved_key_urls(),
                {"https://api.deepseek.com", "https://openrouter.ai/api/v1"},
            )

            # 2) models.check_models() sends Bearer key from .env
            mdata = models.read_models()
            mdata["presets"] = {"DeepSeek": {"provider": "svc-deepseek", "model": "deepseek-chat"}}
            models._write_json(models._models_path(), mdata)
            seen_auth: list[str] = []

            async def _fake_post(client_self: Any, url: str, *, headers: Any = None, json: Any = None, **kw: Any) -> httpx.Response:
                seen_auth.append((headers or {}).get("Authorization", ""))
                return httpx.Response(200, json={"id": "ok"}, request=httpx.Request("POST", url))

            with mock.patch.object(httpx.AsyncClient, "post", _fake_post):
                checked = asyncio.run(models.check_models(["DeepSeek"]))
            self.assertTrue(checked["health"]["DeepSeek"]["ok"])
            self.assertEqual(seen_auth, ["Bearer sk-ds-dotenv-only"])

            # 3) models._share_key() copies from .env into target env var
            models._share_key("svc-openrouter", "OPENROUTER_API_KEY")
            self.assertEqual(os.environ.get("OPENROUTER_API_KEY"), "sk-or-dotenv-only")

            # 4) connections: preset env_var discovery & custom secret removal from .env
            gh_preset = next(p for p in connections.presets_payload()["presets"] if p["name"] == "github")
            self.assertTrue(any(f["configured"] for f in gh_preset["required_fields"]))
            connections.run_action("enable", "github", {})
            gh_after = next(p for p in connections.presets_payload()["presets"] if p["name"] == "github")
            self.assertNotEqual(gh_after["status"], "missing_credentials")
            self.assertTrue(gh_after["configured"])

            connections.remove_custom_secrets("my-custom")
            self.assertNotIn("COMPANION_MCP_MY_CUSTOM_TOKEN", (self.home / ".env").read_text(encoding="utf-8"))

            # 5) vault.payload() and credential_request see keys in .env
            vault._write_keys([{"env": "CUSTOM_VAULT_TOKEN", "label": "Custom", "created_at": 1}])
            v_payload = vault.payload()
            self.assertEqual(v_payload["keys"][0]["set"], True)
            cred_res = json.loads(
                vault._handle_request({"kind": "api_key", "service": "CustomSvc", "env": "CUSTOM_VAULT_TOKEN"})
            )
            self.assertTrue(cred_res.get("exists"))

            # 6) web_search_handler auto-picks Tavily from .env
            with mock.patch("muse.tools.web._search_tavily", return_value=[{"url": "https://a.example", "title": "A", "description": "B"}]) as mock_tv:
                ws_res = json.loads(web_search_handler({"query": "muse native"}))
            self.assertEqual(ws_res["backend"], "tavily")
            mock_tv.assert_called_once_with("muse native", 8, "tvly-dotenv-only")

            # 7) catalog.fetch_deepinfra_video_models sends Authorization header from .env
            seen_di_headers: list[dict[str, str]] = []

            def _fake_di_get(url: str, *, headers: Any = None, **kw: Any) -> httpx.Response:
                seen_di_headers.append(dict(headers or {}))
                return httpx.Response(
                    200,
                    json=[{"model_name": "wan/video-1", "tags": ["video-gen"], "description": "Wan video"}],
                    request=httpx.Request("GET", url),
                )

            catalog.reset_deepinfra_cache()
            with mock.patch("muse.media.catalog.httpx.get", side_effect=_fake_di_get):
                vids = catalog.fetch_deepinfra_video_models(force_refresh=True)
            self.assertEqual(len(vids), 1)
            self.assertEqual(seen_di_headers[0].get("Authorization"), "Bearer di-dotenv-only")

    def test_3_codex_oauth_jwt_expiry_and_keep_signed_in_reimport(self) -> None:
        from companion.modules import oauth_login
        from muse.llm import oauth

        fresh_jwt = _make_jwt({"exp": int(time.time()) + 3600})
        expired_24h_jwt = _make_jwt({"exp": int(time.time()) - 86400})
        cli_fresh_jwt = _make_jwt({"exp": int(time.time()) + 7200})

        oauth._save_codex_tokens({"access_token": fresh_jwt, "refresh_token": "rt_fresh"}, set_active=False)
        self.assertTrue(oauth.get_codex_auth_status()["logged_in"])
        self.assertTrue(oauth_login.signed_in("chatgpt"))

        # Store 24h-expired JWT in Muse auth.json and a fresh CLI token in CODEX_HOME/auth.json
        oauth._save_codex_tokens({"access_token": expired_24h_jwt, "refresh_token": "rt_stale"}, set_active=False)
        codex_cli_dir = self.home / "codex-cli-home"
        codex_cli_dir.mkdir(parents=True, exist_ok=True)
        (codex_cli_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": cli_fresh_jwt, "refresh_token": "rt_cli_new"}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_cli_dir)}):
            self.assertFalse(oauth.get_codex_auth_status()["logged_in"])
            self.assertFalse(oauth_login.signed_in("chatgpt"))
            self.assertTrue(oauth_login.keep_signed_in())
            self.assertTrue(oauth.get_codex_auth_status()["logged_in"])
            self.assertTrue(oauth_login.signed_in("chatgpt"))
            self.assertEqual(oauth._read_codex_tokens()["tokens"]["access_token"], cli_fresh_jwt)

    def test_4_chat_completions_strip_id_gemini_parallel_sse_and_reasoning_efforts(self) -> None:
        from companion.modules import models
        from muse.llm.chat_completions import OpenAIChatTransport
        from muse.llm.responses import OpenAIResponsesTransport
        from muse.llm.types import ToolCallComplete

        transport = OpenAIChatTransport(base_url="https://api.openai.com/v1", api_key="sk-test", provider="openai")
        payload, _, _ = transport.build_request_payload(
            model="gpt-4o",
            messages=[
                {"id": 1, "role": "system", "content": "sys"},
                {"id": 2, "role": "user", "content": "hello"},
                {
                    "id": 3,
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_keep", "type": "function", "function": {"name": "ping", "arguments": "{}"}}],
                },
                {"id": 4, "role": "tool", "tool_call_id": "call_keep", "content": "pong"},
            ],
        )
        for msg in payload["messages"]:
            self.assertNotIn("id", msg)
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["id"], "call_keep")

        # Gemini OpenAI-compat parallel tool calls omitting "index"
        def _gemini_parallel_handler(_req: httpx.Request) -> httpx.Response:
            return _sse_response(
                [
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"function":{"arguments":"\\"Tokyo\\"}"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_2","type":"function","function":{"name":"get_time","arguments":"{\\"tz\\":\\"UTC\\"}"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                    "data: [DONE]",
                ]
            )

        async def _run_parallel() -> list[ToolCallComplete]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_gemini_parallel_handler)) as client:
                gem = OpenAIChatTransport(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    api_key="gk",
                    provider="google",
                    client=client,
                )
                evs = [ev async for ev in gem.stream(model="gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}])]
                return [ev for ev in evs if isinstance(ev, ToolCallComplete)]

        tcs = asyncio.run(_run_parallel())
        self.assertEqual(len(tcs), 2)
        self.assertEqual((tcs[0].id, tcs[0].name, json.loads(tcs[0].arguments)), ("call_1", "get_weather", {"city": "Tokyo"}))
        self.assertEqual((tcs[1].id, tcs[1].name, json.loads(tcs[1].arguments)), ("call_2", "get_time", {"tz": "UTC"}))

        # Also verify single-chunk parallel tool_calls list omitting both "index" and "id"
        def _gemini_single_chunk_handler(_req: httpx.Request) -> httpx.Response:
            return _sse_response(
                [
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"type":"function","function":{"name":"tool_a","arguments":"{\\"x\\":1}"}},{"type":"function","function":{"name":"tool_b","arguments":"{\\"y\\":2}"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                    "data: [DONE]",
                ]
            )

        async def _run_single_chunk() -> list[ToolCallComplete]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_gemini_single_chunk_handler)) as client:
                gem = OpenAIChatTransport(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    api_key="gk",
                    provider="google",
                    client=client,
                )
                evs = [ev async for ev in gem.stream(model="gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}])]
                return [ev for ev in evs if isinstance(ev, ToolCallComplete)]

        tcs_single = asyncio.run(_run_single_chunk())
        self.assertEqual([(tc.name, json.loads(tc.arguments)) for tc in tcs_single], [("tool_a", {"x": 1}), ("tool_b", {"y": 2})])

        # Reasoning efforts compatibility across chat_completions & responses
        resp_t = OpenAIResponsesTransport(base_url="https://api.openai.com/v1", api_key="sk-test", provider="openai")
        codex_t = OpenAIResponsesTransport(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key=_make_jwt({"exp": int(time.time()) + 3600}),
            provider="openai-codex",
        )
        for eff in models.REASONING_EFFORTS:
            rc = {"effort": eff}
            oai_p, _, _ = transport.build_request_payload(
                model="gpt-5.4", messages=[{"role": "user", "content": "hi"}], reasoning_config=rc
            )
            if eff == "none":
                self.assertEqual(oai_p.get("reasoning_effort"), "minimal")
            elif eff in {"xhigh", "max"}:
                self.assertEqual(oai_p.get("reasoning_effort"), "high")
            elif eff == "adaptive":
                self.assertEqual(oai_p.get("reasoning_effort"), "medium")
            else:
                self.assertEqual(oai_p.get("reasoning_effort"), eff)

            rp, _, _, _ = resp_t.build_request_payload(
                model="gpt-5.4", messages=[{"role": "user", "content": "hi"}], reasoning_config=rc
            )
            self.assertIn("reasoning", rp)
            if eff == "none":
                self.assertEqual(rp["reasoning"], {"effort": "none"})
            elif eff in {"xhigh", "max"}:
                self.assertEqual(rp["reasoning"]["effort"], "high")
            elif eff == "adaptive":
                self.assertEqual(rp["reasoning"]["effort"], "medium")
            else:
                self.assertEqual(rp["reasoning"]["effort"], eff)

            cp, _, _, _ = codex_t.build_request_payload(
                model="gpt-5.4", messages=[{"role": "user", "content": "hi"}], reasoning_config=rc
            )
            if eff == "none":
                self.assertNotIn("reasoning", cp)
            elif eff == "max":
                self.assertEqual(cp["reasoning"]["effort"], "xhigh")
            elif eff == "adaptive":
                self.assertEqual(cp["reasoning"]["effort"], "medium")

    def test_5_providers_env_expansion_scalar_model_and_dynamic_oauth(self) -> None:
        from muse.llm import oauth
        from muse.llm.providers import resolve_runtime_provider

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MY_CUSTOM_KEY", None)
            os.environ.pop("MY_CUSTOM_HOST", None)
            os.environ.pop("MY_CUSTOM_HDR", None)
            self._write_dotenv_only(
                {
                    "MY_CUSTOM_KEY": "sk-expanded-from-dotenv",
                    "MY_CUSTOM_HOST": "https://relay.example.com/v1",
                    "MY_CUSTOM_HDR": "hdr-secret-val",
                }
            )
            (self.home / "config.yaml").write_text(
                "model: gpt-4o\n"
                "providers:\n"
                "  custom_p:\n"
                '    base_url: "${MY_CUSTOM_HOST}"\n'
                '    api_key: "${MY_CUSTOM_KEY}"\n'
                "    extra_headers:\n"
                '      X-Relay-Auth: "${MY_CUSTOM_HDR}"\n',
                encoding="utf-8",
            )
            invalidate_caches()

            rt_custom = resolve_runtime_provider(requested="custom_p")
            self.assertEqual(rt_custom["model"], "gpt-4o")
            self.assertEqual(rt_custom["base_url"], "https://relay.example.com/v1")
            self.assertEqual(rt_custom["api_key"], "sk-expanded-from-dotenv")
            self.assertEqual(rt_custom["extra_headers"], {"X-Relay-Auth": "hdr-secret-val"})

            # Dynamic OAuth resolution on rotated tokens when api_key is omitted or blank
            tok1 = _make_jwt({"exp": int(time.time()) + 3600, "sub": "1"})
            tok2 = _make_jwt({"exp": int(time.time()) + 7200, "sub": "2"})
            oauth._save_codex_tokens({"access_token": tok1, "refresh_token": "rt1"}, set_active=False)
            self.assertEqual(resolve_runtime_provider(requested="openai-codex", api_key="")["api_key"], tok1)
            oauth._save_codex_tokens({"access_token": tok2, "refresh_token": "rt2"}, set_active=False)
            self.assertEqual(resolve_runtime_provider(requested="openai-codex", api_key="   ")["api_key"], tok2)

    def test_6_cold_boot_stt_available_for_mistral_xai_oauth_and_openai_variants(self) -> None:
        from companion.modules import stt_sensevoice
        from muse.llm import oauth

        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("MISTRAL_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY", "VOICE_TOOLS_OPENAI_KEY")
        }
        with mock.patch.dict(os.environ, clean_env, clear=True), mock.patch.object(
            stt_sensevoice, "missing", return_value=["SenseVoice 模型"]
        ), mock.patch("importlib.util.find_spec", side_effect=lambda name: None if name in ("faster_whisper", "mistralai", "openai") else object()):
            # 1) Mistral on cold boot (muse.media.stt not in sys.modules, mistralai not installed)
            sys.modules.pop("muse.media.stt", None)
            self._write_dotenv_only({"MISTRAL_API_KEY": "msk-dotenv"})
            save_config({"stt": {"provider": "mistral"}})
            self.assertTrue(stt_sensevoice.stt_available())

            # 2) xAI with valid xai-oauth token in auth.json (no XAI_API_KEY)
            sys.modules.pop("muse.media.stt", None)
            self._write_dotenv_only({})
            oauth._save_xai_oauth_tokens(
                {
                    "access_token": _make_jwt({"exp": int(time.time()) + 7200}),
                    "refresh_token": "xai_rt",
                },
                set_active=False,
            )
            save_config({"stt": {"provider": "xai"}})
            self.assertTrue(stt_sensevoice.stt_available())

            # 3) OpenAI with VOICE_TOOLS_OPENAI_KEY only
            sys.modules.pop("muse.media.stt", None)
            self._write_dotenv_only({"VOICE_TOOLS_OPENAI_KEY": "sk-voice-only"})
            save_config({"stt": {"provider": "openai"}})
            self.assertTrue(stt_sensevoice.stt_available())

            # 4) OpenAI with local base_url and no API key
            sys.modules.pop("muse.media.stt", None)
            self._write_dotenv_only({})
            save_config({"stt": {"provider": "openai", "openai": {"base_url": "http://127.0.0.1:8080/v1"}}})
            self.assertTrue(stt_sensevoice.stt_available())

    def test_7_media_delivery_tags_and_creations_paths_with_spaces(self) -> None:
        from companion.modules import creations
        from muse.media import image_gen, tts, video

        for schema in (image_gen.IMAGE_GENERATE_SCHEMA, video.VIDEO_GENERATE_SCHEMA, tts.TEXT_TO_SPEECH_SCHEMA):
            self.assertIn("MEDIA:", schema["description"])

        spaced_img_dir = self.home / "my images 作品"
        spaced_vid_dir = self.home / "my videos 作品"
        spaced_aud_dir = self.home / "my audio 作品"
        spaced_img_dir.mkdir(parents=True, exist_ok=True)
        spaced_vid_dir.mkdir(parents=True, exist_ok=True)
        spaced_aud_dir.mkdir(parents=True, exist_ok=True)

        img_file = spaced_img_dir / "cat 1.png"
        vid_file = spaced_vid_dir / "clip 1.mp4"
        aud_file = spaced_aud_dir / "voice 1.mp3"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        vid_file.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        aud_file.write_bytes(b"ID3fakeaudio")

        img_res = image_gen.image_success(
            image=str(img_file), model="flux", prompt="cute cat", aspect_ratio="square", provider="fal"
        )
        vid_res = video.success_response(
            video=str(vid_file), model="wan", prompt="ocean waves", provider="fal"
        )
        self.assertEqual(img_res["media_tag"], f"MEDIA:{img_file}")
        self.assertEqual(vid_res["media_tag"], f"MEDIA:{vid_file}")

        # Verify _media_in parses JSON dict fields (video_path, file_path, audio_path, path, media_tag) before regex
        for field_name, target_file in (
            ("video_path", vid_file),
            ("file_path", aud_file),
            ("audio_path", aud_file),
            ("path", img_file),
            ("media_tag", Path(f"MEDIA:{img_file}")),
        ):
            val_str = f"MEDIA:{img_file}" if field_name == "media_tag" else str(target_file)
            _, extracted_files = creations._media_in(json.dumps({field_name: val_str}, ensure_ascii=False))
            expected_path = img_file if field_name == "media_tag" else target_file
            self.assertEqual(extracted_files, [expected_path], f"Failed extracting {field_name}")

        asyncio.run(creations.collect_generated(json.dumps(img_res, ensure_ascii=False), title="小猫", prompt="cute cat"))
        asyncio.run(creations.collect_generated(json.dumps(vid_res, ensure_ascii=False), title="海浪", prompt="ocean waves"))
        asyncio.run(creations.collect_generated(f"MEDIA:{aud_file}", title="问候", prompt="hello"))

        listed = creations.list_creations(lambda rel: f"/signed/{rel}")
        self.assertEqual(listed["counts"]["image"], 1)
        self.assertEqual(listed["counts"]["video"], 1)
        self.assertEqual(listed["counts"]["audio"], 1)

    def test_8_db_legacy_reasoning_migration_and_post_clarify_undo(self) -> None:
        from muse.llm.anthropic_messages import AnthropicMessagesTransport
        from muse.llm.responses import OpenAIResponsesTransport
        from muse.turn_actor import db_rows_to_llm_messages

        # Part A: Legacy state.db stringified reasoning_details & codex_reasoning_items -> list[dict]
        state_db = self.home / "state.db"
        now = time.time()
        thinking_blocks = [{"type": "thinking", "thinking": "careful analysis", "signature": "sig_ant_1"}]
        codex_items = [
            {
                "type": "reasoning",
                "id": "rs_leg_1",
                "encrypted_content": "ENC_LEGACY_1",
                "_issuer_kind": "codex_backend",
                "_issuer_model": "gpt-5.4",
            }
        ]
        with contextlib.closing(sqlite3.connect(state_db)) as conn:
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, started_at REAL)"
            )
            conn.execute(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                "reasoning_details TEXT, codex_reasoning_items TEXT, timestamp REAL)"
            )
            conn.execute("INSERT INTO sessions VALUES ('s_leg', 'companion', 'chat_leg', ?)", (now,))
            conn.execute(
                "INSERT INTO messages VALUES (1, 's_leg', 'user', 'hi', NULL, NULL, ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO messages VALUES (2, 's_leg', 'assistant', 'hello', ?, ?, ?)",
                (json.dumps(thinking_blocks), json.dumps(codex_items), now + 1),
            )
            conn.execute(
                "INSERT INTO messages VALUES (3, 's_leg', 'user', 'follow up', NULL, NULL, ?)",
                (now + 2,),
            )
            conn.commit()

        counts = sync_from_legacy_state_db(self.home)
        self.assertEqual(counts["messages"], 3)

        with MuseDB(self.home / "muse.db") as db:
            rows = db.get_messages("s_leg", active_only=True)
            self.assertIsInstance(rows[1]["extra"]["reasoning_details"], list)
            self.assertIsInstance(rows[1]["extra"]["codex_reasoning_items"], list)

            llm_msgs = db_rows_to_llm_messages(rows)
            ant = AnthropicMessagesTransport(base_url="https://api.anthropic.com", api_key="sk-ant")
            ant_payload = asyncio.run(ant.build_request_payload(model="claude-sonnet-4-6", messages=llm_msgs))
            self.assertEqual(ant_payload["messages"][1]["content"][0]["type"], "thinking")
            self.assertEqual(ant_payload["messages"][1]["content"][0]["signature"], "sig_ant_1")

            codex_t = OpenAIResponsesTransport(
                base_url="https://chatgpt.com/backend-api/codex",
                api_key=_make_jwt({"exp": int(time.time()) + 3600}),
                provider="openai-codex",
            )
            codex_payload, _, _, _ = codex_t.build_request_payload(model="gpt-5.4", messages=llm_msgs)
            replayed_enc = [i["encrypted_content"] for i in codex_payload["input"] if i.get("type") == "reasoning"]
            self.assertEqual(replayed_enc, ["ENC_LEGACY_1"])

            # Part B: Post-clarify undo_last_exchange keeps turn_A intact and leaves zero orphan tool calls
            sid = db.create_session(chat_id="chat_clarify", session_id="s_clarify")
            db.append_message(sid, "chat_clarify", "user", "Deploy this please", turn_id="turn_A")
            db.append_message(
                sid,
                "chat_clarify",
                "assistant",
                "",
                turn_id="turn_A",
                tool_calls=[
                    {
                        "id": "tc_clarify_1",
                        "type": "function",
                        "function": {"name": "clarify", "arguments": '{"question":"Env?","choices":["staging","prod"]}'},
                    }
                ],
            )
            pid = db.append_message(
                sid,
                "chat_clarify",
                "tool",
                "[Awaiting user clarification]",
                turn_id="turn_A",
                tool_call_id="tc_clarify_1",
                tool_name="clarify",
                extra={"clarify_id": "cid_1", "awaiting_user": True},
            )
            # Simulate user answering "prod" on turn_B
            db.update_message(
                pid,
                content="prod",
                turn_id="turn_B",
                extra={"clarify_id": "cid_1", "awaiting_user": False},
            )
            db.append_message(sid, "chat_clarify", "assistant", "Deployed to prod.", turn_id="turn_B")

            changed = db.undo_last_exchange(sid)
            self.assertEqual(changed, 2)
            after_b = db.get_messages(sid, active_only=True)
            self.assertEqual([r["role"] for r in after_b], ["user", "assistant", "tool"])
            self.assertEqual([r["turn_id"] for r in after_b], ["turn_A", "turn_A", "turn_A"])
            self.assertEqual(after_b[0]["content"], "Deploy this please")
            self.assertEqual(after_b[2]["content"], "[Interrupted before completion]")
            self.assertTrue(after_b[2]["extra"].get("undone"))
            self.assertEqual(db.heal_orphan_tool_calls(sid), 0)

            # Second undo removes turn_A completely
            changed_a = db.undo_last_exchange(sid, turn_id="turn_A")
            self.assertEqual(changed_a, 3)
            self.assertEqual(db.get_messages(sid, active_only=True), [])


if __name__ == "__main__":
    unittest.main()
