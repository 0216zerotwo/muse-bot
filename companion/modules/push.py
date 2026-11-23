"""Web Push: reach the user's phone when there is news (ported from nanobot).

The companion pushes when it has something the user did not just ask for: a
message it sent on its own (早报, 主动关心, reminders, an approval outcome) or an
action waiting for approval. Ordinary replies are not pushed. Subscriptions and
the VAPID key live in ``<companion dir>/push``; subscriptions a push service
reports as gone are dropped.

Messages are encrypted (RFC 8291, aes128gcm) and signed (VAPID, RFC 8292) here
with ``http_ece`` and ``py_vapid`` and sent with httpx.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from . import net_safety
from .paths import get_webui_dir

logger = logging.getLogger(__name__)

# Push services require a mailto: contact in the VAPID claims. A placeholder at
# the reserved example.com domain avoids sending the user's address to them.
_VAPID_SUBJECT = "mailto:personal-bot@example.com"
_TTL_S = 24 * 3600
_BODY_CHARS = 140
_LOCK = threading.Lock()
_background: set[asyncio.Task[Any]] = set()


def available() -> bool:
    """Whether the encryption/signing packages are installed (plugin.yaml pip_dependencies)."""
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in ("py_vapid", "http_ece"))


def _push_dir() -> Path:
    return get_webui_dir() / "push"


def _vapid():
    from py_vapid import Vapid02

    path = _push_dir() / "vapid_private.pem"
    with _LOCK:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Vapid02()
            key.generate_keys()
            key.save_key(str(path))
            os.chmod(path, 0o600)
        return Vapid02.from_file(str(path))


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key() -> str:
    """Application server key for ``PushManager.subscribe`` (URL-safe base64, no padding)."""
    from cryptography.hazmat.primitives import serialization

    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _subscriptions_path() -> Path:
    return _push_dir() / "subscriptions.json"


def list_subscriptions() -> list[dict[str, Any]]:
    try:
        data = json.loads(_subscriptions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save(subscriptions: list[dict[str, Any]]) -> None:
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(subscriptions, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _valid(subscription: Any) -> bool:
    if not isinstance(subscription, dict):
        return False
    endpoint, keys = subscription.get("endpoint"), subscription.get("keys")
    return (
        isinstance(endpoint, str)
        and endpoint.startswith("https://")
        and net_safety.is_safe_url_target(endpoint)
        and isinstance(keys, dict)
        and isinstance(keys.get("p256dh"), str)
        and isinstance(keys.get("auth"), str)
    )


def add_subscription(subscription: Any) -> int:
    if not _valid(subscription):
        raise ValueError("invalid push subscription")
    entry = {"endpoint": subscription["endpoint"],
             "keys": {"p256dh": subscription["keys"]["p256dh"], "auth": subscription["keys"]["auth"]}}
    with _LOCK:
        subscriptions = [s for s in list_subscriptions() if s.get("endpoint") != entry["endpoint"]]
        subscriptions.append(entry)
        _save(subscriptions)
        return len(subscriptions)


def remove_subscription(endpoint: str) -> int:
    with _LOCK:
        subscriptions = [s for s in list_subscriptions() if s.get("endpoint") != endpoint]
        _save(subscriptions)
        return len(subscriptions)


def _request(subscription: dict[str, Any], payload: bytes, vapid: Any) -> tuple[dict[str, str], bytes]:
    """Headers and encrypted body for one subscription."""
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec

    body = http_ece.encrypt(
        payload, salt=os.urandom(16), private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_b64decode(subscription["keys"]["p256dh"]), auth_secret=_b64decode(subscription["keys"]["auth"]),
        version="aes128gcm",
    )
    endpoint = urlparse(subscription["endpoint"])
    claims = {"sub": _VAPID_SUBJECT, "aud": f"{endpoint.scheme}://{endpoint.netloc}", "exp": int(time.time()) + 12 * 3600}
    headers = {str(k): str(v) for k, v in vapid.sign(claims).items()}
    headers.update({"TTL": str(_TTL_S), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream"})
    return headers, body


async def send_push(title: str, body: str, *, tag: Optional[str] = None, url: str = "/companion.html") -> int:
    """Send to every subscribed device; returns how many accepted it."""
    import httpx

    subscriptions = list_subscriptions()
    if not subscriptions or not available():
        return 0
    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url}, ensure_ascii=False).encode("utf-8")
    vapid = await asyncio.to_thread(_vapid)
    sent = 0
    # trust_env: honour HTTPS_PROXY where push services are only reachable via a proxy.
    async with httpx.AsyncClient(timeout=15, trust_env=True, follow_redirects=False) as client:
        for subscription in subscriptions:
            endpoint = str(subscription.get("endpoint") or "")
            if not endpoint.startswith("https://") or not net_safety.is_safe_url(endpoint):
                logger.warning("companion: skipped unsafe push subscription endpoint")
                continue
            try:
                headers, data = _request(subscription, payload, vapid)
                response = await client.post(endpoint, headers=headers, content=data)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("companion: push service unreachable: %s", exc)
                continue
            if response.status_code in (404, 410):
                remove_subscription(subscription["endpoint"])
                logger.info("companion: a push subscription expired and was removed")
            elif response.status_code >= 400:
                logger.warning("companion: push failed (%s): %s", response.status_code, response.text[:200])
            else:
                sent += 1
    return sent


def schedule_push(title: str, body: str, *, tag: Optional[str] = None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Fire-and-forget push; never delays delivery. ``loop`` when called off the event loop."""
    if not list_subscriptions():
        return
    if loop is not None:
        asyncio.run_coroutine_threadsafe(send_push(title, body, tag=tag), loop)
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = running.create_task(send_push(title, body, tag=tag))
    _background.add(task)
    task.add_done_callback(_background.discard)


def preview(text: str) -> str:
    """Plain one-paragraph preview of a Markdown reply for a notification body."""
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"[*_`#>~|]", "", plain)
    plain = " ".join(plain.split())
    return plain if len(plain) <= _BODY_CHARS else plain[: _BODY_CHARS - 1] + "…"
