"""On-demand `tool_search` handler for discovering & activating overflow tools (>128 limit)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from muse.tools.registry import TOOL_SEARCH_SCHEMA, ToolRegistry

TOOLSET = "tool_search"


def tool_search_handler(
    args: dict[str, Any],
    *,
    registry: Optional[ToolRegistry] = None,
    session_id: str = "",
    enabled_toolsets: Optional[Iterable[str]] = None,
    extra_mcp_tools: Sequence[dict[str, Any]] = (),
    **_: Any,
) -> str:
    """Search registered and MCP tools by keyword and activate matches for `session_id`."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"success": False, "error": "Parameter 'query' is required.", "tools": []},
            ensure_ascii=False,
        )
    if registry is None:
        return json.dumps(
            {"success": False, "error": "No active ToolRegistry bound.", "tools": []},
            ensure_ascii=False,
        )
    limit = max(1, min(50, int(args.get("limit") or 10)))
    matches = registry.search(
        query,
        limit=limit,
        enabled_toolsets=enabled_toolsets,
        extra_mcp_tools=extra_mcp_tools,
        activate_for_session=session_id,
    )
    return json.dumps(
        {
            "success": True,
            "query": query,
            "count": len(matches),
            "activated": [m["name"] for m in matches],
            "tools": matches,
        },
        ensure_ascii=False,
    )


def register_tool_search(registry: ToolRegistry) -> None:
    """Register `tool_search` (kept dormant in `get_definitions` unless total tools > 128)."""
    registry.register(
        "tool_search",
        toolset=TOOLSET,
        schema=TOOL_SEARCH_SCHEMA,
        handler=tool_search_handler,
        always_loaded=False,
    )


__all__ = [
    "TOOLSET",
    "TOOL_SEARCH_SCHEMA",
    "register_tool_search",
    "tool_search_handler",
]
