"""Golden wire-protocol and import-seam contracts for Web PWA, iPhone, and Android clients.

Locks down:
1. Exact HTTP & WebSocket route table on ``companion/server.py`` (40 HTTP routes + 2 WS routes,
   plus static root mount when ``web/dist`` exists) and the 4 signed-URL path prefixes.
2. Exact ``MUTATIONS`` action table for ``webui_request`` frames (47 mutations).
3. ``/webui/bootstrap`` local 200 payload and ``ETag`` / ``304 Not Modified`` thread-sync wire contract.
4. AST static boundary check: zero ``.py`` files in ``companion/`` import ``hermes_constants``,
   ``hermes_time``, or ``hermes_cli.config``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import unittest
from pathlib import Path

from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import TestClient

from _loader import ROOT, chat_store, temp_dir
from test_auth_devices import _companion_pkg

EXPECTED_HTTP_AND_WS_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/", ("GET",)),
    ("/ws/computer", ("WS",)),
    ("/api/webui/companion/computer", ("GET",)),
    ("/webui/bootstrap", ("GET",)),
    ("/api/pair", ("POST",)),
    ("/api/pair/code", ("POST",)),
    ("/api/webui/devices", ("GET",)),
    ("/api/sessions", ("GET",)),
    ("/api/sessions/{key}/webui-thread", ("GET",)),
    ("/api/webui/companion", ("GET",)),
    ("/api/webui/approvals", ("GET",)),
    ("/api/webui/companion/search", ("GET",)),
    ("/api/webui/approvals/grants", ("GET",)),
    ("/api/webui/companion/models", ("GET",)),
    ("/api/webui/companion/model-catalog", ("GET",)),
    ("/api/webui/companion/usage", ("GET",)),
    ("/api/webui/companion/routines", ("GET",)),
    ("/api/webui/companion/memory", ("GET",)),
    ("/api/webui/companion/memory-doc", ("GET",)),
    ("/api/webui/companion/soul", ("GET",)),
    ("/api/webui/companion/goals", ("GET",)),
    ("/api/webui/companion/triggers", ("GET",)),
    ("/api/webui/companion/feed", ("GET",)),
    ("/api/webui/companion/skills", ("GET",)),
    ("/api/webui/companion/permissions", ("GET",)),
    ("/api/webui/companion/vault", ("GET",)),
    ("/api/webui/companion/helpers", ("GET",)),
    ("/api/webui/companion/triggers/{trigger_id}/state", ("GET",)),
    ("/hooks/{trigger_id}", ("POST",)),
    ("/api/webui/companion/ideas", ("GET",)),
    ("/api/webui/companion/creations", ("GET",)),
    ("/api/webui/companion/creations/file/{sig}/{payload}", ("GET",)),
    ("/api/webui/companion/avatars", ("GET",)),
    ("/api/settings/mcp-presets", ("GET",)),
    ("/api/webui/automations", ("GET",)),
    ("/api/webui/automations/{job_id}/runs/{run_id}", ("GET",)),
    ("/api/webui/push", ("GET",)),
    ("/api/webui/companion/avatars/file/{sig}/{payload}", ("GET",)),
    ("/api/webui/companion/widgets/{sig}/{payload}", ("GET",)),
    ("/api/media/{sig}/{payload}", ("GET",)),
    ("/api/attachments", ("POST",)),
    ("/ws", ("WS",)),
)

EXPECTED_MUTATIONS: frozenset[str] = frozenset({
    "approval.decide",
    "approval.grants.revoke",
    "automation.delete",
    "automation.disable",
    "automation.enable",
    "automation.run",
    "companion.avatars.delete",
    "companion.avatars.from_creation",
    "companion.avatars.upload",
    "companion.chats.change",
    "companion.creations.hide",
    "companion.devices.remove",
    "companion.feed.change",
    "companion.goals.change",
    "companion.groups.change",
    "companion.helpers.change",
    "companion.ideas.refresh",
    "companion.memory.doc",
    "companion.memory.forget",
    "companion.memory.learn",
    "companion.memory.remember",
    "companion.memory.tidy",
    "companion.memory.undo",
    "companion.models.add",
    "companion.models.check",
    "companion.models.remove",
    "companion.models.update",
    "companion.models.vendor",
    "companion.oauth.import",
    "companion.oauth.start",
    "companion.oauth.status",
    "companion.permissions.set",
    "companion.routines.update",
    "companion.skills.change",
    "companion.soul.update",
    "companion.triggers.change",
    "companion.update",
    "companion.vault.change",
    "push.subscribe",
    "push.test",
    "push.unsubscribe",
    "settings.mcp.custom",
    "settings.mcp.disable",
    "settings.mcp.enable",
    "settings.mcp.remove",
    "settings.mcp.test",
    "settings.mcp.tools",
})

FORBIDDEN_LEGACY_IMPORTS: frozenset[str] = frozenset({
    "hermes_constants",
    "hermes_time",
    "hermes_cli.config",
    "hermes_cli.auth",
    "hermes_cli.auth_codex",
    "hermes_cli.auth_xai",
    "hermes_cli.auth_constants",
    "hermes_cli.codex_models",
    "hermes_cli.runtime_provider",
    "agent.auxiliary_client",
    "agent.models_dev",
    "agent.secret_scope",
    "agent.transcription_provider",
    "agent.vault_store",
    "agent.video_gen_provider",
    "cron",
    "cron.jobs",
    "tools.browser_vault_tool",
    "tools.image_generation_tool",
    "tools.mcp_oauth",
    "tools.mcp_tool_config",
    "tools.mcp_tool_discovery",
    "tools.mcp_tool_lifecycle",
    "tools.mcp_tool_loop",
    "tools.memory_tool",
    "tools.memory_tool_store",
    "tools.registry",
    "tools.skill_manager_tool",
    "tools.skills_tool",
    "tools.transcription_tools",
    "tools.url_safety",
    "tools.video_generation_tool",
    "tools.vision_tools",
    "tools.web_tools",
})


def _make_stub_adapter(folder: Path, *, with_web_dist: bool = False):
    pkg = _companion_pkg()
    auth_mod = importlib.import_module(f"{pkg}.auth")
    devices_mod = importlib.import_module(f"{pkg}.devices")
    media_mod = importlib.import_module(f"{pkg}.media")
    server_mod = importlib.import_module(f"{pkg}.server")
    web_dist = folder / "web-dist"
    if with_web_dist:
        web_dist.mkdir(parents=True, exist_ok=True)
        (web_dist / "companion.html").write_text("<!doctype html><title>Muse Bot</title>\n", encoding="utf-8")

    class StubAdapter:
        def __init__(self):
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

    return server_mod, StubAdapter()


class WireAndSeamContractTest(unittest.TestCase):
    def test_exact_http_and_websocket_routes_and_static_mount(self):
        folder = temp_dir("companion-wire-")
        server_mod, adapter = _make_stub_adapter(folder, with_web_dist=True)
        compressed = server_mod.build_app(adapter)
        starlette_app = compressed.app

        actual_routes: list[tuple[str, tuple[str, ...]]] = []
        static_mounts: list[tuple[str, str]] = []
        for route in starlette_app.routes:
            if isinstance(route, WebSocketRoute):
                actual_routes.append((route.path, ("WS",)))
            elif isinstance(route, Route):
                methods = tuple(sorted((route.methods or set()) - {"HEAD", "OPTIONS"}))
                actual_routes.append((route.path, methods))
            elif isinstance(route, Mount):
                static_mounts.append((route.path, route.name or ""))

        self.assertEqual(tuple(actual_routes), EXPECTED_HTTP_AND_WS_ROUTES)
        self.assertEqual(static_mounts, [("", "web")])

        # Signed URL prefixes relied upon by Web, iOS, and Android MainActivity.FILE_PATHS
        self.assertEqual(server_mod.WS_PATH, "/ws")
        self.assertEqual(server_mod.MAX_FRAME_BYTES, 36 * 1024 * 1024)
        self.assertEqual(server_mod.CREATIONS_PREFIX, "/api/webui/companion/creations/file")
        self.assertEqual(server_mod.AVATARS_PREFIX, "/api/webui/companion/avatars/file")

    def test_exact_webui_request_mutations_table(self):
        pkg = _companion_pkg()
        server_mod = importlib.import_module(f"{pkg}.server")
        self.assertEqual(frozenset(server_mod.MUTATIONS.keys()), EXPECTED_MUTATIONS)
        for name, handler in server_mod.MUTATIONS.items():
            with self.subTest(mutation=name):
                self.assertTrue(callable(handler))
                self.assertTrue(inspect.iscoroutinefunction(handler))

    def test_bootstrap_and_thread_etag_304_wire_invariants(self):
        folder = temp_dir("companion-etag-")
        server_mod, adapter = _make_stub_adapter(folder, with_web_dist=True)
        chat_id = adapter.store.create()
        adapter.store.append(
            chat_id,
            {"event": "user_message", "text": "你好", "starts_turn": True, "turn_id": "t1", "turn_phase": "user"},
        )
        app = server_mod.build_app(adapter)

        with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
            boot = client.get("/webui/bootstrap")
            self.assertEqual(boot.status_code, 200)
            body = boot.json()
            self.assertEqual(body["ws_path"], "/ws")
            self.assertEqual(body["limits"]["transport"]["max_frame_bytes"], 36 * 1024 * 1024)
            api_token = body["api_token"]

            headers = {"Authorization": f"Bearer {api_token}"}
            first = client.get(f"/api/sessions/websocket:{chat_id}/webui-thread", headers=headers)
            self.assertEqual(first.status_code, 200)
            etag = first.headers.get("ETag")
            self.assertTrue(etag)

            cached = client.get(
                f"/api/sessions/websocket:{chat_id}/webui-thread",
                headers={**headers, "If-None-Match": etag},
            )
            self.assertEqual(cached.status_code, 304)

    def test_ast_zero_forbidden_hermes_config_time_constants_imports_in_companion(self):
        violations: list[str] = []
        py_files = sorted(ROOT.rglob("*.py"))
        self.assertGreater(len(py_files), 30)

        for py_file in py_files:
            rel = py_file.relative_to(ROOT).as_posix()
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in FORBIDDEN_LEGACY_IMPORTS or any(
                            alias.name.startswith(prefix + ".") for prefix in FORBIDDEN_LEGACY_IMPORTS
                        ):
                            violations.append(f"{rel}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod in FORBIDDEN_LEGACY_IMPORTS or any(
                        mod.startswith(prefix + ".") for prefix in FORBIDDEN_LEGACY_IMPORTS
                    ):
                        violations.append(f"{rel}:{node.lineno}: from {mod} import ...")
                    elif mod:
                        for alias in node.names:
                            full = f"{mod}.{alias.name}"
                            if full in FORBIDDEN_LEGACY_IMPORTS:
                                violations.append(f"{rel}:{node.lineno}: from {mod} import {alias.name}")

        self.assertEqual(violations, [], f"Forbidden legacy Hermes imports found in companion/:\n" + "\n".join(violations))

    def test_persona_notices_stay_in_sync_with_muse_notices(self):
        from _loader import load
        from muse import notices

        _, (persona,) = load("persona")
        self.assertEqual(set(persona._COMPANION_ZH.keys()), set(notices.NOTICE_KEYS))
        self.assertEqual(notices.validate_overlay(persona._COMPANION_ZH), [])


if __name__ == "__main__":
    unittest.main()
