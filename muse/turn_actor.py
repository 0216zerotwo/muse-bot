"""Per-chat async turn actor (`ChatTurnActor`) and 4-level inbound priority router."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .context import (
    ActorKind, CancelScope, ContextCompactor, PromptAssembler, TurnExecutionContext,
    build_content_parts_sync as _build_content_parts_sync, run_in_turn_thread, turn_context_scope,
)
from .interceptors import scrub_url_secrets
from .notices import render_notice
from .streaming import (
    APPROVE_COMMAND, CANCEL_COMMAND, ChatSink, DraftSegmentStreamer, classify_turn_failure,
    extract_media_lines, failure_notice_zh, format_clarify_card, format_slash_confirm_card,
    new_history_id, strip_card_fences, translate_retry_status,
)

logger = logging.getLogger(__name__)

STOP_COMMAND = "/stop"
RETRY_COMMAND = "/retry"
COMPACT_COMMAND = "/compact"
GOAL_CONTROLS = frozenset({"/goal stop", "/goal clear", "/goal pause", "/goal resume"})
CONFIRMABLE_COMMANDS = frozenset({"new", "reset", "undo"})
SYSTEM_TURN_PREFIX = "webui-system:"
CLARIFY_PLACEHOLDER = "[Awaiting user response]"
CANCELLED_TOOL_RESULT = "[Cancelled by user]"
CLARIFY_TIMEOUT_S = 3600.0
SLASH_CONFIRM_TIMEOUT_S = 300.0
SOUL_RENEW_DELAY_S = 3.0


@dataclass(slots=True)
class InboundMessage:
    text: str
    turn_id: str
    media_urls: list[dict[str, Any]] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    internal: bool = False
    is_retry: bool = False


@dataclass(slots=True)
class PendingClarify:
    clarify_id: str
    chat_id: str
    turn_id_a: str
    tool_call_id: str
    tool_message_id: int
    question: str
    choices: list[str]
    multi_select: bool
    future: asyncio.Future[tuple[str, str, list[tuple[str, str]]]]
    created_at: float
    timeout_s: float = CLARIFY_TIMEOUT_S


@dataclass(slots=True)
class PendingSlashConfirm:
    confirm_id: str
    chat_id: str
    command: str
    confirm_turn_id: str
    target_turn_id: Optional[str]
    created_at: float
    timeout_s: float = SLASH_CONFIRM_TIMEOUT_S


def db_rows_to_llm_messages(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert `MuseDB.get_messages(...)` rows into OpenAI/Anthropic-compatible message dicts."""
    out: list[dict[str, Any]] = []
    extra_keys = (
        "reasoning_content", "extra_content", "reasoning_details",
        "codex_reasoning_items", "anthropic_content_blocks",
    )
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        parts = extra.get("content_parts")
        if not isinstance(parts, list) and row.get("role") == "user" and isinstance(extra.get("files"), list):
            parts = _build_content_parts_sync(str(row.get("content") or ""), extra["files"])
        content: Any = parts if isinstance(parts, list) else (row.get("content") or "")
        msg: dict[str, Any] = {"role": str(row.get("role") or "user"), "content": content}
        if isinstance(row.get("id"), int):
            msg["id"] = row["id"]
        if row.get("tool_calls"):
            msg["tool_calls"] = row["tool_calls"]
        if row.get("tool_call_id"):
            msg["tool_call_id"] = row["tool_call_id"]
        if row.get("tool_name"):
            msg["tool_name"] = msg["name"] = row["tool_name"]
        for key in extra_keys:
            if extra.get(key) is not None:
                msg[key] = extra[key]
        out.append(msg)
    return out


StreamLLMFn = Callable[..., AsyncIterator[Any]]
ToolExecFn = Callable[[dict[str, Any], TurnExecutionContext], Awaitable[str]]
GoalControlFn = Callable[[str, str], Awaitable[None]]


class ChatTurnActor:
    """Single-chat async actor implementing the 4-level inbound priority router & turn loop."""

    def __init__(
        self, chat_id: str, *, db: Any, sink: ChatSink, stream_llm: StreamLLMFn,
        execute_tool: Optional[ToolExecFn] = None, assembler: Optional[PromptAssembler] = None,
        compactor: Optional[ContextCompactor] = None, facing_chat_id: Optional[str] = None,
        actor_kind: ActorKind = "companion", helper_id: Optional[str] = None,
        enabled_toolsets: Sequence[str] = (), on_goal_control: Optional[GoalControlFn] = None,
        goal_controller: Any = None, close_browser_fn: Optional[Callable[[str], Awaitable[None]]] = None,
        context_window: int = 128_000, max_iterations: int = 25,
        clarify_timeout_s: float = CLARIFY_TIMEOUT_S,
        slash_confirm_timeout_s: float = SLASH_CONFIRM_TIMEOUT_S,
        home: Optional[Path] = None, model_name: str = "", provider_name: str = "", base_url: str = "",
    ) -> None:
        is_cron = actor_kind == "cron" or str(chat_id).startswith("cron:")
        self.chat_id = chat_id
        self.facing_chat_id = (facing_chat_id or "") if is_cron else (facing_chat_id or chat_id)
        self.db, self.sink, self.stream_llm, self.execute_tool = db, sink, stream_llm, execute_tool
        self.assembler = assembler or PromptAssembler()
        self.compactor = compactor or ContextCompactor()
        self.actor_kind: ActorKind = actor_kind
        self.helper_id, self.enabled_toolsets = helper_id, tuple(enabled_toolsets)
        self.on_goal_control = on_goal_control
        self.goal_controller = goal_controller
        self._close_browser_fn = close_browser_fn
        self.context_window, self.max_iterations = context_window, max_iterations
        self.clarify_timeout_s, self.slash_confirm_timeout_s = clarify_timeout_s, slash_confirm_timeout_s
        self.home, self.model_name, self.provider_name, self.base_url = home, model_name, provider_name, base_url

        self.active_turn_id: Optional[str] = None
        self._phase: Literal["idle", "llm", "tools", "clarify_wait"] = "idle"
        self._cancel_scope: Optional[CancelScope] = None
        self._runner_task: Optional[asyncio.Task[Any]] = None
        self._pending_clarify: Optional[PendingClarify] = None
        self._pending_confirm: Optional[PendingSlashConfirm] = None
        self._steer_queue: list[InboundMessage] = []
        self.inbox: deque[InboundMessage] = deque()
        self._active_tool_calls: list[dict[str, Any]] = []
        self._completed_tool_ids: set[str] = set()
        self._streamer: Optional[DraftSegmentStreamer] = None
        self._model_override: Optional[dict[str, Any]] = None
        self._renew_timer: Optional[asyncio.TimerHandle] = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    def _set_active_turn(self, turn_id: Optional[str]) -> None:
        self.active_turn_id = turn_id
        self.sink.set_active_turn(self.chat_id, turn_id)

    @property
    def _session_source(self) -> str:
        return "cron" if (self.actor_kind == "cron" or str(self.chat_id).startswith("cron:")) else "companion"

    def _active_session(self) -> dict[str, Any]:
        model = (self._model_override or {}).get("model") or self.model_name or None
        return self.db.get_or_create_active_session(self.chat_id, source=self._session_source, model=model)

    def set_model_override(self, override: Optional[dict[str, Any]]) -> None:
        self._model_override = dict(override) if override else None

    async def wait_idle(self, timeout: Optional[float] = None) -> None:
        if timeout is None:
            await self._idle_event.wait()
        else:
            await asyncio.wait_for(self._idle_event.wait(), timeout=timeout)

    async def _emit_answer(self, turn_id: str, text: str) -> None:
        await self.sink.emit(self.chat_id, {
            "event": "message", "text": text, "turn_id": turn_id,
            "turn_phase": "answer", "projection_id": new_history_id(),
        }, persist=True)

    async def _emit_turn_end(
        self, turn_id: Optional[str], *, outcome: str = "completed", persist: bool = True, **extra: Any,
    ) -> None:
        ev: dict[str, Any] = {"event": "turn_end", "turn_phase": "complete", **extra}
        if turn_id is not None:
            ev.update(turn_id=turn_id, outcome=outcome)
        await self.sink.emit(self.chat_id, ev, persist=persist)

    async def renew_session(self, *, silent: bool = True, model: Optional[str] = None) -> bool:
        """Silently rotate active `session_id` in `muse.db` (zero chat bubbles when `silent=True`)."""
        if self.active_turn_id is not None or self._phase != "idle":
            return False
        src = self._session_source
        if isinstance(prev := self.db.get_active_session(self.chat_id, source=src), dict) and prev.get("id"):
            self.assembler.evict_session(str(prev["id"]))
        chosen = model or (self._model_override or {}).get("model") or self.model_name or None
        self.db.renew_session(self.chat_id, source=src, model=chosen)
        if not silent:
            await self.sink.emit(self.chat_id, {
                "event": "message", "text": render_notice("gateway.reset.header_new"),
                "projection_id": new_history_id(),
            }, persist=True)
        return True

    def schedule_soul_renewal(self, *, delay_s: float = SOUL_RENEW_DELAY_S) -> None:
        """Schedule debounced 3s silent `renew_session` after `SOUL.md` edit."""
        if self._renew_timer is not None:
            self._renew_timer.cancel()
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            self._renew_timer = None
            loop.create_task(self.renew_session(silent=True))

        self._renew_timer = loop.call_later(delay_s, _fire)

    async def deliver_proactive(self, text: str, *, job_id: str, attach_to_session: bool = False) -> None:
        """P3 cron/proactive delivery: NEVER serialize `turn_id`, never close active draft."""
        self.sink.ensure_chat(self.chat_id)
        content = self.sink.transform_cards(self.chat_id, text, include_pick_card=False).strip()
        if not content:
            return
        body: dict[str, Any] = {"event": "message", "text": content, "projection_id": new_history_id()}
        if source := self.sink.source_for_job(job_id):
            body["source"] = source
            self.sink.schedule_push(
                self.sink.companion_name(), strip_card_fences(content)[:120],
                tag=str(source.get("label") or job_id),
            )
        await self.sink.emit(self.chat_id, body, persist=True)
        if attach_to_session:
            self.db.append_message(
                self._active_session()["id"], self.chat_id, "assistant", content,
                turn_id=None, extra={"job_id": job_id, "proactive": True},
            )

    async def handle_stop(self) -> str:
        """P0 `/stop`: cancel active turn & queued inbox turns, OR emit `turn_end` when idle (iOS `stopGoal()`)."""
        self._steer_queue.clear()
        queued = list(self.inbox)
        self.inbox.clear()
        if (pending := self._pending_clarify) is not None:
            self._pending_clarify = None
            if not pending.future.done():
                pending.future.cancel()
            with contextlib.suppress(Exception):
                self.db.update_message(
                    pending.tool_message_id, content=CANCELLED_TOOL_RESULT,
                    extra={"clarify_id": pending.clarify_id, "awaiting_user": False, "cancelled": True},
                )
            if self._cancel_scope is not None:
                self._cancel_scope.cancel("user_stop")
            await self._drain_runner(timeout=1.0)
            for item in queued:
                if item.turn_id:
                    await self._emit_turn_end(item.turn_id, outcome="cancelled", persist=True)
            await self._emit_turn_end(None, persist=False)
            return ""

        active_tid = self.active_turn_id
        if active_tid is None:
            if queued:
                for item in queued:
                    if item.turn_id:
                        await self._emit_turn_end(item.turn_id, outcome="cancelled", persist=True)
            else:
                await self._emit_turn_end(None, persist=False)
            return ""
        if self._cancel_scope is not None:
            self._cancel_scope.cancel("user_stop")
        await self._drain_runner(timeout=2.0, cancel=True)
        if self.active_turn_id == active_tid:
            self._set_active_turn(None)
            await self._emit_turn_end(active_tid, outcome="cancelled", persist=True)
        for item in queued:
            if item.turn_id and item.turn_id != active_tid:
                await self._emit_turn_end(item.turn_id, outcome="cancelled", persist=True)
        return active_tid

    async def _drain_runner(self, *, timeout: float = 1.0, cancel: bool = False) -> None:
        if (rt := self._runner_task) is not None and not rt.done():
            if cancel:
                rt.cancel()
            await asyncio.wait({rt}, timeout=timeout)
            if rt.done() and not rt.cancelled():
                _ = rt.exception()

    async def _cancel_pending_clarify(self, reason: str = "slash_command") -> None:
        if (pending := self._pending_clarify) is None:
            return
        self._pending_clarify = None
        if not pending.future.done():
            pending.future.cancel()
        with contextlib.suppress(Exception):
            self.db.update_message(
                pending.tool_message_id, content=CANCELLED_TOOL_RESULT,
                extra={"clarify_id": pending.clarify_id, "awaiting_user": False, "cancelled": True},
            )
        if self._cancel_scope is not None:
            self._cancel_scope.cancel(reason)
        await self._drain_runner(timeout=1.0)

    async def _invoke_goal_control(self, command: str) -> Any:
        if self.on_goal_control is not None:
            return await self.on_goal_control(self.chat_id, command)
        if self.goal_controller is not None:
            return await self.goal_controller.handle_control(self.chat_id, command)
        if self.db is not None:
            low = command.strip().lower()
            if low in ("/goal stop", "/goal clear"):
                self.db.clear_goal(self.chat_id)
            elif low in ("/goal pause", "/goal resume") and (goal := self.db.get_goal(self.chat_id)):
                st = "paused" if low == "/goal pause" else "active"
                self.db.upsert_goal(
                    self.chat_id, status=st, objective=str(goal.get("objective") or ""),
                    session_key=goal.get("session_key"), gate=goal.get("gate"),
                    iteration=int(goal.get("iteration") or 0),
                    max_iterations=int(goal.get("max_iterations") or 25),
                    state={**dict(goal.get("state") or {}), "status": st},
                )
        return None

    async def _handle_goal_control(self, command: str) -> None:
        low = command.strip().lower()
        await self._invoke_goal_control(command)
        await self.sink.push_goal_state(self.chat_id)
        if low == "/goal resume" and self.goal_controller is not None:
            st = self.goal_controller.load_state(self.chat_id)
            if st is not None and st.status == "active" and self.active_turn_id is None and self._phase == "idle":
                await self.submit_internal(self.goal_controller._build_continuation_prompt(st))

    async def _handle_typed_goal_command(self, stripped: str, lowered: str, resolved_tid: str) -> Optional[str]:
        if lowered.startswith("/goal ") and not lowered.startswith(("/goal status", "/goal gate")):
            await self._cancel_pending_clarify("goal_start")
        ctrl = self.goal_controller
        if ctrl is None:
            from .goal_loop import GoalLoopController
            ctrl = GoalLoopController(db=self.db, adapter=self.sink)
        reply, kick = await ctrl.execute_typed_command(self.chat_id, stripped, control_fn=(lambda _c, cmd: self._invoke_goal_control(cmd)))
        if reply is not None:
            await self._emit_answer(resolved_tid, reply)
            await self._emit_turn_end(resolved_tid)
        await self.sink.push_goal_state(self.chat_id)
        return None if reply is not None else kick

    async def submit(
        self, text: str, turn_id: Optional[str] = None, *,
        media_urls: Optional[list[dict[str, Any]]] = None,
        files: Optional[list[tuple[str, str]]] = None, internal: bool = False,
        job_id: Optional[str] = None, attach_to_session: bool = False, emit_user_message: bool = True,
    ) -> str:
        """Route inbound messages strictly across P0 -> P3 -> P1 -> P2 priorities."""
        stripped, lowered = (text or "").strip(), (text or "").strip().lower()
        if stripped == STOP_COMMAND:
            return await self.handle_stop()
        if lowered in GOAL_CONTROLS:
            await self._handle_goal_control(lowered)
            return ""
        if turn_id and turn_id.startswith(SYSTEM_TURN_PREFIX):
            return ""
        if job_id:
            await self.deliver_proactive(text, job_id=job_id, attach_to_session=attach_to_session)
            return ""

        self.sink.ensure_chat(self.chat_id)
        resolved_tid, now = turn_id or uuid.uuid4().hex, time.monotonic()
        if self._pending_confirm and now - self._pending_confirm.created_at > self._pending_confirm.timeout_s:
            self._pending_confirm = None
        if self._pending_clarify and now - self._pending_clarify.created_at > self._pending_clarify.timeout_s:
            self._pending_clarify = None

        # Persist & broadcast `user_message` FIRST so iOS `canonicalTurns` clears `outbox` (clarify Invariant #3).
        if not internal and emit_user_message:
            user_ev: dict[str, Any] = {
                "event": "user_message", "text": text, "starts_turn": True,
                "turn_id": resolved_tid, "turn_phase": "user",
            }
            if media_urls:
                user_ev["media_urls"] = list(media_urls)
            await self.sink.emit(self.chat_id, user_ev, persist=True)

        if self._pending_confirm and lowered in (APPROVE_COMMAND, CANCEL_COMMAND):
            await self._resolve_slash_confirm(lowered == APPROVE_COMMAND, resolved_tid)
            return resolved_tid

        if not internal and self.active_turn_id is None and self._phase in ("idle", "clarify_wait"):
            slash_cmd = lowered.lstrip("/").split(" ", 1)[0] if lowered.startswith("/") else ""
            if slash_cmd in CONFIRMABLE_COMMANDS:
                await self._begin_slash_confirm(slash_cmd, resolved_tid)
                return resolved_tid
            if lowered == COMPACT_COMMAND:
                await self._handle_manual_compact(resolved_tid)
                return resolved_tid

        if not internal and (lowered in ("/goal", "/subgoal") or lowered.startswith(("/goal ", "/subgoal "))):
            if (kick_prompt := await self._handle_typed_goal_command(stripped, lowered, resolved_tid)) is None:
                return resolved_tid
            text = kick_prompt

        if self._pending_clarify and not internal:
            entry, self._pending_clarify = self._pending_clarify, None
            if not entry.future.done():
                self._set_active_turn(resolved_tid)
                entry.future.set_result((text, resolved_tid, list(files or [])))
                return resolved_tid

        msg = InboundMessage(text=text, turn_id=resolved_tid, media_urls=list(media_urls or []), files=list(files or []), internal=internal)
        if self.active_turn_id is not None or self._phase != "idle":
            if not internal and self._phase == "tools":
                self._steer_queue.append(msg)
                ack = render_notice("gateway.busy.steered_head") + render_notice("gateway.busy.steered_tail")
                await self.sink.post_notice(self.chat_id, ack)
            else:
                self.inbox.append(msg)
                if not internal:
                    ack = render_notice("gateway.busy.queued_head") + render_notice("gateway.busy.queued_tail")
                    await self.sink.post_notice(self.chat_id, ack)
            return resolved_tid

        self._launch_turn(msg)
        return resolved_tid

    async def submit_internal(self, text: str) -> str:
        return await self.submit(text, uuid.uuid4().hex, internal=True, emit_user_message=False)

    async def regenerate(self, turn_id: str) -> str:
        """重新生成: rollback `turn_id` in `ChatStore` & `muse.db` and re-run reusing `turn_id`."""
        if self.active_turn_id is not None or self._phase != "idle":
            raise ValueError("还在回复，等它说完再重新生成")
        events = self.sink.store_events(self.chat_id)
        idx = next(
            (i for i in range(len(events) - 1, -1, -1)
             if events[i].get("event") == "user_message" and events[i].get("starts_turn")),
            None,
        )
        if idx is None or events[idx].get("turn_id") != turn_id or any(
            e.get("turn_id") not in (None, turn_id) for e in events[idx + 1 :]
        ):
            raise ValueError("只能重新生成最后一条回复")
        if events[idx].get("media_urls"):
            raise ValueError("带图片或文件的消息暂时不能重新生成")
        orig_text = str(events[idx].get("text") or "")
        self.sink.remove_store_events(
            self.chat_id, lambda e: e.get("turn_id") == turn_id and e.get("event") != "user_message",
        )
        session_id = self._active_session()["id"]
        self.db.rollback_turn_for_retry(session_id, turn_id)
        if not orig_text:
            active_rows = self.db.get_messages(session_id, active_only=True)
            orig_text = next((str(r.get("content") or "") for r in reversed(active_rows) if r.get("role") in ("user", "tool") and r.get("content")), "")
        self._launch_turn(InboundMessage(text=orig_text, turn_id=turn_id, is_retry=True))
        return turn_id

    async def _begin_slash_confirm(self, command: str, turn_id: str) -> None:
        target_tid: Optional[str] = None
        if command == "undo":
            for ev in reversed(self.sink.store_events(self.chat_id)):
                if ev.get("event") == "user_message" and ev.get("starts_turn") and ev.get("turn_id") != turn_id:
                    target_tid = str(ev.get("turn_id") or "") or None
                    break
        self._pending_confirm = PendingSlashConfirm(
            uuid.uuid4().hex[:12], self.chat_id, command, turn_id,
            target_tid, time.monotonic(), self.slash_confirm_timeout_s,
        )
        await self._emit_answer(turn_id, format_slash_confirm_card(command) or f"确认执行 /{command} 吗？")
        await self._emit_turn_end(turn_id)

    def _undo_db_exchange(self, session_id: str, target_turn_id: Optional[str]) -> None:
        rows = self.db.get_messages(session_id, active_only=True)
        if target_turn_id and (matching := [r for r in rows if r.get("turn_id") == target_turn_id]):
            first, first_id = matching[0], int(matching[0]["id"])
            if first.get("role") == "tool" and first.get("tool_name") == "clarify":
                if tail_ids := [int(r["id"]) for r in rows if int(r["id"]) > first_id]:
                    self.db.deactivate_messages(tail_ids)
                prev_tid = next((str(r.get("turn_id") or "") for r in reversed(rows) if int(r["id"]) < first_id and r.get("role") == "assistant"), "") or None
                prev_extra = first.get("extra") if isinstance(first.get("extra"), dict) else {}
                self.db.update_message(first_id, content=CANCELLED_TOOL_RESULT, turn_id=prev_tid, extra={**prev_extra, "awaiting_user": False, "undone": True})
                return
            if drop_ids := [int(r["id"]) for r in rows if int(r["id"]) >= first_id]:
                self.db.deactivate_messages(drop_ids)
                return
        try:
            self.db.undo_last_exchange(session_id, turn_id=target_turn_id)
        except TypeError:
            self.db.undo_last_exchange(session_id)

    async def _resolve_slash_confirm(self, approved: bool, turn_id: str) -> None:
        if (pending := self._pending_confirm) is None:
            return
        self._pending_confirm = None
        if not approved:
            await self._emit_answer(turn_id, "好，取消了。")
            await self._emit_turn_end(turn_id)
        elif pending.command in ("new", "reset"):
            await self._cancel_pending_clarify(pending.command)
            await self.renew_session(silent=True)
            await self._emit_answer(turn_id, render_notice("gateway.reset.header_new"))
            await self._emit_turn_end(turn_id)
        elif pending.command == "undo":
            await self._cancel_pending_clarify("undo")
            self._undo_db_exchange(self._active_session()["id"], pending.target_turn_id)
            drop_tids = {t for t in (pending.target_turn_id, pending.confirm_turn_id, turn_id) if t}
            self.sink.remove_store_events(self.chat_id, lambda e: e.get("turn_id") in drop_tids)
            await self.sink.post_notice(self.chat_id, "已撤回上一轮对话。")
            await self._emit_turn_end(turn_id, persist=False)

    async def _handle_manual_compact(self, turn_id: str) -> None:
        prev_phase = self._phase
        self._set_active_turn(turn_id)
        try:
            session = self._active_session()
            res = await self.compactor.tier2_compact(
                db_rows_to_llm_messages(self.db.get_messages(session["id"], active_only=True)),
                db=self.db, session_id=session["id"], chat_id=self.chat_id, turn_id=turn_id,
                context_window=self.context_window, existing_checkpoint=str(session.get("summary_checkpoint") or ""),
                force=True, emit_event=lambda ev: self.sink.emit(self.chat_id, ev, persist=True),
            )
            msg = "已整理前面的对话内容，关键信息都保留好了。" if res.compacted else "目前对话还很精简，暂时不需要整理。"
            await self._emit_answer(turn_id, msg)
        finally:
            self._phase = prev_phase
            self._set_active_turn(None)
            await self._emit_turn_end(turn_id)

    def _launch_turn(self, msg: InboundMessage) -> None:
        self._idle_event.clear()
        self._set_active_turn(msg.turn_id)
        self._phase = "llm"
        self._runner_task = asyncio.get_running_loop().create_task(self._run_turn_wrapper(msg))

    async def _run_turn_wrapper(self, msg: InboundMessage) -> None:
        try:
            await self._execute_turn(msg)
        finally:
            self._runner_task = self._cancel_scope = self._streamer = None
            self._active_tool_calls, self._completed_tool_ids, self._phase = [], set(), "idle"
            if self.inbox:
                self._launch_turn(self.inbox.popleft())
            else:
                self._idle_event.set()

    async def _finalize_turn_hooks(self, *, succeeded: bool) -> None:
        on_done = getattr(self.sink, "on_turn_complete", None)
        if callable(on_done):
            with contextlib.suppress(Exception):
                await on_done(self.chat_id, succeeded=succeeded)
        else:
            if succeeded:
                with contextlib.suppress(Exception):
                    await self.sink.push_goal_state(self.chat_id)
            if self._close_browser_fn is not None:
                with contextlib.suppress(Exception):
                    await self._close_browser_fn(self.chat_id)

    async def _execute_turn(self, msg: InboundMessage) -> None:
        session = self._active_session()
        session_id, current_turn_id = str(session["id"]), msg.turn_id
        self.db.heal_orphan_tool_calls(session_id)
        user_msg_id: Optional[int] = None
        if not msg.is_retry:
            extra: Optional[dict[str, Any]] = None
            if msg.files:
                parts = await run_in_turn_thread(_build_content_parts_sync, msg.text, msg.files)
                extra = {"files": [list(f) for f in msg.files], **({"content_parts": parts} if parts else {})}
            user_msg_id = self.db.append_message(session_id, self.chat_id, "user", msg.text, turn_id=current_turn_id, extra=extra)

        scope = self._cancel_scope = CancelScope()
        if cur_task := asyncio.current_task():
            scope.register_task(cur_task)
        ctx = TurnExecutionContext(
            self.chat_id, self.facing_chat_id, session_id, current_turn_id,
            self.actor_kind, self.helper_id, self.enabled_toolsets, scope,
        )
        streamer = self._streamer = DraftSegmentStreamer(self.sink, self.chat_id, current_turn_id)
        turn_closed_by_clarify = False
        last_final_text = ""

        try:
            with turn_context_scope(ctx):
                prior_history = db_rows_to_llm_messages(self.db.get_messages(session_id, active_only=True))
                pre_llm_context = await self._collect_pre_llm_context(
                    msg.text, session_id, prior_history[:-1] if prior_history else [], internal=msg.internal,
                )
                for _ in range(self.max_iterations):
                    scope.check()
                    self._phase = "llm"
                    session = self.db.get_session(session_id) or session
                    llm_msgs = db_rows_to_llm_messages(self.db.get_messages(session_id, active_only=True))
                    sys_prompt = self.assembler.assemble(
                        session_id=session_id, chat_id=self.chat_id, db=self.db,
                        home=self.home, summary_checkpoint=session.get("summary_checkpoint"),
                    )
                    llm_msgs = self.compactor.tier1_prune(llm_msgs, system_prompt=sys_prompt).messages
                    t2 = await self.compactor.tier2_compact(
                        llm_msgs, db=self.db, session_id=session_id, chat_id=self.chat_id,
                        turn_id=current_turn_id, context_window=self.context_window,
                        system_prompt=sys_prompt, existing_checkpoint=str(session.get("summary_checkpoint") or ""),
                        emit_event=lambda ev: self.sink.emit(self.chat_id, ev, persist=True),
                    )
                    if t2.compacted:
                        llm_msgs = t2.messages
                        sys_prompt = self.assembler.assemble(
                            session_id=session_id, chat_id=self.chat_id, db=self.db,
                            home=self.home, summary_checkpoint=t2.summary_checkpoint,
                        )
                    wire_msgs = self._inject_pre_llm_context(llm_msgs, pre_llm_context, target_msg_id=user_msg_id)

                    raw_text, tool_calls, msg_extra, usage = await self._invoke_llm_with_overflow_retry(
                        wire_msgs, sys_prompt, session_id, current_turn_id, streamer, scope,
                    )
                    self._record_usage(session_id, usage)
                    self.db.append_message(
                        session_id, self.chat_id, "assistant", raw_text,
                        turn_id=current_turn_id, tool_calls=tool_calls or None, extra=msg_extra or None,
                    )

                    if not tool_calls:
                        final_text, media = await streamer.close_segment(final_raw=raw_text, for_tools=False)
                        await streamer.flush_media(media, reply_context=final_text)
                        last_final_text = final_text or raw_text
                        break

                    pre_text, media = await streamer.close_segment(final_raw=raw_text, for_tools=True)
                    await streamer.flush_media(media, reply_context=pre_text)
                    self._phase, self._active_tool_calls, self._completed_tool_ids = "tools", list(tool_calls), set()
                    clarify_files_b: list[tuple[str, str]] = []

                    for tc in tool_calls:
                        scope.check()
                        tc_id = str(tc.get("id") or uuid.uuid4().hex[:12])
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        tc_name = str(fn.get("name") or tc.get("name") or "")
                        if tc_name == "clarify":
                            turn_closed_by_clarify = True
                            answered = await self._handle_clarify_tool_call(
                                tc=tc, tc_id=tc_id, session_id=session_id, turn_id_a=current_turn_id,
                            )
                            if answered is None:
                                await self._finalize_turn_hooks(succeeded=False)
                                return
                            _, current_turn_id, files_b = answered
                            if files_b:
                                clarify_files_b.extend(files_b)
                            turn_closed_by_clarify, ctx.turn_id = False, current_turn_id
                            streamer = self._streamer = DraftSegmentStreamer(self.sink, self.chat_id, current_turn_id)
                            continue
                        res = (
                            await self.execute_tool(tc, ctx)
                            if self.execute_tool is not None
                            else json.dumps({"error": "No tool executor configured"})
                        )
                        content = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                        self.db.append_message(
                            session_id, self.chat_id, "tool", content,
                            turn_id=current_turn_id, tool_call_id=tc_id, tool_name=tc_name,
                        )
                        self._completed_tool_ids.add(tc_id)

                    self._active_tool_calls = []
                    if clarify_files_b:
                        c_parts = await run_in_turn_thread(
                            _build_content_parts_sync, "[User attached files with clarify response]", clarify_files_b,
                        )
                        self.db.append_message(
                            session_id, self.chat_id, "user", "[User attached files with clarify response]",
                            turn_id=current_turn_id,
                            extra={"files": [list(f) for f in clarify_files_b], "clarify_attachment": True, **({"content_parts": c_parts} if c_parts else {})},
                        )
                    if self._steer_queue:
                        steered, self._steer_queue = list(self._steer_queue), []
                        for sm in steered:
                            s_extra: dict[str, Any] = {"steered_turn_id": sm.turn_id}
                            if sm.files:
                                s_parts = await run_in_turn_thread(_build_content_parts_sync, sm.text, sm.files)
                                s_extra["files"] = [list(f) for f in sm.files]
                                if s_parts:
                                    s_extra["content_parts"] = s_parts
                            self.db.append_message(
                                session_id, self.chat_id, "user", sm.text,
                                turn_id=current_turn_id, extra=s_extra,
                            )
                else:
                    await self.sink.post_notice(
                        self.chat_id, render_notice("gateway.errors.stopped_before_finishing", reason="")
                    )
        except asyncio.CancelledError:
            self._write_cancelled_tool_results(session_id, current_turn_id)
            if not turn_closed_by_clarify:
                with contextlib.suppress(Exception):
                    await streamer.close_segment(for_tools=False)
                self._set_active_turn(None)
                await self._emit_turn_end(current_turn_id, outcome="cancelled")
            await self._finalize_turn_hooks(succeeded=False)
            return
        except Exception as exc:
            error_kind, attempts, _ = classify_turn_failure(exc)
            with contextlib.suppress(Exception):
                await streamer.close_segment(for_tools=False)
            if not streamer.emitted_any_text:
                await self.sink.post_notice(self.chat_id, failure_notice_zh(error_kind))
            self._set_active_turn(None)
            await self._emit_turn_end(
                current_turn_id, outcome="failed", failure_kind="model",
                failure_error_kind=error_kind, failure_attempts=attempts,
                failure_message=scrub_url_secrets(str(exc))[:300],
            )
            await self._finalize_turn_hooks(succeeded=False)
            return

        self._set_active_turn(None)
        await self._emit_turn_end(current_turn_id, outcome="completed")
        await self._finalize_turn_hooks(succeeded=True)
        await self._maybe_continue_goal(last_final_text)

    async def _maybe_continue_goal(self, last_response: str) -> None:
        ctrl = self.goal_controller
        if ctrl is None or self.actor_kind != "companion" or getattr(self.db, "_closed", False):
            return
        try:
            st = ctrl.load_state(self.chat_id)
        except Exception:
            return
        if st is None or st.status != "active":
            return
        decision = await ctrl.evaluate_after_turn(self.chat_id, last_response)
        if not decision.should_continue or not decision.continuation_prompt or getattr(self.db, "_closed", False):
            return
        if decision.verdict == "wait" and decision.wait_directive:
            if not await ctrl.wait_for_barrier(self.chat_id, decision.wait_directive):
                return
        if getattr(self.db, "_closed", False):
            return
        st_after = ctrl.load_state(self.chat_id)
        if st_after is not None and st_after.status == "active" and not any(m.internal for m in self.inbox):
            self.inbox.append(InboundMessage(
                text=decision.continuation_prompt, turn_id=f"goal-{uuid.uuid4().hex[:10]}", internal=True,
            ))

    async def _collect_pre_llm_context(
        self,
        user_text: str,
        session_id: str,
        history: list[dict[str, Any]],
        *,
        internal: bool = False,
    ) -> str:
        """Build per-turn Companion helper identity/routing & presentation router hints without touching system prompt."""
        parts: list[str] = []
        with contextlib.suppress(Exception):
            from companion.modules import helpers as comp_helpers

            hctx = await run_in_turn_thread(comp_helpers.context_for, self.chat_id, user_text)
            if isinstance(hctx, str) and hctx.strip():
                parts.append(hctx.strip())
        if not internal and self.actor_kind != "cron":
            with contextlib.suppress(Exception):
                from companion.modules import router as comp_router

                res = await run_in_turn_thread(
                    comp_router.on_pre_llm_call,
                    platform="companion",
                    session_id=session_id,
                    user_message=user_text,
                    conversation_history=history,
                )
                rctx = res.get("context") if isinstance(res, dict) else None
                if isinstance(rctx, str) and rctx.strip():
                    parts.append(rctx.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _inject_pre_llm_context(
        messages: list[dict[str, Any]], extra: str, *, target_msg_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if not extra or not messages:
            return messages
        out = list(messages)
        target_idx: Optional[int] = None
        if target_msg_id is not None:
            for idx in range(len(out) - 1, -1, -1):
                if out[idx].get("role") == "user" and out[idx].get("id") == target_msg_id:
                    target_idx = idx
                    break
        if target_idx is None:
            for idx in range(len(out) - 1, -1, -1):
                if out[idx].get("role") == "user":
                    target_idx = idx
                    break
        if target_idx is not None:
            copy_msg = dict(out[target_idx])
            content = copy_msg.get("content")
            if isinstance(content, list):
                copy_msg["content"] = [*content, {"type": "text", "text": f"\n\n{extra}"}]
            else:
                base = str(content or "")
                copy_msg["content"] = f"{base}\n\n{extra}" if base else extra
            out[target_idx] = copy_msg
        return out

    async def _invoke_llm_with_overflow_retry(
        self, llm_msgs: list[dict[str, Any]], system_prompt: str, session_id: str,
        turn_id: str, streamer: DraftSegmentStreamer, scope: CancelScope,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], Any]:
        retried, active = False, llm_msgs
        while True:
            try:
                wire = [{"role": "system", "content": system_prompt}, *active] if system_prompt else active
                return await self._consume_llm_stream(wire, turn_id, streamer, scope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if classify_turn_failure(exc)[2] and not retried:
                    res = await self.compactor.tier3_emergency_drop(
                        active, db=self.db, session_id=session_id, chat_id=self.chat_id, turn_id=turn_id,
                        emit_event=lambda ev: self.sink.emit(self.chat_id, ev, persist=True),
                    )
                    if res.compacted:
                        retried, active = True, res.messages
                        continue
                raise

    async def _consume_llm_stream(
        self, messages: list[dict[str, Any]], turn_id: str, streamer: DraftSegmentStreamer, scope: CancelScope,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], Any]:
        raw_parts, reasoning_parts, tool_calls, thinking_blocks, encrypted_items = [], [], [], [], []
        msg_extra_content, usage = None, None
        async for ev in self.stream_llm(messages=messages, model_override=self._model_override, cancel_scope=scope):
            scope.check()
            etype = type(ev).__name__
            if etype == "RetryStatusEvent":
                await self.sink.emit(
                    self.chat_id, translate_retry_status(ev, chat_id=self.chat_id, turn_id=turn_id), persist=False,
                )
            elif etype == "TextDelta" and (chunk := str(getattr(ev, "text", "") or "")):
                raw_parts.append(chunk)
                await streamer.push_delta(chunk)
            elif etype == "ReasoningDelta" and (chunk := str(getattr(ev, "text", "") or "")):
                reasoning_parts.append(chunk)
                await streamer.push_reasoning(chunk)
            elif etype == "ToolCallComplete":
                fn: dict[str, Any] = {
                    "name": str(getattr(ev, "name", "")), "arguments": str(getattr(ev, "arguments", "") or ""),
                }
                if getattr(ev, "thought_signature", None):
                    fn["thought_signature"] = ev.thought_signature
                tc: dict[str, Any] = {
                    "id": str(getattr(ev, "id", "") or uuid.uuid4().hex[:12]), "type": "function", "function": fn,
                }
                if getattr(ev, "extra_content", None):
                    tc["extra_content"] = ev.extra_content
                tool_calls.append(tc)
            elif etype == "ThinkingBlockComplete" and callable(getattr(ev, "to_block_dict", None)):
                thinking_blocks.append(ev.to_block_dict())
            elif etype == "EncryptedReasoningItem" and callable(getattr(ev, "to_storage_dict", None)):
                encrypted_items.append(ev.to_storage_dict())
            elif etype == "MessageCarrierEvent" and getattr(ev, "extra_content", None):
                msg_extra_content = ev.extra_content
            elif etype == "UsageInfo":
                usage = ev
            elif etype == "StreamDone" and getattr(ev, "usage", None) is not None:
                usage = ev.usage

        raw_extra = (
            ("reasoning_content", "".join(reasoning_parts) or None),
            ("extra_content", msg_extra_content),
            ("reasoning_details", thinking_blocks or None),
            ("codex_reasoning_items", encrypted_items or None),
        )
        return "".join(raw_parts), tool_calls, {k: v for k, v in raw_extra if v is not None}, usage

    async def _handle_clarify_tool_call(
        self, *, tc: dict[str, Any], tc_id: str, session_id: str, turn_id_a: str,
    ) -> Optional[tuple[str, str, list[tuple[str, str]]]]:
        """Enforce all 4 `clarify` invariants across `turn_A` -> await -> `turn_B`."""
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        raw_args = fn.get("arguments") or tc.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (ValueError, TypeError):
            args = {}
        question = str(args.get("question") or "").strip() or "请选择："
        choices = args.get("choices") if isinstance(args.get("choices"), list) else None
        multi, clarify_id = bool(args.get("multi_select", False)), uuid.uuid4().hex[:12]

        # Invariant 1: choices card stripping (Recommended)/(推荐), emit message(turn_A) + turn_end(turn_A)
        self.sink.note_turn_output(self.chat_id, turn_id_a)
        await self._emit_answer(turn_id_a, format_clarify_card(question, choices, multi_select=multi))
        await self._emit_turn_end(turn_id_a)

        # Invariant 2: write placeholder tool row to muse.db BEFORE awaiting AND set active_turn_id = None
        placeholder_id = self.db.append_message(
            session_id, self.chat_id, "tool", CLARIFY_PLACEHOLDER, turn_id=turn_id_a,
            tool_call_id=tc_id, tool_name="clarify", extra={"clarify_id": clarify_id, "awaiting_user": True},
        )
        self._completed_tool_ids.add(tc_id)
        self._set_active_turn(None)
        self._phase = "clarify_wait"
        fut: asyncio.Future[tuple[str, str, list[tuple[str, str]]]] = asyncio.get_running_loop().create_future()
        self._pending_clarify = PendingClarify(
            clarify_id, self.chat_id, turn_id_a, tc_id, placeholder_id,
            question, [str(c) for c in (choices or [])], multi, fut, time.monotonic(), self.clarify_timeout_s,
        )
        try:
            answer_text, turn_id_b, files_b = await asyncio.wait_for(fut, timeout=self.clarify_timeout_s)
        except asyncio.TimeoutError:
            self._pending_clarify = None
            self.db.update_message(
                placeholder_id, content="[Clarify timed out without user response]",
                extra={"clarify_id": clarify_id, "awaiting_user": False, "timed_out": True},
            )
            return None

        # Invariant 3 & 4: update placeholder row with user's answer & `turn_id = turn_B`
        c_extra: dict[str, Any] = {"clarify_id": clarify_id, "awaiting_user": False}
        if files_b:
            c_extra["files"] = [list(f) for f in files_b]
        self.db.update_message(
            placeholder_id, content=answer_text, turn_id=turn_id_b, extra=c_extra,
        )
        self._set_active_turn(turn_id_b)
        self._phase = "llm"
        return answer_text, turn_id_b, files_b

    def _write_cancelled_tool_results(self, session_id: str, turn_id: str) -> None:
        for tc in self._active_tool_calls:
            tc_id = str(tc.get("id") or "")
            if not tc_id or tc_id in self._completed_tool_ids:
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            with contextlib.suppress(Exception):
                self.db.append_message(
                    session_id, self.chat_id, "tool", CANCELLED_TOOL_RESULT, turn_id=turn_id,
                    tool_call_id=tc_id, tool_name=str(fn.get("name") or tc.get("name") or ""),
                    extra={"cancelled": True},
                )
            self._completed_tool_ids.add(tc_id)

    def _record_usage(self, session_id: str, usage: Any) -> None:
        if usage is None or not callable(getattr(self.db, "record_usage", None)):
            return
        in_tok, out_tok = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
        if not (in_tok or out_tok):
            return
        ov = self._model_override or {}
        with contextlib.suppress(Exception):
            self.db.record_usage(
                session_id, str(ov.get("model") or self.model_name or "unknown"),
                provider=str(ov.get("provider") or self.provider_name or ""),
                base_url=str(ov.get("base_url") or self.base_url or ""),
                source=self._session_source, chat_id=self.chat_id,
                input_tokens=in_tok, output_tokens=out_tok,
                cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
                reasoning_tokens=int(getattr(usage, "reasoning_tokens", 0) or 0),
            )


__all__ = [
    "APPROVE_COMMAND", "CANCELLED_TOOL_RESULT", "CANCEL_COMMAND", "CLARIFY_PLACEHOLDER", "CLARIFY_TIMEOUT_S",
    "COMPACT_COMMAND", "ChatSink", "ChatTurnActor", "DraftSegmentStreamer", "GOAL_CONTROLS", "InboundMessage",
    "PendingClarify", "PendingSlashConfirm", "RETRY_COMMAND", "SLASH_CONFIRM_TIMEOUT_S", "SOUL_RENEW_DELAY_S",
    "STOP_COMMAND", "SYSTEM_TURN_PREFIX", "classify_turn_failure", "db_rows_to_llm_messages",
    "extract_media_lines", "failure_notice_zh", "format_clarify_card", "format_slash_confirm_card",
    "new_history_id", "translate_retry_status",
]
