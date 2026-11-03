"""Images, videos and files the companion sends into the chat.

Each one is copied into ``<companion dir>/media`` and served at a signed
``/api/media/<sig>/<name>`` URL, the shape the app already renders. Copying
keeps a chat's pictures after the agent's caches are cleaned, and only files
in that folder can ever be served.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import re
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^\w.\-一-鿿]+")
_DOWNLOAD_LIMIT = 200 * 1024 * 1024
MEDIA_PREFIX = "/api/media"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MediaStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "media"
        self._secret = self._load_secret(Path(root) / "media.key")

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        # Stable across restarts: links in saved chats keep working.
        try:
            return bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            secret = secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret.hex(), encoding="ascii")
            return secret

    def sign(self, prefix: str, rel: str) -> str:
        """``<prefix>/<sig>/<payload>`` for a path under some served folder. The prefix is part of
        the signature, so a link for one folder never opens a file in another."""
        payload = _b64(rel.encode("utf-8"))
        return f"{prefix}/{_b64(self._mac(prefix, payload))}/{payload}"

    def verify(self, prefix: str, sig: str, payload: str) -> Optional[str]:
        """The relative path a signed link names, or None when the signature is wrong."""
        try:
            if not hmac.compare_digest(_unb64(sig), self._mac(prefix, payload)):
                return None
            return _unb64(payload).decode("utf-8")
        except (ValueError, UnicodeError):
            return None

    def _mac(self, prefix: str, payload: str) -> bytes:
        return hmac.new(self._secret, f"{prefix}\n{payload}".encode("utf-8"), hashlib.sha256).digest()[:16]

    def _sign(self, rel: str) -> str:
        return self.sign(MEDIA_PREFIX, rel)

    def _target(self, name: str) -> Path:
        clean = _SAFE_NAME.sub("_", Path(name).name).strip("._") or "file"
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{uuid.uuid4().hex[:10]}-{clean}"

    def stage_file(self, path: str | Path, name: Optional[str] = None) -> Optional[dict[str, str]]:
        """Copy a local file in; ``{"url", "name"}`` for the chat, or None if it can't be read."""
        source = Path(path).expanduser()
        if not source.is_file():
            logger.warning("companion: media file not found: %s", source)
            return None
        display = name or source.name
        target = self._target(display)
        shutil.copyfile(source, target)
        return {"url": self._sign(target.name), "name": display}

    async def stage_url(self, url: str, name: Optional[str] = None) -> Optional[dict[str, str]]:
        """Download a remote image/video so the phone loads it from this server (remote hosts may
        be unreachable from the phone, and their links expire)."""
        import httpx

        display = name or Path(url.split("?", 1)[0]).name or "image"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > _DOWNLOAD_LIMIT:
                    return None
                if not Path(display).suffix:
                    kind = response.headers.get("content-type", "").split(";")[0].strip()
                    display += mimetypes.guess_extension(kind) or ""
                target = self._target(display)
                target.write_bytes(response.content)
        except Exception as exc:
            logger.warning("companion: could not download media %s: %s", url[:120], exc)
            return None
        return {"url": self._sign(target.name), "name": display}

    def path_of(self, item: Optional[dict[str, str]]) -> Optional[str]:
        """The local copy behind a link this store made."""
        parts = (item or {}).get("url", "").split("/")
        path = self.resolve(parts[-2], parts[-1]) if len(parts) >= 2 else None
        return str(path) if path else None

    def resolve(self, sig: str, payload: str) -> Optional[Path]:
        """The file a signed URL names, or None when the signature or path is wrong."""
        rel = self.verify(MEDIA_PREFIX, sig, payload)
        return child_file(self.root, rel) if rel else None


def child_file(root: Path, rel: str) -> Optional[Path]:
    """``root/rel`` when it is an existing, non-hidden file inside ``root``."""
    if not rel or any(part.startswith(".") for part in Path(rel).parts):
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
