"""Native Agent knowledge base tools: `knowledge_search` and `knowledge_list`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from companion.modules.knowledge import list_knowledge, search_knowledge

TOOLSET = "knowledge"
MAX_CHUNK_EXCERPT_CHARS = 1000


def _format_hit_line(index: int, hit: dict[str, Any]) -> str:
    heading = str(hit.get("heading") or "").strip()
    loc = f"{hit['collection']} / {heading}" if heading else str(hit.get("collection") or "默认")
    body = str(hit.get("content") or hit.get("snippet") or "").strip()
    if len(body) > MAX_CHUNK_EXCERPT_CHARS:
        body = body[:MAX_CHUNK_EXCERPT_CHARS].rstrip() + "…"
    return f"[K{index}] 《{hit['title']}》（{loc}）\n{body}"


def _emit_knowledge_citations(hits: list[dict[str, Any]]) -> None:
    if not hits:
        return
    try:
        from muse.tools.citations import record_citations
    except ImportError:
        return
    record_citations([
        {
            "title": f"{hit['title']}" + (f" · {hit['heading']}" if hit.get("heading") else ""),
            "url": f"#knowledge:{hit['doc_id']}",
            "domain": f"知识库 · {hit['collection']}",
            "snippet": hit["snippet"],
        }
        for hit in hits
    ])


def knowledge_search_handler(
    args: dict[str, Any],
    *,
    home: Optional[Path] = None,
    **_: Any,
) -> str:
    """Search the user's local knowledge base and record structured citations when matched."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {
                "success": False,
                "error": "Parameter 'query' is required.",
                "count": 0,
                "results": [],
                "formatted": "",
            },
            ensure_ascii=False,
        )
    collection = str(args.get("collection") or "").strip() or None
    raw_limit = args.get("limit")
    limit = max(1, min(20, int(raw_limit if raw_limit is not None else 5)))

    hits = search_knowledge(query, collection=collection, limit=limit, home=home)
    _emit_knowledge_citations(hits)

    if hits:
        formatted = "\n\n".join(_format_hit_line(idx, hit) for idx, hit in enumerate(hits, start=1))
    else:
        formatted = "知识库中没有找到与该查询相关的文档片段。"

    return json.dumps(
        {
            "success": True,
            "query": query,
            "collection": collection or "",
            "count": len(hits),
            "formatted": formatted,
            "results": hits,
        },
        ensure_ascii=False,
    )


def knowledge_list_handler(
    args: dict[str, Any],
    *,
    home: Optional[Path] = None,
    **_: Any,
) -> str:
    """List documents, collections, and chunk stats stored in the user's personal knowledge base."""
    collection = str(args.get("collection") or "").strip() or None
    listing = list_knowledge(collection=collection, home=home)
    docs = listing.get("documents") or []
    stats = listing.get("stats") or {"doc_count": 0, "chunk_count": 0, "total_bytes": 0}
    collections = listing.get("collections") or []
    if docs:
        lines = [
            f"共 {stats['doc_count']} 篇文档（{stats['chunk_count']} 个分块），分组：{', '.join(collections) or '默认'}"
        ]
        for idx, doc in enumerate(docs, start=1):
            lines.append(
                f"{idx}. 《{doc['title']}》 [{doc['collection']}] ({doc['filename']}, {doc['chunk_count']} 段)"
            )
        summary = "\n".join(lines)
    else:
        summary = "知识库中还没有收录文档。"
    return json.dumps(
        {
            "success": True,
            "collection": collection or "",
            "summary": summary,
            **listing,
        },
        ensure_ascii=False,
    )


def knowledge_search(query: str, collection: str = "", limit: int = 5, *, home: Optional[Path] = None) -> str:
    """Convenience function calling `knowledge_search_handler`."""
    return knowledge_search_handler({"query": query, "collection": collection, "limit": limit}, home=home)


def knowledge_list(collection: str = "", *, home: Optional[Path] = None) -> str:
    """Convenience function calling `knowledge_list_handler`."""
    return knowledge_list_handler({"collection": collection}, home=home)


KNOWLEDGE_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "knowledge_search",
    "description": (
        "Search the user's personal local knowledge base / document vault (uploaded PDFs, Word/Excel/PPT, "
        "Markdown notes, code, and saved creations) using hybrid FTS5 trigram and CJK substring retrieval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords, Chinese/English phrase, or code identifier to search in the knowledge base.",
            },
            "collection": {
                "type": "string",
                "description": "Optional collection/group filter (leave empty to search all collections).",
                "default": "",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum matching excerpts to return (default 5, max 20).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

KNOWLEDGE_LIST_SCHEMA: dict[str, Any] = {
    "name": "knowledge_list",
    "description": (
        "List documents, collections, and chunk statistics stored in the user's personal knowledge base."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "collection": {
                "type": "string",
                "description": "Optional collection/group filter (leave empty to list all documents).",
                "default": "",
            },
        },
    },
}


def register_knowledge_tools(registry: Any) -> None:
    registry.register(
        "knowledge_search",
        toolset=TOOLSET,
        schema=KNOWLEDGE_SEARCH_SCHEMA,
        handler=knowledge_search_handler,
    )
    registry.register(
        "knowledge_list",
        toolset=TOOLSET,
        schema=KNOWLEDGE_LIST_SCHEMA,
        handler=knowledge_list_handler,
    )


__all__ = [
    "KNOWLEDGE_LIST_SCHEMA",
    "KNOWLEDGE_SEARCH_SCHEMA",
    "TOOLSET",
    "knowledge_list",
    "knowledge_list_handler",
    "knowledge_search",
    "knowledge_search_handler",
    "register_knowledge_tools",
]
