"""Models.dev registry fetcher using ``httpx`` + ETag conditional GET + atomic disk cache."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL_SECONDS = 4 * 3600
_RETRY_BACKOFF_SECONDS = 300

_cache_data: dict[str, Any] = {}
_cache_time: float = 0.0
_cache_home: Optional[str] = None
_retry_after: float = 0.0
_fetch_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_in_flight = False


class _NotModified(Exception):
    pass


def _get_home_dir() -> Path:
    try:
        from muse.paths import get_muse_home
        return Path(get_muse_home())
    except ImportError:
        pass
    for key in ("MUSE_HOME", "HERMES_HOME"):
        if val := os.environ.get(key, "").strip():
            return Path(val).expanduser()
    return Path.home() / ".muse"


def _cache_paths() -> tuple[Path, Path]:
    home = _get_home_dir()
    return home / "models_dev_cache.json", home / "models_dev_cache.etag"


def _get_models_dev_url() -> str:
    with contextlib.suppress(Exception):
        from muse.config_store import read_raw_config
        cfg = read_raw_config()
        url = (cfg.get("models_dev") or {}).get("url") if isinstance(cfg, dict) and isinstance(cfg.get("models_dev"), dict) else None
        if isinstance(url, str) and url.strip():
            return url.strip()
    return MODELS_DEV_URL


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _quarantine(cache_path: Path, etag_path: Path) -> None:
    with contextlib.suppress(OSError):
        cache_path.rename(cache_path.with_suffix(".json.corrupt"))
    with contextlib.suppress(OSError):
        etag_path.unlink(missing_ok=True)


def _load_disk_cache() -> dict[str, Any]:
    cache_path, etag_path = _cache_paths()
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and data:
            return data
    except Exception as exc:
        logger.warning("Failed to load models.dev cache; quarantining: %s", exc)
    _quarantine(cache_path, etag_path)
    return {}


def _disk_age() -> Optional[float]:
    cache_path, _ = _cache_paths()
    try:
        age = time.time() - cache_path.stat().st_mtime if cache_path.is_file() else -1.0
        return age if age >= 0 else None
    except OSError:
        return None


def _save_disk_cache(data: dict[str, Any], etag: str = "") -> None:
    cache_path, etag_path = _cache_paths()
    with contextlib.suppress(Exception):
        _atomic_write_text(cache_path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if etag:
        with contextlib.suppress(Exception):
            _atomic_write_text(etag_path, etag)


def _fetch_from_network(*, conditional: bool = False) -> tuple[dict[str, Any], str]:
    headers = {"Accept": "application/json"}
    if conditional:
        _, etag_path = _cache_paths()
        with contextlib.suppress(Exception):
            if etag_path.is_file() and (etag := etag_path.read_text(encoding="utf-8-sig").strip()):
                headers["If-None-Match"] = etag
    resp = httpx.get(_get_models_dev_url(), headers=headers, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True)
    if resp.status_code == 304:
        raise _NotModified()
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data:
        raise ValueError("models.dev returned an empty or invalid registry")
    return data, resp.headers.get("etag", "")


def _sync_home_scope() -> None:
    global _cache_data, _cache_time, _cache_home, _retry_after
    cur = str(_get_home_dir())
    if _cache_home != cur:
        _cache_data, _cache_time, _retry_after, _cache_home = {}, 0.0, 0.0, cur


def reset_models_dev_cache() -> None:
    global _cache_data, _cache_time, _cache_home, _retry_after, _refresh_in_flight
    with _fetch_lock:
        _cache_data, _cache_time, _cache_home, _retry_after = {}, 0.0, None, 0.0
    with _refresh_lock:
        _refresh_in_flight = False


def _refresh_locked() -> Optional[dict[str, Any]]:
    global _cache_data, _cache_time, _retry_after
    try:
        data, etag = _fetch_from_network(conditional=bool(_cache_data))
        _save_disk_cache(data, etag)
        _cache_data, _cache_time, _retry_after = data, time.time(), 0.0
        return data
    except _NotModified:
        if not _cache_data:
            with contextlib.suppress(OSError):
                _cache_paths()[1].unlink(missing_ok=True)
            _retry_after = time.time() + _RETRY_BACKOFF_SECONDS
            return None
        _cache_time, _retry_after = time.time(), 0.0
        return _cache_data
    except Exception as exc:
        _retry_after = time.time() + _RETRY_BACKOFF_SECONDS
        logger.debug("models.dev refresh failed; backoff %ds (%s)", _RETRY_BACKOFF_SECONDS, type(exc).__name__)
        return None


def _start_bg_refresh() -> None:
    global _refresh_in_flight
    if time.time() < _retry_after:
        return
    with _refresh_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def _run() -> None:
        global _refresh_in_flight
        try:
            with _fetch_lock:
                _refresh_locked()
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    try:
        threading.Thread(target=_run, name="muse-models-dev-refresh", daemon=True).start()
    except Exception:
        with _refresh_lock:
            _refresh_in_flight = False


def fetch_models_dev(force_refresh: bool = False, *, allow_network: bool = True) -> dict[str, Any]:
    """Fetch models.dev registry with ETag conditional GET & ``<HOME>/models_dev_cache.json``."""
    global _cache_data, _cache_time
    _sync_home_scope()
    if not allow_network:
        if not _cache_data and (disk := _load_disk_cache()):
            _cache_data = disk
            age = _disk_age()
            _cache_time = time.time() - age if age is not None else 0.0
        return _cache_data

    if not force_refresh:
        if _cache_data:
            if (time.time() - _cache_time) >= _CACHE_TTL_SECONDS:
                _cache_time = max(_cache_time, time.time() - _CACHE_TTL_SECONDS + _RETRY_BACKOFF_SECONDS)
                _start_bg_refresh()
            return _cache_data
        if (age := _disk_age()) is not None and (disk := _load_disk_cache()):
            _cache_data = disk
            if age >= _CACHE_TTL_SECONDS:
                _cache_time = max(_cache_time, time.time() - _CACHE_TTL_SECONDS + _RETRY_BACKOFF_SECONDS)
                _start_bg_refresh()
            else:
                _cache_time = time.time() - age
            return _cache_data
        if time.time() < _retry_after:
            return _cache_data

    with _fetch_lock:
        _sync_home_scope()
        if not force_refresh and (_cache_data or time.time() < _retry_after):
            return _cache_data
        if force_refresh and not _cache_data and (disk := _load_disk_cache()):
            _cache_data, _cache_time = disk, 0.0
        if (served := _refresh_locked()) is not None:
            return served
        if not _cache_data:
            _cache_data, _cache_time = _load_disk_cache(), 0.0
        return _cache_data
