"""Unit tests for PR-8 Phase 1: pyproject.toml/uv.lock, .gitattributes, tools/setup.py, muse/app.py, muse/cli.py."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from muse import app as muse_app
from muse import cli as muse_cli
from muse import runtime as muse_runtime
from muse.db import MuseDB, close_all
from muse.paths import get_repo_root, muse_home_override


class PackagingAndGitattributesTest(unittest.TestCase):
    def test_pyproject_toml_and_uv_lock_contract(self):
        root = get_repo_root()
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        proj = pyproject["project"]
        self.assertEqual(proj["requires-python"], ">=3.11")
        self.assertEqual(proj["scripts"]["musebot"], "muse.cli:main")
        self.assertEqual(len(proj["dependencies"]), 22)

        deps_blob = "\n".join(proj["dependencies"]).lower()
        for pkg in (
            "starlette", "uvicorn", "websockets", "python-multipart", "httpx[socks]", "truststore",
            "openai", "anthropic", "mcp", "pydantic", "ruamel.yaml", "python-dotenv",
            "cryptography", "croniter", "psutil", "tzdata", "pillow", "pillow-heif",
            "py-vapid", "http-ece", "ddgs", "pypdf",
        ):
            self.assertIn(pkg, deps_blob)

        self.assertIn("tts", proj["optional-dependencies"])
        self.assertIn("office", proj["optional-dependencies"])
        self.assertTrue((root / "uv.lock").is_file())
        self.assertGreater((root / "uv.lock").stat().st_size, 1000)

    def test_gitattributes_lf_and_android_ps1_bom_preserved(self):
        root = get_repo_root()
        attrs = (root / ".gitattributes").read_text(encoding="utf-8")
        for rule in ("*.py text eol=lf", "pyproject.toml text eol=lf", "uv.lock text eol=lf", "*.yaml text eol=lf", "*.yml text eol=lf", "*.ps1 text eol=crlf"):
            self.assertIn(rule, attrs)
        ps1 = (root / "android" / "install.ps1").read_bytes()
        self.assertTrue(ps1.startswith(b"\xef\xbb\xbf"), "android/install.ps1 must keep UTF-8 BOM")
        self.assertIn(b"\r\n", ps1, "android/install.ps1 must keep CRLF")


class SetupScriptTest(unittest.TestCase):
    def test_setup_py_help_and_default_browser_mcp_config(self):
        root = get_repo_root()
        script = root / "tools" / "setup.py"
        res = subprocess.run([sys.executable, str(script), "--help"], cwd=str(root), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.strip())
        self.assertIn("--skip-web", res.stdout)

        with tempfile.TemporaryDirectory(prefix="muse-setup-test-") as td:
            home = Path(td) / "muse-home"
            res_run = subprocess.run(
                [sys.executable, str(script), "--home", str(home), "--skip-sync", "--skip-web"],
                cwd=str(root),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_run.returncode, 0, res_run.stderr)
            cfg_path = home / "config.yaml"
            self.assertTrue(cfg_path.is_file())
            cfg_text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("mcp_servers:", cfg_text)
            self.assertIn("@playwright/mcp@0.0.83", cfg_text)
            self.assertIn("http://127.0.0.1:9333", cfg_text)

            # Never overwrites an existing config.yaml
            cfg_path.write_text("# user custom\ncustom_key: 1\n", encoding="utf-8")
            res_run2 = subprocess.run(
                [sys.executable, str(script), "--home", str(home), "--skip-sync", "--skip-web"],
                cwd=str(root),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_run2.returncode, 0, res_run2.stderr)
            self.assertIn("custom_key: 1", cfg_path.read_text(encoding="utf-8"))


class NativeAppAndSharedDBTest(unittest.TestCase):
    def tearDown(self):
        muse_runtime.close_all_shared_dbs()
        close_all()

    def test_shared_db_reuses_handle_per_home_and_reopens_after_close(self):
        with tempfile.TemporaryDirectory(prefix="muse-shared-db-") as td:
            h1 = Path(td) / "h1"
            h2 = Path(td) / "h2"
            db1a = muse_runtime.get_shared_db(h1)
            db1b = muse_runtime.get_shared_db(h1)
            db2 = muse_runtime.get_shared_db(h2)
            self.assertIs(db1a, db1b)
            self.assertIsNot(db1a, db2)
            muse_runtime.close_shared_db(h1)
            db1c = muse_runtime.get_shared_db(h1)
            self.assertIsNot(db1a, db1c)
            muse_runtime.close_all_shared_dbs()

    def test_single_asgi_app_bootstrap_cold_start_and_lifespan(self):
        import companion.server  # noqa: F401
        from muse.llm.auxiliary import get_runtime_loop

        with tempfile.TemporaryDirectory(prefix="muse-app-test-") as td:
            home = Path(td) / "home"
            web_dist = Path(td) / "web-dist"
            web_dist.mkdir(parents=True)
            (web_dist / "companion.html").write_text("<!doctype html><title>Muse Bot</title>\n", encoding="utf-8")

            t0 = time.monotonic()
            asgi = muse_app.create_app(home=home, web_dist=web_dist, start_background_services=False)
            with TestClient(asgi, client=("127.0.0.1", 51234), base_url="http://127.0.0.1:8766") as client:
                boot = client.get("/webui/bootstrap")
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.assertEqual(boot.status_code, 200)
                body = boot.json()
                self.assertEqual(body["ws_path"], "/ws")
                self.assertTrue(body["api_token"])
                self.assertIsNotNone(get_runtime_loop())
            self.assertIsNone(get_runtime_loop())
            self.assertLess(elapsed_ms, 2000.0)
            muse_runtime.close_all_shared_dbs()


class CliPackAndDoctorTest(unittest.TestCase):
    def tearDown(self):
        muse_runtime.close_all_shared_dbs()
        close_all()

    def test_pack_clean_and_with_data_posix_tar_sqlite_backup_and_mcp_rewrite(self):
        with tempfile.TemporaryDirectory(prefix="muse-pack-test-") as td:
            tmp = Path(td)
            web_dist = tmp / "web-dist"
            web_dist.mkdir()
            (web_dist / "companion.html").write_text("<!doctype html><body>Muse</body>\n", encoding="utf-8")

            # 1) Clean bundle (no data/secrets)
            clean_out = tmp / "clean.tar.gz"
            archive = muse_cli.pack_server_bundle(
                output=clean_out,
                with_data=False,
                version="v2.0.0-test",
                web_dist=web_dist,
            )
            self.assertTrue(archive.is_file())
            with tarfile.open(archive, "r:gz") as tf:
                names = tf.getnames()
                self.assertTrue(all("\\" not in n for n in names))
                self.assertTrue(all(m.uid == 0 and m.gid == 0 for m in tf.getmembers()))
                self.assertIn("musebot-server-v2.0.0-test/pyproject.toml", names)
                self.assertIn("musebot-server-v2.0.0-test/uv.lock", names)
                self.assertIn("musebot-server-v2.0.0-test/muse/cli.py", names)
                self.assertIn("musebot-server-v2.0.0-test/web/companion.html", names)
                self.assertFalse(any("/data/" in n for n in names))

            # 2) --with-data bundle: hot SQLite backup of muse.db + state.db, 0600 perms, MCP rewrite
            home = tmp / "home"
            (home / "companion" / "stt" / "pylib").mkdir(parents=True)
            (home / "companion" / "mcp" / "browser").mkdir(parents=True)
            (home / ".env").write_text("OPENAI_API_KEY=sk-dummy\n", encoding="utf-8")
            (home / "SOUL.md").write_text("Muse persona\n", encoding="utf-8")
            (home / "companion" / "state.json").write_text('{"name":"Muse"}\n', encoding="utf-8")
            (home / "companion" / "devices.json").write_text('[{"id":"pc"}]\n', encoding="utf-8")
            (home / "companion" / "stt" / "pylib" / "win.pyd").write_bytes(b"bin")
            (home / "companion" / "mcp" / "browser" / "cookies.db").write_bytes(b"secret")
            (home / "config.yaml").write_text(
                "\n".join([
                    "security:",
                    "  fake_ip_ranges:",
                    "    - 198.18.0.0/15",
                    "mcp_servers:",
                    "  github:",
                    "    command: npx",
                    "    cwd: 'D:\\\\work\\\\muse-home\\\\companion\\\\mcp\\\\github'",
                    "  browser:",
                    "    command: npx",
                    "    args: [-y, '@playwright/mcp@0.0.83', --browser, msedge]",
                    "    cwd: 'D:\\\\work\\\\muse-home\\\\companion\\\\mcp\\\\browser'",
                    "",
                ]),
                encoding="utf-8",
            )
            with MuseDB(home / "muse.db") as db:
                db.create_session("chat-1", model="gpt-4o")
            conn = sqlite3.connect(str(home / "state.db"))
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('ok')")
            conn.commit()
            conn.close()

            priv_out = tmp / "priv.tar.gz"
            muse_cli.pack_server_bundle(
                output=priv_out,
                with_data=True,
                version="v2.0.0-priv",
                home=home,
                web_dist=web_dist,
            )
            with tarfile.open(priv_out, "r:gz") as tf:
                by_name = {m.name: m for m in tf.getmembers()}
                prefix = "musebot-server-v2.0.0-priv/data"
                self.assertIn(f"{prefix}/muse.db", by_name)
                self.assertIn(f"{prefix}/state.db", by_name)
                self.assertIn(f"{prefix}/companion/state.json", by_name)
                self.assertEqual(by_name[f"{prefix}/.env"].mode & 0o777, 0o600)
                self.assertEqual(by_name[f"{prefix}/muse.db"].mode & 0o777, 0o600)
                self.assertNotIn(f"{prefix}/companion/devices.json", by_name)
                self.assertFalse(any("stt/pylib" in k for k in by_name))
                self.assertFalse(any("companion/mcp/" in k for k in by_name))

                cfg_txt = tf.extractfile(by_name[f"{prefix}/config.yaml"]).read().decode("utf-8")
                self.assertNotIn("fake_ip_ranges", cfg_txt)
                self.assertNotIn("msedge", cfg_txt)
                self.assertNotIn("@playwright/mcp", cfg_txt)
                self.assertIn("playwright-mcp", cfg_txt)
                self.assertIn("/opt/data/companion/mcp/github", cfg_txt)
                self.assertIn("/opt/data/companion/mcp/browser", cfg_txt)
                self.assertIn("http://127.0.0.1:9333", cfg_txt)

                # Verify backed-up muse.db is intact SQLite
                extracted_db = tmp / "extracted_muse.db"
                extracted_db.write_bytes(tf.extractfile(by_name[f"{prefix}/muse.db"]).read())
                c2 = sqlite3.connect(str(extracted_db))
                try:
                    self.assertEqual(c2.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(c2.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
                finally:
                    c2.close()

    def test_doctor_json_and_clean_legacy_safety_gate(self):
        with tempfile.TemporaryDirectory(prefix="muse-doctor-test-") as td:
            fake_repo = Path(td) / "repo"
            home = Path(td) / "home"
            (fake_repo / "hermes").mkdir(parents=True)
            (fake_repo / "hermes-env").mkdir(parents=True)
            (home / "tools").mkdir(parents=True)
            unmod_skill = home / "skills" / "old-dev-skill"
            mod_skill = home / "skills" / "edited-skill"
            unmod_skill.mkdir(parents=True)
            mod_skill.mkdir(parents=True)
            unmod_bytes = b"# Old Bundled Skill\n"
            (unmod_skill / "SKILL.md").write_bytes(unmod_bytes)
            (mod_skill / "SKILL.md").write_bytes(b"# User Edited Skill!\n")
            manifest = {
                "skills": {
                    "old-dev-skill": hashlib.sha256(unmod_bytes).hexdigest(),
                    "edited-skill": hashlib.sha256(b"# Original Content\n").hexdigest(),
                }
            }
            (home / "skills" / ".bundled_manifest").write_text(json.dumps(manifest), encoding="utf-8")

            # 1) Before muse.db exists: doctor NEVER creates muse.db AND --clean-legacy refuses!
            rep1 = muse_cli.run_doctor(home=home, repo_root=fake_repo, clean_legacy=True)
            self.assertFalse((home / "muse.db").exists())
            self.assertFalse(rep1["clean_legacy"]["executed"])
            self.assertTrue((fake_repo / "hermes").exists())
            self.assertTrue(unmod_skill.exists())

            # 2) Once healthy muse.db exists: --clean-legacy removes legacy dirs & unmodified skills,
            #    while preserving user-modified skills!
            with MuseDB(home / "muse.db"):
                pass
            rep2 = muse_cli.run_doctor(home=home, repo_root=fake_repo, clean_legacy=True)
            self.assertTrue(rep2["database"]["healthy"])
            self.assertTrue(rep2["clean_legacy"]["executed"])
            self.assertFalse((fake_repo / "hermes").exists())
            self.assertFalse((fake_repo / "hermes-env").exists())
            self.assertFalse((home / "tools").exists())
            self.assertFalse(unmod_skill.exists())
            self.assertTrue(mod_skill.exists())
            self.assertIn("edited-skill", rep2["clean_legacy"]["preserved_modified_skills"])

            # 3) CLI `--json` outputs valid JSON cleanly
            buf = io.StringIO()
            with muse_home_override(home), mock.patch("sys.stdout", buf):
                rc = muse_cli.main(["doctor", "--json", "--home", str(home)])
            self.assertIn(rc, (0, 1))
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["sqlite"]["fts5_trigram"], True)


if __name__ == "__main__":
    unittest.main()
