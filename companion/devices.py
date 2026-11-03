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
or by clearing the app's data. Too many wrong codes lock pairing for a while.
"""

from __future__ import annotations

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
_LOCK_S = 10 * 60
_MAX_DEVICES = 50


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch in _ALPHABET)


class Devices:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "devices.json"
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}  # normalized code -> expiry
        self._failures: list[float] = []

    # -- storage -------------------------------------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _write(self, devices: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(devices, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def any_paired(self) -> bool:
        return bool(self._read())

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

    def pair(self, code: str, name: str) -> Optional[dict[str, Any]]:
        """Trade a code for a device token: ``{"device_id", "token"}``, or ``None`` (wrong code,
        expired, or pairing locked after too many wrong tries)."""
        offered = normalize_code(code)
        with self._lock:
            now = time.time()
            self._failures = [t for t in self._failures if now - t < _LOCK_S]
            if len(self._failures) >= _MAX_FAILURES:
                return None
            match = next((c for c, t in self._codes.items() if t > now and hmac.compare_digest(c, offered)), None)
            if match is None:
                self._failures.append(now)
                return None
            del self._codes[match]
            token = secrets.token_urlsafe(32)
            device = {"id": uuid.uuid4().hex[:12], "name": " ".join(str(name or "").split())[:40] or "手机",
                      "token_hash": _hash(token), "paired_at": int(now * 1000), "last_seen": int(now * 1000)}
            devices = [d for d in self._read()][-(_MAX_DEVICES - 1):] + [device]
            self._write(devices)
        return {"device_id": device["id"], "token": token}

    def locked(self) -> bool:
        now = time.time()
        return len([t for t in self._failures if now - t < _LOCK_S]) >= _MAX_FAILURES

    # -- signed-in devices ---------------------------------------------------------------------------

    def check(self, token: Optional[str]) -> Optional[str]:
        """The device id for a device token, or ``None``. Notes when it was last seen."""
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            devices = self._read()
            device = next((d for d in devices if hmac.compare_digest(d.get("token_hash", ""), digest)), None)
            if device is None:
                return None
            now = int(time.time() * 1000)
            if now - int(device.get("last_seen") or 0) > 60_000:  # don't rewrite the file on every request
                device["last_seen"] = now
                self._write(devices)
            return device["id"]

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in d.items() if k != "token_hash"} for d in self._read()]

    def remove(self, device_id: str) -> bool:
        with self._lock:
            devices = self._read()
            kept = [d for d in devices if d.get("id") != device_id]
            if len(kept) == len(devices):
                return False
            self._write(kept)
            return True
