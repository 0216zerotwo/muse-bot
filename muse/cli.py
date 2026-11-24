"""Cross-platform `musebot` CLI: serve | setup | pair | pack | doctor."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .paths import bind_muse_home, get_muse_home, get_repo_root, reset_muse_home

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_SECRET_REL_PATHS = (
    ".env",
    "auth.json",
    "muse.db",
    "companion/media.key",
    "companion/devices.json",
    "companion/vapid_private.pem",
)
_PACK_DATA_ITEMS = (
    ".env",
    "SOUL.md",
    "auth.json",
    "memories",
    "skills",
    "sessions",
    "cron/jobs.json",
    "channel_directory.json",
    "projects.db",
    "vault",
)
_COMPANION_PACK_EXCLUDES = frozenset({"mcp", "devices.json", "attachments", "tmp", "__pycache__"})
_PACK_SECRET_NAMES = frozenset({".env", "auth.json", "muse.db", "state.db", "media.key", "vapid_private.pem"})


# ── Pure-Python `musebot pack` helpers ───────────────────────────────────────


def rewrite_config_for_vps(raw_cfg: Any) -> dict[str, Any]:
    """Strip desktop-only settings and rewrite MCP paths for `/opt/data` on VPS."""
    cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
    security = cfg.get("security")
    if isinstance(security, dict):
        security = dict(security)
        security.pop("fake_ip_ranges", None)
        if security:
            cfg["security"] = security
        else:
            cfg.pop("security", None)

    servers = dict(cfg.get("mcp_servers")) if isinstance(cfg.get("mcp_servers"), dict) else {}
    for name, srv_raw in list(servers.items()):
        if not isinstance(srv_raw, dict):
            continue
        srv = dict(srv_raw)
        cwd = srv.get("cwd")
        if isinstance(cwd, str) and (
            _WIN_DRIVE_RE.match(cwd) or "companion/mcp/" in cwd.replace("\\", "/") or name == "browser"
        ):
            srv["cwd"] = f"/opt/data/companion/mcp/{name}"
        if name == "browser":
            cmd = srv.get("command")
            rewrite_builtin = cmd in ("npx", "npx.cmd", "playwright-mcp", None)
            if rewrite_builtin:
                srv["command"] = "playwright-mcp"
            cleaned: list[str] = []
            skip_next = False
            for item in list(srv.get("args") or []):
                s = str(item)
                if skip_next:
                    skip_next = False
                    continue
                if s == "--browser":
                    skip_next = True
                    continue
                if s.startswith("--browser="):
                    continue
                if rewrite_builtin and (
                    s in ("-y", "--yes") or s == "@playwright/mcp" or s.startswith("@playwright/mcp@")
                ):
                    continue
                cleaned.append(s)
            if "--cdp-endpoint" not in cleaned and not any(a.startswith("--cdp-endpoint=") for a in cleaned):
                cleaned.extend(["--cdp-endpoint", "http://127.0.0.1:9333"])
            if "--no-webmcp" not in cleaned:
                cleaned.append("--no-webmcp")
            srv["args"] = cleaned
        servers[name] = srv

    if "browser" not in servers:
        servers["browser"] = {
            "command": "playwright-mcp",
            "args": ["--no-webmcp", "--cdp-endpoint", "http://127.0.0.1:9333"],
            "timeout": 120,
            "connect_timeout": 120,
            "cwd": "/opt/data/companion/mcp/browser",
        }
    cfg["mcp_servers"] = servers
    return cfg


def _dump_vps_config_yaml(cfg: dict[str, Any]) -> bytes:
    header = "# Migrated server config (musebot pack --with-data)\n"
    buf = io.StringIO()
    buf.write(header)
    try:
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.default_flow_style = False
        yaml.allow_unicode = True
        yaml.sort_base_mapping_type_on_output = False
        yaml.dump(cfg, buf)
    except ImportError:
        buf.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return buf.getvalue().encode("utf-8")


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _hot_backup_sqlite(src_db: Path, dst_db: Path) -> None:
    """Create a consistent SQLite backup even while the database is live in WAL mode."""
    with tempfile.TemporaryDirectory(prefix="muse-bk-") as tmp:
        stage = Path(tmp) / src_db.name
        shutil.copy2(src_db, stage)
        for suffix in ("-wal", "-shm"):
            side = src_db.with_name(f"{src_db.name}{suffix}")
            if side.is_file():
                with contextlib.suppress(OSError):
                    shutil.copy2(side, Path(tmp) / side.name)
        src = sqlite3.connect(str(stage), timeout=5.0)
        dst = sqlite3.connect(str(dst_db), timeout=5.0)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()


def _tar_add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes, *, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=arcname.replace("\\", "/"))
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tf.addfile(info, io.BytesIO(data))


def _tar_add_file(tf: tarfile.TarFile, src: Path, arcname: str, *, mode: Optional[int] = None) -> None:
    info = tf.gettarinfo(str(src), arcname=arcname.replace("\\", "/"))
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if mode is not None:
        info.mode = mode
    elif info.isfile():
        info.mode = 0o755 if src.suffix == ".sh" else 0o644
    with src.open("rb") as fh:
        tf.addfile(info, fh)


def _tar_add_tree(
    tf: tarfile.TarFile,
    src_root: Path,
    arc_prefix: str,
    *,
    skip_dirs: frozenset[str] = frozenset({"__pycache__", ".git", "node_modules"}),
    skip_suffixes: frozenset[str] = frozenset({".pyc", ".pyo"}),
    skip_rel_prefixes: tuple[str, ...] = (),
    secret_mode: Optional[int] = None,
) -> None:
    for path in sorted(src_root.rglob("*")):
        rel = path.relative_to(src_root)
        parts = rel.parts
        if any(p in skip_dirs for p in parts):
            continue
        rel_posix = rel.as_posix()
        if any(rel_posix == p or rel_posix.startswith(p + "/") for p in skip_rel_prefixes):
            continue
        if path.is_dir():
            continue
        if path.suffix in skip_suffixes:
            continue
        file_mode = secret_mode
        if file_mode is None and (path.name in _PACK_SECRET_NAMES or "vault" in parts):
            file_mode = 0o600
        _tar_add_file(tf, path, f"{arc_prefix}/{rel_posix}", mode=file_mode)


def pack_server_bundle(
    *,
    output: Optional[Path] = None,
    with_data: bool = False,
    version: Optional[str] = None,
    home: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    web_dist: Optional[Path] = None,
) -> Path:
    """Build a VPS deployment archive in pure Python (POSIX `/` paths, `0600` data, SQLite backup)."""
    root = (repo_root or get_repo_root()).resolve()
    web = (web_dist or (root / "web" / "dist")).resolve()
    if not (web / "companion.html").is_file():
        raise FileNotFoundError(f"web/dist missing companion.html ({web}); run `npm run build` in web/ first.")

    release = (version or os.environ.get("MUSEBOT_VERSION") or f"dev-{datetime.now().strftime('%Y%m%d-%H%M')}").strip()
    top = f"musebot-server-{release}"
    out_path = output.resolve() if output is not None else (root / "deploy" / "dist" / f"{top}.tar.gz")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data_home = (home or get_muse_home()).resolve()
    if with_data and not data_home.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_home}")

    tmp_archive = out_path.with_suffix(".tmp.tar.gz")
    try:
        with tarfile.open(tmp_archive, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
            # Core source packages & root lockfiles
            _tar_add_tree(tf, root / "muse", f"{top}/muse", skip_rel_prefixes=("tests",))
            _tar_add_tree(tf, root / "companion", f"{top}/companion", skip_rel_prefixes=("tests",))
            _tar_add_tree(tf, web, f"{top}/web")
            for rel_src, arc_rel in (
                ("pyproject.toml", "pyproject.toml"),
                ("uv.lock", "uv.lock"),
                ("tools/companion-pair.py", "tools/companion-pair.py"),
                ("deploy/vps/Dockerfile", "Dockerfile"),
                ("deploy/vps/install.sh", "install.sh"),
                ("deploy/vps/uninstall.sh", "uninstall.sh"),
                ("deploy/vps/README.md", "README.md"),
                ("LICENSE", "LICENSE"),
                ("NOTICE", "NOTICE"),
            ):
                src_file = root / rel_src
                if src_file.is_file():
                    _tar_add_file(tf, src_file, f"{top}/{arc_rel}")
            _tar_add_bytes(tf, f"{top}/VERSION", f"{release}\n".encode("utf-8"), mode=0o644)

            if with_data:
                for item in _PACK_DATA_ITEMS:
                    src = data_home / item
                    if not src.exists():
                        continue
                    if src.is_dir():
                        _tar_add_tree(tf, src, f"{top}/data/{item}", secret_mode=0o600 if item == "vault" else None)
                    else:
                        fmode = 0o600 if src.name in _PACK_SECRET_NAMES else 0o644
                        _tar_add_file(tf, src, f"{top}/data/{item}", mode=fmode)

                comp_dir = data_home / "companion"
                if comp_dir.is_dir():
                    _tar_add_tree(
                        tf,
                        comp_dir,
                        f"{top}/data/companion",
                        skip_dirs=_COMPANION_PACK_EXCLUDES,
                        skip_rel_prefixes=("stt/pylib", "devices.json"),
                    )

                with tempfile.TemporaryDirectory(prefix="muse-pack-db-") as db_tmp:
                    for db_name in ("muse.db", "state.db"):
                        src_db = data_home / db_name
                        if src_db.is_file():
                            dst_db = Path(db_tmp) / db_name
                            _hot_backup_sqlite(src_db, dst_db)
                            _tar_add_file(tf, dst_db, f"{top}/data/{db_name}", mode=0o600)

                cfg_path = data_home / "config.yaml"
                if cfg_path.is_file():
                    rewritten = rewrite_config_for_vps(_load_yaml_file(cfg_path))
                    _tar_add_bytes(tf, f"{top}/data/config.yaml", _dump_vps_config_yaml(rewritten), mode=0o600)

        os.replace(tmp_archive, out_path)
        if with_data and os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(out_path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_archive.unlink(missing_ok=True)
        raise
    return out_path


# ── `musebot doctor` & `--clean-legacy` ──────────────────────────────────────


def _probe_sqlite_trigram() -> dict[str, Any]:
    ver_info = sqlite3.sqlite_version_info
    trigram_ok = False
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _t USING fts5(body, tokenize='trigram')")
            trigram_ok = True
        finally:
            conn.close()
    except sqlite3.Error:
        trigram_ok = False
    return {
        "version": sqlite3.sqlite_version,
        "meets_min_3_51_3": ver_info >= (3, 51, 3),
        "fts5_trigram": trigram_ok,
        "ok": ver_info >= (3, 51, 3) and trigram_ok,
    }


def _probe_muse_db(home: Path) -> dict[str, Any]:
    db_path = home / "muse.db"
    if not db_path.is_file():
        return {"exists": False, "healthy": False, "path": str(db_path), "detail": "muse.db not created yet"}
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            healthy = bool(row and str(row[0]).lower() == "ok")
        finally:
            conn.close()
        return {"exists": True, "healthy": healthy, "path": str(db_path), "detail": "ok" if healthy else str(row)}
    except sqlite3.Error as exc:
        return {"exists": True, "healthy": False, "path": str(db_path), "detail": type(exc).__name__}


def _probe_permissions(home: Path) -> dict[str, Any]:
    if os.name != "posix":
        return {"ok": True, "platform": os.name, "insecure_files": []}
    insecure: list[str] = []
    for rel in _SECRET_REL_PATHS:
        candidate = home / rel
        if candidate.is_file():
            mode = stat.S_IMODE(os.lstat(candidate).st_mode)
            if mode & 0o077:
                insecure.append(f"{rel} ({oct(mode)})")
    return {"ok": not insecure, "platform": "posix", "insecure_files": insecure}


def _find_chromium() -> Optional[str]:
    for cand in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        if found := shutil.which(cand):
            return found
    if os.name == "nt":
        for win_path in (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        ):
            if win_path.is_file():
                return str(win_path)
    return None


def _clean_legacy_artifacts(repo_root: Path, home: Path, db_report: dict[str, Any]) -> dict[str, Any]:
    """Remove legacy ~900MB dirs and unmodified `.bundled_manifest` skills once `muse.db` is healthy."""
    if not db_report.get("healthy"):
        return {
            "executed": False,
            "reason": "muse.db is missing or unhealthy; refusing to remove legacy directories",
            "removed": [],
            "preserved_modified_skills": [],
        }

    removed: list[str] = []
    preserved_skills: list[str] = []

    for legacy_dir in (repo_root / "hermes", repo_root / "hermes-env", home / "tools"):
        if legacy_dir.exists() and legacy_dir.is_dir():
            shutil.rmtree(legacy_dir)
            removed.append(str(legacy_dir))

    skills_dir = home / "skills"
    manifest_path = skills_dir / ".bundled_manifest"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
        entries = manifest.get("skills") if isinstance(manifest, dict) and isinstance(manifest.get("skills"), dict) else (
            manifest if isinstance(manifest, dict) else {}
        )
        for rel_skill, expected_hash in list(entries.items()):
            if not isinstance(rel_skill, str) or not isinstance(expected_hash, str):
                continue
            target = (skills_dir / rel_skill).resolve()
            try:
                target.relative_to(skills_dir.resolve())
            except ValueError:
                continue
            if not target.exists():
                continue
            skill_file = target / "SKILL.md" if target.is_dir() else target
            if not skill_file.is_file():
                continue
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            if digest == expected_hash.strip().lower():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                removed.append(str(target))
            else:
                preserved_skills.append(rel_skill)

    return {
        "executed": True,
        "removed": removed,
        "preserved_modified_skills": preserved_skills,
    }


def run_doctor(*, home: Optional[Path] = None, repo_root: Optional[Path] = None, clean_legacy: bool = False) -> dict[str, Any]:
    root = (repo_root or get_repo_root()).resolve()
    data_home = (home or get_muse_home()).resolve()
    sqlite_info = _probe_sqlite_trigram()
    db_info = _probe_muse_db(data_home)
    perms_info = _probe_permissions(data_home)

    chromium = _find_chromium()
    playwright_cmd = shutil.which("playwright-mcp") or shutil.which("npx")
    ffmpeg_bin = shutil.which("ffmpeg")
    sensevoice_dir = data_home / "companion" / "stt"
    sensevoice_ready = sensevoice_dir.is_dir() and any(sensevoice_dir.iterdir())

    cfg = _load_yaml_file(data_home / "config.yaml") if (data_home / "config.yaml").is_file() else {}
    mcp_servers = sorted((cfg.get("mcp_servers") or {}).keys()) if isinstance(cfg.get("mcp_servers"), dict) else []
    models_json = data_home / "companion" / "models.json"
    preset_count = 0
    if models_json.is_file():
        with contextlib.suppress(Exception):
            preset_count = len((json.loads(models_json.read_text(encoding="utf-8")) or {}).get("presets") or {})

    checks: dict[str, Any] = {
        "version": __version__,
        "home": str(data_home),
        "python": {"version": ".".join(map(str, sys.version_info[:3])), "ok": sys.version_info >= (3, 11)},
        "sqlite": sqlite_info,
        "database": db_info,
        "permissions": perms_info,
        "browser": {"chromium": chromium, "playwright_runner": playwright_cmd, "ok": bool(chromium and playwright_cmd)},
        "voice": {"ffmpeg": ffmpeg_bin, "sensevoice_ready": sensevoice_ready},
        "models": {"preset_count": preset_count},
        "mcp": {"configured_servers": mcp_servers},
    }
    if clean_legacy:
        checks["clean_legacy"] = _clean_legacy_artifacts(root, data_home, db_info)
    checks["ok"] = bool(checks["python"]["ok"] and sqlite_info["ok"] and perms_info["ok"])
    return checks


# ── Subcommand handlers ──────────────────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.home:
        os.environ["MUSE_HOME"] = str(Path(args.home).expanduser())
    if args.engine:
        os.environ["MUSE_ENGINE"] = args.engine

    from .app import create_app

    app = create_app(host=args.host, port=args.port, home=args.home)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        ws="websockets",
        ws_max_size=36 * 1024 * 1024,
    )
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    setup_path = get_repo_root() / "tools" / "setup.py"
    spec = importlib.util.spec_from_file_location("muse_tools_setup", setup_path)
    if spec is None or spec.loader is None:
        print(f"[stop] setup script not found: {setup_path}", file=sys.stderr)
        return 1
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.run_setup(home=args.home, skip_sync=args.skip_sync, skip_web=args.skip_web))


def _cmd_pair(args: argparse.Namespace) -> int:
    pair_script = get_repo_root() / "tools" / "companion-pair.py"
    if not pair_script.is_file():
        print(f"[stop] pair script not found: {pair_script}", file=sys.stderr)
        return 1
    cmd = [sys.executable, str(pair_script), *list(args.pair_args)]
    return subprocess.run(cmd).returncode


def _cmd_pack(args: argparse.Namespace) -> int:
    out_path = Path(args.output).expanduser() if args.output else None
    home = Path(args.home).expanduser() if args.home else None
    web_dist = Path(args.web_dist).expanduser() if args.web_dist else None
    try:
        archive = pack_server_bundle(
            output=out_path,
            with_data=bool(args.with_data),
            version=args.version,
            home=home,
            web_dist=web_dist,
        )
    except Exception as exc:
        print(f"[stop] pack failed: {exc}", file=sys.stderr)
        return 1
    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Packed: {archive} ({size_mb:.2f} MB)")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser() if args.home else None
    token = bind_muse_home(home) if home is not None else None
    try:
        report = run_doctor(home=home, clean_legacy=bool(args.clean_legacy))
    finally:
        if token is not None:
            reset_muse_home(token)

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Muse Bot doctor (v{report['version']}) — home: {report['home']}")
        print(f"  Python  : {report['python']['version']} ({'OK' if report['python']['ok'] else 'FAIL'})")
        sq = report["sqlite"]
        print(f"  SQLite  : {sq['version']} trigram={sq['fts5_trigram']} ({'OK' if sq['ok'] else 'FAIL'})")
        db = report["database"]
        print(f"  muse.db : exists={db['exists']} healthy={db['healthy']} ({db['detail']})")
        print(f"  Perms   : {'OK' if report['permissions']['ok'] else report['permissions']['insecure_files']}")
        print(f"  Browser : chromium={bool(report['browser']['chromium'])} runner={bool(report['browser']['playwright_runner'])}")
        print(f"  Voice   : ffmpeg={bool(report['voice']['ffmpeg'])} sensevoice={report['voice']['sensevoice_ready']}")
        print(f"  Models  : {report['models']['preset_count']} presets | MCP: {', '.join(report['mcp']['configured_servers']) or 'none'}")
        if "clean_legacy" in report:
            cl = report["clean_legacy"]
            print(f"  Clean   : executed={cl['executed']} removed={len(cl['removed'])} preserved_skills={len(cl['preserved_modified_skills'])}")
    if args.clean_legacy and not report.get("clean_legacy", {}).get("executed"):
        return 1
    return 0 if report["ok"] else 1


def _default_host() -> str:
    return (os.environ.get("COMPANION_HOST") or "").strip() or "127.0.0.1"


def _default_port() -> int:
    raw = (os.environ.get("COMPANION_PORT") or "").strip()
    try:
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    return 8766


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musebot", description="Muse Bot native server CLI")
    parser.add_argument("--version", action="version", version=f"musebot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the unified Muse Bot ASGI server")
    p_serve.add_argument("--host", default=_default_host())
    p_serve.add_argument("--port", type=int, default=_default_port())
    p_serve.add_argument("--home", default=None)
    p_serve.add_argument("--engine", choices=("native", "hermes"), default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_setup = sub.add_parser("setup", help="Initialize .venv, default config.yaml, and web/dist")
    p_setup.add_argument("--home", default=None)
    p_setup.add_argument("--skip-sync", action="store_true")
    p_setup.add_argument("--skip-web", action="store_true")
    p_setup.set_defaults(func=_cmd_setup)

    p_pair = sub.add_parser("pair", help="Generate a mobile pairing code via tools/companion-pair.py")
    p_pair.add_argument("pair_args", nargs=argparse.REMAINDER)
    p_pair.set_defaults(func=_cmd_pair)

    p_pack = sub.add_parser("pack", help="Pack a VPS deployment archive in pure Python (no Git Bash)")
    p_pack.add_argument("--with-data", action="store_true", help="Include migrated data & secrets (0600)")
    p_pack.add_argument("-o", "--output", default=None, help="Output .tar.gz archive path")
    p_pack.add_argument("--version", default=None, help="Release version label")
    p_pack.add_argument("--home", default=None, help="Data home directory")
    p_pack.add_argument("--web-dist", default=None, help="Path to built web/dist directory")
    p_pack.set_defaults(func=_cmd_pack)

    p_doc = sub.add_parser("doctor", help="Inspect runtime health and optionally clean legacy Hermes files")
    p_doc.add_argument("--json", dest="as_json", action="store_true", help="Output JSON report")
    p_doc.add_argument("--clean-legacy", action="store_true", help="Remove legacy dirs & unmodified bundled skills")
    p_doc.add_argument("--home", default=None, help="Data home directory")
    p_doc.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
