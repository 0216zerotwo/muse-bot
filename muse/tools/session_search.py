"""`session_search` tool backed by `MuseDB.search_messages` (FTS5 trigram + CJK fallback)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from muse.db import MuseDB, sync_from_legacy_state_db
from muse.paths import get_legacy_state_db_path, get_muse_db_path, get_muse_home

TOOLSET = "session_search"
MAX_SNIPPET_CHARS = 500


def _format_snippet(content: str, query: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    clean = " ".join((content or "").split())
    if len(clean) <= max_chars:
        return clean
    pos = clean.lower().find(query.lower())
    if pos < 0:
        return clean[:max_chars] + "..."
    half = max_chars // 2
    start = max(0, pos - half)
    end = min(len(clean), start + max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def session_search_handler(
    args: dict[str, Any],
    *,
    db: Optional[MuseDB] = None,
    home: Optional[Path] = None,
    **_: Any,
) -> str:
    """Search historical messages across sessions in `<HOME>/muse.db`."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"success": False, "error": "Parameter 'query' is required.", "results": []},
            ensure_ascii=False,
        )
    chat_id = str(args["chat_id"]).strip() if args.get("chat_id") else None
    limit = max(1, min(50, int(args.get("limit") or 20)))

    if db is not None:
        rows = db.search_messages(query, chat_id=chat_id, limit=limit)
    else:
        resolved_home = Path(home) if home is not None else get_muse_home()
        muse_db_path = get_muse_db_path(resolved_home)
        legacy_path = get_legacy_state_db_path(resolved_home)
        if not muse_db_path.exists() and not legacy_path.exists():
            return json.dumps(
                {"success": True, "query": query, "count": 0, "results": []},
                ensure_ascii=False,
            )
        if legacy_path.exists():
            sync_from_legacy_state_db(home=resolved_home)
        if not muse_db_path.exists():
            return json.dumps(
                {"success": True, "query": query, "count": 0, "results": []},
                ensure_ascii=False,
            )
        from muse.runtime import use_shared_db

        with use_shared_db(db_path=muse_db_path) as local_db:
            rows = local_db.search_messages(query, chat_id=chat_id, limit=limit)

    formatted = [
        {
            "message_id": r["id"],
            "session_id": r["session_id"],
            "chat_id": r["chat_id"],
            "role": r["role"],
            "created_at": r["created_at"],
            "snippet": _format_snippet(str(r.get("content") or ""), query),
        }
        for r in rows
    ]
    return json.dumps(
        {
            "success": True,
            "query": query,
            "count": len(formatted),
            "results": formatted,
        },
        ensure_ascii=False,
    )


SESSION_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "session_search",
    "description": (
        "Search past conversation messages across sessions using full-text trigram and "
        "CJK substring matching."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords or phrase to search in past conversations.",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional chat_id filter to restrict search to one conversation.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum matching messages to return (default 20, max 50).",
                "default": 20,
            },
        },
        "required": ["query"],
    },
}


def register_session_search_tools(registry: Any) -> None:
    registry.register(
        "session_search",
        toolset=TOOLSET,
        schema=SESSION_SEARCH_SCHEMA,
        handler=session_search_handler,
    )


__all__ = [
    "SESSION_SEARCH_SCHEMA",
    "TOOLSET",
    "register_session_search_tools",
    "session_search_handler",
]
