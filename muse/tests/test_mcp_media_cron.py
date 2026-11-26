"""Comprehensive tests for PR-7: muse.mcp, muse.media, and muse.cron."""

from __future__ import annotations

import asyncio
import base64
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import httpx
from PIL import Image

from muse.config_store import save_config, save_env_value
from muse.cron import jobs as cron_jobs
from muse.cron.scheduler import CronScheduler, is_exempt_self_trigger_url
from muse.cron.tool import _handle_cronjob_manage
from muse.mcp.config import (
    build_browser_preset,
    build_stdio_env,
    expand_server_config,
    load_mcp_servers,
    mcp_tool_name,
    mcp_toolset_name,
    resolve_stdio_command,
)
from muse.mcp.manager import AsyncMCPManager, _open_sdk_session
from muse.media import image_gen, stt, tts, video, vision
from muse.paths import muse_home_override


def _make_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 128, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _make_fake_jwt(claims: dict[str, Any]) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{enc({'alg': 'RS256', 'typ': 'JWT'})}.{enc(claims)}.sig"


class MCPConfigAndManagerTest(unittest.TestCase):
    def test_env_expansion_across_env_headers_args_and_url_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                save_env_value("COMPANION_MCP_DEMO_KEY", "secret-val")
                save_env_value("COMPANION_MCP_DEMO_ARG", "/data/dir")
                save_config(
                    {
                        "mcp_servers": {
                            "demo-srv": {
                                "command": "npx",
                                "args": ["--dir", "${COMPANION_MCP_DEMO_ARG}"],
                                "env": {"API_KEY": "${COMPANION_MCP_DEMO_KEY}"},
                                "headers": {"Authorization": "Bearer ${COMPANION_MCP_DEMO_KEY}"},
                                "url": "https://example.com/mcp?api_key=${COMPANION_MCP_DEMO_KEY}&x=1",
                            }
                        }
                    }
                )
                servers = load_mcp_servers()
                srv = servers["demo-srv"]
                self.assertEqual(srv["args"], ["--dir", "/data/dir"])
                self.assertEqual(srv["env"]["API_KEY"], "secret-val")
                self.assertEqual(srv["headers"]["Authorization"], "Bearer secret-val")
                self.assertIn("api_key=secret-val", srv["url"])
                self.assertNotIn("${", srv["url"])

    def test_browser_preset_local_vs_docker(self):
        local = build_browser_preset(in_docker=False)
        self.assertEqual(local["command"], "npx")
        self.assertIn("@playwright/mcp@0.0.83", local["args"])
        self.assertIn("--no-webmcp", local["args"])
        self.assertIn("http://127.0.0.1:9333", local["args"])

        docker = build_browser_preset(in_docker=True)
        self.assertEqual(docker["command"], "playwright-mcp")
        self.assertIn("--no-webmcp", docker["args"])
        self.assertIn("http://127.0.0.1:9333", docker["args"])

    def test_windows_stdio_cmd_resolution_and_utf8_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_name = "muse_fake_npx.cmd" if os.name == "nt" else "muse_fake_npx"
            fake_exe = Path(tmp) / cmd_name
            fake_exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
            if os.name != "nt":
                os.chmod(fake_exe, 0o755)

            merged = build_stdio_env({"PATH": tmp + os.pathsep + os.environ.get("PATH", "")})
            self.assertEqual(merged["PYTHONUTF8"], "1")
            self.assertEqual(merged["PYTHONIOENCODING"], "utf-8")

            resolved = resolve_stdio_command("muse_fake_npx", merged)
            self.assertTrue(Path(resolved).is_file())
            if os.name == "nt":
                self.assertTrue(resolved.lower().endswith(".cmd"))

    def test_sdk_stdio_parameters_enforce_utf8_replace(self):
        captured: dict[str, Any] = {}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                return type("R", (), {"tools": []})()

        class FakeStdioCtx:
            def __init__(self, params):
                captured["params"] = params

            async def __aenter__(self):
                return (object(), object())

            async def __aexit__(self, *args):
                return None

        async def run():
            with (
                mock.patch("mcp.client.stdio.stdio_client", side_effect=FakeStdioCtx),
                mock.patch("mcp.ClientSession", return_value=FakeSession()),
            ):
                async with AsyncExitStack() as stack:
                    await _open_sdk_session(stack, "browser", {"command": "python", "args": ["-V"]})

        asyncio.run(run())
        params = captured["params"]
        self.assertEqual(params.encoding, "utf-8")
        self.assertEqual(params.encoding_error_handler, "replace")
        self.assertEqual(params.env["PYTHONUTF8"], "1")
        self.assertEqual(params.env["PYTHONIOENCODING"], "utf-8")

    def test_inflight_refcount_lease_live_reload_drains_without_cutting_turn(self):
        events: list[str] = []
        call_started = asyncio.Event()
        release_call = asyncio.Event()

        class FakeSession:
            def __init__(self, gen_label: str) -> None:
                self.gen_label = gen_label

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
                events.append(f"call_start:{self.gen_label}:{name}")
                if self.gen_label == "gen1":
                    call_started.set()
                    await release_call.wait()
                events.append(f"call_end:{self.gen_label}:{name}")
                return {"content": [{"type": "text", "text": f"{self.gen_label}:{arguments.get('x')}"}]}

        gen_counter = {"n": 0}

        async def fake_opener(stack: AsyncExitStack, name: str, cfg: dict[str, Any]):
            gen_counter["n"] += 1
            label = f"gen{gen_counter['n']}"
            events.append(f"open:{label}")
            stack.callback(lambda lbl=label: events.append(f"close:{lbl}"))
            tools = [("navigate", "Open page", {"type": "object", "properties": {"x": {"type": "string"}}})]
            return FakeSession(label), tools

        async def run():
            mgr = AsyncMCPManager(opener=fake_opener, drain_timeout=5.0)
            res1 = await mgr.reload_mcp({"my-browser": {"command": "v1"}})
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["added"], ["my-browser"])
            self.assertEqual(
                mgr.get_tool_names_for_toolset(mcp_toolset_name("my-browser")),
                [mcp_tool_name("my-browser", "navigate")],
            )

            turn_snapshot = mgr.snapshot_tools()
            full_name = mcp_tool_name("my-browser", "navigate")
            inflight_task = asyncio.create_task(
                mgr.call_tool(full_name, {"x": "first"}, snapshot=turn_snapshot)
            )
            await asyncio.wait_for(call_started.wait(), timeout=2.0)

            reload_task = asyncio.create_task(
                mgr.reload_mcp({"my-browser": {"command": "v2"}}, wait_for_drain=True)
            )
            # Wait briefly to confirm gen1 is NOT closed while in-flight call is active.
            await asyncio.sleep(0.05)
            self.assertIn("open:gen2", events)
            self.assertNotIn("close:gen1", events)

            # New turns immediately hit gen2 while old turn finishes on gen1.
            new_out = await mgr.call_tool(full_name, {"x": "second"})
            self.assertEqual(new_out, "gen2:second")

            release_call.set()
            old_out = await asyncio.wait_for(inflight_task, timeout=2.0)
            self.assertEqual(old_out, "gen1:first")

            res2 = await asyncio.wait_for(reload_task, timeout=2.0)
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["reloaded"], ["my-browser"])
            self.assertIn("close:gen1", events)
            self.assertLess(events.index("call_end:gen1:navigate"), events.index("close:gen1"))

            probed = await mgr.probe_server("my-browser", {"command": "v2"})
            self.assertEqual(probed, ["navigate"])
            await mgr.shutdown()
            self.assertIn("close:gen2", events)

        asyncio.run(run())


class MediaSubsystemsTest(unittest.TestCase):
    def test_codex_image_gen_and_edit_headers_and_endpoints(self):
        png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
        fake_jwt = _make_fake_jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct_12345",
                    "chatgpt_data_residency": "us",
                }
            }
        )
        calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            req_body = json.loads(request.content.decode("utf-8"))
            calls.append((str(request.url), dict(request.headers), req_body))
            return httpx.Response(
                200,
                json={"data": [{"b64_json": png_b64}], "quality": "medium", "size": "1024x1024"},
                headers={"x-codex-imagegen-request-id": "req-abc"},
            )

        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client

        def patched_client(*args, **kwargs):
            kwargs["transport"] = transport
            return orig_client(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                (Path(tmp) / "auth.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "providers": {
                                "openai-codex": {
                                    "tokens": {"access_token": fake_jwt, "refresh_token": "rt_1"}
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                save_config({"image_gen": {"provider": "openai-codex", "model": "gpt-image-2-medium"}})
                ref_png = Path(tmp) / "ref.png"
                ref_png.write_bytes(_make_png_bytes())

                with mock.patch("muse.media.image_gen.httpx.Client", side_effect=patched_client):
                    gen_res = json.loads(
                        image_gen._handle_image_generate(
                            {"prompt": "a cute orange cat", "aspect_ratio": "square"}
                        )
                    )
                    edit_res = json.loads(
                        image_gen._handle_image_generate(
                            {"prompt": "add a red scarf", "aspect_ratio": "square", "image_url": str(ref_png)}
                        )
                    )

                self.assertTrue(gen_res["success"])
                self.assertEqual(gen_res["provider"], "openai-codex")
                self.assertTrue(Path(gen_res["image"]).is_file())
                self.assertTrue(edit_res["success"])
                self.assertEqual(edit_res["modality"], "image")

                self.assertTrue(calls[0][0].endswith("/images/generations"))
                self.assertEqual(calls[0][1]["chatgpt-account-id"], "acct_12345")
                self.assertEqual(calls[0][1]["x-openai-internal-codex-residency"], "us")
                self.assertTrue(calls[1][0].endswith("/images/edits"))
                self.assertTrue(calls[1][2]["images"][0]["image_url"].startswith("data:image/png;base64,"))

    def test_all_other_image_backends_openai_xai_openrouter_fal(self):
        png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
        data_url = f"data:image/png;base64,{png_b64}"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api.openai.com" in url or "api.x.ai" in url:
                return httpx.Response(200, json={"data": [{"b64_json": png_b64}]})
            if "openrouter.ai" in url:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]},
                )
            if "queue.fal.run" in url:
                return httpx.Response(200, json={"images": [{"url": "https://cdn.example.com/out.png"}]})
            return httpx.Response(404)

        orig_client = httpx.Client

        def patched_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return orig_client(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                save_env_value("OPENAI_API_KEY", "sk-test")
                save_env_value("XAI_API_KEY", "xai-test")
                save_env_value("OPENROUTER_API_KEY", "or-test")
                save_env_value("FAL_KEY", "fal-test")
                with (
                    mock.patch("muse.media.image_gen.httpx.Client", side_effect=patched_client),
                    mock.patch("muse.media.image_gen.save_url_image", return_value=Path(tmp) / "fal.png"),
                ):
                    for pname in ("openai", "xai", "openrouter", "fal"):
                        res = image_gen.image_generate("sunset", provider=pname)
                        self.assertTrue(res["success"], f"{pname} failed: {res}")
                        self.assertEqual(res["provider"], pname)

    def test_video_provider_abc_and_builtin_backends(self):
        class CustomPartnerVideo(video.VideoGenProvider):
            name = "companion-test"

            def is_available(self) -> bool:
                return True

            def list_models(self) -> list[dict[str, Any]]:
                return [{"id": "partner-v1", "display": "Partner V1"}]

            def generate(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
                return video.success_response(
                    video="/tmp/clip.mp4",
                    model=kwargs.get("model") or "partner-v1",
                    prompt=prompt,
                    provider=self.name,
                )

        video.register_video_gen_provider(CustomPartnerVideo())
        names = {p.name for p in video.list_video_gen_providers()}
        self.assertTrue({"xai", "openrouter", "fal", "deepinfra", "companion-test"}.issubset(names))

        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                save_config({"video_gen": {"provider": "companion-test", "model": "partner-v1"}})
                raw = json.loads(video._handle_video_generate({"prompt": "waving cat", "duration": 4}))
                self.assertTrue(raw["success"])
                self.assertEqual(raw["video"], "/tmp/clip.mp4")

                # Exercise all 4 built-in video backends (xai, openrouter, fal, deepinfra).
                def vid_handler(request: httpx.Request) -> httpx.Response:
                    url = str(request.url)
                    if "api.x.ai" in url and request.method == "POST":
                        return httpx.Response(200, json={"request_id": "xreq1"})
                    if "api.x.ai" in url and request.method == "GET":
                        return httpx.Response(
                            200,
                            json={
                                "status": "done",
                                "video": {"url": "https://cdn.x.ai/v.mp4", "duration": 5},
                            },
                        )
                    if "openrouter.ai" in url and request.method == "POST":
                        return httpx.Response(200, json={"id": "orv1"})
                    if "openrouter.ai" in url and request.method == "GET":
                        return httpx.Response(200, json={"status": "completed"})
                    if "queue.fal.run" in url:
                        return httpx.Response(200, json={"video": {"url": "https://cdn.fal.media/v.mp4"}})
                    if "deepinfra.com" in url and request.method == "POST":
                        return httpx.Response(200, json={"id": "div1", "status": "completed"})
                    return httpx.Response(404)

                orig_client = httpx.Client

                def patched_vid_client(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(vid_handler)
                    return orig_client(*args, **kwargs)

                save_env_value("XAI_API_KEY", "xai-v")
                save_env_value("OPENROUTER_API_KEY", "or-v")
                save_env_value("FAL_KEY", "fal-v")
                save_env_value("DEEPINFRA_API_KEY", "di-v")
                saved_mp4 = Path(tmp) / "out.mp4"
                saved_mp4.write_bytes(b"mp4")
                with (
                    mock.patch("muse.media.video.httpx.Client", side_effect=patched_vid_client),
                    mock.patch("muse.media.video.save_url_video", return_value=saved_mp4),
                ):
                    for pname, mod_id in (
                        ("xai", "grok-imagine-video"),
                        ("openrouter", "minimax/hailuo-3-max"),
                        ("fal", "pixverse-v6"),
                        ("deepinfra", "wan/t2v"),
                    ):
                        vres = video.video_generate("cat waving", provider=pname, model=mod_id, duration=4)
                        self.assertTrue(vres["success"], f"{pname} video failed: {vres}")
                        self.assertEqual(vres["provider"], pname)

    def test_vision_blocks_secret_and_ssrf_and_analyzes_local_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                secret = Path(tmp) / ".env"
                secret.write_text("API_KEY=123\n", encoding="utf-8")
                res_secret = json.loads(asyncio.run(vision.vision_analyze_tool(str(secret))))
                self.assertFalse(res_secret["success"])

                res_ssrf = json.loads(
                    asyncio.run(vision.vision_analyze_tool("http://169.254.169.254/latest/meta-data/"))
                )
                self.assertFalse(res_ssrf["success"])

                img = Path(tmp) / "cat.png"
                img.write_bytes(_make_png_bytes())

                async def fake_aux(**kwargs):
                    return {
                        "model": "gpt-4o",
                        "choices": [{"message": {"content": "An orange square cat icon."}}],
                    }

                with mock.patch.object(vision, "async_call_llm", side_effect=fake_aux):
                    ok_res = json.loads(asyncio.run(vision.vision_analyze_tool(str(img), "describe")))
                self.assertTrue(ok_res["success"])
                self.assertIn("orange", ok_res["analysis"])

    def test_stt_secret_guard_error_prefixes_custom_provider_and_6_cloud_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                env_file = Path(tmp) / ".env"
                env_file.write_text("SECRET=1\n", encoding="utf-8")
                blocked = stt.transcribe_audio(str(env_file))
                self.assertFalse(blocked["success"])
                self.assertIn("secret", blocked["error"].lower())

                wav = Path(tmp) / "clip.wav"
                wav.write_bytes(b"RIFF0000WAVE")

                save_config({"stt": {"enabled": False}})
                disabled = stt.transcribe_audio(str(wav))
                self.assertFalse(disabled["success"])
                self.assertTrue(disabled["error"].startswith("STT is disabled"))

                save_config({"stt": {"enabled": True, "provider": "local"}})
                with mock.patch.object(stt, "_has_faster_whisper", return_value=False):
                    no_local = stt.transcribe_audio(str(wav))
                self.assertFalse(no_local["success"])
                self.assertTrue(no_local["error"].startswith("No installed local STT"))

                save_config({"stt": {"enabled": True}})
                with (
                    mock.patch.object(stt, "_has_faster_whisper", return_value=False),
                    mock.patch.dict(stt._CUSTOM_PROVIDERS, {}, clear=True),
                    mock.patch.dict(
                        os.environ,
                        {
                            "OPENAI_API_KEY": "",
                            "VOICE_TOOLS_OPENAI_KEY": "",
                            "GROQ_API_KEY": "",
                            "MISTRAL_API_KEY": "",
                            "XAI_API_KEY": "",
                            "ELEVENLABS_API_KEY": "",
                            "DEEPINFRA_API_KEY": "",
                            "MUSE_LOCAL_STT_COMMAND": "",
                            "HERMES_LOCAL_STT_COMMAND": "",
                            "HERMES_STT_COMMAND": "",
                        },
                    ),
                ):
                    none_res = stt.transcribe_audio(str(wav))
                self.assertFalse(none_res["success"])
                self.assertTrue(none_res["error"].startswith("No STT provider"))

                # Custom TranscriptionProvider (SenseVoice drop-in contract)
                class FakeSenseVoice(stt.TranscriptionProvider):
                    @property
                    def name(self) -> str:
                        return "sensevoice-test"

                    def is_available(self) -> bool:
                        return True

                    def transcribe(self, file_path: str, **extra: Any) -> dict[str, Any]:
                        return {"success": True, "transcript": "你好世界", "provider": self.name}

                with mock.patch.dict(stt._CUSTOM_PROVIDERS, {}, clear=True):
                    stt.register_transcription_provider(FakeSenseVoice())
                    save_config({"stt": {"enabled": True, "provider": "sensevoice-test"}})
                    sv_res = stt.transcribe_audio(str(wav))
                self.assertTrue(sv_res["success"])
                self.assertEqual(sv_res["transcript"], "你好世界")

                # Local command via MUSE_LOCAL_STT_COMMAND
                cmd = f'"{sys.executable}" -c "print(\'from-local-cmd\')"'
                save_config({"stt": {"enabled": True, "provider": "local_command"}})
                with mock.patch.dict(os.environ, {"MUSE_LOCAL_STT_COMMAND": cmd}):
                    cmd_res = stt.transcribe_audio(str(wav))
                self.assertTrue(cmd_res["success"])
                self.assertEqual(cmd_res["transcript"], "from-local-cmd")

                # All 6 cloud STT backends
                seen_urls: list[str] = []

                def stt_handler(request: httpx.Request) -> httpx.Response:
                    seen_urls.append(str(request.url))
                    return httpx.Response(200, json={"text": f"ok:{request.url.host}"})

                orig_client = httpx.Client

                def patched_client(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(stt_handler)
                    return orig_client(*args, **kwargs)

                env_keys = {
                    "OPENAI_API_KEY": "k1",
                    "GROQ_API_KEY": "k2",
                    "MISTRAL_API_KEY": "k3",
                    "XAI_API_KEY": "k4",
                    "ELEVENLABS_API_KEY": "k5",
                    "DEEPINFRA_API_KEY": "k6",
                }
                with (
                    mock.patch.dict(os.environ, env_keys),
                    mock.patch("muse.media.stt.httpx.Client", side_effect=patched_client),
                ):
                    for cloud_name in ("openai", "groq", "mistral", "xai", "elevenlabs", "deepinfra"):
                        save_config({"stt": {"enabled": True, "provider": cloud_name}})
                        cres = stt.transcribe_audio(str(wav))
                        self.assertTrue(cres["success"], f"{cloud_name} failed: {cres}")
                        self.assertEqual(cres["provider"], cloud_name)
                self.assertEqual(len(seen_urls), 6)

    def test_tts_openai_and_secret_path_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                secret_out = Path(tmp) / ".env"
                bad = tts.text_to_speech("hello", output_path=str(secret_out))
                self.assertFalse(bad["success"])

                orig_client = httpx.Client

                def patched_client(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(
                        lambda req: httpx.Response(200, content=b"ID3mp3bytes")
                    )
                    return orig_client(*args, **kwargs)

                save_env_value("OPENAI_API_KEY", "sk-tts")
                with mock.patch("muse.media.tts.httpx.Client", side_effect=patched_client):
                    res = tts.text_to_speech("hello muse", provider="openai")
                self.assertTrue(res["success"])
                self.assertTrue(Path(res["file_path"]).is_file())
                self.assertTrue(res["media_tag"].startswith("MEDIA:"))


class CronSubsystemTest(unittest.TestCase):
    def test_schedule_parsing_crud_and_cronjob_manage_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                s_cron = cron_jobs.parse_schedule("30 8 * * *")
                self.assertEqual(s_cron["kind"], "cron")
                self.assertEqual(s_cron["expr"], "30 8 * * *")

                s_int = cron_jobs.parse_schedule("every 25m")
                self.assertEqual(s_int["kind"], "interval")
                self.assertEqual(s_int["minutes"], 25)
                self.assertIsNotNone(cron_jobs.compute_next_run(s_int))

                res = json.loads(
                    _handle_cronjob_manage(
                        {
                            "action": "create",
                            "name": "喝水提醒",
                            "prompt": "提醒用户喝水",
                            "schedule": {"kind": "interval", "minutes": 60},
                            "enabled_toolsets": ["companion", "web"],
                        },
                        chat_id="chat-main",
                    )
                )
                self.assertTrue(res["success"])
                job = res["job"]
                self.assertEqual(job["deliver"], "origin")
                self.assertEqual(job["origin"], {"platform": "companion", "chat_id": "chat-main"})
                self.assertEqual(job["schedule"]["minutes"], 60)
                self.assertEqual(job["enabled_toolsets"], ["companion", "web"])

                listed = json.loads(_handle_cronjob_manage({"action": "list"}))
                self.assertEqual(len(listed["jobs"]), 1)

                cron_jobs.pause_job(job["id"], reason="paused")
                self.assertFalse(cron_jobs.get_job(job["id"])["enabled"])
                cron_jobs.resume_job(job["id"])
                self.assertTrue(cron_jobs.get_job(job["id"])["enabled"])

    def test_self_trigger_loopback_exemption_and_ssrf_enforcement(self):
        base = "http://127.0.0.1:8766"
        self.assertTrue(
            is_exempt_self_trigger_url(
                "http://127.0.0.1:8766/api/webui/companion/triggers/abc123/state?key=secret",
                self_base_url=base,
            )
        )
        self.assertFalse(is_exempt_self_trigger_url("http://127.0.0.1:8766/api/other", self_base_url=base))
        self.assertFalse(
            is_exempt_self_trigger_url(
                "http://127.0.0.1:9999/api/webui/companion/triggers/abc123/state?key=secret",
                self_base_url=base,
            )
        )
        self.assertFalse(
            is_exempt_self_trigger_url(
                "http://127.0.0.1:8766/api/webui/companion/triggers/../secret/state",
                self_base_url=base,
            )
        )

        state_box = {"body": "initial payload"}

        class TriggerHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/webui/companion/triggers/t1/state"):
                    raw = state_box["body"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(403)
                    self.end_headers()

            def log_message(self, fmt, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TriggerHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        prompts_seen: list[str] = []
        delivered: list[str] = []

        async def fake_run_job(job: dict[str, Any], prompt: str) -> str:
            prompts_seen.append(prompt)
            return "[SILENT]" if len(prompts_seen) == 1 else "发现变更，已通知！"

        async def fake_deliver(job: dict[str, Any], text: str) -> None:
            delivered.append(text)

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                with muse_home_override(tmp):
                    self_base = f"http://127.0.0.1:{port}"
                    scheduler = CronScheduler(
                        run_job_fn=fake_run_job,
                        deliver_fn=fake_deliver,
                        self_base_url=self_base,
                    )
                    job = cron_jobs.create_job(
                        prompt="检查变化",
                        schedule="0 4 * * 1",
                        name="自动触发：测试",
                        deliver="companion:c1",
                        attach_to_session=True,
                        monitor_url=f"{self_base}/api/webui/companion/triggers/t1/state?key=k1",
                    )

                    # Even with a bogus HTTP_PROXY set, self-trigger fetch uses trust_env=False and succeeds!
                    with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1"}):
                        r1 = await scheduler.run_job(cron_jobs.get_job(job["id"]))
                        self.assertEqual(r1["status"], "ok")
                        self.assertTrue(r1["silent"])
                        self.assertIn("Monitor Baseline", prompts_seen[0])
                        self.assertEqual(delivered, [])  # [SILENT] suppressed delivery!

                        # Unchanged output -> agent run suppressed!
                        r2 = await scheduler.run_job(cron_jobs.get_job(job["id"]))
                        self.assertEqual(r2["status"], "no_change")
                        self.assertEqual(len(prompts_seen), 1)

                        # Changed output -> MONITOR CHANGE DETECTED + diff injected!
                        state_box["body"] = "updated payload v2"
                        r3 = await scheduler.run_job(cron_jobs.get_job(job["id"]))
                        self.assertEqual(r3["status"], "ok")
                        self.assertFalse(r3["silent"])
                        self.assertIn("## MONITOR CHANGE DETECTED", prompts_seen[1])
                        self.assertIn("updated payload v2", prompts_seen[1])
                        self.assertEqual(delivered, ["发现变更，已通知！"])

                    # Verify report markdown files exist in automations.py format
                    out_dir = cron_jobs.get_cron_output_dir() / job["id"]
                    reports = list(out_dir.glob("*.md"))
                    self.assertGreaterEqual(len(reports), 1)

                    # Non-exempt loopback URL MUST be blocked by SSRF!
                    bad_job = cron_jobs.create_job(
                        prompt="evil",
                        schedule="0 4 * * 1",
                        monitor_url=f"{self_base}/admin/internal",
                    )
                    r_bad = await scheduler.run_job(cron_jobs.get_job(bad_job["id"]))
                    self.assertEqual(r_bad["status"], "error")
                    self.assertIn("SSRF", r_bad["error"])

        asyncio.run(run())

    def test_trigger_job_instant_cross_thread_wakeup(self):
        fired = asyncio.Event()
        fired_at: list[float] = []

        async def fake_run(job: dict[str, Any], prompt: str) -> str:
            fired_at.append(time.monotonic())
            fired.set()
            return "ran"

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                with muse_home_override(tmp):
                    job = cron_jobs.create_job(
                        prompt="webhook job",
                        schedule="0 4 * * 1",
                        name="Webhook",
                        deliver="local",
                    )
                    scheduler = CronScheduler(run_job_fn=fake_run, poll_interval_s=30.0)
                    await scheduler.start()
                    try:
                        await asyncio.sleep(0.05)
                        started = time.monotonic()

                        def _cross_thread_trigger():
                            with muse_home_override(tmp):
                                cron_jobs.trigger_job(job["id"])

                        t = threading.Thread(target=_cross_thread_trigger)
                        t.start()
                        t.join(timeout=2.0)
                        await asyncio.wait_for(fired.wait(), timeout=2.0)
                        elapsed = fired_at[0] - started
                        self.assertLess(elapsed, 0.5, f"Expected millisecond wake, took {elapsed:.3f}s")
                    finally:
                        await scheduler.stop()

        asyncio.run(run())


class Phase2RewiringAndRegistryIntegrationTest(unittest.TestCase):
    def test_builtin_registry_has_20_core_tools_and_engine_reload_mcp(self) -> None:
        from muse.engine import ChatTurnEngine
        from muse.tools import create_default_tool_registry

        reg = create_default_tool_registry()
        names = {e.name for e in reg.list_entries(check_availability=False)}
        self.assertEqual(len(names), 21)
        self.assertIsNotNone(reg.get("tool_search"))
        for expected in (
            "deep_research",
            "vision_analyze",
            "image_generate",
            "video_generate",
            "text_to_speech",
            "cronjob_manage",
        ):
            self.assertIn(expected, names)

        reloaded = []
        shutdowns = []

        class FakeMCP:
            async def reload_mcp(self):
                reloaded.append(True)
                return {"ok": True, "message": "", "failed": [], "added": ["demo"]}

            async def shutdown(self):
                shutdowns.append(True)

        class DummySink:
            store = None

        async def run():
            eng = ChatTurnEngine(DummySink(), mcp_manager=FakeMCP())
            res = await eng.reload_mcp()
            self.assertTrue(res["ok"])
            self.assertEqual(res["added"], ["demo"])
            await eng.stop()

        asyncio.run(run())
        self.assertEqual(reloaded, [True])
        self.assertEqual(shutdowns, [True])

    def test_companion_7_video_backends_sensevoice_and_connections_async(self) -> None:
        from companion.modules import connections, stt_sensevoice, video_backends
        from muse.mcp import AsyncMCPManager, set_mcp_manager
        from muse.media.stt import TranscriptionProvider, get_transcription_provider
        from muse.media.video import VideoGenProvider, list_video_gen_providers

        for cls in video_backends.BACKENDS:
            self.assertTrue(issubclass(cls, VideoGenProvider))
        video_backends.register_native()
        all_video_names = {p.name for p in list_video_gen_providers()}
        for expected in (
            "xai",
            "openrouter",
            "fal",
            "deepinfra",
            "companion-minimax",
            "companion-ark",
            "companion-dashscope",
        ):
            self.assertIn(expected, all_video_names)

        sv = get_transcription_provider("sensevoice")
        self.assertIsNotNone(sv)
        self.assertIsInstance(sv, TranscriptionProvider)
        self.assertEqual(sv.name, stt_sensevoice.NAME)

        async def _fake_opener(stack, name, cfg):
            del stack, name, cfg
            raw = [
                ("search_repos", "Search repositories", {"type": "object", "properties": {}}),
                ("create_issue", "Create an issue", {"type": "object", "properties": {}}),
            ]
            return object(), raw

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                with muse_home_override(tmp):
                    save_config(
                        {
                            "mcp_servers": {
                                "demo": {"command": "echo", "args": ["ok"], "enabled": True}
                            }
                        }
                    )
                    mgr = AsyncMCPManager(opener=_fake_opener, drain_timeout=1.0)
                    set_mcp_manager(mgr)
                    try:
                        probe = await connections.test_server_async("demo")
                        self.assertTrue(probe["ok"])
                        self.assertEqual(probe["tool_names"], ["create_issue", "search_repos"])

                        reloaded = await connections.apply_live_async()
                        self.assertTrue(reloaded["ok"])
                        self.assertIn("demo", connections._runtime())
                        self.assertEqual(
                            connections._registered_tools("demo"),
                            ["create_issue", "search_repos"],
                        )
                    finally:
                        await mgr.shutdown()
                        set_mcp_manager(None)

        asyncio.run(run())

    def test_audit_fixes_native_providers_jobs_schema_ctx_origin_lazy_sv_and_redact(self) -> None:
        from companion.modules import stt_sensevoice, video_backends
        from muse.context import TurnExecutionContext
        from muse.cron import jobs as cron_jobs
        from muse.cron.scheduler import CronScheduler
        from muse.media import stt as stt_mod
        from muse.media.video import VideoGenProvider
        from muse.tools import create_default_tool_registry

        # 1) Native video & SenseVoice providers satisfy Muse ABCs and register cleanly.
        registered_v: list[Any] = []
        registered_s: list[Any] = []

        class FakePluginCtx:
            def register_video_gen_provider(self, p: Any) -> None:
                registered_v.append(p)

            def register_transcription_provider(self, p: Any) -> None:
                registered_s.append(p)

        video_backends.register(FakePluginCtx())
        stt_sensevoice.register(FakePluginCtx())
        self.assertEqual(len(registered_v), 3)
        self.assertTrue(all(isinstance(p, VideoGenProvider) for p in registered_v))
        self.assertEqual(len(registered_s), 1)
        self.assertIsInstance(registered_s[0], stt_mod.TranscriptionProvider)

        with tempfile.TemporaryDirectory() as tmp:
            with muse_home_override(tmp):
                # 2) jobs.json repeat dict schema + trigger_job manual_run_* + finite repeat.
                once_job = cron_jobs.create_job(prompt="ping", schedule="in 10m", name="Once")
                self.assertEqual(once_job["repeat"], {"times": 1, "completed": 0})
                self.assertIn("schedule_display", once_job)
                self.assertIn("monitor_state", once_job)

                rep_job = cron_jobs.create_job(
                    prompt="twice", schedule="every 15m", repeat=2, name="Twice"
                )
                self.assertEqual(rep_job["repeat"], {"times": 2, "completed": 0})
                trig = cron_jobs.trigger_job(rep_job["id"], extra_prompt="run now note")
                self.assertIsNotNone(trig)
                self.assertEqual(trig["manual_run_at"], trig["next_run_at"])
                self.assertEqual(trig["manual_run_prompt"], "run now note")

                after1 = cron_jobs.mark_job_run(rep_job["id"], success=True, output="ok1")
                self.assertTrue(after1["enabled"])
                self.assertEqual(after1["repeat"], {"times": 2, "completed": 1})
                self.assertNotIn("manual_run_at", after1)
                self.assertNotIn("manual_run_prompt", after1)

                after2 = cron_jobs.mark_job_run(rep_job["id"], success=True, output="ok2")
                self.assertFalse(after2["enabled"])
                self.assertEqual(after2["state"], "completed")
                self.assertEqual(after2["repeat"], {"times": 2, "completed": 2})

                # 3) ToolRegistry.dispatch passes ctx -> cronjob_manage derives origin.
                reg = create_default_tool_registry()
                ctx = TurnExecutionContext(chat_id="helper-chat-7", facing_chat_id="main-chat")
                raw = asyncio.run(
                    reg.dispatch(
                        "cronjob_manage",
                        {"action": "create", "prompt": "drink water", "schedule": "in 10m"},
                        ctx=ctx,
                    )
                )
                created = json.loads(raw)["job"]
                self.assertEqual(created["deliver"], "origin")
                self.assertEqual(
                    created["origin"], {"platform": "companion", "chat_id": "helper-chat-7"}
                )

                # 4) transcribe_audio lazily registers SenseVoice in native mode.
                audio = Path(tmp) / "voice.wav"
                audio.write_bytes(b"RIFFdemo")
                save_config({"stt": {"enabled": True, "provider": "sensevoice"}})
                stt_mod._CUSTOM_PROVIDERS.pop("sensevoice", None)
                with (
                    mock.patch.object(stt_sensevoice, "missing", return_value=[]),
                    mock.patch.object(
                        stt_sensevoice,
                        "transcribe",
                        return_value={"success": True, "transcript": "你好", "provider": "sensevoice"},
                    ),
                ):
                    res = stt_mod.transcribe_audio(str(audio))
                self.assertTrue(res["success"])
                self.assertEqual(res["transcript"], "你好")

                # 5) monitor_url query key is redacted from error strings & jobs.json last_error.
                secret_val = "SUPER_SECRET_TRIG_987654"
                bad_url = f"http://127.0.0.1:1/api/webui/companion/triggers/t1/state?key={secret_val}"
                mon_job = cron_jobs.create_job(
                    prompt="check",
                    schedule="every 5m",
                    monitor_url=bad_url,
                    name="SecretRedactionTest",
                )
                sched = CronScheduler(self_base_url="http://127.0.0.1:1")
                out = asyncio.run(sched.run_job(mon_job))
                self.assertEqual(out["status"], "error")
                self.assertNotIn(secret_val, out["error"])
                stored = cron_jobs.get_job(mon_job["id"])
                self.assertNotIn(secret_val, str(stored.get("last_error") or ""))


if __name__ == "__main__":
    unittest.main()
