"""Unit tests for PR-1 foundation modules: paths, time_utils, config_store, notices, and engine."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import muse
from muse import config_store, engine, notices, paths, time_utils


class TempDirMixin:
    def make_temp_dir(self, prefix: str = "muse-test-") -> Path:
        folder = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, folder, True)  # type: ignore[attr-defined]
        return folder


class PathsFoundationTest(TempDirMixin, unittest.TestCase):
    def test_version_and_repo_root(self):
        self.assertEqual(muse.__version__, "2.0.0-dev")
        root = paths.get_repo_root()
        self.assertEqual(root, REPO_ROOT)
        self.assertTrue((root / "muse" / "__init__.py").is_file())
        self.assertTrue((root / "companion" / "__init__.py").is_file())

    def test_get_muse_home_precedence_and_no_mkdir_side_effects(self):
        fake_repo = self.make_temp_dir("muse-fake-repo-")
        ctx_home = self.make_temp_dir("muse-ctx-") / "nested-ctx"
        env_muse = self.make_temp_dir("muse-env-") / "nested-muse"
        env_hermes = self.make_temp_dir("hermes-env-") / "nested-hermes"

        clean_env = {k: v for k, v in os.environ.items() if k not in ("MUSE_HOME", "HERMES_HOME")}
        with mock.patch.object(paths, "get_repo_root", return_value=fake_repo):
            # 6) Default fallback -> <repo>/muse-home (without creating it)
            with mock.patch.dict(os.environ, clean_env, clear=True):
                resolved = paths.get_muse_home()
                self.assertEqual(resolved, fake_repo / "muse-home")
                self.assertFalse(resolved.exists())
                self.assertEqual(paths.get_muse_db_path(), fake_repo / "muse-home" / "muse.db")
                self.assertEqual(paths.get_legacy_state_db_path(), fake_repo / "muse-home" / "state.db")
                self.assertFalse((fake_repo / "muse-home").exists())

            # 5) <repo>/hermes-home if exists -> preserved in-place
            (fake_repo / "hermes-home").mkdir()
            with mock.patch.dict(os.environ, clean_env, clear=True):
                self.assertEqual(paths.get_muse_home(), fake_repo / "hermes-home")

            # 4) <repo>/muse-home if exists -> wins over <repo>/hermes-home
            (fake_repo / "muse-home").mkdir()
            with mock.patch.dict(os.environ, clean_env, clear=True):
                self.assertEqual(paths.get_muse_home(), fake_repo / "muse-home")

            # 3) HERMES_HOME env -> wins over repo directories, no mkdir
            with mock.patch.dict(os.environ, {**clean_env, "HERMES_HOME": str(env_hermes)}, clear=True):
                self.assertEqual(paths.get_muse_home(), env_hermes)
                self.assertFalse(env_hermes.exists())

            # 2) MUSE_HOME env -> wins over HERMES_HOME
            with mock.patch.dict(
                os.environ,
                {**clean_env, "MUSE_HOME": str(env_muse), "HERMES_HOME": str(env_hermes)},
                clear=True,
            ):
                self.assertEqual(paths.get_muse_home(), env_muse)
                self.assertFalse(env_muse.exists())

                # 1) _MUSE_HOME_CTX -> highest priority, restores cleanly
                with paths.muse_home_override(ctx_home) as yielded:
                    self.assertEqual(yielded, ctx_home)
                    self.assertEqual(paths.get_muse_home(), ctx_home)
                    self.assertFalse(ctx_home.exists())
                    ensured = paths.ensure_muse_home()
                    self.assertEqual(ensured, ctx_home)
                    self.assertTrue(ctx_home.is_dir())

                self.assertEqual(paths.get_muse_home(), env_muse)

    def test_contextvar_isolation_across_concurrent_async_tasks(self):
        home_a = self.make_temp_dir("muse-async-a-")
        home_b = self.make_temp_dir("muse-async-b-")

        async def worker(target: Path, delay: float) -> tuple[Path, Path]:
            with paths.muse_home_override(target):
                before = paths.get_muse_home()
                await asyncio.sleep(delay)
                after = paths.get_muse_home()
                return before, after

        async def run_both() -> tuple[tuple[Path, Path], tuple[Path, Path]]:
            return await asyncio.gather(worker(home_a, 0.02), worker(home_b, 0.01))

        res_a, res_b = asyncio.run(run_both())
        self.assertEqual(res_a, (home_a, home_a))
        self.assertEqual(res_b, (home_b, home_b))

    def test_active_engine_name_feature_flag(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "MUSE_ENGINE"}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            self.assertEqual(paths.active_engine_name(), "hermes")
        for val, expected in (
            ("hermes", "hermes"),
            ("HERMES", "hermes"),
            ("", "hermes"),
            ("other", "hermes"),
            ("native", "native"),
            ("  NATIVE \n", "native"),
        ):
            with mock.patch.dict(os.environ, {**clean_env, "MUSE_ENGINE": val}, clear=True):
                self.assertEqual(paths.active_engine_name(), expected)


class TimeUtilsFoundationTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        time_utils.reset_cache()
        self.addCleanup(time_utils.reset_cache)

    def test_now_supports_explicit_env_and_config_timezones_with_local_fallback(self):
        home = self.make_temp_dir("muse-tz-")
        clean_env = {
            k: v for k, v in os.environ.items() if k not in ("MUSE_TIMEZONE", "TIMEZONE", "HERMES_TIMEZONE")
        }
        with mock.patch.dict(os.environ, clean_env, clear=True), paths.muse_home_override(home):
            # Local fallback: tz-aware datetime
            local_dt = time_utils.now()
            self.assertIsNotNone(local_dt.tzinfo)
            self.assertEqual(time_utils.get_timezone_name(), "")

            # Explicit tz_name argument
            utc_dt = time_utils.now("UTC")
            self.assertEqual(utc_dt.utcoffset().total_seconds(), 0)

            sh_dt = time_utils.now("Asia/Shanghai")
            self.assertEqual(sh_dt.utcoffset().total_seconds(), 8 * 3600)

            # Config timezone (unaffected by a stray bare TIMEZONE env var during Hermes coexistence)
            config_store.save_config({"timezone": "Asia/Tokyo"}, home)
            with mock.patch.dict(os.environ, {"TIMEZONE": "America/New_York"}):
                time_utils.reset_cache()
                self.assertEqual(time_utils.get_timezone_name(), "Asia/Tokyo")
                self.assertEqual(time_utils.now().utcoffset().total_seconds(), 9 * 3600)

            # HERMES_TIMEZONE beats config.yaml; MUSE_TIMEZONE beats HERMES_TIMEZONE
            with mock.patch.dict(os.environ, {"HERMES_TIMEZONE": "UTC"}):
                time_utils.reset_cache()
                self.assertEqual(time_utils.get_timezone_name(), "UTC")
                self.assertEqual(time_utils.now().utcoffset().total_seconds(), 0)
            with mock.patch.dict(os.environ, {"MUSE_TIMEZONE": "Asia/Shanghai", "HERMES_TIMEZONE": "UTC"}):
                time_utils.reset_cache()
                self.assertEqual(time_utils.get_timezone_name(), "Asia/Shanghai")
                self.assertEqual(time_utils.now().utcoffset().total_seconds(), 8 * 3600)

            # Invalid timezone logs warning and falls back cleanly without raising
            with self.assertLogs("muse.time_utils", level="WARNING"):
                fallback_dt = time_utils.now("Mars/Olympus_Mons")
            self.assertIsNotNone(fallback_dt.tzinfo)

    def test_safe_strftime_handles_lone_surrogates(self):
        dt = time_utils.now("UTC")
        self.assertEqual(time_utils.safe_strftime(dt, "%Y-%m-%d"), dt.strftime("%Y-%m-%d"))
        repaired = time_utils._repair_surrogates("zone-\udce9", encoding="latin-1")
        self.assertEqual(repaired, "zone-é")


class ConfigStoreFoundationTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        config_store.invalidate_caches()
        self.addCleanup(config_store.invalidate_caches)

    def test_roundtrip_yaml_preserves_comments_unknown_keys_quotes_and_secret_templates(self):
        home = self.make_temp_dir("muse cfg 中文 ")
        cfg_path = home / "config.yaml"
        env_path = home / ".env"

        # Missing config.yaml -> {} and no directory side effects on fresh subdir
        fresh = home / "uncreated"
        self.assertEqual(config_store.read_raw_config(fresh), {})
        self.assertFalse(fresh.exists())

        initial_yaml = (
            "# 顶部自定义注释：必须完整保留\n"
            "plugins:\n"
            "  enabled:\n"
            "    - companion-platform  # 行内插件注释\n"
            "  disabled: []\n"
            "streaming:\n"
            "  enabled: true\n"
            "providers:\n"
            "  svc-deepseek:\n"
            "    base_url: https://api.deepseek.com/v1\n"
            "    api_key: ${COMPANION_TEST_DS_KEY}\n"
            "    mode_flag: \"off\"\n"
        )
        cfg_path.write_text(initial_yaml, encoding="utf-8")
        env_path.write_text("# 密钥文件注释\nCOMPANION_TEST_DS_KEY=sk-live-secret-123\n", encoding="utf-8")

        raw = config_store.read_raw_config(home)
        self.assertEqual(raw["providers"]["svc-deepseek"]["api_key"], "${COMPANION_TEST_DS_KEY}")
        self.assertEqual(raw["providers"]["svc-deepseek"]["mode_flag"], "off")
        self.assertIsInstance(raw["providers"]["svc-deepseek"]["mode_flag"], str)

        loaded = config_store.load_config(home)
        self.assertEqual(loaded["providers"]["svc-deepseek"]["api_key"], "sk-live-secret-123")
        self.assertEqual(loaded["plugins"]["enabled"], ["companion-platform"])
        self.assertEqual(loaded["streaming"]["enabled"], True)

        # Mutate loaded (expanded) config and save: secret template & comments & unknown keys must survive!
        loaded["timezone"] = "Asia/Shanghai"
        loaded["approvals"] = {"mode": "off"}
        config_store.save_config(loaded, home)

        saved_text = cfg_path.read_text(encoding="utf-8")
        self.assertIn("# 顶部自定义注释：必须完整保留", saved_text)
        self.assertIn("# 行内插件注释", saved_text)
        self.assertIn("${COMPANION_TEST_DS_KEY}", saved_text)
        self.assertNotIn("sk-live-secret-123", saved_text)

        reloaded_raw = config_store.read_raw_config(home)
        self.assertEqual(reloaded_raw["timezone"], "Asia/Shanghai")
        self.assertEqual(reloaded_raw["approvals"]["mode"], "off")
        self.assertIs(type(reloaded_raw["approvals"]["mode"]), str)
        self.assertEqual(reloaded_raw["plugins"]["enabled"], ["companion-platform"])

    def test_corrupt_yaml_refuses_overwrite_and_serves_last_known_good(self):
        home = self.make_temp_dir("muse-corrupt-")
        cfg_path = home / "config.yaml"
        config_store.save_config({"timezone": "Asia/Shanghai", "custom_flag": 42}, home)
        good = config_store.load_config(home)
        self.assertEqual(good["custom_flag"], 42)

        # Corrupt the YAML on disk
        cfg_path.write_text("timezone: [unclosed\n", encoding="utf-8")
        with self.assertLogs("muse.config_store", level="WARNING"):
            fallback = config_store.load_config(home)
        self.assertEqual(fallback["custom_flag"], 42)

        with self.assertRaises(config_store.InvalidConfigError):
            config_store.save_config({"timezone": "UTC"}, home)

    def test_env_store_live_os_environ_semantics_quoting_denylist_and_permissions(self):
        home = self.make_temp_dir("muse-env-store-")
        key_plain = "MUSE_TEST_API_KEY_1"
        key_spaced = "MUSE_TEST_SPACED_VAL_1"
        key_export = "MUSE_TEST_EXPORTED_1"
        for k in (key_plain, key_spaced, key_export):
            self.addCleanup(os.environ.pop, k, None)

        (home / ".env").write_text(f"# keep comment\nexport {key_export}=old_val\n", encoding="utf-8")

        config_store.save_env_value(key_plain, "sk-abc-123", home)
        self.assertEqual(os.environ.get(key_plain), "sk-abc-123")
        self.assertEqual(config_store.get_env_value(key_plain, home), "sk-abc-123")

        config_store.save_env_value(key_spaced, 'hello "world" # not-a-comment', home)
        self.assertEqual(os.environ.get(key_spaced), 'hello "world" # not-a-comment')

        # Updating an existing `export KEY=...` replaces in-place without duplicating
        config_store.save_env_value(key_export, "new_val", home)
        env_text = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("# keep comment", env_text)
        self.assertEqual(env_text.count(key_export), 1)

        values = config_store.read_env_values(home)
        self.assertEqual(values[key_plain], "sk-abc-123")
        self.assertEqual(values[key_spaced], 'hello "world" # not-a-comment')
        self.assertEqual(values[key_export], "new_val")

        if os.name == "posix":
            mode = stat.S_IMODE((home / ".env").stat().st_mode)
            self.assertEqual(mode, 0o600)

        self.assertTrue(config_store.remove_env_value(key_plain, home))
        self.assertIsNone(os.environ.get(key_plain))
        self.assertIsNone(config_store.get_env_value(key_plain, home))
        self.assertFalse(config_store.remove_env_value(key_plain, home))

        for denied in ("LD_PRELOAD", "PYTHONPATH", "PATH", "MUSE_HOME", "HERMES_HOME", "GIT_CONFIG_COUNT", "BAD-NAME"):
            with self.subTest(denied=denied):
                with self.assertRaises(ValueError):
                    config_store.save_env_value(denied, "1", home)

    def test_concurrent_threads_and_cross_process_lockfiles(self):
        home = self.make_temp_dir("muse-locks-")
        keys = [f"MUSE_CONC_KEY_{i}" for i in range(16)]
        for k in keys:
            self.addCleanup(os.environ.pop, k, None)

        def write_one(idx: int) -> None:
            config_store.save_env_value(keys[idx], f"val-{idx}", home)
            with config_store._CONFIG_LOCK:
                cfg = config_store.read_raw_config(home)
                slots = cfg.setdefault("slots", {})
                slots[f"s{idx}"] = idx
                config_store.save_config(cfg, home)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_one, range(16)))

        self.assertTrue((home / ".env.lock").is_file())
        self.assertTrue((home / ".config.lock").is_file())
        self.assertEqual(list(home.glob("*.tmp")), [])

        env_vals = config_store.read_env_values(home)
        for idx, k in enumerate(keys):
            self.assertEqual(env_vals.get(k), f"val-{idx}")
        cfg_vals = config_store.read_raw_config(home)
        self.assertEqual(len(cfg_vals["slots"]), 16)

        # Verify a separate Python child process can also acquire .env.lock and write atomically
        child_key = "MUSE_CHILD_PROC_KEY"
        self.addCleanup(os.environ.pop, child_key, None)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from muse.config_store import save_env_value; "
                "save_env_value(sys.argv[3], 'from-child', sys.argv[2])",
                str(REPO_ROOT),
                str(home),
                child_key,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(config_store.read_env_values(home).get(child_key), "from-child")


class NoticesAndEngineFoundationTest(TempDirMixin, unittest.TestCase):
    def test_notices_placeholder_contract_and_rendering(self):
        self.assertEqual(len(notices.NOTICE_KEYS), 60)
        self.assertEqual(notices.validate_overlay(notices.DEFAULT_NOTICES_ZH), [])

        notices.validate_notice_template("gateway.errors.usage_limit_resets", "限额将于 {window} 后恢复")
        with self.assertRaises(KeyError):
            notices.validate_notice_template("gateway.unknown.key", "你好")
        with self.assertRaises(ValueError):
            notices.validate_notice_template("gateway.errors.usage_limit_resets", "错误占位符 {unknown_var}")

        rendered = notices.render_notice("gateway.errors.usage_limit_resets", window="2 小时")
        self.assertIn("2 小时", rendered)

        # Missing format kwargs never crash and NEVER leak user/secret values into warning logs
        with self.assertLogs("muse.notices", level="WARNING") as cm:
            unformatted = notices.render_notice(
                "gateway.errors.usage_limit_resets", goal="PRIVATE_USER_GOAL_DO_NOT_LOG"
            )
        self.assertIn("{window}", unformatted)
        joined_logs = "\n".join(cm.output)
        self.assertIn("['goal']", joined_logs)
        self.assertNotIn("PRIVATE_USER_GOAL_DO_NOT_LOG", joined_logs)

    def test_gitignore_protects_muse_home_secrets(self):
        res = subprocess.run(
            ["git", "check-ignore", "-q", "muse-home/auth.json"],
            cwd=str(REPO_ROOT),
            check=False,
        )
        self.assertEqual(res.returncode, 0, "muse-home/auth.json must be ignored by .gitignore")

    def test_engine_protocol_and_hermes_bridge_hooks(self):
        events_handled: list[Any] = []
        resets_called: list[Any] = []
        evicted_keys: list[str] = []

        class DummyRunner:
            def __init__(self):
                self._session_model_overrides: dict[str, Any] = {}

            def _session_key_for_source(self, source: Any) -> str:
                return f"companion:{source.chat_id}"

            def _evict_cached_agent(self, key: str) -> None:
                evicted_keys.append(key)

            async def _handle_reset_command(self, event: Any) -> None:
                resets_called.append(event)

        class DummyAdapter:
            def __init__(self):
                self.gateway_runner = DummyRunner()

            async def handle_message(self, event: Any) -> None:
                events_handled.append(event)

        adapter = DummyAdapter()
        bridge = engine.build_engine(adapter, engine_name="hermes")
        self.assertIsInstance(bridge, engine.AgentEngine)
        self.assertEqual(bridge.name, "hermes")

        asyncio.run(bridge.submit_turn("main", "你好", "t-1"))
        asyncio.run(bridge.stop_turn("main"))
        asyncio.run(bridge.handle_goal_control("main", "/goal pause"))
        self.assertEqual([e.text for e in events_handled], ["你好", "/stop", "/goal pause"])

        self.assertTrue(asyncio.run(bridge.renew_session("main")))
        self.assertEqual(len(resets_called), 1)
        self.assertEqual(resets_called[0].text, "/reset")

        bridge.set_session_model_override("helper-1", {"provider": "m-ds", "model": "deepseek-chat"})
        self.assertEqual(
            adapter.gateway_runner._session_model_overrides["companion:helper-1"],
            {"provider": "m-ds", "model": "deepseek-chat"},
        )
        self.assertEqual(evicted_keys, ["companion:helper-1"])

        bridge.set_session_model_override("helper-1", None)
        self.assertNotIn("companion:helper-1", adapter.gateway_runner._session_model_overrides)

        # Native engine raises cleanly until registered, and dispatches once registered
        with self.assertRaises(NotImplementedError):
            engine.build_engine(adapter, engine_name="native")

        sentinel = types.SimpleNamespace(name="native")
        engine.register_native_engine_factory(lambda adapter=None: sentinel)  # type: ignore[arg-type,return-value]
        self.addCleanup(engine.register_native_engine_factory, None)
        self.assertIs(engine.build_engine(adapter, engine_name="native"), sentinel)


if __name__ == "__main__":
    unittest.main()
