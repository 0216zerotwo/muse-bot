"""Async MCP manager with Windows stdio fixes and in-flight refcount lease live reload."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from muse.mcp.config import (
    build_stdio_env,
    detect_transport,
    filter_tool_names,
    load_mcp_servers,
    mcp_tool_name,
    mcp_toolset_name,
    redact_error,
    resolve_stdio_command,
)

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 60.0
DEFAULT_CALL_TIMEOUT = 120.0
DEFAULT_DRAIN_TIMEOUT = 30.0


@dataclass
class MCPToolSpec:
    """Metadata for one tool exposed by an MCP server generation."""

    server: str
    raw_name: str
    full_name: str
    toolset: str
    description: str
    input_schema: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.full_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class MCPToolBinding:
    """Snapshot binding from a tool name to the exact ``ServerGeneration`` serving it."""

    spec: MCPToolSpec
    generation: "ServerGeneration"


def _normalize_schema(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        schema = dict(raw)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema
    return {"type": "object", "properties": {}}


def _format_call_result(result: Any) -> str:
    """Convert an MCP ``CallToolResult`` (or mock) into a clean string/JSON payload."""
    if isinstance(result, str):
        return result
    is_error = bool(getattr(result, "isError", False))
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        is_error = bool(result.get("isError", False))
        content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            item_type = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            text = getattr(item, "text", None) or (
                item.get("text") if isinstance(item, dict) else None
            )
            if text is not None:
                parts.append(str(text))
            elif item_type == "image":
                mime = getattr(item, "mimeType", None) or (
                    item.get("mimeType") if isinstance(item, dict) else "image/png"
                )
                parts.append(f"[image:{mime}]")
            else:
                parts.append(str(item))
    elif content is not None:
        parts.append(str(content))
    joined = "\n".join(p for p in parts if p).strip()
    if is_error:
        return json.dumps({"success": False, "error": joined or "MCP tool error"}, ensure_ascii=False)
    return joined


def _extract_tools(raw_tools: Any) -> list[tuple[str, str, dict[str, Any]]]:
    items = getattr(raw_tools, "tools", raw_tools)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for item in items or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            desc = str(item.get("description") or "")
            schema = _normalize_schema(item.get("inputSchema") or item.get("input_schema"))
        else:
            name = str(getattr(item, "name", "") or "").strip()
            desc = str(getattr(item, "description", "") or "")
            schema = _normalize_schema(getattr(item, "inputSchema", None))
        if name:
            out.append((name, desc, schema))
    return out


async def _open_sdk_session(
    stack: AsyncExitStack,
    name: str,
    cfg: dict[str, Any],
) -> tuple[Any, list[tuple[str, str, dict[str, Any]]]]:
    """Open an MCP ClientSession via the installed ``mcp`` SDK within ``stack``."""
    from mcp import ClientSession

    transport = detect_transport(cfg)
    connect_timeout = float(cfg.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT)
    read_timeout = float(cfg.get("read_timeout") or DEFAULT_CALL_TIMEOUT)

    if transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        raw_cmd = str(cfg.get("command") or "").strip()
        if not raw_cmd:
            raise ValueError(f"MCP server {name!r} missing stdio command")
        merged_env = build_stdio_env(cfg.get("env") if isinstance(cfg.get("env"), dict) else None)
        resolved_cmd = resolve_stdio_command(raw_cmd, merged_env)
        args = [str(a) for a in (cfg.get("args") or [])]
        cwd = str(cfg["cwd"]) if cfg.get("cwd") else None
        params = StdioServerParameters(
            command=resolved_cmd,
            args=args,
            env=merged_env,
            cwd=cwd,
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
    elif transport == "sse":
        from mcp.client.sse import sse_client

        url = str(cfg.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server {name!r} missing SSE url")
        headers = dict(cfg.get("headers") or {}) or None
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(
                url,
                headers=headers,
                timeout=connect_timeout,
                sse_read_timeout=read_timeout,
            )
        )
    else:
        from mcp.client import streamable_http as sh_mod

        url = str(cfg.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server {name!r} missing streamable_http url")
        headers = dict(cfg.get("headers") or {}) or None
        client_fn = getattr(sh_mod, "streamable_http_client", None) or getattr(
            sh_mod, "streamablehttp_client"
        )
        sig = inspect.signature(client_fn)
        if "http_client" in sig.parameters:
            factory = getattr(sh_mod, "create_mcp_http_client", None)
            timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
            if callable(factory):
                http_client = factory(headers=headers, timeout=timeout)
            else:
                http_client = httpx.AsyncClient(headers=headers, timeout=timeout)
            await stack.enter_async_context(http_client)
            streams = await stack.enter_async_context(client_fn(url, http_client=http_client))
        else:
            streams = await stack.enter_async_context(
                client_fn(url, headers=headers, timeout=connect_timeout)
            )
        read_stream, write_stream = streams[0], streams[1]

    session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
    await session.initialize()
    tools_result = await session.list_tools()
    return session, _extract_tools(tools_result)


SessionOpener = Callable[
    [AsyncExitStack, str, dict[str, Any]],
    Any,
]


@dataclass
class ServerGeneration:
    """One live MCP server connection with task-owned exit stack and refcounted leases."""

    name: str
    config: dict[str, Any]
    generation_id: int
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT
    session: Any = None
    all_tool_names: list[str] = field(default_factory=list)
    tools: dict[str, MCPToolSpec] = field(default_factory=dict)
    status: str = "connecting"
    error: Optional[str] = None
    inflight: int = 0
    superseded: bool = False
    closed: bool = False
    _close_requested: asyncio.Event = field(default_factory=asyncio.Event)
    _drained: asyncio.Event = field(default_factory=asyncio.Event)
    _owner_task: Optional[asyncio.Task[None]] = None

    def __post_init__(self) -> None:
        self._drained.set()

    async def start(self, opener: SessionOpener) -> None:
        """Spawn the task-owned lifecycle coroutine and await connection readiness."""
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        connect_timeout = float(self.config.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT)

        async def _owner() -> None:
            try:
                async with AsyncExitStack() as stack:
                    session, raw_tools = await asyncio.wait_for(
                        opener(stack, self.name, self.config),
                        timeout=connect_timeout,
                    )
                    self.session = session
                    self._populate_tools(raw_tools)
                    self.status = "connected"
                    if not ready.done():
                        ready.set_result(None)
                    await self._close_requested.wait()
                    if self.inflight > 0:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(
                                self._drained.wait(),
                                timeout=self.drain_timeout,
                            )
            except BaseException as exc:
                self.status = "failed"
                self.error = redact_error(str(exc) or type(exc).__name__)
                if not ready.done():
                    ready.set_exception(exc)
            finally:
                self.closed = True
                self.session = None

        self._owner_task = asyncio.create_task(_owner(), name=f"mcp-gen-{self.name}-{self.generation_id}")
        await ready

    def _populate_tools(self, raw_tools: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.all_tool_names = sorted(name for name, _, _ in raw_tools)
        allowed = set(filter_tool_names(self.all_tool_names, self.config))
        toolset = mcp_toolset_name(self.name)
        specs: dict[str, MCPToolSpec] = {}
        for raw_name, desc, schema in raw_tools:
            if raw_name not in allowed:
                continue
            full = mcp_tool_name(self.name, raw_name)
            specs[full] = MCPToolSpec(
                server=self.name,
                raw_name=raw_name,
                full_name=full,
                toolset=toolset,
                description=desc,
                input_schema=schema,
            )
        self.tools = specs

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Any]:
        """Acquire an in-flight refcount lease so reload drains before closing."""
        if self.closed or self.session is None:
            raise RuntimeError(f"MCP server {self.name!r} session is closed")
        self.inflight += 1
        self._drained.clear()
        try:
            yield self.session
        finally:
            self.inflight = max(0, self.inflight - 1)
            if self.inflight == 0:
                self._drained.set()

    async def retire_and_close(self) -> None:
        """Mark superseded, wait for active calls to drain, and close on owner task."""
        self.superseded = True
        if self.inflight == 0:
            self._drained.set()
        self._close_requested.set()
        if self._owner_task is not None:
            with contextlib.suppress(BaseException):
                await self._owner_task

    async def call_tool(self, raw_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool under an active refcount lease."""
        call_timeout = float(self.config.get("call_timeout") or DEFAULT_CALL_TIMEOUT)
        async with self.lease() as session:
            result = await asyncio.wait_for(
                session.call_tool(raw_name, arguments=arguments or {}),
                timeout=call_timeout,
            )
        return _format_call_result(result)


class AsyncMCPManager:
    """Single-event-loop MCP manager supporting stdio, SSE, and Streamable HTTP."""

    def __init__(
        self,
        *,
        opener: Optional[SessionOpener] = None,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        self._opener: SessionOpener = opener or _open_sdk_session
        self._drain_timeout = float(drain_timeout)
        self._generations: dict[str, ServerGeneration] = {}
        self._errors: dict[str, str] = {}
        self._gen_seq = 0
        self._reload_lock = asyncio.Lock()
        self._draining_tasks: set[asyncio.Task[None]] = set()

    def _next_gen_id(self) -> int:
        self._gen_seq += 1
        return self._gen_seq

    def _schedule_retire(self, gen: ServerGeneration) -> asyncio.Task[None]:
        task = asyncio.create_task(
            gen.retire_and_close(),
            name=f"mcp-retire-{gen.name}-{gen.generation_id}",
        )
        self._draining_tasks.add(task)
        task.add_done_callback(self._draining_tasks.discard)
        return task

    async def reload_mcp(
        self,
        configs: Optional[dict[str, dict[str, Any]]] = None,
        *,
        wait_for_drain: bool = True,
    ) -> dict[str, Any]:
        """Reconcile running MCP sessions against config on the main asyncio loop."""
        async with self._reload_lock:
            target_all = load_mcp_servers() if configs is None else dict(configs)
            enabled_cfg = {
                name: cfg
                for name, cfg in target_all.items()
                if isinstance(cfg, dict) and cfg.get("enabled", True) is not False
            }

            added: list[str] = []
            reloaded: list[str] = []
            removed: list[str] = []
            unchanged: list[str] = []
            failed: list[str] = []
            retire_tasks: list[asyncio.Task[None]] = []

            for name in list(self._generations):
                if name not in enabled_cfg:
                    old = self._generations.pop(name)
                    self._errors.pop(name, None)
                    retire_tasks.append(self._schedule_retire(old))
                    removed.append(name)

            for name in list(self._errors):
                if name not in enabled_cfg:
                    self._errors.pop(name, None)

            for name, cfg in enabled_cfg.items():
                current = self._generations.get(name)
                if current is not None and current.status == "connected" and current.config == cfg:
                    unchanged.append(name)
                    continue

                new_gen = ServerGeneration(
                    name=name,
                    config=cfg,
                    generation_id=self._next_gen_id(),
                    drain_timeout=self._drain_timeout,
                )
                try:
                    await new_gen.start(self._opener)
                except Exception as exc:
                    err_text = redact_error(str(exc) or type(exc).__name__)
                    self._errors[name] = err_text
                    failed.append(name)
                    logger.warning("muse.mcp: failed to connect %s: %s", name, err_text)
                    continue

                self._errors.pop(name, None)
                previous = self._generations.get(name)
                self._generations[name] = new_gen
                if previous is not None:
                    retire_tasks.append(self._schedule_retire(previous))
                    reloaded.append(name)
                else:
                    added.append(name)

            if wait_for_drain and retire_tasks:
                await asyncio.gather(*retire_tasks, return_exceptions=True)

            return {
                "ok": not failed,
                "message": "",
                "failed": failed,
                "added": added,
                "reloaded": reloaded,
                "removed": removed,
                "unchanged": unchanged,
            }

    async def probe_server(
        self,
        name: str,
        cfg: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        """Connect once, return all unfiltered tool names, and disconnect cleanly."""
        target_cfg = cfg if cfg is not None else load_mcp_servers().get(name)
        if not isinstance(target_cfg, dict):
            raise KeyError(f"MCP server {name!r} not configured")
        gen = ServerGeneration(
            name=name,
            config=target_cfg,
            generation_id=self._next_gen_id(),
            drain_timeout=self._drain_timeout,
        )
        try:
            await gen.start(self._opener)
            return list(gen.all_tool_names)
        finally:
            await gen.retire_and_close()

    def snapshot_tools(self) -> dict[str, MCPToolBinding]:
        """Capture an immutable per-turn snapshot of active MCP tools and their generations."""
        bindings: dict[str, MCPToolBinding] = {}
        for gen in self._generations.values():
            if gen.status != "connected" or gen.closed:
                continue
            for full_name, spec in gen.tools.items():
                bindings[full_name] = MCPToolBinding(spec=spec, generation=gen)
        return bindings

    def get_tool_definitions(
        self,
        *,
        snapshot: Optional[dict[str, MCPToolBinding]] = None,
        enabled_toolsets: Optional[set[str] | list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return OpenAI-format tool definitions, optionally filtered by toolset."""
        bindings = snapshot if snapshot is not None else self.snapshot_tools()
        allow = set(enabled_toolsets) if enabled_toolsets is not None else None
        return [
            binding.spec.as_openai_tool()
            for binding in bindings.values()
            if allow is None or binding.spec.toolset in allow
        ]

    def get_tool_names_for_toolset(self, toolset: str) -> list[str]:
        """Return sorted full tool names belonging to ``mcp-<server>``."""
        names = [
            full_name
            for gen in self._generations.values()
            if gen.status == "connected"
            for full_name, spec in gen.tools.items()
            if spec.toolset == toolset
        ]
        return sorted(names)

    def get_status(self) -> list[dict[str, Any]]:
        """Return per-server status rows compatible with ``companion/modules/connections.py``."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name, gen in self._generations.items():
            seen.add(name)
            rows.append(
                {
                    "name": name,
                    "status": gen.status,
                    "toolset": mcp_toolset_name(name),
                    "tool_count": len(gen.tools),
                    "tools": sorted(spec.raw_name for spec in gen.tools.values()),
                    "inflight": gen.inflight,
                    "error": gen.error,
                }
            )
        for name, err in self._errors.items():
            if name in seen:
                continue
            rows.append(
                {
                    "name": name,
                    "status": "failed",
                    "toolset": mcp_toolset_name(name),
                    "tool_count": 0,
                    "tools": [],
                    "inflight": 0,
                    "error": err,
                }
            )
        return rows

    async def call_tool(
        self,
        full_name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        snapshot: Optional[dict[str, MCPToolBinding]] = None,
    ) -> str:
        """Invoke an MCP tool using either a turn's snapshot or the live registry."""
        bindings = snapshot if snapshot is not None else self.snapshot_tools()
        binding = bindings.get(full_name)
        if binding is None:
            raise KeyError(f"Unknown MCP tool: {full_name}")
        return await binding.generation.call_tool(binding.spec.raw_name, arguments or {})

    async def shutdown(self) -> None:
        """Drain and close all active and retiring MCP generations."""
        async with self._reload_lock:
            gens = list(self._generations.values())
            self._generations.clear()
            self._errors.clear()
            tasks = [self._schedule_retire(gen) for gen in gens]
            tasks.extend(list(self._draining_tasks))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_DEFAULT_MANAGER: Optional[AsyncMCPManager] = None


def get_mcp_manager() -> AsyncMCPManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = AsyncMCPManager()
    return _DEFAULT_MANAGER


def set_mcp_manager(manager: Optional[AsyncMCPManager]) -> None:
    global _DEFAULT_MANAGER
    _DEFAULT_MANAGER = manager


__all__ = [
    "AsyncMCPManager",
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_DRAIN_TIMEOUT",
    "MCPToolBinding",
    "MCPToolSpec",
    "ServerGeneration",
    "get_mcp_manager",
    "set_mcp_manager",
]
