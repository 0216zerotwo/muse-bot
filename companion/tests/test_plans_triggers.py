"""Automations protection/cleanup, trigger schedules, ensure_jobs sync, runtime capabilities, and localized errors."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest import mock

from starlette.testclient import TestClient

from _loader import chat_store, load, temp_dir
from test_auth_devices import _companion_pkg


def _install_fake_cron():
    jobs_by_id: dict[str, dict] = {}
    counter = {"n": 0}

    cron_jobs = types.ModuleType("cron.jobs")

    def create_job(**kwargs):
        counter["n"] += 1
        jid = f"job-{counter['n']}"
        record = {"id": jid, "enabled": True, **kwargs}
        jobs_by_id[jid] = record
        return record

    def remove_job(job_id: str):
        return jobs_by_id.pop(job_id, None) is not None

    def get_job(job_id: str):
        return jobs_by_id.get(job_id)

    def list_jobs(include_disabled: bool = False):
        return [j for j in jobs_by_id.values() if include_disabled or j.get("enabled", True)]

    def pause_job(job_id: str, reason: str = ""):
        if job_id in jobs_by_id:
            jobs_by_id[job_id]["enabled"] = False

    def resume_job(job_id: str):
        if job_id in jobs_by_id:
            jobs_by_id[job_id]["enabled"] = True

    def update_job(job_id: str, changes: dict):
        if job_id in jobs_by_id:
            jobs_by_id[job_id].update(changes)
        return jobs_by_id.get(job_id)

    cron_jobs.create_job = create_job
    cron_jobs.remove_job = remove_job
    cron_jobs.get_job = get_job
    cron_jobs.list_jobs = list_jobs
    cron_jobs.pause_job = pause_job
    cron_jobs.resume_job = resume_job
    cron_jobs.update_job = update_job
    cron_jobs.get_cron_output_dir = lambda: temp_dir("companion-cron-out-")
    cron_jobs.compute_next_run = lambda schedule, last_run_at=None: None

    cron_pkg = types.ModuleType("cron")
    cron_pkg.jobs = cron_jobs
    return cron_pkg, cron_jobs, jobs_by_id


class PlansTriggersAndAutomationsTest(unittest.TestCase):
    def setUp(self):
        self.folder, (
            self.companion_state,
            self.triggers,
            self.plans,
            self.automations,
            self.routines,
            self.feed,
        ) = load("companion_state", "triggers", "plans", "automations", "routines", "feed")
        cron_pkg, cron_jobs, self.jobs_by_id = _install_fake_cron()
        self.enterContext(mock.patch.dict(sys.modules, {"cron": cron_pkg, "cron.jobs": cron_jobs}))
        self.enterContext(
            mock.patch.object(self.triggers.net_safety, "is_safe_url_target", return_value=True)
        )

    def test_trigger_schedules_handle_daily_1440_divisors_and_non_divisor_minutes(self):
        self.assertEqual(self.triggers._schedule_for({"kind": "webhook"}), "0 4 * * 1")
        self.assertEqual(self.triggers._schedule_for({"kind": "page", "every": 30}), "*/30 * * * *")
        self.assertEqual(self.triggers._schedule_for({"kind": "page", "every": 25}), "every 25m")
        self.assertEqual(self.triggers._schedule_for({"kind": "page", "every": 120}), "0 */2 * * *")
        self.assertEqual(self.triggers._schedule_for({"kind": "page", "every": 300}), "every 300m")
        # 1440m (24h) must NOT emit invalid ``0 */24 * * *`` cron syntax.
        self.assertEqual(self.triggers._schedule_for({"kind": "page", "every": 1440}), "0 0 * * *")

    def test_ensure_jobs_populates_missing_trigger_and_goal_jobs_once_chat_id_exists(self):
        # Created before onboarding/first chat sets chat_id -> job_id is None initially.
        trig = self.triggers.add_trigger(
            "page", "官网更新", "更新了告诉我", url="https://example.com/news", every=1440
        )
        goal = self.plans.add_goal("备考雅思", ["背单词"], checkin="daily 08:30")
        self.assertIsNone(trig["job_id"])
        self.assertIsNone(goal["job_id"])
        self.assertEqual(self.jobs_by_id, {})

        # Once chat_id is set and ensure_jobs() runs (as adapter.sync_identity does), jobs are created.
        self.companion_state.update_companion_state({"chat_id": "main-chat"})
        self.triggers.ensure_jobs()
        self.plans.ensure_jobs()

        trig_after = self.triggers._read()[0]
        goal_after = self.plans.list_goals()["goals"][0]
        self.assertIsNotNone(trig_after["job_id"])
        self.assertIsNotNone(goal_after["job_id"])
        self.assertEqual(self.jobs_by_id[trig_after["job_id"]]["schedule"], "0 0 * * *")
        self.assertEqual(self.jobs_by_id[goal_after["job_id"]]["schedule"], "30 8 * * *")

    def test_protected_automations_cannot_be_deleted_and_deleting_user_jobs_cleans_refs(self):
        self.companion_state.update_companion_state({"chat_id": "main-chat"})
        self.routines.update_routines({"checkins": True})
        self.feed.ensure_job()
        trig = self.triggers.add_trigger(
            "page", "价格监控", "降价提醒", url="https://example.com/item", every=30
        )
        goal = self.plans.add_goal("每周跑步", ["跑三次"], checkin="weekly mon 09:00")

        checkin_jid = next(j["id"] for j in self.jobs_by_id.values() if j["name"] == self.routines.CHECKIN_JOB_NAME)
        feed_jid = next(j["id"] for j in self.jobs_by_id.values() if j["name"] == self.feed.JOB_NAME)

        with self.assertRaisesRegex(ValueError, "系统内置任务不能删除"):
            self.automations.run_action("delete", checkin_jid)
        with self.assertRaisesRegex(ValueError, "系统内置任务不能删除"):
            self.automations.run_action("delete", feed_jid)

        # Deleting a trigger's or goal's cron job from automations cleans up stale job_id references!
        self.automations.run_action("delete", trig["job_id"])
        trig_now = self.triggers._read()[0]
        self.assertIsNone(trig_now["job_id"])
        self.assertTrue(trig_now["paused"])

        self.automations.run_action("delete", goal["job_id"])
        goal_now = self.plans._read()[0]
        self.assertIsNone(goal_now["job_id"])
        self.assertIsNone(goal_now["checkin"])


class CapabilitiesAndLocalizedErrorsTest(unittest.TestCase):
    def test_companion_and_bootstrap_expose_capabilities_and_chinese_mutation_errors(self):
        folder = temp_dir("companion-caps-")
        pkg = _companion_pkg()
        paths_mod = importlib.import_module(f"{pkg}.modules.paths")
        auth_mod = importlib.import_module(f"{pkg}.auth")
        devices_mod = importlib.import_module(f"{pkg}.devices")
        media_mod = importlib.import_module(f"{pkg}.media")
        server_mod = importlib.import_module(f"{pkg}.server")
        stt_mod = importlib.import_module(f"{pkg}.modules.stt_sensevoice")
        push_mod = importlib.import_module(f"{pkg}.modules.push")
        computer_mod = importlib.import_module(f"{pkg}.modules.computer")
        store = chat_store(folder)

        class StubAdapter:
            def __init__(self):
                self.tokens = auth_mod.TokenIssuer("")
                self.devices = devices_mod.Devices(folder)
                self.media = media_mod.MediaStore(folder)
                self.store = store
                self.web_dist = folder / "no-dist"
                self.socket_devices = {}
                self._turns = {}

            def sync_identity(self):
                pass

            def attach(self, chat_id, socket):
                pass

            def detach_all(self, socket):
                pass

        adapter = StubAdapter()
        app = server_mod.build_app(adapter)

        with (
            mock.patch.object(paths_mod, "get_webui_dir", return_value=folder),
            mock.patch.object(stt_mod, "stt_available", return_value=False),
            mock.patch.object(push_mod, "available", return_value=True),
            mock.patch.object(computer_mod, "_executable", return_value="/usr/bin/chromium"),
        ):
            with TestClient(app, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
                boot = client.get("/webui/bootstrap")
                self.assertEqual(boot.status_code, 200)
                boot_json = boot.json()
                self.assertEqual(
                    boot_json["capabilities"],
                    {"stt": False, "push": True, "browser": True},
                )

                api_tok = boot_json["api_token"]
                ws_tok = boot_json["token"]
                comp = client.get("/api/webui/companion", headers={"Authorization": f"Bearer {api_tok}"})
                self.assertEqual(comp.status_code, 200)
                self.assertEqual(
                    comp.json()["capabilities"],
                    {"stt": False, "push": True, "browser": True},
                )

                with client.websocket_connect(f"ws://127.0.0.1:8766/ws?token={ws_tok}") as ws:
                    self.assertEqual(ws.receive_json()["event"], "ready")
                    # companion.update preserves capabilities in its response payload.
                    ws.send_json(
                        {
                            "type": "webui_request",
                            "request_id": "u1",
                            "action": "companion.update",
                            "payload": {"changes": {"name": "小橘"}},
                        }
                    )
                    updated = ws.receive_json()
                    self.assertTrue(updated["ok"])
                    self.assertEqual(updated["result"]["name"], "小橘")
                    self.assertEqual(
                        updated["result"]["capabilities"],
                        {"stt": False, "push": True, "browser": True},
                    )

                    # Invalid mutation returns Chinese error message rather than raw English.
                    ws.send_json(
                        {
                            "type": "webui_request",
                            "request_id": "bad1",
                            "action": "companion.update",
                            "payload": {"changes": {"tone": "nonexistent"}},
                        }
                    )
                    bad = ws.receive_json()
                    self.assertFalse(bad["ok"])
                    self.assertEqual(bad["error"]["status"], 400)
                    self.assertIn("语气格式不正确", bad["error"]["message"])

                    ws.send_json(
                        {
                            "type": "webui_request",
                            "request_id": "bad2",
                            "action": "no.such.action",
                            "payload": {},
                        }
                    )
                    unknown = ws.receive_json()
                    self.assertFalse(unknown["ok"])
                    self.assertEqual(unknown["error"]["status"], 404)
                    self.assertIn("不支持的请求操作", unknown["error"]["message"])

    def test_stt_available_respects_enabled_flag_and_sensevoice_missing(self):
        _, (stt_mod,) = load("stt_sensevoice")
        with (
            mock.patch.object(stt_mod, "missing", return_value=[]),
            mock.patch.object(stt_mod, "_raw_stt_config", return_value={}),
        ):
            self.assertTrue(stt_mod.stt_available())

        with (
            mock.patch.object(stt_mod, "missing", return_value=[]),
            mock.patch.object(stt_mod, "_raw_stt_config", return_value={"enabled": False}),
        ):
            self.assertFalse(stt_mod.stt_available())

        with (
            mock.patch.object(stt_mod, "missing", return_value=["ffmpeg"]),
            mock.patch.object(stt_mod, "_raw_stt_config", return_value={"provider": "sensevoice"}),
        ):
            self.assertFalse(stt_mod.stt_available())


if __name__ == "__main__":
    unittest.main()
