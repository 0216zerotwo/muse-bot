"""Security tests: secrets_guard, exfiltration blocking, vault env denylist, widgets, SSRF & body caps."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import unittest
from pathlib import Path
from unittest import mock

import httpx
from starlette.testclient import TestClient

from _loader import chat_store, load, temp_dir
from test_auth_devices import _companion_pkg


class SecretsGuardTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.guard, self.widgets, self.creations, self.avatar_make) = load(
            "secrets_guard", "widgets", "creations", "avatar_make"
        )
        pkg = _companion_pkg()
        self.media_mod = importlib.import_module(f"{pkg}.media")
        self.media = self.media_mod.MediaStore(self.folder)

    def test_blocks_secret_files_across_local_and_browser_tools_but_allows_env_example(self):
        check = self.guard.check
        for tool, args in (
            ("read_file", {"path": "/opt/hermes/.env"}),
            ("terminal", {"command": "cat ~/.hermes/.env.production"}),
            ("read_file", {"path": "C:/Users/me/.codex/auth.json"}),
            ("read_file", {"path": "companion/devices.json"}),
            ("read_file", {"path": "companion/triggers.json"}),
            ("read_file", {"path": "companion/push/vapid_private.pem"}),
            ("read_file", {"path": "companion/push/subscriptions.json"}),
            ("read_file", {"path": "/home/u/.ssh/id_ed25519"}),
            ("browser_exec", {"code": "goto_url('file:///etc/passwd')"}),
            ("browser_navigate", {"url": "file:///opt/data/.env"}),
            ("mcp__browser__browser_navigate", {"url": "file:///C:/hermes-home/.env"}),
            ("mcp__browser__browser_file_upload", {"paths": ["/opt/data/.env"]}),
            ("write_file", {"path": ".env", "content": "X=1"}),
            ("edit_file", {"path": "companion/devices.json", "old_string": "[]", "new_string": "[1]"}),
        ):
            with self.subTest(tool=tool, args=args):
                self.assertEqual(check(tool, args), self.guard.MESSAGE)

        # Public URLs containing /vault or /cookies, clicking elements mentioning /Cookies, and
        # writing ordinary docs whose body mentions .env / ~/.ssh/config must NEVER false-positive.
        for tool, args in (
            ("read_file", {"path": "deploy/.env.example"}),
            ("mcp__browser__browser_navigate", {"url": "https://example.com"}),
            ("browser_navigate", {"url": "https://github.com/hashicorp/vault"}),
            ("browser_navigate", {"url": "https://example.com/cookies"}),
            ("mcp__browser__browser_click", {"element": "Accept /Cookies"}),
            ("browser_exec", {"code": "goto_url('https://github.com/hashicorp/vault')\nprint(page_text())"}),
            ("write_file", {"path": "README.md", "content": "Copy .env.example to .env and check ~/.ssh/config"}),
            ("edit_file", {"path": "docs/setup.md", "old_string": "a", "new_string": "Put keys in .env or auth.json"}),
        ):
            with self.subTest(tool=tool, args=args):
                self.assertIsNone(check(tool, args))

        self.assertFalse(self.guard.is_secret_path("https://github.com/hashicorp/vault"))
        self.assertFalse(self.guard.is_secret_path("https://example.com/cookies"))
        self.assertFalse(self.guard.is_secret_path("line1\n/opt/other\n" * 100))
        self.assertEqual(check("terminal", {"command": "hermes gateway restart"}), self.guard.SELF_CHANGE_MESSAGE)
        self.assertEqual(
            check("write_file", {"path": "hermes-home/config.yaml", "content": "model: test"}),
            self.guard.SELF_CHANGE_MESSAGE,
        )

    def test_prevents_secret_exfiltration_via_media_widgets_creations_and_avatar_reference(self):
        env_file = self.folder / ".env"
        env_file.write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
        devices_file = self.folder / "devices.json"
        devices_file.write_text("[]", encoding="utf-8")
        ssh_dir = self.folder / ".ssh"
        ssh_dir.mkdir()
        ssh_html = ssh_dir / "id_rsa.html"
        ssh_html.write_text("<h1>secret</h1>", encoding="utf-8")
        safe_doc = self.folder / "report.md"
        safe_doc.write_text("# 周报", encoding="utf-8")
        safe_html = self.folder / "chart.html"
        safe_html.write_text("<div>ok</div>", encoding="utf-8")

        self.assertIsNone(self.media.stage_file(env_file))
        self.assertIsNone(self.media.stage_file(devices_file))
        staged = self.media.stage_file(safe_doc)
        self.assertIsNotNone(staged)

        self.assertIsNone(self.creations.import_file(env_file, key="k1", title="env", origin="chat"))
        self.assertIsNotNone(self.creations.import_file(safe_doc, key="k2", title="周报", origin="chat"))

        self.assertIsNone(self.widgets.stage(ssh_html))
        widget_name = self.widgets.stage(safe_html)
        self.assertIsNotNone(widget_name)
        rendered = self.widgets.page(widget_name).decode("utf-8")
        self.assertIn("navigator.userActivation", rendered)

        with self.assertRaises(ValueError):
            self.avatar_make.make("小企鹅", reference=str(env_file))

    def test_signed_paths_block_traversal_and_cross_prefix_reuse(self):
        secret_outside = self.folder / "outside.txt"
        secret_outside.write_text("nope", encoding="utf-8")
        self.assertIsNone(self.media_mod.child_file(self.media.root, "../outside.txt"))
        self.assertIsNone(self.media_mod.child_file(self.media.root, ".hidden"))

        staged = self.media.stage_file(secret_outside)
        parts = staged["url"].split("/")
        sig, payload = parts[-2], parts[-1]
        self.assertIsNotNone(self.media.verify("/api/media", sig, payload))
        self.assertIsNone(self.media.verify("/api/webui/companion/avatars/file", sig, payload))
        self.assertIsNone(self.media.verify("/api/webui/companion/widgets", sig, payload))


class VaultEnvDenylistTest(unittest.TestCase):
    def setUp(self):
        _, (self.vault,) = load("vault")

    def test_rejects_system_and_runtime_env_vars(self):
        for bad in (
            "PATH", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "HTTPS_PROXY", "HTTP_PROXY",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HERMES_HOME", "COMPANION_AUTH_SECRET", "SSH_AUTH_SOCK",
        ):
            with self.subTest(env=bad):
                self.assertFalse(self.vault.is_allowed_env_name(bad))
                res = json.loads(self.vault._handle_request({"kind": "api_key", "service": "GitHub", "env": bad}))
                self.assertFalse(res["success"])
        self.assertTrue(self.vault.is_allowed_env_name("GITHUB_TOKEN"))


class SsrfAndStreamCapTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.net_safety, self.triggers, self.push) = load("net_safety", "triggers", "push")

    def test_blocks_loopback_private_metadata_and_ipv6_mapped_urls(self):
        for unsafe in (
            "http://127.0.0.1:9333/json/version",
            "http://localhost:8766/webui/bootstrap",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.200/latest/meta-data/",
            "http://[::1]:8766/",
            "http://[::ffff:127.0.0.1]:9333/",
            "http://[fd00:ec2::254]/latest/",
            "http://192.168.1.10/admin",
            "http://100.64.0.5/internal",
            "file:///etc/passwd",
            "https://user:pass@93.184.216.34/",
        ):
            with self.subTest(url=unsafe):
                self.assertFalse(self.net_safety._builtin_is_safe_url(unsafe))

        self.assertTrue(self.net_safety._builtin_is_safe_url("https://93.184.216.34/image.png"))

    def test_redirect_to_loopback_is_blocked_and_triggers_push_validate_urls(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "93.184.216.34":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1:9333/json/version"})
            return httpx.Response(200, content=b"secret")

        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client
        with mock.patch.object(httpx, "Client", side_effect=lambda **kw: orig_client(transport=transport, **kw)):
            with mock.patch.object(self.net_safety, "is_safe_url", side_effect=self.net_safety._builtin_is_safe_url):
                with self.assertRaises(ValueError):
                    self.net_safety.fetch_bytes("https://93.184.216.34/start", max_bytes=1024)

        with mock.patch.object(self.net_safety, "is_safe_url", side_effect=self.net_safety._builtin_is_safe_url):
            with self.assertRaises(ValueError):
                self.triggers.add_trigger("page", "内网", "看变化", url="http://127.0.0.1:9333/json/list")
            with self.assertRaises(ValueError):
                self.push.add_subscription({
                    "endpoint": "https://127.0.0.1/push",
                    "keys": {"p256dh": "a", "auth": "b"},
                })

    def test_bounded_request_streams_reject_oversized_payloads(self):
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        approvals_mod = importlib.import_module(f"{pkg}.approvals")
        server_mod = importlib.import_module(f"{pkg}.server")
        folder = temp_dir("companion-cap-")

        class Adapter:
            tokens = auth_mod.TokenIssuer(None)
            devices = devices_mod.Devices(folder)
            store = chat_store(folder)
            media = media_mod.MediaStore(folder)
            socket_devices = {}
            _turns = {}
            _subs = {}
            web_dist = folder / "none"
            approvals = None

            def keep_attachment(self, body, mime, name):
                return "ok"

        adapter = Adapter()
        adapter.approvals = approvals_mod.Approvals(adapter, folder)
        tok = adapter.tokens.issue("d1", purpose="api")
        app = server_mod.build_app(adapter)

        orig_limit = server_mod._ATTACHMENT_LIMIT
        orig_hook_limit = server_mod._HOOK_STREAM_LIMIT
        server_mod._ATTACHMENT_LIMIT = 1024
        server_mod._HOOK_STREAM_LIMIT = 2048
        self.addCleanup(setattr, server_mod, "_ATTACHMENT_LIMIT", orig_limit)
        self.addCleanup(setattr, server_mod, "_HOOK_STREAM_LIMIT", orig_hook_limit)

        with TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8766") as client:
            too_big = client.post(
                "/api/attachments",
                content=b"x" * 2048,
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "image/png"},
            )
            self.assertEqual(too_big.status_code, 413)
            hook_big = client.post("/hooks/t1?key=k", content=b"y" * 4096)
            self.assertEqual(hook_big.status_code, 413)


if __name__ == "__main__":
    unittest.main()
