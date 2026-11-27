"""Native agent execution engine interface (``AgentEngine``) and ``ChatTurnEngine``."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from typing import Any, Optional, Protocol, runtime_checkable

from .config_store import load_config
from .paths import EngineName


def _build_reasoning_config(effort: Any) -> Optional[dict[str, Any]]:
    if isinstance(effort, dict):
        return dict(effort)
    raw = str(effort or "").strip().lower()
    if not raw:
        return None
    if raw in ("none", "off", "disabled", "false"):
        return {"enabled": False, "effort": "none"}
    return {"enabled": True, "effort": raw}


def _resolve_chat_vision(ov: dict[str, Any], runtime: dict[str, Any], cfg: dict[str, Any]) -> bool:
    if "vision" in ov and ov["vision"] is not None:
        return bool(ov["vision"])
    prov_id = str(runtime.get("provider") or "").strip()
    mod_name = str(runtime.get("model") or "").strip()
    providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    p_entry = providers_cfg.get(prov_id) or providers_cfg.get(prov_id.lower())
    if isinstance(p_entry, dict) and "vision" in p_entry and p_entry["vision"] is not None:
        return bool(p_entry["vision"])
    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict) and "vision" in model_cfg and model_cfg["vision"] is not None:
        return bool(model_cfg["vision"])
    with contextlib.suppress(Exception):
        from companion.modules import models as comp_models

        data = comp_models.read_models() or {}
        presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
        rmodels = ((data.get("router") or {}).get("models") or {}) if isinstance(data.get("router"), dict) else {}
        preset_name = str(ov.get("preset") or "").strip()
        if not preset_name:
            active = comp_models.chat_preset(data)
            if active and active in presets and presets[active].get("model") == mod_name and comp_models.runtime_provider(active, presets[active]) == prov_id:
                preset_name = active
        if not preset_name:
            preset_name = next(
                (n for n, p in presets.items() if isinstance(p, dict) and comp_models.runtime_provider(n, p) == prov_id and p.get("model") == mod_name),
                "",
            ) or next((n for n, p in presets.items() if isinstance(p, dict) and p.get("model") == mod_name), "")
        info = rmodels.get(preset_name) if preset_name else rmodels.get(mod_name)
        if isinstance(info, dict) and "vision" in info:
            return bool(info["vision"])
    return True


@runtime_checkable
class AgentEngine(Protocol):
    """Unified interface implemented by the native Muse execution engine."""

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
        mcp_manager: Any = None,
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
        self._mcp_manager = mcp_manager
        self._mcp_active = False
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
        emit_fn = getattr(sink, "emit", None)

        async def _emit_chat_event(chat_id: str, event: dict[str, Any], persist: bool) -> None:
            if callable(emit_fn):
                await emit_fn(chat_id, event, persist=persist)

        async def _wait_chat_idle(chat_id: str, timeout: float) -> None:
            actor = self._actors.get(chat_id)
            if actor is not None:
                await actor.wait_idle(timeout=timeout)

        self.helper_orchestrator = HelperOrchestrator(
            self._run_child_actor_turn,
            store=getattr(sink, "store", None),
            emit_chat_event_fn=_emit_chat_event if callable(emit_fn) else None,
            post_as_helper_fn=getattr(sink, "post_as_helper", None),
            submit_internal_fn=self.submit_internal,
            wait_chat_idle_fn=_wait_chat_idle,
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
            if (bt := getattr(actor, "_goal_barrier_task", None)) is not None and not bt.done():
                bt.cancel()
                await asyncio.wait({bt}, timeout=1.0)
            if actor.active_turn_id is not None or getattr(actor, "_pending_clarify", None) is not None:
                await actor.handle_stop()
            elif (rt := getattr(actor, "_runner_task", None)) is not None and not rt.done():
                rt.cancel()
                await asyncio.wait({rt}, timeout=1.0)
                if rt.done() and not rt.cancelled():
                    _ = rt.exception()
        if self._mcp_active and self._mcp_manager is not None:
            await self._mcp_manager.shutdown()
            self._mcp_active = False
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
        from .helpers_orch import resolve_helper_model_override
        from .llm.auxiliary import create_transport, describe_images_if_blind, get_pooled_async_client
        from .llm.providers import resolve_runtime_provider

        del cancel_scope
        ov = dict(model_override) if model_override else {}
        if not ov and actor is not None and getattr(actor, "actor_kind", "") == "helper":
            _, _, hrow = self._resolve_actor_helper(actor.chat_id)
            if hrow is not None:
                ov = resolve_helper_model_override(hrow) or {}

        cfg = await asyncio.to_thread(load_config)
        candidates: list[dict[str, Any]] = [ov]
        raw_fallbacks = cfg.get("fallback_providers")
        if isinstance(raw_fallbacks, list):
            for fb in raw_fallbacks:
                if isinstance(fb, dict) and (fb.get("provider") or fb.get("model")):
                    candidates.append(dict(fb))
                elif isinstance(fb, str) and fb.strip():
                    candidates.append({"provider": fb.strip()})

        seen_targets: set[tuple[str, str, str]] = set()
        last_exc: Optional[BaseException] = None
        overflow_exc: Optional[BaseException] = None
        tools = self._tools_provider() if self._tools_provider is not None else None

        for idx, cand in enumerate(candidates):
            runtime = await asyncio.to_thread(
                resolve_runtime_provider,
                cand.get("provider"),
                target_model=cand.get("model"),
                config=cfg,
                base_url=cand.get("base_url"),
                api_key=cand.get("api_key"),
                api_mode=cand.get("api_mode"),
            )
            sig = (str(runtime.get("provider") or ""), str(runtime.get("model") or ""), str(runtime.get("base_url") or ""))
            if idx > 0 and sig in seen_targets:
                continue
            seen_targets.add(sig)

            if actor is not None:
                actor.model_name = str(runtime.get("model") or actor.model_name)
                actor.provider_name = str(runtime.get("provider") or "")
                actor.base_url = str(runtime.get("base_url") or "")
                if isinstance(ctx_len := runtime.get("context_length"), int) and ctx_len > 0:
                    actor.context_window = ctx_len

            chat_vision = _resolve_chat_vision(cand, runtime, cfg)
            prepared = await describe_images_if_blind(messages, chat_vision=chat_vision, config=cfg)
            effort = cand.get("reasoning_effort") if "reasoning_effort" in cand and cand.get("reasoning_effort") is not None else runtime.get("reasoning_effort")
            reasoning_config = _build_reasoning_config(effort)
            transport = create_transport(
                api_mode=str(runtime.get("api_mode") or "chat_completions"),
                base_url=str(runtime.get("base_url") or ""),
                api_key=str(runtime.get("api_key") or ""),
                provider=str(runtime.get("provider") or ""),
                default_headers=runtime.get("extra_headers") or None,
                client=get_pooled_async_client(),
            )
            emitted_output = False
            try:
                stream_kw: dict[str, Any] = {
                    "model": str(runtime.get("model") or ""),
                    "messages": prepared,
                    "tools": tools,
                    "max_tokens": runtime.get("max_tokens"),
                    "temperature": runtime.get("temperature"),
                    "extra_body": runtime.get("extra_body") or None,
                }
                if reasoning_config is not None:
                    stream_kw["reasoning_config"] = reasoning_config
                async for ev in transport.stream(**stream_kw):
                    if type(ev).__name__ in (
                        "TextDelta", "ReasoningDelta", "ToolCallDelta", "ToolCallComplete",
                        "ThinkingBlockComplete", "EncryptedReasoningItem",
                    ):
                        emitted_output = True
                    yield ev
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from .turn_actor import classify_turn_failure

                is_overflow = classify_turn_failure(exc)[2]
                if idx == 0 and is_overflow:
                    overflow_exc = exc
                last_exc = exc
                if emitted_output or (idx == 0 and is_overflow and not getattr(actor, "_overflow_retried", False)):
                    raise
                if idx >= len(candidates) - 1:
                    raise (overflow_exc or exc)
                continue
        if overflow_exc is not None:
            raise overflow_exc
        if last_exc is not None:
            raise last_exc

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
        prev_toolsets, prev_override = actor.enabled_toolsets, actor._model_override
        try:
            actor.max_iterations = self._configured_max_turns()
            if ctx is not None:
                kind = getattr(ctx, "actor_kind", prev_kind)
                actor.actor_kind = kind
                is_cron = kind == "cron" or str(chat_id).startswith("cron:")
                raw_facing = str(getattr(ctx, "facing_chat_id", "") or "")
                actor.facing_chat_id = ("" if raw_facing.startswith("cron:") else raw_facing) if is_cron else (raw_facing or chat_id)
                actor.helper_id = getattr(ctx, "helper_id", prev_helper_id)
                actor.enabled_toolsets = tuple(getattr(ctx, "enabled_toolsets", ()) or ())
            actor.set_model_override(model_override)
            await actor.submit(prompt, turn_id, emit_user_message=False)
            await actor.wait_idle()
        finally:
            actor.facing_chat_id = prev_facing
            actor.actor_kind = prev_kind
            actor.helper_id = prev_helper_id
            actor.enabled_toolsets = prev_toolsets
            actor.set_model_override(prev_override)
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
    def _configured_max_turns() -> int:
        with contextlib.suppress(Exception):
            cfg = load_config()
            raw = (cfg.get("agent") or {}).get("max_turns") if isinstance(cfg.get("agent"), dict) else cfg.get("max_turns")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
        return 25

    @staticmethod
    def _resolve_actor_helper(chat_id: str) -> tuple[Any, Optional[str], Optional[dict[str, Any]]]:
        if str(chat_id).startswith("cron:"):
            return "cron", None, None
        try:
            from companion.modules import helpers

            row = helpers.helper_for_chat(chat_id)
            if isinstance(row, dict) and row.get("id"):
                return "helper", str(row["id"]), row
        except Exception:
            pass
        return "companion", None, None

    @classmethod
    def _default_actor_identity(cls, chat_id: str) -> tuple[Any, Optional[str]]:
        kind, hid, _ = cls._resolve_actor_helper(chat_id)
        return kind, hid

    def _apply_model_override(self, actor: Any, ov: Optional[dict[str, Any]], *, from_store: bool = False) -> None:
        if from_store:
            with contextlib.suppress(Exception):
                from companion.modules.models import resolve_sink_session_override
                ok, ov = resolve_sink_session_override(self._sink, actor.chat_id)
                if not ok:
                    return
        actor.set_model_override(ov)
        ctx = ov.get("context_length") if isinstance(ov, dict) else None
        if isinstance(ctx, int) and ctx > 0:
            actor.context_window = ctx
        elif ov is None and not from_store:
            actor.context_window = 128_000

    def _refresh_actor_settings(self, actor: Any) -> None:
        from .helpers_orch import resolve_helper_model_override

        actor.max_iterations = self._configured_max_turns()
        kind, hid, hrow = self._resolve_actor_helper(actor.chat_id)
        if hrow is not None:
            actor.actor_kind, actor.helper_id = "helper", hid
            self._apply_model_override(actor, resolve_helper_model_override(hrow))
        elif getattr(actor, "actor_kind", "") == "helper":
            actor.actor_kind, actor.helper_id = kind, None
            self._apply_model_override(actor, None)
            if kind == "companion":
                self._apply_model_override(actor, None, from_store=True)
        elif kind == "companion" and actor._model_override is None:
            self._apply_model_override(actor, None, from_store=True)

    def get_actor(self, chat_id: str) -> Any:
        from .helpers_orch import resolve_helper_model_override
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
            default_kind, default_helper_id, helper_row = self._resolve_actor_helper(chat_id)
            close_browser_fn = self.pipeline.close_browser_after_task if self.pipeline is not None else None
            actor = ChatTurnActor(
                chat_id,
                db=self._ensure_db(),
                sink=self._sink,
                stream_llm=_bound_stream,
                execute_tool=tool_fn,
                assembler=self._assembler,
                compactor=self._compactor,
                facing_chat_id="" if default_kind == "cron" else chat_id,
                actor_kind=default_kind,
                helper_id=default_helper_id,
                on_goal_control=self._on_goal_control or self._default_goal_control,
                goal_controller=self.goal_controller,
                close_browser_fn=close_browser_fn,
                max_iterations=self._configured_max_turns(),
            )
            if helper_row is not None:
                self._apply_model_override(actor, resolve_helper_model_override(helper_row))
            elif default_kind == "companion":
                self._apply_model_override(actor, None, from_store=True)
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
        self._refresh_actor_settings(actor)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        await actor.submit(text, turn_id, files=files, emit_user_message=False)

    async def submit_internal(self, chat_id: str, text: str) -> str:
        actor = self.get_actor(chat_id)
        self._refresh_actor_settings(actor)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        return await actor.submit_internal(text)

    async def stop_turn(self, chat_id: str) -> None:
        await self.get_actor(chat_id).handle_stop()

    async def handle_goal_control(self, chat_id: str, command: str) -> None:
        await self.get_actor(chat_id).submit(command.strip(), f"webui-system:{uuid.uuid4().hex}")

    async def renew_session(self, chat_id: str) -> bool:
        return await self.get_actor(chat_id).renew_session(silent=True)

    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None:
        self._apply_model_override(self.get_actor(chat_id), override)

    async def regenerate(self, chat_id: str, turn_id: str) -> str:
        actor = self.get_actor(chat_id)
        self._refresh_actor_settings(actor)
        actor.facing_chat_id = self.helper_orchestrator.facing_chat(chat_id) or chat_id
        return await actor.regenerate(turn_id)

    async def delete_turn(self, chat_id: str, turn_id: str, *, role: Optional[str] = None) -> int:
        from .turn_actor import CANCELLED_TOOL_RESULT

        db = self._ensure_db()
        sess = db.get_active_session(chat_id, source="companion") if callable(getattr(db, "get_active_session", None)) else None
        if not sess or not sess.get("id") or not turn_id:
            return 0
        rows = db.get_messages(str(sess["id"]), active_only=True)
        turn_rows = [r for r in rows if r.get("turn_id") == turn_id]
        if not turn_rows:
            return 0
        remaining = [e for e in self._sink.store_events(chat_id) if e.get("turn_id") == turn_id and e.get("event") != "turn_end"]
        clarify_row = next((r for r in turn_rows if r.get("role") == "tool" and r.get("tool_name") == "clarify"), None)
        if clarify_row is not None:
            cid = int(clarify_row["id"])
            if role == "reply" and remaining:
                to_drop = [int(r["id"]) for r in turn_rows if int(r["id"]) != cid and r.get("role") != "user"]
                return int(db.deactivate_messages(to_drop)) if to_drop else 0
            prev_tid = next((str(p.get("turn_id") or "") for p in reversed(rows) if int(p["id"]) < cid and p.get("role") == "assistant"), "") or None
            db.update_message(cid, content=CANCELLED_TOOL_RESULT, turn_id=prev_tid, extra={"awaiting_user": False, "deleted": True})
            to_drop = [int(r["id"]) for r in turn_rows if int(r["id"]) != cid]
            self._sink.remove_store_events(chat_id, lambda e: e.get("turn_id") == turn_id)
            return (int(db.deactivate_messages(to_drop)) if to_drop else 0) + 1
        to_drop = []
        for r in turn_rows:
            rid = int(r["id"])
            if not remaining or role is None:
                to_drop.append(rid)
            elif role == "user" and r.get("role") == "user":
                to_drop.append(rid)
            elif role == "reply" and r.get("role") != "user":
                to_drop.append(rid)
        return int(db.deactivate_messages(to_drop)) if to_drop else 0

    async def delete_chat(self, chat_id: str) -> None:
        actor = self._actors.pop(chat_id, None)
        if actor is not None:
            if getattr(actor, "_renew_timer", None) is not None:
                actor._renew_timer.cancel()
            if (bt := getattr(actor, "_goal_barrier_task", None)) is not None and not bt.done():
                bt.cancel()
                await asyncio.wait({bt}, timeout=1.0)
            if (
                actor.active_turn_id is not None
                or actor._pending_clarify is not None
                or ((rt := getattr(actor, "_runner_task", None)) is not None and not rt.done())
            ):
                with contextlib.suppress(Exception):
                    await actor.handle_stop()
        db = self._ensure_db()
        if callable(getattr(db, "get_active_session", None)):
            while (sess := db.get_active_session(chat_id, source="companion")) and sess.get("id"):
                sid = str(sess["id"])
                self._assembler.evict_session(sid)
                db.update_session(sid, ended_at=time.time(), active=False)
        if callable(getattr(db, "clear_goal", None)):
            with contextlib.suppress(Exception):
                db.clear_goal(chat_id)

    async def deliver_proactive(
        self,
        chat_id: str,
        text: str,
        *,
        job_id: str,
        attach_to_session: bool = False,
    ) -> None:
        await self.get_actor(chat_id).deliver_proactive(text, job_id=job_id, attach_to_session=attach_to_session)

    def _ensure_mcp_manager(self) -> Any:
        if self._mcp_manager is None:
            from .mcp import get_mcp_manager

            self._mcp_manager = get_mcp_manager()
        return self._mcp_manager

    async def reload_mcp(self) -> dict[str, Any]:
        self._mcp_active = True
        return await self._ensure_mcp_manager().reload_mcp()


_NATIVE_ENGINE_FACTORY: Optional[Callable[..., AgentEngine]] = None


def register_native_engine_factory(factory: Optional[Callable[..., AgentEngine]]) -> None:
    global _NATIVE_ENGINE_FACTORY
    _NATIVE_ENGINE_FACTORY = factory


def build_engine(adapter: Any = None, *, runner: Any = None, engine_name: Optional[EngineName] = None) -> AgentEngine:
    del runner, engine_name
    if _NATIVE_ENGINE_FACTORY is not None:
        return _NATIVE_ENGINE_FACTORY(adapter=adapter)
    from .runtime import create_native_engine

    return create_native_engine(adapter=adapter)


__all__ = [
    "AgentEngine",
    "ChatTurnEngine",
    "EngineName",
    "build_engine",
    "register_native_engine_factory",
]
