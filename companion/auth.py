"""Sign-in for the companion web app: short-lived tokens from ``/webui/bootstrap``.

Same contract as the web app expects (nanobot's): the client receives a WebSocket ``token`` plus
an ``api_token`` for HTTP calls (``Authorization: Bearer``). It proves who it is with a paired
device's token (see ``devices``), or the optional ``COMPANION_AUTH_SECRET`` password in
``X-Nanobot-Auth``. Browsers on this machine itself need neither.

Each session token remembers the device it was issued to, so removing a device ends its sessions,
and each owner is bounded independently so a polling client never evicts another device's tokens.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time

TOKEN_TTL_S = 24 * 60 * 60
_MAX_TOKENS = 2048
_MAX_PER_OWNER = 32
_REUSE_WINDOW_S = 60.0


class TokenIssuer:
    def __init__(self, secret: str | None) -> None:
        self._secret = secret or ""
        self._lock = threading.Lock()
        # token -> (expiry, device id, issued_at, purpose)
        self._tokens: dict[str, tuple[float, str | None, float, str]] = {}

    def password_ok(self, offered: str | None, *, local: bool) -> bool:
        if local and not offered:
            return True
        if not self._secret or not offered:
            return False
        return hmac.compare_digest(offered.encode("utf-8"), self._secret.encode("utf-8"))

    def issue(self, device: str | None = None, *, reuse: bool = False, purpose: str = "") -> str:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            if reuse and device is not None:
                for token, (expires, owner, issued_at, kind) in reversed(list(self._tokens.items())):
                    if (
                        owner == device
                        and kind == purpose
                        and now - issued_at <= _REUSE_WINDOW_S
                        and expires > now + TOKEN_TTL_S / 2
                    ):
                        return token
            self._enforce_owner_cap_locked(device)
            while len(self._tokens) >= _MAX_TOKENS:
                oldest = min(self._tokens.items(), key=lambda item: item[1][0])[0]
                self._tokens.pop(oldest, None)
            token = secrets.token_urlsafe(32)
            self._tokens[token] = (now + TOKEN_TTL_S, device, now, purpose)
            return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            entry = self._tokens.get(token)
            if not entry:
                return False
            if entry[0] <= now:
                self._tokens.pop(token, None)
                return False
            return True

    def device_of(self, token: str | None) -> str | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            entry = self._tokens.get(token)
            if not entry or entry[0] <= now:
                return None
            return entry[1]

    def revoke_device(self, device: str) -> None:
        with self._lock:
            for token, (_, owner, _, _) in list(self._tokens.items()):
                if owner == device:
                    del self._tokens[token]

    def _enforce_owner_cap_locked(self, device: str | None) -> None:
        owned = [tok for tok, (_, owner, _, _) in self._tokens.items() if owner == device]
        while len(owned) >= _MAX_PER_OWNER:
            victim = owned.pop(0)
            self._tokens.pop(victim, None)

    def _prune_locked(self, now: float) -> None:
        for token, (expires, _, _, _) in list(self._tokens.items()):
            if expires <= now:
                del self._tokens[token]
