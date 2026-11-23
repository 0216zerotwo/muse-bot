"""Authentication, paired devices, DNS-rebinding/Origin guards, and pairing throttling tests."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from _loader import ROOT, chat_store, ensure_hermes_stubs, temp_dir


def _companion_pkg():
    ensure_hermes_stubs()
    name = "companion_auth_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        sys.modules[name] = importlib.util.module_from_spec(spec)
    return name


class TokenIssuerTest(unittest.TestCase):
    def setUp(self):
        pkg = _companion_pkg()
        self.auth = importlib.import_module(f"{pkg}.auth")

    def test_password_and_local_rules(self):
        issuer = self.auth.TokenIssuer("s3cret")
        self.assertTrue(issuer.password_ok(None, local=True))
        self.assertFalse(issuer.password_ok("wrong", local=True))
        self.assertFalse(issuer.password_ok(None, local=False))
        self.assertTrue(issuer.password_ok("s3cret", local=False))
        self.assertFalse(issuer.password_ok("wrong", local=False))

    def test_per_device_cap_never_evicts_other_devices_and_reuses_polling_tokens(self):
        issuer = self.auth.TokenIssuer(None)
        keep_ws = issuer.issue("phone-a", reuse=True, purpose="ws")
        keep_api = issuer.issue("phone-a", reuse=True, purpose="api")
        self.assertNotEqual(keep_ws, keep_api)
        # Rapid polling within the reuse window returns the same pair instead of bloating tokens.
        self.assertEqual(issuer.issue("phone-a", reuse=True, purpose="ws"), keep_ws)
        self.assertEqual(issuer.issue("phone-a", reuse=True, purpose="api"), keep_api)

        # Flooding tokens for another owner ("phone-b" or None) never evicts phone-a's tokens.
        for _ in range(self.auth._MAX_PER_OWNER + 20):
            issuer.issue("phone-b")
            issuer.issue(None)
        self.assertTrue(issuer.valid(keep_ws))
        self.assertTrue(issuer.valid(keep_api))
        self.assertEqual(issuer.device_of(keep_ws), "phone-a")

        # Revoking phone-b leaves phone-a intact; revoking phone-a invalidates its tokens immediately.
        issuer.revoke_device("phone-b")
        self.assertTrue(issuer.valid(keep_ws))
        issuer.revoke_device("phone-a")
        self.assertFalse(issuer.valid(keep_ws))
        self.assertFalse(issuer.valid(keep_api))


class DevicesTest(unittest.TestCase):
    def setUp(self):
        pkg = _companion_pkg()
        self.devices_mod = importlib.import_module(f"{pkg}.devices")
        self.folder = temp_dir("companion-devices-")
        self.devices = self.devices_mod.Devices(self.folder)

    def test_pairing_one_time_code_and_revocation(self):
        self.assertFalse(self.devices.any_paired())
        code = self.devices.new_code()["code"]
        paired = self.devices.pair(code.lower().replace("-", " "), " 我的 iPhone ")
        self.assertIsNotNone(paired)
        self.assertTrue(self.devices.any_paired())
        # Same code cannot be reused.
        self.assertIsNone(self.devices.pair(code, "again"))
        self.assertEqual(self.devices.check(paired["token"]), paired["device_id"])
        listed = self.devices.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "我的 iPhone")
        self.assertNotIn("token_hash", listed[0])
        self.assertTrue(self.devices.remove(paired["device_id"]))
        self.assertIsNone(self.devices.check(paired["token"]))

    def test_per_ip_throttling_does_not_lock_out_owner_on_another_ip(self):
        code = self.devices.new_code()["code"]
        for _ in range(self.devices_mod._MAX_IP_FAILURES):
            self.assertIsNone(self.devices.pair("WRNG-2345", "bot", client_ip="203.0.113.9"))
        self.assertTrue(self.devices.locked("203.0.113.9"))
        # Attacker IP cannot redeem even the right code while locked.
        self.assertIsNone(self.devices.pair(code, "bot", client_ip="203.0.113.9"))
        # Legitimate owner on another IP is NOT locked out.
        self.assertFalse(self.devices.locked("198.51.100.4"))
        paired = self.devices.pair(code, "我的手机", client_ip="198.51.100.4")
        self.assertIsNotNone(paired)


class ServerOriginAndRebindingTest(unittest.TestCase):
    def setUp(self):
        pkg = _companion_pkg()
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        approvals_mod = importlib.import_module(f"{pkg}.approvals")
        self.server_mod = importlib.import_module(f"{pkg}.server")
        folder = temp_dir("companion-srv-")

        class FakeAdapter:
            def __init__(self):
                self.tokens = auth_mod.TokenIssuer("topsecret")
                self.devices = devices_mod.Devices(folder)
                self.store = chat_store(folder)
                self.media = media_mod.MediaStore(folder)
                self.approvals = approvals_mod.Approvals(self, folder)
                self.socket_devices = {}
                self._turns = {}
                self._subs = {}
                self.web_dist = folder / "no-dist"
                self.kept = []

            def companion_name(self):
                return "伙伴"

            def attach(self, chat_id, socket):
                self._subs.setdefault(chat_id, set()).add(socket)

            def detach_all(self, socket):
                for s in self._subs.values():
                    s.discard(socket)

            def keep_attachment(self, body, mime, name):
                self.kept.append((len(body), mime, name))
                return "ref-1"

        self.adapter = FakeAdapter()
        self.app = self.server_mod.build_app(self.adapter)

    def test_bootstrap_blocks_dns_rebinding_cross_origin_and_proxy_spoofing(self):
        with TestClient(self.app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as local:
            # Legitimate local browser / Vite dev server works without password.
            ok = local.get("/webui/bootstrap")
            self.assertEqual(ok.status_code, 200)
            dev_ok = local.get("/webui/bootstrap", headers={"Origin": "http://localhost:5173"})
            self.assertEqual(dev_ok.status_code, 200)

            # DNS rebinding: loopback IP, but Host header is attacker domain -> denied!
            rebind = local.get("/webui/bootstrap", headers={"Host": "rebind.attacker.example:8766"})
            self.assertEqual(rebind.status_code, 401)

            # Cross-origin website probing 127.0.0.1 -> 403 forbidden_origin!
            cross = local.get("/webui/bootstrap", headers={"Origin": "https://evil.example"})
            self.assertEqual(cross.status_code, 403)
            self.assertEqual(cross.json()["error"], "forbidden_origin")

            # Sandboxed opaque iframe (Origin: null) -> 403 forbidden_origin!
            opaque = local.get("/webui/bootstrap", headers={"Origin": "null"})
            self.assertEqual(opaque.status_code, 403)

            # Reverse-proxied request from loopback is never treated as local.
            proxied = local.get("/webui/bootstrap", headers={"X-Forwarded-For": "203.0.113.5"})
            self.assertEqual(proxied.status_code, 401)

    def test_pair_code_sanitizes_base_and_ignores_untrusted_forwarded_host(self):
        with TestClient(self.app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as local:
            # Local console script (companion-pair.py) can specify a clean public HTTPS base.
            res = local.post("/api/pair/code?base=https://bot.example.com/")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["link"].startswith("https://bot.example.com/companion.html#pair="))

            # Malformed base with query/fragment/userinfo is rejected back to _public_base.
            bad = local.post("/api/pair/code?base=https://user:pw@evil.example/x?y=1")
            self.assertEqual(bad.status_code, 200)
            self.assertTrue(bad.json()["link"].startswith("http://127.0.0.1:8766/companion.html#pair="))

        api_token = self.adapter.tokens.issue("dev-1", purpose="api")
        with TestClient(self.app, client=("198.51.100.77", 51234), base_url="http://203.0.113.10:8766") as remote:
            # Remote caller cannot override ?base= or spoof X-Forwarded-Host from a public peer IP.
            res = remote.post(
                "/api/pair/code?base=https://phish.example",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "X-Forwarded-Host": "spoofed.example",
                    "X-Forwarded-Proto": "https",
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["link"].startswith("http://203.0.113.10:8766/companion.html#pair="))

    def test_websocket_rejects_cross_origin_and_accepts_same_origin(self):
        tok = self.adapter.tokens.issue("dev-1", purpose="ws")
        with TestClient(self.app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            with self.assertRaises(Exception):
                with client.websocket_connect(
                    f"ws://127.0.0.1:8766/ws?token={tok}", headers={"Origin": "https://evil.example"}
                ):
                    pass
            with client.websocket_connect(
                f"ws://127.0.0.1:8766/ws?token={tok}", headers={"Origin": "http://127.0.0.1:8766"}
            ) as ws:
                ready = ws.receive_json()
                self.assertEqual(ready["event"], "ready")


if __name__ == "__main__":
    unittest.main()
