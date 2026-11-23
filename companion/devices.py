"""Paired devices: connect a phone to the companion once, and it stays connected.

The protocol is HTTPS + WebSocket, as before; what changes is how a device signs in:

1. The server hands out a **pairing code** (``ABCD-2345``): on start when no device is paired yet
   (logged), from ``tools/companion-pair.py`` on the server, or from 设置 → 连接 → 添加新设备 on a
   device that is already connected. A code is good once, for 10 minutes.
2. The app sends the code to ``POST /api/pair`` and gets a **device token** back. The phone keeps
   it (the page's storage, and on Android the app's own preferences); the server keeps only its
   hash, with the device's name and when it was last seen.
3. From then on ``/webui/bootstrap`` takes the device token (``Authorization: Device …``) and issues
   the short-lived session tokens as before. When those lapse, or the server restarts and forgets
   them, the app gets new ones with the device token, without asking.

A device stays connected until it is removed: by itself (断开这台设备), from another device's list,
or by clearing the app's data. Too many wrong codes lock pairing for a while (per IP first, so a
single probing host cannot lock out the owner on another network, plus a global ceiling).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

CODE_TTL_S = 10 * 60
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
_MAX_FAILURES = 10
_MAX_IP_FAILURES = 5
_MAX_GLOBAL_FAILURES = 25
_LOCK_S = 10 * 60
_MAX_DEVICES = 50


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch in _ALPHABET)


def _atomic_write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


class Devices:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "devices.json"
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}  # normalized code -> expiry
        self._failures: list[float] = []
        self._ip_failures: dict[str, list[float]] = {}

    # -- storage -------------------------------------------------------------------------------------

    def _read_unlocked(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _write_unlocked(self, devices: list[dict[str, Any]]) -> None:
        _atomic_write_private(self.path, json.dumps(devices, ensure_ascii=False, indent=2))

    def any_paired(self) -> bool:
        with self._lock:
            return bool(self._read_unlocked())

    # -- pairing codes -------------------------------------------------------------------------------

    def new_code(self) -> dict[str, Any]:
        """A fresh code, shown as ``ABCD-2345``; earlier unused codes stay valid until they lapse."""
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        expires = time.time() + CODE_TTL_S
        with self._lock:
            now = time.time()
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            self._codes[raw] = expires
        return {"code": f"{raw[:4]}-{raw[4:]}", "expires_at": int(expires * 1000)}

    def _prune_failures_locked(self, now: float) -> None:
        self._failures = [t for t in self._failures if now - t < _LOCK_S]
        for ip, times in list(self._ip_failures.items()):
            recent = [t for t in times if now - t < _LOCK_S]
            if recent:
                self._ip_failures[ip] = recent
            else:
                del self._ip_failures[ip]

    def _is_locked_locked(self, now: float, client_ip: str = "") -> bool:
        self._prune_failures_locked(now)
        if client_ip:
            if len(self._ip_failures.get(client_ip, ())) >= _MAX_IP_FAILURES:
                return True
            return len(self._failures) >= _MAX_GLOBAL_FAILURES
        return len(self._failures) >= _MAX_FAILURES

    def pair(self, code: str, name: str, client_ip: str = "") -> Optional[dict[str, Any]]:
        """Trade a code for a device token: ``{"device_id", "token"}``, or ``None`` (wrong code,
        expired, or pairing locked after too many wrong tries)."""
        offered = normalize_code(code)
        ip = str(client_ip or "").strip()
        with self._lock:
            now = time.time()
            if self._is_locked_locked(now, ip):
                return None
            match = next((c for c, t in self._codes.items() if t > now and hmac.compare_digest(c, offered)), None)
            if match is None:
                self._failures.append(now)
                if ip:
                    self._ip_failures.setdefault(ip, []).append(now)
                return None
            del self._codes[match]
            token = secrets.token_urlsafe(32)
            device = {"id": uuid.uuid4().hex[:12], "name": " ".join(str(name or "").split())[:40] or "手机",
                      "token_hash": _hash(token), "paired_at": int(now * 1000), "last_seen": int(now * 1000)}
            devices = [d for d in self._read_unlocked()][-(_MAX_DEVICES - 1):] + [device]
            self._write_unlocked(devices)
        return {"device_id": device["id"], "token": token}

    def locked(self, client_ip: str = "") -> bool:
        with self._lock:
            return self._is_locked_locked(time.time(), str(client_ip or "").strip())

    # -- signed-in devices ---------------------------------------------------------------------------

    def check(self, token: Optional[str]) -> Optional[str]:
        """The device id for a device token, or ``None``. Notes when it was last seen."""
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            devices = self._read_unlocked()
            device = next((d for d in devices if hmac.compare_digest(d.get("token_hash", ""), digest)), None)
            if device is None:
                return None
            now = int(time.time() * 1000)
            if now - int(device.get("last_seen") or 0) > 60_000:  # don't rewrite the file on every request
                device["last_seen"] = now
                self._write_unlocked(devices)
            return device["id"]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in d.items() if k != "token_hash"} for d in self._read_unlocked()]

    def remove(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read_unlocked()
            kept = [d for d in devices if d.get("id") != device_id]
            if len(kept) == len(devices):
                return False
            self._write_unlocked(kept)
            return True
