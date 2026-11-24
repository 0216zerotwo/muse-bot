"""Native Muse runtime assembly (`NativeAgentEngine`) and shared `MuseDB` registry."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from .db import MuseDB
from .engine import ChatTurnEngine
from .paths import get_muse_db_path, get_muse_home

logger = logging.getLogger(__name__)

# ── Process-wide shared MuseDB registry (keyed by resolved DB path) ──────────

_DB_REGISTRY_LOCK = threading.RLock()
_SHARED_DBS: dict[str, MuseDB] = {}
_LOOP_LOCK = threading.Lock()
_RUNTIME_LOOP: Optional[asyncio.AbstractEventLoop] = None


def register_runtime_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Register (or clear) the main ASGI event loop so sync `call_llm` reuses its HTTP pool."""
    global _RUNTIME_LOOP
    with _LOOP_LOCK:
        _RUNTIME_LOOP = loop


def get_runtime_loop() -> Optional[asyncio.AbstractEventLoop]:
    with _LOOP_LOCK:
        loop = _RUNTIME_LOOP
    return loop if (loop is not None and not loop.is_closed() and loop.is_running()) else None


def _db_key(home: Optional[Path | str] = None, *, db_path: Optional[Path | str] = None) -> tuple[Path, str]:
    target = Path(db_path).expanduser() if db_path is not None else get_muse_db_path(Path(home) if home is not None else None)
    return target, str(target.resolve(strict=False))


def peek_shared_db(home: Optional[Path | str] = None, *, db_path: Optional[Path | str] = None) -> Optional[MuseDB]:
    """Return an already-open cached `MuseDB` for `home` / `db_path`, or `None` without opening one."""
    _, key = _db_key(home, db_path=db_path)
    with _DB_REGISTRY_LOCK:
        existing = _SHARED_DBS.get(key)
        if existing is not None and not getattr(existing, "_closed", False):
            return existing
        if existing is not None:
            _SHARED_DBS.pop(key, None)
    return None


def get_shared_db(home: Optional[Path | str] = None, *, db_path: Optional[Path | str] = None) -> MuseDB:
    """Return a process-wide reusable `MuseDB` instance for `home` / `db_path`."""
    target, key = _db_key(home, db_path=db_path)
    with _DB_REGISTRY_LOCK:
        existing = _SHARED_DBS.get(key)
        if existing is not None and not getattr(existing, "_closed", False):
            return existing
        db = MuseDB(target)
        _SHARED_DBS[key] = db
        return db


@contextmanager
def use_shared_db(home: Optional[Path | str] = None, *, db_path: Optional[Path | str] = None) -> Iterator[MuseDB]:
    """Borrow an active runtime's shared `MuseDB` if registered, else open-and-close ephemerally.

    In a running server process this eliminates per-request DDL/WAL setup; in one-shot CLI
    calls or isolated unit tests it releases SQLite file handles immediately on exit (Windows-safe).
    """
    existing = peek_shared_db(home, db_path=db_path)
    if existing is not None:
        yield existing
        return
    target, _ = _db_key(home, db_path=db_path)
    with MuseDB(target) as db:
        yield db


def close_shared_db(home: Optional[Path | str] = None, *, db_path: Optional[Path | str] = None) -> None:
    """Close and evict a specific cached `MuseDB` handle."""
    _, key = _db_key(home, db_path=db_path)
    with _DB_REGISTRY_LOCK:
        db = _SHARED_DBS.pop(key, None)
    if db is not None:
        db.close()


def close_all_shared_dbs() -> None:
    """Close all cached `MuseDB` handles (called on graceful shutdown / tests)."""
    with _DB_REGISTRY_LOCK:
        dbs = list(_SHARED_DBS.values())
        _SHARED_DBS.clear()
    for db in dbs:
        with contextlib.suppress(Exception):
            db.close()


# ── Log privacy & HTTP client URL-scrubber bootstrap ─────────────────────────


def install_runtime_privacy() -> None:
    """Attach log privacy filters and lock `httpx`/`httpcore` loggers to WARNING."""
    with contextlib.suppress(ImportError):
        from . import interceptors

        install_fn = getattr(interceptors, "install_privacy_hardening", None) or getattr(
            interceptors, "install_log_privacy", None
        )
        if callable(install_fn):
            install_fn()
    with contextlib.suppress(Exception):
        from companion.modules import log_privacy

        log_privacy.install()
    for http_name in ("httpx", "httpcore"):
        logging.getLogger(http_name).setLevel(logging.WARNING)


# ── Duck-typed registration context for Companion's 23 product tools ─────────


class CompanionToolContext:
    """Adapter exposing `register_tool` / `register_hook` for Companion modules."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self.hooks: dict[str, list[Callable[..., Any]]] = {}

    def register_tool(
        self,
        name: str,
        schema: Optional[dict[str, Any]] = None,
        handler: Optional[Callable[..., Any]] = None,
        *,
        toolset: str = "companion",
        **extra: Any,
    ) -> None:
        if self._registry is None or handler is None:
            return
        reg_fn = getattr(self._registry, "register", None)
        if callable(reg_fn):
            reg_fn(name=name, schema=schema, handler=handler, toolset=toolset, **extra)

    def register_hook(self, event_name: str, callback: Callable[..., Any]) -> None:
        self.hooks.setdefault(str(event_name), []).append(callback)


_COMPANION_TOOL_MODULES = (
    "plans",
    "triggers",
    "feed",
    "helpers",
    "connections",
    "vault",
    "avatar_make",
    "avatar_motion",
)


def _optional_import(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


# ── NativeAgentEngine (MuseRuntime) ──────────────────────────────────────────


class NativeAgentEngine(ChatTurnEngine):
    """Full native async runtime assembling `ChatTurnEngine`, tools, MCP, cron, and goals.

    Inherits `ChatTurnEngine`'s `ChatTurnActor` registry, `HelperOrchestrator`,
    `GoalLoopController`, `ToolInterceptorPipeline`, and `NativeApprovalCoordinator`,
    and wires the process-wide shared `MuseDB`, built-in + 23 Companion tools,
    `AsyncMCPManager`, `CronScheduler`, and `muse.llm` runtime loop lifecycle.
    """

    def __init__(
        self,
        adapter: Any = None,
        *,
        home: Optional[Path | str] = None,
        db: Optional[MuseDB] = None,
        stream_llm: Optional[Callable[..., Any]] = None,
        execute_tool: Optional[Callable[..., Any]] = None,
        start_background_services: bool = True,
    ) -> None:
        self._adapter = adapter
        self._home = Path(home).expanduser() if home is not None else None
        self._explicit_db = db
        self._owns_shared_db = False
        self._start_bg = start_background_services

        self._tool_registry: Any = None
        self._cron_scheduler: Any = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        super().__init__(
            adapter,
            db=db,
            stream_llm=stream_llm,
            execute_tool=execute_tool,
            raw_tool_executor=None if execute_tool is not None else self._dispatch_registered_tool,
            tools_provider=self._collect_tools_schema,
        )
        # Shared DB lifecycle is managed by NativeAgentEngine, not ChatTurnEngine._owns_db.
        self._owns_db = False

    @property
    def db(self) -> MuseDB:
        return self._ensure_db()

    def _ensure_db(self) -> MuseDB:
        if self._db is None or getattr(self._db, "_closed", False):
            if self._explicit_db is not None and not getattr(self._explicit_db, "_closed", False):
                self._db = self._explicit_db
            else:
                self._db = get_shared_db(self._home)
                self._owns_shared_db = True
        return self._db

    @property
    def tool_registry(self) -> Any:
        return self._ensure_tool_registry()

    @property
    def mcp_manager(self) -> Any:
        return self._ensure_mcp_manager()

    @property
    def cron_scheduler(self) -> Any:
        return self._ensure_cron_scheduler()

    def _ensure_tool_registry(self) -> Any:
        if self._tool_registry is None:
            tools_mod = _optional_import("muse.tools")
            factory = getattr(tools_mod, "create_default_tool_registry", None) if tools_mod is not None else None
            if callable(factory):
                self._tool_registry = factory()
                self._register_companion_tools(self._tool_registry)
        return self._tool_registry

    def _ensure_cron_scheduler(self) -> Any:
        if self._cron_scheduler is None:
            cron_mod = _optional_import("muse.cron")
            sched_cls = getattr(cron_mod, "CronScheduler", None) if cron_mod is not None else None
            if sched_cls is not None:
                self._cron_scheduler = sched_cls(
                    run_job_fn=self._run_cron_job,
                    deliver_fn=self._deliver_cron_job,
                )
        return self._cron_scheduler

    def _init_subsystems(self) -> None:
        self._ensure_tool_registry()
        self._ensure_mcp_manager()
        self._ensure_cron_scheduler()

    def _register_companion_tools(self, registry: Any) -> None:
        ctx = CompanionToolContext(registry)
        for mod_name in _COMPANION_TOOL_MODULES:
            mod = _optional_import(f"companion.modules.{mod_name}")
            reg_fn = getattr(mod, "register_tools", None) if mod is not None else None
            if callable(reg_fn):
                with contextlib.suppress(Exception):
                    reg_fn(ctx)

        # Computer / browser hand-over tool takes (ctx, chat_for_session, notify_hand_over).
        computer_mod = _optional_import("companion.modules.computer")
        if computer_mod is not None and callable(getattr(computer_mod, "register_tools", None)):
            with contextlib.suppress(Exception):
                computer_mod.ensure_cdp_config()
                computer_mod.share_with_builtin_tools()
            with contextlib.suppress(Exception):
                activity_mod = _optional_import("companion.activity")
                chat_for_session = (
                    (lambda sid: activity_mod._chat_for_session(str(sid)) if sid else None)
                    if activity_mod is not None
                    else (lambda _sid: None)
                )

                def _notify_hand_over(request: dict[str, Any]) -> None:
                    if self._adapter is not None and callable(getattr(self._adapter, "schedule_push", None)):
                        name = self._adapter.companion_name() if callable(getattr(self._adapter, "companion_name", None)) else "伙伴"
                        self._adapter.schedule_push(name, f"需要你来一下：{request.get('reason') or ''}", tag="computer")

                computer_mod.register_tools(ctx, chat_for_session, _notify_hand_over)

        # Native video backends (Hailuo/Seedance/Wan) & local SenseVoice STT.
        for extra_mod_name, enable in (("video_backends", False), ("stt_sensevoice", True)):
            mod = _optional_import(f"companion.modules.{extra_mod_name}")
            if mod is None:
                continue
            reg_native = getattr(mod, "register_native", None)
            if callable(reg_native):
                with contextlib.suppress(Exception):
                    reg_native()
            if enable and callable(getattr(mod, "enable", None)):
                with contextlib.suppress(Exception):
                    mod.enable()

    async def _dispatch_registered_tool(self, name: str, args: dict[str, Any], inv: Any) -> str:
        ctx = getattr(inv, "ctx", None)
        if name == "helper_ask":
            return await self.helper_orchestrator.handle_helper_ask(args, ctx=ctx)
        if name == "browser_request":
            return await self.helper_orchestrator.handle_browser_request(args, ctx=ctx)
        if str(name).startswith("mcp__"):
            mgr = self._ensure_mcp_manager()
            call_fn = getattr(mgr, "call_tool", None)
            if callable(call_fn):
                return str(await call_fn(name, args))
        reg = self._ensure_tool_registry()
        if reg is not None:
            dispatch_fn = getattr(reg, "dispatch", None)
            if callable(dispatch_fn):
                sid = str(getattr(ctx, "session_id", "") or "")
                res = dispatch_fn(name, args, session_id=sid, ctx=ctx)
                if asyncio.iscoroutine(res) or isinstance(res, Awaitable):
                    return str(await res)
                return str(res)
        return f"Tool not available: {name}"

    def _collect_tools_schema(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        mcp_defs: list[dict[str, Any]] = []
        if self._mcp_manager is not None and callable(getattr(self._mcp_manager, "get_tool_definitions", None)):
            with contextlib.suppress(Exception):
                mcp_defs = list(self._mcp_manager.get_tool_definitions())
        reg = self._ensure_tool_registry()
        if reg is not None and callable(getattr(reg, "get_definitions", None)):
            schemas.extend(reg.get_definitions(extra_mcp_tools=mcp_defs))
        elif mcp_defs:
            schemas.extend(mcp_defs)
        return schemas

    async def _run_cron_job(self, job: dict[str, Any], prompt: str) -> str:
        job_id = str(job.get("id") or uuid.uuid4().hex[:8])
        override: Optional[dict[str, Any]] = None
        if job.get("model") or job.get("provider"):
            override = {k: job[k] for k in ("provider", "model", "base_url") if job.get(k)}
        return await self._run_child_actor_turn(
            chat_id=f"cron:{job_id}",
            prompt=prompt,
            turn_id=f"cron-{job_id}-{uuid.uuid4().hex[:6]}",
            model_override=override,
        )

    async def _deliver_cron_job(self, job: dict[str, Any], content: str) -> None:
        from .cron import resolve_delivery_chat_id

        chat_id = resolve_delivery_chat_id(job)
        if not chat_id:
            return
        await self.deliver_proactive(
            chat_id,
            content,
            job_id=str(job.get("id") or ""),
            attach_to_session=bool(job.get("attach_to_session", False)),
        )

    def get_actor(self, chat_id: str) -> Any:
        actor = super().get_actor(chat_id)
        if self._home is not None and getattr(actor, "home", None) is None:
            actor.home = self._home
        return actor

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        install_runtime_privacy()
        self._loop = asyncio.get_running_loop()
        register_runtime_loop(self._loop)
        await super().start()

        if self._start_bg:
            task = asyncio.create_task(self._warm_background_services())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _warm_background_services(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._ensure_tool_registry)
        sched = self._ensure_cron_scheduler()
        if sched is not None:
            with contextlib.suppress(Exception):
                await sched.start()
        try:
            await self.reload_mcp()
        except Exception as exc:
            logger.warning("muse.runtime: initial MCP connect skipped (%s)", type(exc).__name__)

    async def stop(self) -> None:
        self._started = False
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        if self._cron_scheduler is not None:
            with contextlib.suppress(Exception):
                await self._cron_scheduler.stop()

        await super().stop()

        if self._owns_shared_db:
            self._owns_shared_db = False
            self._db = None
            close_shared_db(self._home)

        register_runtime_loop(None)
        import sys

        aux_mod = sys.modules.get("muse.llm.auxiliary")
        if aux_mod is not None and callable(getattr(aux_mod, "close_shared_clients", None)):
            with contextlib.suppress(Exception):
                await aux_mod.close_shared_clients()


def create_native_engine(adapter: Any = None, **kwargs: Any) -> NativeAgentEngine:
    """Factory used by `muse.engine.build_engine(..., engine_name='native')`."""
    return NativeAgentEngine(adapter=adapter, **kwargs)


__all__ = [
    "CompanionToolContext",
    "NativeAgentEngine",
    "close_all_shared_dbs",
    "close_shared_db",
    "create_native_engine",
    "get_runtime_loop",
    "get_shared_db",
    "install_runtime_privacy",
    "peek_shared_db",
    "register_runtime_loop",
    "use_shared_db",
]
