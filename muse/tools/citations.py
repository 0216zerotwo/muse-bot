"""Turn-scoped structured citation collector (`ContextVar` + thread-safe mutable bag).

Tools (`web_search`, `web_extract`, `deep_research`, `knowledge_search`) call
`record_citations(...)` during a turn. Because a mutable collector instance is
stored in the `ContextVar`, `asyncio.to_thread` / `contextvars.copy_context().run`
automatically share the same collector across worker threads and return the
numbered `1..N` citations to the main turn coroutine.
"""

from __future__ import annotations

import contextlib
import threading
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar, Token
from typing import Any, Optional

from muse.interceptors import scrub_url_secrets

MAX_TURN_CITATIONS = 12
_KNOWLEDGE_PREFIX = "#knowledge:"


class _CitationCollector:
    __slots__ = ("_lock", "_items", "_by_key")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._by_key: dict[str, int] = {}

    def record(self, items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        recorded: list[dict[str, Any]] = []
        with self._lock:
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                clean_url, dedup_key, default_domain = _normalize_source_url(str(raw.get("url") or ""))
                if not clean_url:
                    continue
                domain = _clean_text(raw.get("domain"), 80) or default_domain
                title = _clean_text(raw.get("title"), 160) or domain or clean_url
                snippet = _clean_text(
                    scrub_url_secrets(str(raw.get("snippet") or raw.get("description") or "")),
                    280,
                )

                existing_idx = self._by_key.get(dedup_key)
                if existing_idx is not None:
                    entry = self._items[existing_idx]
                    if entry["title"] in ("", entry["domain"], entry["url"]) and title not in ("", domain, clean_url):
                        entry["title"] = title
                    if not entry["snippet"] and snippet:
                        entry["snippet"] = snippet
                    recorded.append(dict(entry))
                    continue

                if len(self._items) >= MAX_TURN_CITATIONS:
                    continue

                entry = {
                    "id": len(self._items) + 1,
                    "title": title,
                    "url": clean_url,
                    "domain": domain,
                    "snippet": snippet,
                }
                self._by_key[dedup_key] = len(self._items)
                self._items.append(entry)
                recorded.append(dict(entry))
        return recorded

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._items]


_TURN_CITATIONS: ContextVar[Optional[_CitationCollector]] = ContextVar("_TURN_CITATIONS", default=None)


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _normalize_source_url(raw_url: str) -> tuple[str, str, str]:
    text = str(raw_url or "").strip()
    if not text:
        return "", "", ""
    if text.startswith(_KNOWLEDGE_PREFIX):
        doc_id = text[len(_KNOWLEDGE_PREFIX) :].strip()
        if not doc_id:
            return "", "", ""
        canonical = f"{_KNOWLEDGE_PREFIX}{doc_id}"
        return canonical, canonical, "知识库"

    scrubbed = scrub_url_secrets(text)
    try:
        parsed = urllib.parse.urlsplit(scrubbed)
    except ValueError:
        return "", "", ""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return "", "", ""

    # Strip embedded userinfo (user:pass@) so credentials never leak into citations.
    port_suffix = f":{parsed.port}" if parsed.port else ""
    netloc = f"[{host}]{port_suffix}" if ":" in host else f"{host}{port_suffix}"
    clean_url = urllib.parse.urlunsplit((scheme, netloc, parsed.path or "", parsed.query or "", parsed.fragment or ""))
    clean_url = scrub_url_secrets(clean_url)

    norm_path = (parsed.path or "").rstrip("/")
    dedup_key = urllib.parse.urlunsplit((scheme, netloc, norm_path, parsed.query or "", ""))
    domain = host[4:] if host.startswith("www.") and len(host) > 4 else host
    return clean_url, dedup_key, domain


def start_turn_citations() -> Token[Optional[_CitationCollector]]:
    """Initialize a fresh per-turn citation collector in the current context."""
    return _TURN_CITATIONS.set(_CitationCollector())


def reset_turn_citations(token: Token[Optional[_CitationCollector]]) -> None:
    """Restore the previous citation collector context."""
    _TURN_CITATIONS.reset(token)


@contextlib.contextmanager
def turn_citations_scope() -> Iterator[None]:
    """Context manager scoping structured citations to a single turn."""
    token = start_turn_citations()
    try:
        yield
    finally:
        reset_turn_citations(token)


def record_citations(items: Iterable[Mapping[str, Any]] | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Record one or more citation sources into the active turn collector."""
    collector = _TURN_CITATIONS.get()
    if collector is None:
        return []
    batch: Iterable[Mapping[str, Any]] = [items] if isinstance(items, Mapping) else items
    return collector.record(batch)


def get_turn_citations() -> list[dict[str, Any]]:
    """Return a snapshot of all structured citations recorded in the active turn."""
    collector = _TURN_CITATIONS.get()
    return collector.snapshot() if collector is not None else []


__all__ = [
    "MAX_TURN_CITATIONS",
    "get_turn_citations",
    "record_citations",
    "reset_turn_citations",
    "start_turn_citations",
    "turn_citations_scope",
]
