"""Shared security, cache, SSRF, and OAuth credential helpers for ``muse.media``."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlsplit

import httpx

from muse.config_store import get_env_value, load_config
from muse.paths import get_muse_home

logger = logging.getLogger(__name__)

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata.goog"}
)
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_BENCHMARK_V4 = ipaddress.ip_network("198.18.0.0/15")
_AWS_META_V6 = ipaddress.ip_address("fd00:ec2::254")

_FALLBACK_SECRET_RE = re.compile(
    r"(?:^|[\s/\\'\"`=:(,@])\.env(?!(?:\.example|\.sample|\.template)\b)(?:\.[a-z0-9_-]+)?"
    r"(?=$|[\s'\"`;|&)>,])"
    r"|(?:^|[\s/\\'\"`=:(@])(?:auth\.json|media\.key|vapid_private\.pem|devices\.json|"
    r"triggers\.json|subscriptions\.json|vault-keys\.json|approval-grants\.json|muse\.db|state\.db)"
    r"(?=$|[\s'\"`;|&)>,])"
    r"|[/\\]vault(?:[/\\]|$)|[/\\]\.codex[/\\]+auth\.json\b|[/\\]\.ssh[/\\]+"
    r"|[/\\]\.aws[/\\]+credentials\b|[/\\]\.gnupg[/\\]|(?:^|[/\\])_?\.netrc\b",
    re.IGNORECASE,
)


def is_secret_path(path_or_text: str | Path) -> bool:
    """Return True if ``path_or_text`` references a protected secret file or directory."""
    raw = str(path_or_text or "").strip()
    if not raw:
        return False
    try:
        from muse.interceptors import is_secret_path as interceptor_check

        if interceptor_check(raw):
            return True
    except Exception:
        pass
    try:
        from companion.modules.secrets_guard import is_secret_path as companion_check

        if companion_check(raw):
            return True
    except Exception:
        pass
    low = raw.lower()
    if low.startswith(("http://", "https://", "data:")):
        return False
    if low.startswith("file://"):
        return True
    if _FALLBACK_SECRET_RE.search(raw):
        return True
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = Path(raw).expanduser().resolve()
        if _FALLBACK_SECRET_RE.search(str(resolved)) or _FALLBACK_SECRET_RE.search(resolved.as_posix()):
            return True
        home = get_muse_home().resolve()
        for target in (
            home / ".env",
            home / "auth.json",
            home / "vault",
            home / "muse.db",
            home / "state.db",
            home / "companion" / "media.key",
            home / "companion" / "devices.json",
            home / "companion" / "triggers.json",
        ):
            if resolved == target or target in resolved.parents:
                return True
    return False


def _ip_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _ip_blocked(addr.ipv4_mapped)
    if addr == _AWS_META_V6:
        return True
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return isinstance(addr, ipaddress.IPv4Address) and (
        addr in _CGNAT_V4 or addr in _BENCHMARK_V4
    )


def is_safe_url(url: str) -> bool:
    """Validate that ``url`` is a public HTTP(S) URL safe against SSRF."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https") or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return not _ip_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        from muse.tools.web import is_safe_public_url

        if not is_safe_public_url(url):
            return False
    except Exception:
        pass
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            if _ip_blocked(ipaddress.ip_address(raw_ip)):
                return False
        except ValueError:
            return False
    return True


def fetch_url_bytes(
    url: str,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    timeout: float = 60.0,
    headers: Optional[dict[str, str]] = None,
    max_redirects: int = 4,
    trusted_origin: bool = False,
) -> tuple[bytes, dict[str, str]]:
    """Synchronously download ``url`` with per-hop SSRF checks and a hard byte cap."""
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for hop in range(max_redirects + 1):
            if not (trusted_origin and hop == 0) and not is_safe_url(current):
                raise ValueError(f"Blocked unsafe URL: {urlsplit(current).hostname or current[:60]}")
            with client.stream("GET", current) as resp:
                if resp.status_code in _REDIRECT_CODES and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(65_536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks), {k.lower(): v for k, v in resp.headers.items()}
    raise ValueError("Too many redirects")


def get_secret(name: str) -> str:
    """Resolve an API key or secret from ``.env`` / process environment."""
    if not name:
        return ""
    val = get_env_value(name)
    if val:
        return str(val).strip()
    return str(os.environ.get(name, "") or "").strip()


def load_media_section(section: str) -> dict[str, Any]:
    """Read a top-level section (e.g. ``image_gen``, ``video_gen``, ``stt``, ``tts``) from config."""
    try:
        cfg = load_config()
    except Exception:
        return {}
    val = cfg.get(section) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_media_cache_dir(subdir: str) -> Path:
    """Ensure and return ``<MUSE_HOME>/cache/<subdir>`` with owner-only POSIX perms."""
    target = get_muse_home() / "cache" / subdir
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
    return target


def _safe_prefix(prefix: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(prefix or fallback)).strip("_")
    return (cleaned or fallback)[:48]


def save_bytes_media(
    subdir: str,
    raw: bytes,
    *,
    prefix: str = "media",
    extension: str = "bin",
) -> Path:
    """Write raw bytes to ``<MUSE_HOME>/cache/<subdir>/<prefix>_<ts>_<rand>.<ext>``."""
    folder = get_media_cache_dir(subdir)
    ext = re.sub(r"[^a-z0-9]+", "", str(extension or "bin").lower()) or "bin"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = folder / f"{_safe_prefix(prefix, 'media')}_{stamp}_{uuid.uuid4().hex[:8]}.{ext}"
    path.write_bytes(raw)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return path


def save_b64_media(
    subdir: str,
    b64_data: str,
    *,
    prefix: str = "media",
    extension: str = "png",
) -> Path:
    """Decode base64 data (stripping optional ``data:...;base64,`` prefix) and save."""
    payload = b64_data.split(",", 1)[1] if b64_data.startswith("data:") and "," in b64_data else b64_data
    return save_bytes_media(subdir, base64.b64decode(payload), prefix=prefix, extension=extension)


def sniff_image_mime(raw: bytes) -> Optional[str]:
    """Identify common raster image MIME types from magic bytes."""
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_source_image_bytes(ref: str, *, max_bytes: int = 25 * 1024 * 1024) -> tuple[bytes, str, str]:
    """Load ``(bytes, filename, mime)`` from an HTTP(S) URL, data URI, or local path."""
    candidate = str(ref or "").strip()
    if not candidate:
        raise ValueError("Empty image input")
    low = candidate.lower()
    if low.startswith("data:"):
        if "," not in candidate:
            raise ValueError("Malformed data URL")
        header, b64 = candidate.split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError("Only base64 image data URLs are supported")
        raw = base64.b64decode(b64)
        if len(raw) > max_bytes:
            raise ValueError(f"Image data URL exceeds {max_bytes} bytes")
        mime = sniff_image_mime(raw) or "image/png"
        ext = mime.split("/", 1)[1]
        return raw, f"image.{ext}", mime
    if low.startswith(("http://", "https://")):
        raw, headers = fetch_url_bytes(candidate, max_bytes=max_bytes, timeout=60.0)
        mime = sniff_image_mime(raw) or headers.get("content-type", "image/png").split(";", 1)[0].strip()
        name = candidate.split("?", 1)[0].rsplit("/", 1)[-1] or "image.png"
        return raw, name, mime
    if is_secret_path(candidate):
        raise PermissionError("Refusing to read protected secret file as image input")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image file not found: {candidate}")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"Image file exceeds {max_bytes} bytes: {candidate}")
    mime = sniff_image_mime(raw) or "image/png"
    return raw, path.name, mime


def image_to_data_url(ref: str, *, max_bytes: int = 25 * 1024 * 1024) -> str:
    """Convert any local path, data URL, or safe HTTP(S) image URL to an inline data URL."""
    raw, _, mime = load_source_image_bytes(ref, max_bytes=max_bytes)
    if not mime.startswith("image/"):
        raise ValueError(f"Unsupported image MIME type: {mime}")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    seg = token.split(".")[1]
    seg += "=" * ((4 - len(seg) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_official_codex_url(base_url: str) -> bool:
    try:
        p = urlparse(str(base_url or "").strip())
        path = p.path.rstrip("/")
        return (
            p.scheme == "https"
            and (p.hostname or "").lower() == "chatgpt.com"
            and p.port in (None, 443)
            and (path == "/backend-api/codex" or path.startswith("/backend-api/codex/"))
        )
    except Exception:
        return False


def codex_headers(access_token: str, *, base_url: str = DEFAULT_CODEX_BASE_URL) -> dict[str, str]:
    """Build Codex Cloudflare + JWT account/residency headers."""
    try:
        from muse.llm.oauth import codex_cloudflare_headers

        return dict(codex_cloudflare_headers(access_token, base_url=base_url))
    except ImportError:
        pass
    headers = (
        {"User-Agent": "muse-bot/2.0", "originator": "muse-bot"}
        if _is_official_codex_url(base_url)
        else {"User-Agent": "codex_cli_rs/0.0.0 (Muse Bot)", "originator": "codex_cli_rs"}
    )
    auth = _decode_jwt_claims(access_token).get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        acct = auth.get("chatgpt_account_id")
        if isinstance(acct, str) and acct.strip():
            headers["ChatGPT-Account-ID"] = acct.strip()
        res = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
        if isinstance(res, str) and res.strip():
            headers["x-openai-internal-codex-residency"] = res.strip()
    return headers


def resolve_codex_credentials() -> tuple[Optional[str], str]:
    """Resolve ``(access_token, base_url)`` for ``openai-codex`` via ``muse.llm.oauth`` or ``auth.json``."""
    default_url = (
        os.environ.get("MUSE_CODEX_BASE_URL")
        or os.environ.get("HERMES_CODEX_BASE_URL")
        or DEFAULT_CODEX_BASE_URL
    ).strip().rstrip("/")
    try:
        from muse.llm.oauth import resolve_codex_runtime_credentials

        creds = resolve_codex_runtime_credentials()
        tok = str(creds.get("api_key") or "").strip()
        url = str(creds.get("base_url") or default_url).strip().rstrip("/")
        if tok:
            return tok, url
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("muse.media: codex oauth resolution failed (%s)", type(exc).__name__)
        return None, default_url

    auth_file = get_muse_home() / "auth.json"
    if not auth_file.is_file():
        return None, default_url
    try:
        store = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return None, default_url
    prov = (store.get("providers") or {}).get("openai-codex")
    if isinstance(prov, dict):
        tokens = prov.get("tokens")
        if isinstance(tokens, dict) and str(tokens.get("access_token") or "").strip():
            return str(tokens["access_token"]).strip(), default_url
    pool = (store.get("credential_pool") or {}).get("openai-codex")
    for entry in pool if isinstance(pool, list) else ():
        if isinstance(entry, dict) and str(entry.get("access_token") or "").strip():
            url = str(entry.get("base_url") or default_url).strip().rstrip("/")
            return str(entry["access_token"]).strip(), url
    return None, default_url


def resolve_xai_credentials(*, prefer_api_key: bool = False) -> dict[str, str]:
    """Resolve xAI credentials (``XAI_API_KEY`` or ``xai-oauth``)."""
    direct_key = get_secret("XAI_API_KEY")
    direct_base = (get_secret("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL).rstrip("/")

    if prefer_api_key and direct_key:
        return {"provider": "xai", "api_key": direct_key, "base_url": direct_base}

    try:
        from muse.llm.oauth import resolve_xai_oauth_runtime_credentials

        creds = resolve_xai_oauth_runtime_credentials()
        tok = str(creds.get("api_key") or "").strip()
        if tok:
            return {
                "provider": "xai-oauth",
                "api_key": tok,
                "base_url": str(creds.get("base_url") or DEFAULT_XAI_BASE_URL).strip().rstrip("/"),
            }
    except ImportError:
        auth_file = get_muse_home() / "auth.json"
        if auth_file.is_file():
            with contextlib.suppress(Exception):
                store = json.loads(auth_file.read_text(encoding="utf-8-sig"))
                prov = (store.get("providers") or {}).get("xai-oauth")
                tokens = prov.get("tokens") if isinstance(prov, dict) else None
                if isinstance(tokens, dict) and str(tokens.get("access_token") or "").strip():
                    return {
                        "provider": "xai-oauth",
                        "api_key": str(tokens["access_token"]).strip(),
                        "base_url": DEFAULT_XAI_BASE_URL,
                    }
    except Exception as exc:
        logger.debug("muse.media: xai-oauth resolution failed (%s)", type(exc).__name__)

    if direct_key:
        return {"provider": "xai", "api_key": direct_key, "base_url": direct_base}
    return {"provider": "xai", "api_key": "", "base_url": direct_base}


__all__ = [
    "DEFAULT_CODEX_BASE_URL",
    "DEFAULT_OPENROUTER_BASE_URL",
    "DEFAULT_XAI_BASE_URL",
    "codex_headers",
    "fetch_url_bytes",
    "get_media_cache_dir",
    "get_secret",
    "image_to_data_url",
    "is_safe_url",
    "is_secret_path",
    "load_media_section",
    "load_source_image_bytes",
    "resolve_codex_credentials",
    "resolve_xai_credentials",
    "save_b64_media",
    "save_bytes_media",
    "sniff_image_mime",
]
