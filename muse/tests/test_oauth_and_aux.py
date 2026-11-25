"""Unit tests for muse.llm.oauth, muse.llm.auxiliary, muse.llm.models_dev, and muse.media.catalog."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from muse.llm import auxiliary, models_dev, oauth
from muse.media import catalog


def _make_jwt(payload: dict[str, Any]) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


class OAuthStoreAndFlowsTest(unittest.TestCase):
    def setUp(self):
        from muse.paths import bind_muse_home

        self._tmp = tempfile.TemporaryDirectory(prefix="muse-oauth-test-")
        self.home = Path(self._tmp.name)
        self._home_token = bind_muse_home(self.home)

    def tearDown(self):
        from muse.paths import reset_muse_home

        reset_muse_home(self._home_token)
        self._tmp.cleanup()

    def test_codex_store_jwt_headers_import_and_auto_refresh(self):
        fresh_jwt = _make_jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct_abc",
                    "chatgpt_compute_residency": "eu",
                },
            }
        )
        hdrs = oauth.codex_account_headers(fresh_jwt)
        self.assertEqual(hdrs, {"ChatGPT-Account-ID": "acct_abc", "x-openai-internal-codex-residency": "eu"})

        # Save tokens atomically to <HOME>/auth.json with Hermes-compatible <HOME>/auth.lock
        oauth._save_codex_tokens({"access_token": fresh_jwt, "refresh_token": "rt_1"}, set_active=False)
        auth_file = self.home / "auth.json"
        lock_file = self.home / "auth.lock"
        self.assertTrue(auth_file.is_file())
        self.assertTrue(lock_file.exists())
        self.assertFalse((self.home / ".auth.lock").exists())

        # Regression: holding an OS lock on <HOME>/auth.lock (as Hermes does during coexistence)
        # must block muse's _auth_store_lock until TimeoutError!
        lock_file.write_text(" ", encoding="utf-8")
        with lock_file.open("r+", encoding="utf-8") as ext_fh:
            if oauth.fcntl is not None:
                oauth.fcntl.flock(ext_fh.fileno(), oauth.fcntl.LOCK_EX | oauth.fcntl.LOCK_NB)
            elif oauth.msvcrt is not None:
                ext_fh.seek(0)
                oauth.msvcrt.locking(ext_fh.fileno(), oauth.msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaises(TimeoutError):
                    with oauth._auth_store_lock(timeout_seconds=0.15):
                        pass
            finally:
                if oauth.fcntl is not None:
                    oauth.fcntl.flock(ext_fh.fileno(), oauth.fcntl.LOCK_UN)
                elif oauth.msvcrt is not None:
                    ext_fh.seek(0)
                    oauth.msvcrt.locking(ext_fh.fileno(), oauth.msvcrt.LK_UNLCK, 1)

        status = oauth.get_codex_auth_status()
        self.assertTrue(status["logged_in"])

        # Expiring token triggers refresh via https://auth.openai.com/oauth/token and rotates stored tokens.
        expiring_jwt = _make_jwt({"exp": int(time.time()) + 10})
        rotated_jwt = _make_jwt({"exp": int(time.time()) + 7200})
        oauth._save_codex_tokens({"access_token": expiring_jwt, "refresh_token": "rt_old"}, set_active=False)

        def fake_post(client_self, url, **kwargs):
            self.assertEqual(url, oauth.CODEX_OAUTH_TOKEN_URL)
            self.assertEqual(kwargs["data"]["refresh_token"], "rt_old")
            return httpx.Response(200, json={"access_token": rotated_jwt, "refresh_token": "rt_new"})

        with mock.patch.object(httpx.Client, "post", fake_post):
            creds = oauth.resolve_codex_runtime_credentials()
        self.assertEqual(creds["api_key"], rotated_jwt)
        stored = oauth._read_codex_tokens()
        self.assertEqual(stored["tokens"]["refresh_token"], "rt_new")

        # Import from ~/.codex/auth.json
        codex_dir = self.home / "codex-cli"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": fresh_jwt, "refresh_token": "cli_rt"}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_dir)}):
            imported = oauth._import_codex_cli_tokens()
        self.assertIsNotNone(imported)
        self.assertEqual(imported["refresh_token"], "cli_rt")

        # Expired Codex CLI token is rejected.
        expired_jwt = _make_jwt({"exp": int(time.time()) - 60})
        (codex_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": expired_jwt, "refresh_token": "cli_rt"}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_dir)}):
            self.assertIsNone(oauth._import_codex_cli_tokens())

        # Self-heal: if refresh_token was rotated by Codex CLI (HTTP 401 refresh_token_reused),
        # resolve_codex_runtime_credentials adopts the fresher pair from ~/.codex/auth.json!
        healed_jwt = _make_jwt({"exp": int(time.time()) + 7200})
        oauth._save_codex_tokens({"access_token": expiring_jwt, "refresh_token": "stale_rt"}, set_active=False)
        (codex_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": healed_jwt, "refresh_token": "cli_fresh_rt"}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_dir)}):
            with mock.patch.object(
                httpx.Client,
                "post",
                lambda *_a, **_kw: httpx.Response(401, json={"error": {"code": "refresh_token_reused"}}),
            ):
                healed = oauth.resolve_codex_runtime_credentials()
        self.assertEqual(healed["api_key"], healed_jwt)
        self.assertEqual(oauth._read_codex_tokens()["tokens"]["refresh_token"], "cli_fresh_rt")

    def test_xai_oauth_discovery_validation_and_proactive_rotating_refresh(self):
        # Reject non-x.ai MITM discovery endpoints.
        with self.assertRaises(oauth.AuthError):
            oauth._xai_validate_oauth_endpoint("https://evil.example/token", field="token_endpoint")
        with self.assertRaises(oauth.AuthError):
            oauth._xai_validate_oauth_endpoint("http://auth.x.ai/token", field="token_endpoint")

        # Token with 50 min left (under 1h proactive skew? wait: >45m and <=60m -> 1h skew triggers refresh!)
        soon_expiring = _make_jwt({"exp": int(time.time()) + 50 * 60})
        rotated = _make_jwt({"exp": int(time.time()) + 6 * 3600})
        oauth._save_xai_oauth_tokens(
            {"access_token": soon_expiring, "refresh_token": "xai_rt_1", "token_type": "Bearer"},
            discovery={
                "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
            set_active=False,
        )

        def fake_post(client_self, url, **kwargs):
            self.assertEqual(url, "https://auth.x.ai/oauth2/token")
            self.assertEqual(kwargs["data"]["refresh_token"], "xai_rt_1")
            return httpx.Response(
                200,
                json={"access_token": rotated, "refresh_token": "xai_rt_2", "token_type": "Bearer", "expires_in": 21600},
            )

        with mock.patch.object(httpx.Client, "post", fake_post):
            creds = oauth.resolve_xai_oauth_runtime_credentials()
        self.assertEqual(creds["api_key"], rotated)
        stored = oauth._read_xai_oauth_tokens()
        self.assertEqual(stored["tokens"]["refresh_token"], "xai_rt_2")
        # Existing discovery fields (authorization_endpoint) must survive token refresh!
        self.assertEqual(stored["discovery"]["authorization_endpoint"], "https://auth.x.ai/oauth2/auth")
        self.assertEqual(stored["discovery"]["token_endpoint"], "https://auth.x.ai/oauth2/token")

        # RFC 8628 device-code polling handles authorization_pending -> 200.
        poll_count = {"n": 0}

        def device_handler(request: httpx.Request) -> httpx.Response:
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": rotated, "refresh_token": "xai_rt_3"})

        with mock.patch("muse.llm.oauth.time.sleep", lambda _s: None):
            with httpx.Client(transport=httpx.MockTransport(device_handler)) as c:
                payload = oauth._xai_oauth_poll_device_token(
                    c,
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    device_code="dev_1",
                    expires_in=60,
                    poll_interval=1,
                )
        self.assertEqual(payload["refresh_token"], "xai_rt_3")
        self.assertEqual(poll_count["n"], 2)


class AuxiliaryClientAndVisionFallbackTest(unittest.TestCase):
    def tearDown(self):
        auxiliary.register_runtime_loop(None)

    def test_sync_call_llm_standalone_and_worker_thread_runtime_loop_pool(self):
        def sse_ok(_request: httpx.Request) -> httpx.Response:
            raw = (
                'data: {"choices":[{"index":0,"delta":{"content":"pong"}}]}\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=raw)

        # 1) Standalone sync call_llm (no registered loop -> local asyncio.run).
        mock_transport = httpx.MockTransport(sse_ok)
        orig_async_client = httpx.AsyncClient

        def patched_client(*args, **kwargs):
            kwargs.setdefault("transport", mock_transport)
            return orig_async_client(*args, **kwargs)

        with mock.patch("httpx.AsyncClient", side_effect=patched_client):
            resp = auxiliary.call_llm(
                messages=[{"role": "user", "content": "ping"}],
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
                model="gpt-4o-mini",
            )
        self.assertEqual(resp.choices[0].message.content, "pong")

        # 2) Registered main runtime loop: sync call_llm from worker thread dispatches via
        #    run_coroutine_threadsafe and reuses the main loop's pooled httpx.AsyncClient!
        loop = asyncio.new_event_loop()
        started = threading.Event()
        observed_clients: list[int] = []

        def run_loop():
            asyncio.set_event_loop(loop)
            auxiliary.register_runtime_loop(loop)
            started.set()
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        started.wait(timeout=5)
        try:
            # Inject mock transport into the pooled client for the registered loop.
            pooled = httpx.AsyncClient(transport=mock_transport)
            with auxiliary._LOOP_LOCK:
                auxiliary._loop_clients[id(loop)] = pooled

            orig_get_pooled = auxiliary.get_pooled_async_client

            def spy_get_pooled(lp=None):
                c = orig_get_pooled(lp)
                if c is not None:
                    observed_clients.append(id(c))
                return c

            with mock.patch.object(auxiliary, "get_pooled_async_client", side_effect=spy_get_pooled):
                r1 = auxiliary.call_llm(
                    messages=[{"role": "user", "content": "1"}],
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                    model="gpt-4o-mini",
                )
                r2 = auxiliary.call_llm(
                    messages=[{"role": "user", "content": "2"}],
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                    model="gpt-4o-mini",
                )
            self.assertEqual(r1.choices[0].message.content, "pong")
            self.assertEqual(r2.choices[0].message.content, "pong")
            self.assertEqual(len(observed_clients), 2)
            self.assertEqual(observed_clients[0], id(pooled))
            self.assertEqual(observed_clients[1], id(pooled))
        finally:
            asyncio.run_coroutine_threadsafe(auxiliary.close_shared_clients(), loop).result(timeout=5)
            auxiliary.register_runtime_loop(None)
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()

    def test_describe_images_if_blind(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "图里是什么？"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                ],
            }
        ]
        # When chat model supports vision -> returned unchanged.
        same = asyncio.run(auxiliary.describe_images_if_blind(msgs, chat_vision=True))
        self.assertIs(same, msgs)

        # When chat model is blind -> calls configured vision model and replaces image parts with description.
        def vision_handler(_req: httpx.Request) -> httpx.Response:
            raw = (
                'data: {"choices":[{"index":0,"delta":{"content":"一只橘猫坐在窗台上看雨。"}}]}\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=raw)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(vision_handler)) as client:
                return await auxiliary.describe_images_if_blind(
                    msgs,
                    chat_vision=False,
                    vision_base_url="https://api.openai.com/v1",
                    vision_api_key="sk-v",
                    vision_model="gpt-4o",
                    client=client,
                )

        rewritten = asyncio.run(run())
        content = rewritten[0]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("橘猫", content)
        # Original messages untouched.
        self.assertEqual(msgs[0]["content"][1]["type"], "image_url")

    def test_sync_call_llm_inside_running_loop_no_deadlock_and_engine_usage_guard(self):
        from muse.db import MuseDB, close_all
        from muse.paths import get_muse_db_path, muse_home_override

        def sse_ok(_req: httpx.Request) -> httpx.Response:
            raw = (
                'data: {"choices":[{"index":0,"delta":{"content":"safe"}}]}\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"prompt_cache_hit_tokens":80}}\n'
                "data: [DONE]\n\n"
            ).encode("utf-8")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=raw)

        orig_async_client = httpx.AsyncClient
        mock_transport = httpx.MockTransport(sse_ok)

        def patched_client(*args, **kwargs):
            kwargs.setdefault("transport", mock_transport)
            return orig_async_client(*args, **kwargs)

        # 1) Calling sync call_llm from inside a running event loop thread must NOT deadlock.
        async def invoke_from_running_loop():
            return auxiliary.call_llm(
                task="",
                messages=[{"role": "user", "content": "hi"}],
                base_url="https://api.deepseek.com/v1",
                api_key="sk-test",
                model="deepseek-chat",
            )

        with tempfile.TemporaryDirectory(prefix="muse-usage-test-") as td:
            with muse_home_override(td), mock.patch("httpx.AsyncClient", side_effect=patched_client):
                # Explicit task="" skips usage recording and does not create muse.db:
                resp = asyncio.run(invoke_from_running_loop())
                self.assertEqual(resp.choices[0].message.content, "safe")
                self.assertFalse(get_muse_db_path().exists())

                # Default call_llm records system usage to real muse.db without double-counting cache:
                auxiliary.call_llm(
                    messages=[{"role": "user", "content": "hi"}],
                    base_url="https://api.deepseek.com/v1",
                    api_key="sk-test",
                    model="deepseek-chat",
                )
                auxiliary.call_llm(
                    task="",
                    messages=[{"role": "user", "content": "skip"}],
                    base_url="https://api.deepseek.com/v1",
                    api_key="sk-test",
                    model="deepseek-chat",
                )
                self.assertTrue(get_muse_db_path().is_file())
                with MuseDB() as db:
                    rows = db.query_usage_since(0.0)
                close_all()
        self.assertEqual(len(rows), 1)
        model, base_url, task, _source, calls, total_in, total_out, cached = rows[0]
        self.assertEqual((model, base_url, task, calls), ("deepseek-chat", "https://api.deepseek.com/v1", "aux", 1))
        # No double counting: 20 uncached + 80 cache_read == 100 total input tokens!
        self.assertEqual((total_in, total_out, cached), (100, 20, 80))


class ModelsDevAndMediaCatalogTest(unittest.TestCase):
    def setUp(self):
        from muse.paths import bind_muse_home

        self._tmp = tempfile.TemporaryDirectory(prefix="muse-catalog-test-")
        self.home = Path(self._tmp.name)
        self._home_token = bind_muse_home(self.home)
        models_dev.reset_models_dev_cache()
        catalog.reset_deepinfra_cache()

    def tearDown(self):
        from muse.paths import reset_muse_home

        reset_muse_home(self._home_token)
        models_dev.reset_models_dev_cache()
        catalog.reset_deepinfra_cache()
        self._tmp.cleanup()

    def test_fetch_models_dev_etag_cache_and_corrupt_quarantine(self):
        seen_headers: list[dict[str, str]] = []
        sample = {"openai": {"models": {"gpt-4o": {"name": "GPT-4o", "tool_call": True}}}}

        def fake_get(url, headers=None, **kwargs):
            seen_headers.append(dict(headers or {}))
            if headers and headers.get("If-None-Match") == '"etag-v1"':
                return httpx.Response(304, request=httpx.Request("GET", url))
            return httpx.Response(200, json=sample, headers={"ETag": '"etag-v1"'}, request=httpx.Request("GET", url))

        with mock.patch("muse.llm.models_dev.httpx.get", side_effect=fake_get):
            data = models_dev.fetch_models_dev(allow_network=True)
            self.assertEqual(data, sample)
            self.assertTrue((self.home / "models_dev_cache.json").is_file())
            self.assertEqual((self.home / "models_dev_cache.etag").read_text(encoding="utf-8").strip(), '"etag-v1"')

            # Offline read from disk after memory clear.
            models_dev.reset_models_dev_cache()
            offline = models_dev.fetch_models_dev(allow_network=False)
            self.assertEqual(offline, sample)

            # Force refresh sends If-None-Match and honors 304 Not Modified.
            reconfirmed = models_dev.fetch_models_dev(force_refresh=True, allow_network=True)
            self.assertEqual(reconfirmed, sample)
            self.assertEqual(seen_headers[-1].get("If-None-Match"), '"etag-v1"')

        # Corrupt disk cache is quarantined cleanly.
        models_dev.reset_models_dev_cache()
        (self.home / "models_dev_cache.json").write_text("{corrupt", encoding="utf-8")
        self.assertEqual(models_dev.fetch_models_dev(allow_network=False), {})
        self.assertTrue((self.home / "models_dev_cache.json.corrupt").is_file())

    def test_fal_static_catalog_counts_and_deepinfra_video_fetcher(self):
        self.assertEqual(len(catalog.FAL_IMAGE_MODELS), 25)
        self.assertEqual(len(catalog.FAL_VIDEO_MODELS), 20)
        self.assertEqual(len(catalog.backend_models("image", "fal")), 25)
        self.assertEqual(len(catalog.backend_models("video", "fal")), 20)

        # DeepInfra video-gen filter + offline [] fallback.
        payload = [
            {"model_name": "Wan-AI/Wan2.2", "tags": ["video-gen"], "description": "Wan video model"},
            {"model_name": "meta-llama/Llama-3", "tags": ["text-generation"], "description": "LLM"},
            {"id": "kling/kling-v2", "metadata": {"tags": ["video-gen"], "description": "Kling clip"}},
        ]

        def ok_get(url, **kwargs):
            self.assertEqual(url, catalog.DEEPINFRA_MODELS_LIST_URL)
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        with mock.patch("muse.media.catalog.httpx.get", side_effect=ok_get):
            videos = catalog.fetch_deepinfra_video_models(force_refresh=True)
        self.assertEqual([v["id"] for v in videos], ["Wan-AI/Wan2.2", "kling/kling-v2"])

        # Offline / timeout fallback returns [].
        catalog.reset_deepinfra_cache()
        with mock.patch("muse.media.catalog.httpx.get", side_effect=httpx.ReadTimeout("timeout")):
            offline_videos = catalog.fetch_deepinfra_video_models(force_refresh=True)
        self.assertEqual(offline_videos, [])

    def test_companion_model_catalog_and_oauth_login_wired_to_muse(self):
        from companion.modules import model_catalog, oauth_login
        from muse.paths import muse_home_override

        # model_catalog._backend_models delegates directly to muse.media.catalog (no plugins.* import).
        self.assertEqual(len(model_catalog._backend_models("image", "fal")), 25)
        self.assertEqual(len(model_catalog._backend_models("video", "fal")), 20)

        # oauth_login reads/imports via muse.llm.oauth with isolated muse home.
        fresh_jwt = _make_jwt({"exp": int(time.time()) + 3600})
        codex_dir = self.home / "codex-home"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": fresh_jwt, "refresh_token": "rt_local"}}),
            encoding="utf-8",
        )
        with muse_home_override(self.home), mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_dir)}):
            self.assertFalse(oauth_login.signed_in("chatgpt"))
            self.assertTrue(oauth_login.local_login("chatgpt"))
            oauth_login.import_local("chatgpt")
            self.assertTrue(oauth_login.signed_in("chatgpt"))


if __name__ == "__main__":
    unittest.main()
