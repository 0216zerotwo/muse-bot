"""Timezone-aware clock utilities for Muse (drop-in replacement for ``hermes_time``)."""

from __future__ import annotations

import contextlib
import locale
import logging
import os
import re
import sys
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .paths import get_config_path

logger = logging.getLogger(__name__)
_cache_lock = threading.Lock()
_tz_cache: dict[tuple[object, ...], tuple[str, Optional[ZoneInfo]]] = {}
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_ESCAPED_BYTES_RE = re.compile(r"[\x00-\x7f\udc80-\udcff]*")


def _repair_surrogates(text: str, encoding: Optional[str] = None) -> str:
    if text.isascii() or not _SURROGATE_RE.search(text):
        return text
    if _ESCAPED_BYTES_RE.fullmatch(text):
        with contextlib.suppress(LookupError, UnicodeError):
            return text.encode("ascii", "surrogateescape").decode(encoding or locale.getencoding())
    return _SURROGATE_RE.sub("\ufffd", text)


def safe_strftime(value: datetime, fmt: str) -> str:
    try:
        return _repair_surrogates(value.strftime(fmt))
    except UnicodeEncodeError:
        zone = _repair_surrogates(value.tzname() or "").replace("%", "%%")
        return _repair_surrogates(value.strftime(re.sub(r"%([%Z])", lambda m: zone if m.group(1) == "Z" else "%%", fmt)))


def _env_timezone() -> str:
    for key in ("MUSE_TIMEZONE", "HERMES_TIMEZONE"):
        if val := os.environ.get(key, "").strip():
            return val
    return ""


def _read_config_timezone() -> str:
    with contextlib.suppress(Exception):
        from .config_store import read_raw_config

        tz_cfg = (read_raw_config() or {}).get("timezone", "")
        if isinstance(tz_cfg, str) and tz_cfg.strip():
            return tz_cfg.strip()
    return ""


def _parse_zoneinfo(name: str) -> Optional[ZoneInfo]:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        logger.warning("Invalid timezone %r: %s. Falling back to server local time.", name, exc)
        return None


def _timezone_entry() -> tuple[str, Optional[ZoneInfo]]:
    env_tz = _env_timezone()
    if env_tz:
        identity: tuple[object, ...] = ("env", env_tz)
    else:
        cfg_path = get_config_path()
        try:
            st = cfg_path.stat()
            identity = ("config", str(cfg_path), st.st_mtime_ns, st.st_size)
        except OSError:
            identity = ("config", str(cfg_path), 0, 0)
    with _cache_lock:
        if identity in _tz_cache:
            return _tz_cache[identity]
    raw_name = env_tz or _read_config_timezone()
    tz = _parse_zoneinfo(raw_name)
    entry = (raw_name if tz is not None else "", tz)
    with _cache_lock:
        return _tz_cache.setdefault(identity, entry)


def get_timezone(tz_name: str | None = None) -> Optional[ZoneInfo]:
    return _parse_zoneinfo(tz_name.strip()) if (tz_name is not None and tz_name.strip()) else _timezone_entry()[1]


def get_timezone_name() -> str:
    return _timezone_entry()[0]


def reset_cache() -> None:
    with _cache_lock:
        _tz_cache.clear()
    if (legacy_mod := sys.modules.get("hermes_time")) is not None and callable(fn := getattr(legacy_mod, "reset_cache", None)):
        with contextlib.suppress(Exception):
            fn()


def now(tz_name: str | None = None) -> datetime:
    tz = get_timezone(tz_name)
    return datetime.now(tz) if tz is not None else datetime.now().astimezone()


__all__ = ["get_timezone", "get_timezone_name", "now", "reset_cache", "safe_strftime"]
