"""查找聊天记录 v2: FTS5-accelerated cross-session search with ChatStore fallback and match offsets.

Combines `MuseDB.search_messages(...)` (FTS5 trigram BM25 + CJK `LIKE`) with `ChatStore` transcript
scanning so pure `ChatStore` fixtures and indexed SQLite transcripts are seamlessly merged and
deduplicated by `(chat_id, turn_id, role)`. Cards and UI blocks are stripped before matching so
internal widget markup never leaks into results. Hidden `desk` and `cron` chats are always excluded.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

_CARD = re.compile(r"```(?:card|json|ui)[ \t]*\n[\s\S]*?(?:\n```|$)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AROUND = 28
_DEFAULT_LIMIT = 30
_MAX_LIMIT = 50
_VALID_SCOPES = frozenset({"all", "main", "sides", "helpers"})


def _plain(text: str) -> str:
    text = _CARD.sub(" ", text or "")
    text = _IMAGE.sub(r" \1 ", text)
    text = _LINK.sub(r"\1", text)
    return " ".join(text.replace("**", "").split())


def _snippet(text: str, at: int, length: int) -> str:
    start, end = max(0, at - _AROUND), min(len(text), at + length + _AROUND)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _normalize_role_filter(role: str | None) -> str | None:
    raw = str(role or "").strip().lower()
    if raw in ("", "all"):
        return None
    if raw == "user":
        return "user"
    if raw in ("assistant", "reply"):
        return "assistant"
    return None


def _normalize_chat_id(chat_id: str | None) -> str | None:
    raw = str(chat_id or "").strip()
    if not raw:
        return None
    return raw[len("websocket:") :] if raw.startswith("websocket:") else raw


def _find_match_position(text: str, needle: str, tokens: list[str]) -> tuple[int, int]:
    folded = text.casefold()
    at = folded.find(needle)
    if at >= 0:
        return at, len(needle)
    if len(tokens) > 1 and all(tok in folded for tok in tokens):
        first_at = min(folded.find(tok) for tok in tokens)
        matched_tok = next(tok for tok in tokens if folded.find(tok) == first_at)
        return first_at, len(matched_tok)
    return -1, 0


def _compute_matches(snippet: str, words: str, tokens: list[str]) -> list[list[int]]:
    """Return sorted non-overlapping `[start, end]` character intervals matching query in `snippet`."""
    if not snippet or not words:
        return []
    candidates: list[str] = []
    seen_low: set[str] = set()
    for term in (words, *tokens):
        cleaned = term.strip()
        if not cleaned:
            continue
        low = cleaned.casefold()
        if low not in seen_low:
            seen_low.add(low)
            candidates.append(cleaned)
    if not candidates:
        return []
    candidates.sort(key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(c) for c in candidates), re.IGNORECASE)
    return [[m.start(), m.end()] for m in pattern.finditer(snippet)]


def _eligible_chats(
    index: dict[str, dict[str, Any]],
    *,
    main_chat_id: str | None,
    target_chat_id: str | None,
    scope: str,
) -> dict[str, dict[str, Any]]:
    allowed: dict[str, dict[str, Any]] = {}
    for cid, meta in index.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("desk") or meta.get("cron") or str(cid).startswith("cron:"):
            continue
        if target_chat_id is not None and cid != target_chat_id:
            continue
        is_helper = bool(meta.get("helper"))
        is_main = bool(main_chat_id and cid == main_chat_id)
        if scope == "main":
            if not is_main:
                continue
        elif scope == "sides":
            if is_main or is_helper:
                continue
        elif scope == "helpers":
            if not is_helper:
                continue
        else:
            # Default scope ("all"): exclude helper working chats unless that specific chat_id is requested.
            if is_helper and target_chat_id is None:
                continue
        allowed[cid] = meta
    return allowed


def _chat_title(cid: str, meta: dict[str, Any], main_chat_id: str | None) -> str:
    if main_chat_id and cid == main_chat_id:
        return "主要聊天"
    return str(meta.get("title") or meta.get("helper") or "旁聊")


def _build_hit(
    *,
    cid: str,
    meta: dict[str, Any],
    main_chat_id: str | None,
    hit_role: str,
    turn_id: str | None,
    snippet: str,
    at_ms: int,
    words: str,
    tokens: list[str],
) -> dict[str, Any]:
    title = _chat_title(cid, meta, main_chat_id)
    is_main = bool(main_chat_id and cid == main_chat_id)
    helper_name = str(meta.get("helper") or "")
    matches = _compute_matches(snippet, words, tokens)
    return {
        "chat_id": cid,
        "key": f"websocket:{cid}",
        "chat_title": title,
        "title": title,
        "main": is_main,
        "helper": helper_name,
        "turn_id": turn_id,
        "role": hit_role,
        "text": snippet,
        "snippet": snippet,
        "at_ms": at_ms,
        "created_at_ms": at_ms,
        "matches": matches,
    }


def _dedup_key(cid: str, turn_id: Any, role: str, at_ms: int, snippet: str) -> tuple[str, str, str]:
    tid = str(turn_id or "").strip()
    if tid:
        return (cid, tid, role)
    return (cid, f"@{at_ms // 1000}:{snippet[:24]}", role)


def _search_musedb(
    *,
    query: str,
    allowed: dict[str, dict[str, Any]],
    main_chat_id: str | None,
    target_chat_id: str | None,
    role_filter: str | None,
    limit: int,
    words: str,
    needle: str,
    tokens: list[str],
    db: Any = None,
) -> list[dict[str, Any]]:
    roles = [role_filter] if role_filter else ["user", "assistant"]
    fetch_limit = min(200, max(limit * 4, 60))

    def _run_query(conn_db: Any) -> list[dict[str, Any]]:
        return list(
            conn_db.search_messages(
                query,
                chat_id=target_chat_id,
                roles=roles,
                limit=fetch_limit,
            )
            or []
        )

    rows: list[dict[str, Any]] = []
    try:
        if db is not None:
            rows = _run_query(db)
        else:
            from muse.paths import get_muse_db_path
            from muse.runtime import peek_shared_db, use_shared_db

            if peek_shared_db() is not None or get_muse_db_path().is_file():
                with use_shared_db() as shared_db:
                    rows = _run_query(shared_db)
    except Exception:
        return []

    hits: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("chat_id") or "")
        meta = allowed.get(cid)
        if meta is None:
            continue
        hit_role = str(row.get("role") or "")
        if hit_role not in ("user", "assistant"):
            continue
        if role_filter and hit_role != role_filter:
            continue
        plain = _plain(str(row.get("content") or ""))
        at, match_len = _find_match_position(plain, needle, tokens)
        if at < 0:
            continue
        snip = _snippet(plain, at, match_len)
        created_s = float(row.get("created_at") or 0.0)
        at_ms = int(round(created_s * 1000)) if created_s > 0 else 0
        hits.append(
            _build_hit(
                cid=cid,
                meta=meta,
                main_chat_id=main_chat_id,
                hit_role=hit_role,
                turn_id=row.get("turn_id"),
                snippet=snip,
                at_ms=at_ms,
                words=words,
                tokens=tokens,
            )
        )
    return hits


def search(
    store: Any,
    query: str,
    main_chat_id: str | None = None,
    *,
    main_chat: str | None = None,
    chat_id: str | None = None,
    role: str | None = None,
    scope: str = "all",
    limit: int = _DEFAULT_LIMIT,
    db: Any = None,
) -> dict[str, Any]:
    """Search messages across chats via MuseDB FTS5 + ChatStore fallback with filters & match spans."""
    words = " ".join(str(query or "").split())
    if not words:
        return {"query": "", "hits": [], "more": False}

    eff_main = main_chat_id if main_chat_id is not None else main_chat
    eff_chat_id = _normalize_chat_id(chat_id)
    eff_role = _normalize_role_filter(role)
    eff_scope = str(scope or "all").strip().lower()
    if eff_scope not in _VALID_SCOPES:
        eff_scope = "all"
    try:
        eff_limit = max(1, min(_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        eff_limit = _DEFAULT_LIMIT

    needle = words.casefold()
    tokens = [tok.casefold() for tok in words.split() if tok]
    index = store._read_index() if hasattr(store, "_read_index") else {}
    allowed = _eligible_chats(
        index,
        main_chat_id=eff_main,
        target_chat_id=eff_chat_id,
        scope=eff_scope,
    )
    if not allowed:
        return {"query": words, "hits": [], "more": False}

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    # 1) MuseDB FTS5 trigram BM25 + CJK LIKE lookup
    for hit in _search_musedb(
        query=words,
        allowed=allowed,
        main_chat_id=eff_main,
        target_chat_id=eff_chat_id,
        role_filter=eff_role,
        limit=eff_limit,
        words=words,
        needle=needle,
        tokens=tokens,
        db=db,
    ):
        key = _dedup_key(hit["chat_id"], hit.get("turn_id"), hit["role"], hit["created_at_ms"], hit["snippet"])
        if key not in merged:
            merged[key] = hit

    # 2) ChatStore scan fallback / supplement (ensures pure ChatStore fixtures & non-DB rows are 100% searchable)
    for cid, meta in allowed.items():
        with contextlib.suppress(Exception):
            events = store.events(cid)
            for event in events:
                kind = event.get("event")
                if kind not in ("user_message", "message") or event.get("kind") == "tool_hint":
                    continue
                if (event.get("source") or {}).get("kind") == "notice":
                    continue
                hit_role = "user" if kind == "user_message" else "assistant"
                if eff_role and hit_role != eff_role:
                    continue
                plain = _plain(str(event.get("text") or ""))
                at, match_len = _find_match_position(plain, needle, tokens)
                if at < 0:
                    continue
                snip = _snippet(plain, at, match_len)
                at_ms = int(event.get("created_at_ms") or 0)
                hit = _build_hit(
                    cid=cid,
                    meta=meta,
                    main_chat_id=eff_main,
                    hit_role=hit_role,
                    turn_id=event.get("turn_id"),
                    snippet=snip,
                    at_ms=at_ms,
                    words=words,
                    tokens=tokens,
                )
                key = _dedup_key(cid, hit.get("turn_id"), hit_role, at_ms, snip)
                if key not in merged:
                    merged[key] = hit
                elif at_ms and not merged[key]["created_at_ms"]:
                    merged[key]["at_ms"] = at_ms
                    merged[key]["created_at_ms"] = at_ms

    hits = sorted(merged.values(), key=lambda item: item["created_at_ms"], reverse=True)
    return {"query": words, "hits": hits[:eff_limit], "more": len(hits) > eff_limit}


__all__ = ["search"]
