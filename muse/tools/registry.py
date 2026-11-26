"""Tool registry with toolset filtering and on-demand `tool_search` overflow (>128 tools)."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

MAX_DIRECT_TOOLS = 128

CORE_TOOLSETS = frozenset({
    "companion",
    "clarify",
    "knowledge",
    "memory",
    "session_search",
    "terminal",
    "file",
    "web",
    "skills",
    "browser",
    "vision",
    "image_gen",
    "video_gen",
    "tts",
    "cronjob",
})

TOOL_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "tool_search",
    "description": (
        "Search deferred or overflow tools by keyword and activate matching tools "
        "for subsequent turns in this session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords describing the tool name, domain, or capability.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matching tools to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


def normalize_parameters_schema(raw: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        out = dict(raw)
        out.setdefault("type", "object")
        out.setdefault("properties", {})
        return out
    return {"type": "object", "properties": {}}


@dataclass(slots=True)
class ToolEntry:
    """Registered tool metadata and callable handler."""

    name: str
    toolset: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    is_async: bool = False
    check_fn: Optional[Callable[[], bool]] = None
    emoji: str = ""
    always_loaded: bool = False

    def is_available(self) -> bool:
        if self.check_fn is None:
            return True
        try:
            return bool(self.check_fn())
        except Exception:
            return False

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _invoke_callable(
    handler: Callable[..., Any],
    args_dict: dict[str, Any],
    context_kwargs: dict[str, Any],
) -> Any:
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(args_dict)

    params = list(sig.parameters.values())
    if not params:
        return handler()

    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    has_varpos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    pos_params = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]

    # Standard Hermes / Companion / Muse tool convention: first positional parameter accepts `args` dict.
    if pos_params and pos_params[0].name in ("args", "arguments", "params", "payload"):
        accepted_kw = (
            context_kwargs
            if has_varkw
            else {k: v for k, v in context_kwargs.items() if k in sig.parameters}
        )
        return handler(args_dict, **accepted_kw)

    if len(pos_params) == 1 and pos_params[0].name not in args_dict:
        accepted_kw = (
            context_kwargs
            if has_varkw
            else {k: v for k, v in context_kwargs.items() if k in sig.parameters}
        )
        return handler(args_dict, **accepted_kw)

    merged = dict(args_dict)
    for k, v in context_kwargs.items():
        if has_varkw or k in sig.parameters:
            merged.setdefault(k, v)
    if not has_varkw and not has_varpos:
        merged = {k: v for k, v in merged.items() if k in sig.parameters}
    return handler(**merged)


def _format_tool_output(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    return json.dumps(raw, ensure_ascii=False, default=str)


def _score_tool_match(
    tokens: list[str],
    raw_query: str,
    *,
    name: str,
    toolset: str,
    description: str,
) -> int:
    low_name = name.lower()
    low_set = toolset.lower()
    low_desc = description.lower()
    if not tokens:
        return 1
    score = 0
    if raw_query in low_name:
        score += 20
    elif raw_query in low_set:
        score += 12
    elif raw_query in low_desc:
        score += 8
    for tok in tokens:
        if tok == low_name:
            score += 15
        elif tok in low_name:
            score += 8
        if tok in low_set:
            score += 5
        if tok in low_desc:
            score += 3
    return score


class ToolRegistry:
    """Thread-safe tool registry preserving toolset names and >128 `tool_search` overflow."""

    def __init__(self, *, max_direct_tools: int = MAX_DIRECT_TOOLS) -> None:
        self.max_direct_tools = max(8, int(max_direct_tools))
        self._lock = threading.RLock()
        self._entries: dict[str, ToolEntry] = {}
        self._session_activated: dict[str, set[str]] = {}

    def register(
        self,
        name: str,
        *,
        toolset: str = "core",
        handler: Callable[..., Any],
        schema: Optional[Mapping[str, Any]] = None,
        description: Optional[str] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        is_async: Optional[bool] = None,
        check_fn: Optional[Callable[[], bool]] = None,
        emoji: str = "",
        always_loaded: Optional[bool] = None,
        **_: Any,
    ) -> ToolEntry:
        clean_name = str(name or (schema or {}).get("name") or "").strip()
        if not clean_name:
            raise ValueError("Tool name must be non-empty")
        raw_desc = (
            description
            if description is not None
            else str((schema or {}).get("description") or "")
        )
        raw_params = (
            parameters
            if parameters is not None
            else (schema.get("parameters") if isinstance(schema, Mapping) else None)
        )
        detected_async = (
            bool(is_async)
            if is_async is not None
            else inspect.iscoroutinefunction(handler)
        )
        tset = str(toolset or "core").strip() or "core"
        pinned = (
            bool(always_loaded)
            if always_loaded is not None
            else (tset in CORE_TOOLSETS)
        )
        entry = ToolEntry(
            name=clean_name,
            toolset=tset,
            description=raw_desc,
            parameters=normalize_parameters_schema(raw_params),
            handler=handler,
            is_async=detected_async,
            check_fn=check_fn,
            emoji=emoji,
            always_loaded=pinned,
        )
        with self._lock:
            self._entries[clean_name] = entry
        return entry

    register_tool = register

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def unregister_toolset(self, toolset: str) -> int:
        with self._lock:
            keys = [k for k, v in self._entries.items() if v.toolset == toolset]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def get(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            return self._entries.get(name)

    def toolsets(self) -> list[str]:
        with self._lock:
            return sorted({entry.toolset for entry in self._entries.values()})

    def get_tool_names_for_toolset(self, toolset: str) -> list[str]:
        with self._lock:
            return sorted(
                entry.name
                for entry in self._entries.values()
                if entry.toolset == toolset
            )

    def activate_tools(self, names: Iterable[str], *, session_id: str = "") -> list[str]:
        key = session_id or "_default"
        added: list[str] = []
        with self._lock:
            bucket = self._session_activated.setdefault(key, set())
            for raw in names:
                clean = str(raw or "").strip()
                if clean:
                    bucket.add(clean)
                    added.append(clean)
        return sorted(set(added))

    def clear_session_activations(self, session_id: str) -> None:
        with self._lock:
            self._session_activated.pop(session_id or "_default", None)

    def list_entries(
        self,
        *,
        enabled_toolsets: Optional[Iterable[str]] = None,
        check_availability: bool = True,
    ) -> list[ToolEntry]:
        allow = set(enabled_toolsets) if enabled_toolsets is not None else None
        with self._lock:
            snapshot = list(self._entries.values())
        result: list[ToolEntry] = []
        for entry in snapshot:
            if entry.name == "tool_search":
                continue
            if allow is not None and entry.toolset not in allow:
                continue
            if check_availability and not entry.is_available():
                continue
            result.append(entry)
        return result

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        enabled_toolsets: Optional[Iterable[str]] = None,
        extra_mcp_tools: Sequence[Mapping[str, Any]] = (),
        activate_for_session: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clean_q = str(query or "").strip().lower()
        tokens = [t for t in re.split(r"[\s,;/|]+", clean_q) if t]
        cap = max(1, min(50, int(limit or 10)))

        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for entry in self.list_entries(enabled_toolsets=enabled_toolsets):
            sc = _score_tool_match(
                tokens,
                clean_q,
                name=entry.name,
                toolset=entry.toolset,
                description=entry.description,
            )
            if sc > 0:
                candidates.append((
                    sc,
                    entry.name,
                    {
                        "name": entry.name,
                        "toolset": entry.toolset,
                        "description": entry.description,
                        "parameters": entry.parameters,
                    },
                ))

        allow = set(enabled_toolsets) if enabled_toolsets is not None else None
        seen = {item[1] for item in candidates}
        for raw_tool in extra_mcp_tools:
            fn = raw_tool.get("function") if isinstance(raw_tool, Mapping) else None
            if not isinstance(fn, Mapping):
                continue
            tname = str(fn.get("name") or "").strip()
            if not tname or tname in seen:
                continue
            tdesc = str(fn.get("description") or "")
            tset = str(raw_tool.get("toolset") or "mcp")
            mcp_srv = tname.split("__", 2)[1] if tname.startswith("mcp__") and tname.count("__") >= 2 else ""
            if allow is not None and tset not in allow and "mcp" not in allow and tname not in allow and not (mcp_srv and f"mcp:{mcp_srv}" in allow):
                continue
            sc = _score_tool_match(
                tokens,
                clean_q,
                name=tname,
                toolset=tset,
                description=tdesc,
            )
            if sc > 0:
                candidates.append((
                    sc,
                    tname,
                    {
                        "name": tname,
                        "toolset": tset,
                        "description": tdesc,
                        "parameters": normalize_parameters_schema(fn.get("parameters")),
                    },
                ))

        candidates.sort(key=lambda row: (-row[0], row[1]))
        chosen = [row[2] for row in candidates[:cap]]
        if activate_for_session is not None and chosen:
            self.activate_tools(
                [item["name"] for item in chosen],
                session_id=activate_for_session,
            )
        return chosen

    def get_definitions(
        self,
        *,
        enabled_toolsets: Optional[Iterable[str]] = None,
        session_id: str = "",
        extra_mcp_tools: Sequence[dict[str, Any]] = (),
        max_direct_tools: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        limit = (
            self.max_direct_tools
            if max_direct_tools is None
            else max(8, int(max_direct_tools))
        )
        allow = set(enabled_toolsets) if enabled_toolsets is not None else None
        entries = self.list_entries(enabled_toolsets=enabled_toolsets)
        entry_names = {e.name for e in entries}
        mcp_defs: list[dict[str, Any]] = []
        for t in extra_mcp_tools:
            if not isinstance(t, Mapping) or not isinstance(t.get("function"), Mapping):
                continue
            mname = str(t["function"].get("name") or "")
            if not mname or mname in entry_names:
                continue
            if allow is not None:
                tset = str(t.get("toolset") or "mcp")
                mcp_srv = mname.split("__", 2)[1] if mname.startswith("mcp__") and mname.count("__") >= 2 else ""
                if tset not in allow and "mcp" not in allow and mname not in allow and not (mcp_srv and f"mcp:{mcp_srv}" in allow):
                    continue
            mcp_defs.append(dict(t))

        total = len(entries) + len(mcp_defs)
        if total <= limit:
            return [e.as_openai_tool() for e in entries] + mcp_defs

        # > limit tools active: keep pinned/core tools + session-activated tools + `tool_search`.
        budget = limit - 1
        with self._lock:
            activated = set(self._session_activated.get(session_id or "_default", ()))

        selected: list[dict[str, Any]] = []
        used_names: set[str] = set()

        for entry in entries:
            if len(selected) >= budget:
                break
            if entry.name in activated or entry.always_loaded:
                selected.append(entry.as_openai_tool())
                used_names.add(entry.name)

        for raw_mcp in mcp_defs:
            if len(selected) >= budget:
                break
            mname = str(raw_mcp["function"].get("name") or "")
            if mname in activated and mname not in used_names:
                selected.append(raw_mcp)
                used_names.add(mname)

        for entry in entries:
            if len(selected) >= budget:
                break
            if entry.name not in used_names:
                selected.append(entry.as_openai_tool())
                used_names.add(entry.name)

        for raw_mcp in mcp_defs:
            if len(selected) >= budget:
                break
            mname = str(raw_mcp["function"].get("name") or "")
            if mname and mname not in used_names:
                selected.append(raw_mcp)
                used_names.add(mname)

        search_entry = self.get("tool_search")
        if search_entry is not None:
            selected.append(search_entry.as_openai_tool())
        else:
            selected.append({"type": "function", "function": dict(TOOL_SEARCH_SCHEMA)})
        return selected

    async def dispatch(
        self,
        name: str,
        arguments: dict[str, Any] | str | None = None,
        *,
        session_id: str = "",
        ctx: Any = None,
        enabled_toolsets: Optional[Iterable[str]] = None,
        extra_mcp_tools: Sequence[dict[str, Any]] = (),
        **extra_kwargs: Any,
    ) -> str:
        if isinstance(arguments, str):
            raw_str = arguments.strip()
            parsed = json.loads(raw_str) if raw_str else {}
            args_dict = parsed if isinstance(parsed, dict) else {"value": parsed}
        elif isinstance(arguments, Mapping):
            args_dict = dict(arguments)
        else:
            args_dict = {}

        resolved_sid = session_id or str(getattr(ctx, "session_id", "") or "")
        eff_toolsets = (
            enabled_toolsets
            if enabled_toolsets is not None
            else (getattr(ctx, "enabled_toolsets", None) or None)
        )
        entry = self.get(name)
        if entry is None and name == "tool_search":
            matches = self.search(
                str(args_dict.get("query") or ""),
                limit=int(args_dict.get("limit") or 10),
                enabled_toolsets=eff_toolsets,
                extra_mcp_tools=extra_mcp_tools,
                activate_for_session=resolved_sid,
            )
            return json.dumps(
                {"success": True, "count": len(matches), "tools": matches},
                ensure_ascii=False,
            )
        if entry is None:
            return json.dumps(
                {"success": False, "error": f"Unknown tool: {name}"},
                ensure_ascii=False,
            )
        if not entry.is_available():
            return json.dumps(
                {"success": False, "error": f"Tool {name} is currently unavailable"},
                ensure_ascii=False,
            )

        ctx_kwargs: dict[str, Any] = {
            "session_id": resolved_sid,
            "task_id": resolved_sid,
            "ctx": ctx,
            "registry": self,
            "enabled_toolsets": eff_toolsets,
            "extra_mcp_tools": extra_mcp_tools,
            **extra_kwargs,
        }
        if entry.is_async:
            res = _invoke_callable(entry.handler, args_dict, ctx_kwargs)
            if inspect.isawaitable(res):
                res = await res
            return _format_tool_output(res)
        res = await asyncio.to_thread(_invoke_callable, entry.handler, args_dict, ctx_kwargs)
        return _format_tool_output(res)


__all__ = [
    "CORE_TOOLSETS",
    "MAX_DIRECT_TOOLS",
    "TOOL_SEARCH_SCHEMA",
    "ToolEntry",
    "ToolRegistry",
    "normalize_parameters_schema",
]
