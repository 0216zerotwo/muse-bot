"""Agent execution engine seam (``AgentEngine``) and transitional ``HermesEngineBridge``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any, Optional, Protocol, runtime_checkable

from .paths import EngineName, active_engine_name


@runtime_checkable
class AgentEngine(Protocol):
    """Unified interface implemented by both ``HermesEngineBridge`` and native ``MuseEngine``."""

    @property
    def name(self) -> EngineName: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit_turn(self, chat_id: str, text: str, turn_id: str, files: Optional[list[tuple[str, str]]] = None) -> None: ...
    async def stop_turn(self, chat_id: str) -> None: ...
    async def handle_goal_control(self, chat_id: str, command: str) -> None: ...
    async def renew_session(self, chat_id: str) -> bool: ...
    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None: ...
    async def reload_mcp(self) -> dict[str, Any]: ...


class HermesEngineBridge(AgentEngine):
    """Transitional ``AgentEngine`` implementation wrapping Hermes ``gateway_runner`` hooks."""

    def __init__(self, adapter: Any = None, *, runner: Any = None) -> None:
        self._adapter = adapter
        self._explicit_runner = runner

    @property
    def name(self) -> EngineName:
        return "hermes"

    @property
    def runner(self) -> Any:
        return self._explicit_runner if self._explicit_runner is not None else getattr(self._adapter, "gateway_runner", None)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def _build_event(self, chat_id: str, text: str, message_id: str, files: Optional[list[tuple[str, str]]] = None) -> Any:
        if self._adapter is not None and hasattr(self._adapter, "_event"):
            event = self._adapter._event(chat_id, text, message_id)
        else:
            src = type("BridgeSource", (), {"platform": "companion", "chat_id": chat_id})()
            event = type("BridgeEvent", (), {"text": text, "message_id": message_id, "source": src,
                                             "media_urls": [], "media_types": [], "media_text_inlined": []})()
        if files:
            event.media_urls = [p for p, _ in files]
            event.media_types = [m for _, m in files]
            event.media_text_inlined = [None if m.startswith(("image/", "audio/", "video/")) else False for _, m in files]
            try:
                from gateway.platforms.event import MessageType

                if all(m.startswith("image/") for _, m in files):
                    event.message_type = MessageType.PHOTO
                elif not any(m.startswith(("image/", "audio/", "video/")) for _, m in files):
                    event.message_type = MessageType.DOCUMENT
            except ImportError:
                pass
        return event

    async def submit_turn(self, chat_id: str, text: str, turn_id: str, files: Optional[list[tuple[str, str]]] = None) -> None:
        if self._adapter is None:
            raise RuntimeError("HermesEngineBridge requires a bound adapter to submit turns")
        await self._adapter.handle_message(self._build_event(chat_id, text, turn_id, files=files))

    async def stop_turn(self, chat_id: str) -> None:
        if self._adapter is not None:
            await self._adapter.handle_message(self._build_event(chat_id, "/stop", uuid.uuid4().hex))

    async def handle_goal_control(self, chat_id: str, command: str) -> None:
        if self._adapter is not None:
            await self._adapter.handle_message(self._build_event(chat_id, command.strip(), uuid.uuid4().hex))

    async def renew_session(self, chat_id: str) -> bool:
        reset = getattr(self.runner, "_handle_reset_command", None)
        if not chat_id or not callable(reset):
            return False
        await reset(self._build_event(chat_id, "/reset", uuid.uuid4().hex))
        return True

    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None:
        runner = self.runner
        key_fn = getattr(runner, "_session_key_for_source", None)
        overrides = getattr(runner, "_session_model_overrides", None)
        if runner is None or not chat_id or not callable(key_fn) or not isinstance(overrides, dict):
            return
        key = key_fn(self._build_event(chat_id, "", "").source)
        if override == overrides.get(key):
            return
        if override:
            overrides[key] = override
        else:
            overrides.pop(key, None)
        if callable(evict := getattr(runner, "_evict_cached_agent", None)):
            evict(key)

    async def reload_mcp(self) -> dict[str, Any]:
        def _sync_reload() -> dict[str, Any]:
            from tools import mcp_tool_discovery as discovery
            from tools.mcp_oauth import suppress_interactive_oauth

            with suppress_interactive_oauth():
                result = discovery.reconcile_mcp_servers_with_config()
                failed = discovery.mcp_servers_not_connected()
            if callable(refresh := getattr(self.runner, "_mcp_reload_refresh_cached_agents", None)):
                refresh(False, None)
            return {
                "ok": not failed, "message": "", "failed": list(failed),
                **{k: list(v) for k, v in (result or {}).items() if isinstance(v, (list, tuple, set))},
            }

        return await asyncio.to_thread(_sync_reload)


class ChatTurnEngine(AgentEngine):
    """Native ``AgentEngine`` wiring shared ``MuseDB``, ``muse.llm``, middleware, helpers, and ``GoalLoopController``."""

    def __init__(
        self,
        sink: Any,
        *,
        db: Any = None,
        stream_llm: Optional[Callable[..., Any]] = None,
        execute_tool: Optional[Callable[..., Any]] = None,
        raw_tool_executor: Optional[Callable[..., Any]] = None,
        use_interceptors: bool = False,
        approvals: Any = None,
        on_goal_control: Optional[Callable[..., Any]] = None,
        tools_provider: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ) -> None:
        from .context import ContextCompactor, PromptAssembler
        from .helpers_orch import HelperOrchestrator
        from .interceptors import NativeApprovalCoordinator, ToolInterceptorPipeline

        self._sink = sink
        self._db = db
        self._owns_db = db is None
        self._custom_stream_llm = stream_llm
        self._execute_tool = execute_tool
        self._on_goal_control = on_goal_control
        self._tools_provider = tools_provider
        self._assembler = PromptAssembler()
        self._compactor = ContextCompactor()
        self._actors: dict[str, Any] = {}
        self._goal_controller: Any = None

        resolved_approvals = approvals if approvals is not None else getattr(sink, "approvals", None)
        self.approval_coordinator: Optional[NativeApprovalCoordinator] = (
            NativeApprovalCoordinator(approvals=resolved_approvals, adapter=sink)
            if resolved_approvals is not None
            else None
        )
        self.pipeline: Optional[ToolInterceptorPipeline] = None
        if raw_tool_executor is not None:
            self.pipeline = ToolInterceptorPipeline(raw_tool_executor, coordinator=self.approval_coordinator)
        elif use_interceptors and execute_tool is not None:
            self.pipeline = ToolInterceptorPipeline(
                lambda name, args, inv: execute_tool({"id": inv.call_id, "name": name, "arguments": args}, inv.ctx),
                coordinator=self.approval_coordinator,
            )
        self.helper_orchestrator = HelperOrchestrator(
            self._run_child_actor_turn,
            store=getattr(sink, "store", None),
            post_as_helper_fn=getattr(sink, "post_as_helper", None),
            submit_internal_fn=self.submit_internal,
            set_model_override_fn=self.set_session_model_override,
            delete_desk_session_fn=self._cleanup_desk_actor,
        )

    @property
    def name(self) -> EngineName:
        return "native"

    @property
    def goal_controller(self) -> Any:
        if self._goal_controller is None:
            from .goal_loop import GoalLoopController

            self._goal_controller = GoalLoopController(db=self._ensure_db(), adapter=self._sink)
        return self._goal_controller

    def _ensure_db(self) -> Any:
        if self._db is None:
            from .db import MuseDB

            self._db = MuseDB()
            self._owns_db = True
        return self._db

    async def start(self) -> None:
        from .interceptors import install_privacy_hardening

        install_privacy_hardening()
        db = self._ensure_db()
        if callable(getattr(db, "sync_from_legacy_state_db", None)):
            await asyncio.to_thread(db.sync_from_legacy_state_db)

    async def stop(self) -> None:
        for actor in list(self._actors.values()):
            if actor.active_turn_id is not None:
                await actor.handle_stop()
        if self._goal_controller is not None:
            self._goal_controller.close()
        if self._owns_db and self._db is not None and callable(getattr(self._db, "close", None)):
            self._db.close()
            self._db = None

    async def _default_stream_llm(
        self,
        *,
        messages: list[dict[str, Any]],
        model_override: Optional[dict[str, Any]] = None,
        cancel_scope: Any = None,
        actor: Any = None,
    ) -> Any:
        from .llm.auxiliary import create_transport, describe_images_if_blind, get_pooled_async_client
        from .llm.providers import resolve_runtime_provider

        del cancel_scope
        ov = model_override or {}
        runtime = await asyncio.to_thread(
            resolve_runtime_provider,
            ov.get("provider"),
            target_model=ov.get("model"),
            base_url=ov.get("base_url"),
            api_key=ov.get("api_key"),
            api_mode=ov.get("api_mode"),
        )
        if actor is not None:
            actor.model_name = str(runtime.get("model") or actor.model_name)
            actor.provider_name = str(runtime.get("provider") or actor.provider_name)
            actor.base_url = str(runtime.get("base_url") or actor.base_url)
            if isinstance(ctx_len := runtime.get("context_length"), int) and ctx_len > 0:
                actor.context_window = ctx_len
        chat_vision = bool(ov.get("vision", True))
        prepared = await describe_images_if_blind(messages, chat_vision=chat_vision)
        transport = create_transport(
            api_mode=str(runtime.get("api_mode") or "chat_completions"),
            base_url=str(runtime.get("base_url") or ""),
            api_key=str(runtime.get("api_key") or ""),
            provider=str(runtime.get("provider") or ""),
            default_headers=runtime.get("extra_headers") or None,
            client=get_pooled_async_client(),
        )
        tools = self._tools_provider() if self._tools_provider is not None else None
        async for ev in transport.stream(
            model=str(runtime.get("model") or ""),
            messages=prepared,
            tools=tools,
            max_tokens=runtime.get("max_tokens"),
            temperature=runtime.get("temperature"),
            extra_body=runtime.get("extra_body") or None,
        ):
            yield ev

    async def _actor_execute_tool(self, tc: dict[str, Any], ctx: Any) -> str:
        import json

        if self.pipeline is not None:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "")
            raw_args = fn.get("arguments") if "arguments" in fn else tc.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (dict(raw_args) if isinstance(raw_args, dict) else {})
            except ValueError:
                args = {}
            call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")
            return await self.pipeline.execute(name, args, call_id=call_id, ctx=ctx)
        if self._execute_tool is not None:
            return await self._execute_tool(tc, ctx)
        return json.dumps({"error": "No tool executor configured"})

    async def _default_goal_control(self, chat_id: str, command: str) -> None:
        await self.goal_controller.handle_control(chat_id, command)

    async def _cleanup_desk_actor(self, desk_chat_id: str) -> None:
        self._actors.pop(desk_chat_id, None)
        if self._db is not None and callable(getattr(self._db, "get_active_session", None)):
            sess = self._db.get_active_session(desk_chat_id, source="companion")
            if sess and sess.get("id") and callable(getattr(self._db, "update_session", None)):
                self._db.update_session(str(sess["id"]), active=False)

    async def _run_child_actor_turn(
        self, *, chat_id: str, prompt: str, turn_id: str, ctx: Any = None, model_override: Optional[dict[str, Any]] = None
    ) -> str:
        actor = self.get_actor(chat_id)
        prev_facing, prev_kind, prev_helper_id = actor.facing_chat_id, actor.actor_kind, actor.helper_id
        try:
            if ctx is not None:
                actor.facing_chat_id = str(getattr(ctx, "facing_chat_id", "") or chat_id)
                actor.actor_kind = getattr(ctx, "actor_kind", prev_kind)
                actor.helper_id = getattr(ctx, "helper_id", prev_helper_id)
            actor.set_model_override(model_override)
            await actor.submit(prompt, turn_id, emit_user_message=False)
            await actor.wait_idle()
        finally:
            actor.facing_chat_id = prev_facing
            actor.actor_kind = prev_kind
            actor.helper_id = prev_helper_id
        events = self._sink.store_events(chat_id)
        return "\n\n".join(
            str(e.get("text") or "").strip()
            for e in events
            if e.get("event") == "message" and e.get("turn_phase") == "answer" and e.get("turn_id") == turn_id and str(e.get("text") or "").strip()
        )

    async def execute_approved(self, record: Any, *, skip_policy_gates: bool = True) -> str:
        if self.approval_coordinator is not None and self.pipeline is not None:
            return await self.approval_coordinator.execute_approved(record, skip_policy_gates=skip_policy_gates)
        if self._execute_tool is not None:
            from .context import TurnExecutionContext

            chat_id = str(getattr(record, "session_key", "") or "").split(":", 1)[-1]
            ctx = TurnExecutionContext(chat_id=chat_id, facing_chat_id=chat_id, session_id=f"approved:{record.id}")
            tc = {"id": record.call_id, "name": record.tool_name, "arguments": dict(record.arguments)}
            return await self._execute_tool(tc, ctx)
        raise RuntimeError("No approval executor configured on ChatTurnEngine")

    @staticmethod
    def _default_actor_identity(chat_id: str) -> tuple[Any, Optional[str]]:
        try:
            from companion.modules import helpers

            row = helpers.helper_for_chat(chat_id)
            if isinstance(row, dict) and row.get("id"):
                return "helper", str(row["id"])
        except Exception:
            pass
        return "companion", None

    def get_actor(self, chat_id: str) -> Any:
        from .turn_actor import ChatTurnActor

        actor = self._actors.get(chat_id)
        if actor is None:
            async def _bound_stream(**kw: Any) -> Any:
                stream_fn = self._custom_stream_llm
                if stream_fn is not None:
                    async for ev in stream_fn(**kw):
                        yield ev
                else:
                    async for ev in self._default_stream_llm(actor=actor, **kw):
                        yield ev

            tool_fn = (
                self._actor_execute_tool
                if (self.pipeline is not None or self._execute_tool is not None)
                else None
            )
            default_kind, default_helper_id = self._default_actor_identity(chat_id)
            actor = ChatTurnActor(
                chat_id,
                db=self._ensure_db(),
                sink=self._sink,
                stream_llm=_bound_stream,
                execute_tool=tool_fn,
                assembler=self._assembler,
                compactor=self._compactor,
                facing_chat_id=chat_id,
                actor_kind=default_kind,
                helper_id=default_helper_id,
                on_goal_control=self._on_goal_control or self._default_goal_control,
            )
            self._actors[chat_id] = actor
        return actor

    async def submit_turn(
        self,
        chat_id: str,
        text: str,
        turn_id: str,
        files: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        actor = self.get_actor(chat_id)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        await actor.submit(text, turn_id, files=files, emit_user_message=False)

    async def submit_internal(self, chat_id: str, text: str) -> str:
        actor = self.get_actor(chat_id)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        return await actor.submit_internal(text)

    async def stop_turn(self, chat_id: str) -> None:
        await self.get_actor(chat_id).handle_stop()

    async def handle_goal_control(self, chat_id: str, command: str) -> None:
        await self.get_actor(chat_id).submit(command.strip(), f"webui-system:{uuid.uuid4().hex}")

    async def renew_session(self, chat_id: str) -> bool:
        return await self.get_actor(chat_id).renew_session(silent=True)

    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None:
        self.get_actor(chat_id).set_model_override(override)

    async def regenerate(self, chat_id: str, turn_id: str) -> str:
        actor = self.get_actor(chat_id)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        return await actor.regenerate(turn_id)

    async def deliver_proactive(
        self,
        chat_id: str,
        text: str,
        *,
        job_id: str,
        attach_to_session: bool = False,
    ) -> None:
        await self.get_actor(chat_id).deliver_proactive(text, job_id=job_id, attach_to_session=attach_to_session)

    async def reload_mcp(self) -> dict[str, Any]:
        return {"ok": True, "message": "", "failed": []}


_NATIVE_ENGINE_FACTORY: Optional[Callable[..., AgentEngine]] = None


def register_native_engine_factory(factory: Optional[Callable[..., AgentEngine]]) -> None:
    global _NATIVE_ENGINE_FACTORY
    _NATIVE_ENGINE_FACTORY = factory


def build_engine(adapter: Any = None, *, runner: Any = None, engine_name: Optional[EngineName] = None) -> AgentEngine:
    selected = engine_name or active_engine_name()
    if selected == "native":
        if _NATIVE_ENGINE_FACTORY is None:
            raise NotImplementedError("MUSE_ENGINE=native requested before native MuseEngine runtime is registered.")
        return _NATIVE_ENGINE_FACTORY(adapter=adapter)
    return HermesEngineBridge(adapter=adapter, runner=runner)


__all__ = [
    "AgentEngine",
    "ChatTurnEngine",
    "EngineName",
    "HermesEngineBridge",
    "build_engine",
    "register_native_engine_factory",
]
