"""Sign-in for the companion web app: short-lived tokens from ``/webui/bootstrap``.

Same contract as the web app expects (nanobot's): the client receives a WebSocket ``token`` plus
an ``api_token`` for HTTP calls (``Authorization: Bearer``). It proves who it is with a paired
device's token (see ``devices``), or the optional ``COMPANION_AUTH_SECRET`` password in
``X-Nanobot-Auth``. Browsers on this machine itself need neither.

Each session token remembers the device it was issued to, so removing a device ends its sessions.
"""

from __future__ import annotations

import hmac
import secrets
import time

TOKEN_TTL_S = 24 * 60 * 60
_MAX_TOKENS = 512


class TokenIssuer:
    def __init__(self, secret: str | None) -> None:
        self._secret = secret or ""
        self._tokens: dict[str, tuple[float, str | None]] = {}  # token -> (expiry, device id)

    def password_ok(self, offered: str | None, *, local: bool) -> bool:
        if local and not offered:
            return True
        if not self._secret or not offered:
            return False
        return hmac.compare_digest(offered.encode("utf-8"), self._secret.encode("utf-8"))

    def issue(self, device: str | None = None) -> str:
        self._prune()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (time.time() + TOKEN_TTL_S, device)
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        entry = self._tokens.get(token)
        return bool(entry and entry[0] > time.time())

    def device_of(self, token: str | None) -> str | None:
        entry = self._tokens.get(token or "")
        return entry[1] if entry else None

    def revoke_device(self, device: str) -> None:
        for token, (_, owner) in list(self._tokens.items()):
            if owner == device:
                del self._tokens[token]

    def _prune(self) -> None:
        now = time.time()
        for token, (expires, _) in list(self._tokens.items()):
            if expires <= now:
                del self._tokens[token]
        while len(self._tokens) >= _MAX_TOKENS:
            self._tokens.pop(next(iter(self._tokens)))
