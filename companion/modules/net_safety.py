"""SSRF-safe HTTP fetching and bounded downloads for the companion plugin.

Uses Hermes's ``tools.url_safety.is_safe_url`` when available, plus a standalone
IP/DNS validator so unit tests and standalone tools get identical SSRF protection:

- only ``http://`` and ``https://`` URLs without embedded credentials;
- cloud metadata endpoints (``169.254.169.254``, ``100.100.100.200``, ``fd00:ec2::254``,
  ``metadata.google.internal``), loopback, RFC1918 private, link-local, CGNAT, multicast
  and unspecified addresses are blocked (including IPv4-mapped IPv6 ``::ffff:...``);
- every redirect hop is re-validated before connecting;
- response bodies are streamed with a hard byte ceiling so oversized responses never OOM.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal", "metadata.goog"})
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_BENCHMARK_V4 = ipaddress.ip_network("198.18.0.0/15")
_AWS_META_V6 = ipaddress.ip_address("fd00:ec2::254")


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
    if isinstance(addr, ipaddress.IPv4Address) and (addr in _CGNAT_V4 or addr in _BENCHMARK_V4):
        return True
    return False


def is_safe_url_target(url: str) -> bool:
    """Syntactic + literal-IP SSRF check (no DNS network call). Used at configuration time
    (``triggers.add_trigger``, ``push.add_subscription``) so offline environments and fake-IP TUN
    proxies do not reject valid public domain names before a request is ever made."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not _ip_blocked(literal)


def _builtin_is_safe_url(url: str) -> bool:
    if not is_safe_url_target(url):
        return False
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return not _ip_blocked(literal)
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                                   type=socket.SOCK_STREAM)
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
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if _ip_blocked(ip):
            return False
    return True


def is_safe_url(url: str) -> bool:
    """True when ``url`` is a public HTTP(S) URL safe for server-side fetching."""
    if not is_safe_url_target(url):
        return False
    try:
        from tools.url_safety import is_safe_url as hermes_is_safe_url
    except Exception:
        return _builtin_is_safe_url(url)
    return bool(hermes_is_safe_url(url))


def fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    headers: Optional[dict[str, str]] = None,
    max_redirects: int = 4,
    truncate: bool = False,
) -> tuple[bytes, dict[str, str]]:
    """Synchronously fetch ``url`` with per-hop SSRF validation and bounded streaming."""
    import httpx

    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(max_redirects + 1):
            if not is_safe_url(current):
                raise ValueError(f"blocked unsafe URL: {urlsplit(current).hostname or current[:60]}")
            with client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_CODES and response.headers.get("location"):
                    current = urljoin(current, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(65_536):
                    if truncate and size + len(chunk) >= max_bytes:
                        chunks.append(chunk[: max_bytes - size])
                        size = max_bytes
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks), {k.lower(): v for k, v in response.headers.items()}
    raise ValueError("too many redirects")


async def async_fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 60.0,
    headers: Optional[dict[str, str]] = None,
    max_redirects: int = 4,
    truncate: bool = False,
) -> tuple[bytes, dict[str, str]]:
    """Asynchronously fetch ``url`` with per-hop SSRF validation and bounded streaming."""
    import httpx

    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(max_redirects + 1):
            if not is_safe_url(current):
                raise ValueError(f"blocked unsafe URL: {urlsplit(current).hostname or current[:60]}")
            async with client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_CODES and response.headers.get("location"):
                    current = urljoin(current, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(65_536):
                    if truncate and size + len(chunk) >= max_bytes:
                        chunks.append(chunk[: max_bytes - size])
                        size = max_bytes
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks), {k.lower(): v for k, v in response.headers.items()}
    raise ValueError("too many redirects")


async def async_download_to_file(
    url: str,
    target_dir: Path,
    *,
    max_bytes: int,
    timeout: float = 180.0,
    max_redirects: int = 4,
) -> tuple[Optional[Path], dict[str, str]]:
    """Stream a public URL into a temporary file under ``target_dir``, validating every redirect."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f".download-{os.getpid()}-{datetime.now():%H%M%S%f}-{uuid.uuid4().hex[:6]}"
    current = url
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                if not is_safe_url(current):
                    logger.warning("companion: blocked unsafe media download from %s", urlsplit(current).hostname)
                    return None, {}
                async with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_CODES and response.headers.get("location"):
                        current = urljoin(current, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        return None, {}
                    size = 0
                    with tmp.open("wb") as out:
                        async for chunk in response.aiter_bytes(65_536):
                            size += len(chunk)
                            if size > max_bytes:
                                out.close()
                                tmp.unlink(missing_ok=True)
                                return None, {}
                            out.write(chunk)
                    return tmp, {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.unlink(missing_ok=True)
    return None, {}
