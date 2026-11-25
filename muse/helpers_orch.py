"""Deadlock-free native ``async`` helper & browser-desk orchestrator (`helper_ask` & `browser_request`)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .interceptors import TurnExecutionContext, resolve_turn_context, turn_context

logger = logging.getLogger(__name__)

_MAX_CHAIN = 3
_ASK_TIMEOUT_S = 15 * 60.0
_BACKGROUND_TIMEOUT_S = 60 * 60.0


def _comp_mod(override: Any, name: str) -> Any:
    return override if override is not None else importlib.import_module(f"companion.modules.{name}")


def _json_result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def resolve_helper_model_override(helper: dict[str, Any], *, models_mod: Any = None) -> Optional[dict[str, Any]]:
    """Resolve per-session model override dict for a helper's configured preset (`None` -> main model)."""
    preset_name = str(helper.get("model") or "").strip()
    if not preset_name:
        return None
    models = _comp_mod(models_mod, "models")
    data = models.read_models() or {}
    preset = (data.get("presets") or {}).get(preset_name)
    if not isinstance(preset, dict) or not preset.get("model"):
        return None
    provider = models.runtime_provider(preset_name, preset)
    is_oauth = bool(preset.get("auth")) or str(provider).strip().lower() in ("openai-codex", "xai-oauth")
    runtime: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        from muse.llm.providers import resolve_runtime_provider  # type: ignore[import-not-found]

        runtime = resolve_runtime_provider(
            requested=provider, target_model=preset["model"], allow_oauth_refresh=False,
        )
    rmodels = ((data.get("router") or {}).get("models") or {}) if isinstance(data.get("router"), dict) else {}
    info = rmodels.get(preset_name) if isinstance(rmodels, dict) else None
    vision = bool(info.get("vision")) if isinstance(info, dict) and "vision" in info else True
    out: dict[str, Any] = {
        "preset": preset_name,
        "model": preset["model"],
        "provider": provider,
        "base_url": runtime.get("base_url") or preset.get("base_url"),
        "api_mode": runtime.get("api_mode") or preset.get("api_mode"),
        "vision": vision,
        "reasoning_effort": preset.get("reasoning_effort") if "reasoning_effort" in preset else runtime.get("reasoning_effort"),
        "request_overrides": {},
        "capabilities": {"vision": vision},
    }
    if not is_oauth and runtime.get("api_key"):
        out["api_key"] = runtime["api_key"]
    return out


ChildTurnRunner = Callable[..., Awaitable[str]]


class HelperOrchestrator:
    """Native event-loop orchestrator for `helper_ask`, background `hand_off`, and `browser_request`."""

    MAX_CHAIN = _MAX_CHAIN

    def __init__(
        self, turn_runner: ChildTurnRunner, *, store: Any = None, helpers_mod: Any = None,
        egress_mod: Any = None, plans_mod: Any = None, push_mod: Any = None, widgets_mod: Any = None,
        models_mod: Any = None, main_chat_fn: Optional[Callable[[], Optional[str]]] = None,
        emit_chat_event_fn: Optional[Callable[[str, dict[str, Any], bool], Awaitable[None]]] = None,
        post_as_helper_fn: Optional[Callable[[str, dict[str, Any], str], Awaitable[None]]] = None,
        submit_internal_fn: Optional[Callable[[str, str], Awaitable[Any]]] = None,
        wait_chat_idle_fn: Optional[Callable[[str, float], Awaitable[None]]] = None,
        set_model_override_fn: Optional[Callable[[str, Optional[dict[str, Any]]], None]] = None,
        delete_desk_session_fn: Optional[Callable[[str], Awaitable[None]]] = None,
        ask_timeout_s: float = _ASK_TIMEOUT_S, background_timeout_s: float = _BACKGROUND_TIMEOUT_S,
    ) -> None:
        self._turn_runner = turn_runner
        self.store = store
        self._helpers_mod = helpers_mod
        self._egress_mod = egress_mod
        self._plans_mod = plans_mod
        self._push_mod = push_mod
        self._widgets_mod = widgets_mod
        self._models_mod = models_mod
        self._main_chat_fn = main_chat_fn
        self._emit_chat_event_fn = emit_chat_event_fn
        self._post_as_helper_fn = post_as_helper_fn
        self._submit_internal_fn = submit_internal_fn
        self._wait_chat_idle_fn = wait_chat_idle_fn
        self._set_model_override_fn = set_model_override_fn
        self._delete_desk_session_fn = delete_desk_session_fn
        self.ask_timeout_s = max(0.1, float(ask_timeout_s))
        self.background_timeout_s = max(0.1, float(background_timeout_s))

        self._graph_lock = threading.RLock()
        self._asking: dict[str, str] = {}
        self._asking_queue: dict[str, list[tuple[str, str]]] = {}
        self._helper_origins: dict[str, str] = {}
        self._active_waits: set[str] = set()
        self._desk_chats: set[str] = set()
        self._helper_locks: dict[str, asyncio.Lock] = {}
        self._browser_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    @property
    def helpers(self) -> Any:
        self._helpers_mod = _comp_mod(self._helpers_mod, "helpers")
        return self._helpers_mod

    @property
    def egress(self) -> Any:
        self._egress_mod = _comp_mod(self._egress_mod, "egress")
        return self._egress_mod

    def _main_chat_id(self) -> Optional[str]:
        if self._main_chat_fn is not None:
            return self._main_chat_fn()
        with contextlib.suppress(Exception):
            cid = _comp_mod(None, "companion_state").read_companion_state().get("chat_id")
            return str(cid) if cid else None
        return None

    def is_desk(self, chat_id: Optional[str]) -> bool:
        if not chat_id:
            return False
        if chat_id in self._desk_chats:
            return True
        if self.store is not None:
            with contextlib.suppress(Exception):
                return bool(self.store.meta(chat_id).get("desk"))
        return bool(self.helpers.is_desk(chat_id))

    def helper_origin(self, helper_chat: Optional[str]) -> Optional[str]:
        return self._helper_origins.get(helper_chat or "")

    def facing_chat(self, chat_id: Optional[str]) -> str:
        """Walk active delegation links back to the user-facing chat (`main`, side chat, or group)."""
        current, seen = str(chat_id or ""), set()
        with self._graph_lock:
            for _ in range(self.MAX_CHAIN + 4):
                origin = self._helper_origins.get(current) if (current and current not in seen and current in self._active_waits) else None
                if not origin:
                    break
                seen.add(current)
                current = origin
        return current

    def _waiters_locked(self, callee: str) -> list[str]:
        out = [origin for _, origin in self._asking_queue.get(callee, ())]
        direct = self._asking.get(callee)
        if direct and direct not in out:
            out.insert(0, direct)
        return out

    def _ancestors_and_depth_locked(self, chat_id: str) -> tuple[set[str], int]:
        ancestors, best_depth = {chat_id}, 1
        frontier: list[tuple[str, int, frozenset[str]]] = [(chat_id, 1, frozenset({chat_id}))]
        while frontier:
            node, depth, path = frontier.pop()
            best_depth = max(best_depth, depth)
            if depth < 12:
                for parent in self._waiters_locked(node):
                    ancestors.add(parent)
                    if parent not in path:
                        frontier.append((parent, depth + 1, path | {parent}))
        return ancestors, best_depth

    def _enqueue_waiter(self, origin: str, callee: str, helper_name: str) -> tuple[Optional[str], Optional[str]]:
        token = uuid.uuid4().hex[:12]
        with self._graph_lock:
            ancestors, depth = self._ancestors_and_depth_locked(origin)
            if callee in ancestors:
                return None, f"「{helper_name}」正在等你的结果，不能再交回给它"
            if depth >= self.MAX_CHAIN:
                return None, "已经转了好几手了，这件事你自己做完吧"
            queue = self._asking_queue.setdefault(callee, [])
            queue.append((token, origin))
            self._asking[callee] = queue[0][1]
        return token, None

    def _dequeue_waiter(self, callee: str, token: str) -> None:
        with self._graph_lock:
            queue = self._asking_queue.get(callee)
            if queue is not None:
                queue[:] = [item for item in queue if item[0] != token]
                if queue:
                    self._asking[callee] = queue[0][1]
                else:
                    self._asking_queue.pop(callee, None)
                    self._asking.pop(callee, None)
            else:
                self._asking.pop(callee, None)

    async def _emit_provenance_message(self, target_chat: str, origin: Optional[str], label: str, task: str, turn_id: str) -> None:
        event = {
            "event": "user_message", "text": task, "starts_turn": True, "turn_id": turn_id, "turn_phase": "user",
            "provenance": {"session_message": {"message_id": turn_id, "session": {"id": origin or "", "name": label}}},
        }
        if self._emit_chat_event_fn is not None:
            await self._emit_chat_event_fn(target_chat, event, True)
        elif self.store is not None:
            self.store.ensure(target_chat)
            self.store.append(target_chat, {**event, "chat_id": target_chat})

    async def _post_as_helper(self, chat_id: str, helper: dict[str, Any], text: str) -> None:
        if self._post_as_helper_fn is not None:
            await self._post_as_helper_fn(chat_id, helper, text)
            return
        event = {
            "event": "message", "text": text,
            "source": {"kind": "helper", "id": helper["id"], "label": f"{helper.get('emoji') or '🤖'} {helper['name']}"},
        }
        if self._emit_chat_event_fn is not None:
            await self._emit_chat_event_fn(chat_id, event, True)
        elif self.store is not None:
            self.store.append(chat_id, {**event, "chat_id": chat_id})

    def _origin_label(self, origin: Optional[str]) -> str:
        if not origin:
            return "定时任务"
        if origin == self._main_chat_id():
            return "主要聊天"
        if self.store is not None:
            with contextlib.suppress(Exception):
                if title := self.store.title(origin):
                    return str(title)
        return "旁聊"

    async def ask_helper(self, origin: Optional[str], helper_chat: str, task: str, timeout: Optional[float] = None) -> str:
        """Run one task on `helper_chat` (or an ephemeral browser `desk`) serialized per target chat."""
        helper_row = self.helpers.helper_for_chat(helper_chat)
        desk_mode = self.is_desk(helper_chat)
        if helper_row is None and not desk_mode:
            raise RuntimeError("这个帮手已经删了")
        wait_s = self.ask_timeout_s if timeout is None else max(0.05, float(timeout))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        lock = self._helper_locks.setdefault(helper_chat, asyncio.Lock())
        await asyncio.wait_for(lock.acquire(), timeout=wait_s)
        try:
            return await self._run_handed_turn(origin, helper_chat, helper_row, desk_mode, task, max(0.05, deadline - loop.time()))
        finally:
            lock.release()

    async def _run_handed_turn(
        self, origin: Optional[str], target_chat: str, helper_row: Optional[dict[str, Any]],
        desk_mode: bool, task: str, timeout: float,
    ) -> str:
        turn_id = uuid.uuid4().hex
        with self._graph_lock:
            if origin:
                self._helper_origins[target_chat] = origin
            self._active_waits.add(target_chat)
        facing = self.facing_chat(target_chat)
        if origin:
            self.egress.propagate_chat(origin, target_chat)
        try:
            label = self._origin_label(origin)
            await self._emit_provenance_message(target_chat, origin, label, task, turn_id)
            override = None if (desk_mode or helper_row is None) else resolve_helper_model_override(helper_row, models_mod=self._models_mod)
            if self._set_model_override_fn is not None and not desk_mode:
                with contextlib.suppress(Exception):
                    self._set_model_override_fn(target_chat, override)
            child_ctx = TurnExecutionContext(
                chat_id=target_chat, facing_chat_id=facing or target_chat, session_id=f"companion:{target_chat}",
                turn_id=turn_id, actor_kind="desk" if desk_mode else "helper",
                helper_id=(str(helper_row["id"]) if helper_row and helper_row.get("id") else None),
            )
            with turn_context(child_ctx):
                answer = await asyncio.wait_for(
                    self._turn_runner(chat_id=target_chat, prompt=f"【{label}交给你的事】{task}", turn_id=turn_id, ctx=child_ctx, model_override=override),
                    timeout=timeout,
                )
            return (answer or "").strip() or "（它做完了，但没有写结果）"
        finally:
            if origin:
                self.egress.propagate_chat(target_chat, origin)
            with self._graph_lock:
                self._active_waits.discard(target_chat)
                self._helper_origins.pop(target_chat, None)

    async def browser_for(self, helper: dict[str, Any], task: str, timeout: Optional[float] = None) -> str:
        """Execute `browser_request` inside a fresh ephemeral `desk` chat and destroy it on completion."""
        wait_s = self.ask_timeout_s if timeout is None else max(0.05, float(timeout))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        await asyncio.wait_for(self._browser_lock.acquire(), timeout=wait_s)
        desk_chat: Optional[str] = None
        try:
            desk_chat = await asyncio.to_thread(self.helpers.new_desk, self.store) if self.store is not None else f"desk-{uuid.uuid4().hex[:10]}"
            self._desk_chats.add(desk_chat)
            return await self.ask_helper(str(helper["chat_id"]), desk_chat, task, timeout=max(0.05, deadline - loop.time()))
        finally:
            if desk_chat is not None:
                self._desk_chats.discard(desk_chat)
                self._helper_locks.pop(desk_chat, None)
                if self.store is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.store.delete, desk_chat)
                if self._delete_desk_session_fn is not None:
                    with contextlib.suppress(Exception):
                        await self._delete_desk_session_fn(desk_chat)
            self._browser_lock.release()

    def hand_off(self, origin: str, helper: dict[str, Any], task: str, timeout: Optional[float] = None, goal_id: str = "") -> asyncio.Task[None]:
        """Dispatch background helper work (`wait=False`) on the event loop."""
        wait_s = self.background_timeout_s if timeout is None else max(0.05, float(timeout))

        async def _bg() -> None:
            name = str(helper.get("name") or "帮手")
            try:
                answer, ok = await self.ask_helper(origin, str(helper["chat_id"]), task, timeout=wait_s), True
            except asyncio.TimeoutError:
                answer, ok = "没做完", False
            except Exception as exc:
                answer, ok = str(exc) or "没做完", False
            if goal_id:
                self._log_goal_event(goal_id, "helper_done" if ok else "helper", f"「{name}」{'做完了' if ok else '没做成'}：{answer}", helper=name, emoji=helper.get("emoji"))
            await self._post_as_helper(origin, helper, answer if ok else f"这件事没做成：{answer}")
            self._notify_push(helper, answer)
            if self._wait_chat_idle_fn is not None:
                with contextlib.suppress(Exception):
                    await self._wait_chat_idle_fn(origin, 600.0)
            if self._submit_internal_fn is not None:
                await self._submit_internal_fn(
                    origin,
                    f"【系统通知】你之前在后台交给「{name}」的事{'做完了' if ok else '没做成'}，它的回答已经以它自己的名义显示给用户了，不要复述。"
                    f"\n交给它的事：{task[:300]}\n它的回答：{answer[:3000]}\n"
                    "如果这是为了接下来的某一步，就接着做；否则只用一句很短的话接上（比如点出最要紧的一点，或问用户下一步要不要做什么），不要重复它的回答。",
                )

        bg_task = asyncio.get_running_loop().create_task(_bg())
        self._bg_tasks.add(bg_task)
        bg_task.add_done_callback(self._bg_tasks.discard)
        return bg_task

    def _log_goal_event(self, goal_id: str, kind: str, text: str, **kw: Any) -> None:
        with contextlib.suppress(Exception):
            _comp_mod(self._plans_mod, "plans").log_event(goal_id, kind, text, **kw)

    def _notify_push(self, helper: dict[str, Any], answer: str) -> None:
        with contextlib.suppress(Exception):
            push = _comp_mod(self._push_mod, "push")
            widgets = _comp_mod(self._widgets_mod, "widgets")
            push.schedule_push(f"{helper.get('emoji') or '🤖'} {helper.get('name') or '帮手'}", push.preview(widgets.strip_cards(answer)), tag=f"helper-{helper.get('id', '')}")

    async def handle_helper_ask(self, args: dict[str, Any], *, ctx: Any = None) -> str:
        resolved = resolve_turn_context(ctx)
        hlp = self.helpers
        helper = hlp.find(str(args.get("helper") or ""))
        task = str(args.get("task") or "").strip()
        if not helper:
            return _json_result(success=False, error="没有这个帮手", helpers=[h["name"] for h in hlp.all_helpers()])
        if not task:
            return _json_result(success=False, error="要说清楚交给它什么事")
        goal_id = str(args.get("goal_id") or "")
        origin = str(getattr(resolved, "chat_id", "") or "") or None
        if getattr(resolved, "actor_kind", "") == "desk" or self.is_desk(origin):
            return _json_result(success=False, error="浏览器代办只做浏览器上的事，不交给帮手")
        callee = str(helper["chat_id"])
        if origin == callee:
            return _json_result(success=False, error="帮手不能把事交给自己")

        from_helper_chain = False
        if origin is not None and hlp.helper_for_chat(origin) is not None:
            group = hlp.group_for_chat(self.helper_origin(origin))
            if group is not None:
                if helper["id"] not in (group.get("members") or []):
                    return _json_result(success=False, error="在群聊里只能交给同群的其他帮手")
                origin = str(group["chat_id"])
            else:
                from_helper_chain = True

        if from_helper_chain and origin is not None:
            token, cycle_err = self._enqueue_waiter(origin, callee, str(helper["name"]))
            if token is None:
                return _json_result(success=False, error=cycle_err or "不能继续转交")
            try:
                answer = await self.ask_helper(origin, callee, task[:4000], timeout=self.ask_timeout_s)
            except asyncio.TimeoutError:
                return _json_result(success=False, error=f"「{helper['name']}」还没做完，做完的结果在它的聊天里能看到")
            except Exception as exc:
                return _json_result(success=False, error=str(exc) or type(exc).__name__)
            finally:
                self._dequeue_waiter(callee, token)
            if goal_id:
                self._log_goal_event(goal_id, "helper_done", f"「{helper['name']}」做完了：{answer}", helper=helper["name"], emoji=helper.get("emoji"))
            await self._post_as_helper(origin, helper, answer)
            return _json_result(success=True, helper=helper["name"], posted=True, answer=answer,
                                note="它的回答已经以它自己的名义显示在聊天里了，不要复述；接着把你的活做完、汇总。")

        if goal_id:
            self._log_goal_event(goal_id, "helper", f"交给「{helper['name']}」：{task}", helper=helper["name"], emoji=helper.get("emoji"))
        if args.get("wait") is False and origin:
            self.hand_off(origin, helper, task[:4000], timeout=self.background_timeout_s, goal_id=goal_id)
            return _json_result(success=True, helper=helper["name"], background=True,
                                note=f"已经交给「{helper['name']}」在后台做了，做完它会自己在这里说。告诉用户一声就行，不要等它。")
        try:
            answer = await self.ask_helper(origin, callee, task[:4000], timeout=self.ask_timeout_s)
        except asyncio.TimeoutError:
            return _json_result(success=False, error=f"「{helper['name']}」还没做完，做完的结果在「帮手」页能看到")
        except Exception as exc:
            return _json_result(success=False, error=str(exc) or type(exc).__name__)
        if goal_id:
            self._log_goal_event(goal_id, "helper_done", f"「{helper['name']}」做完了：{answer}", helper=helper["name"], emoji=helper.get("emoji"))
        if origin:
            await self._post_as_helper(origin, helper, answer)
            return _json_result(
                success=True, helper=helper["name"], posted=True, answer=answer,
                note="它的回答已经以它自己的名义显示在聊天里了，不要复述。需要的话补一句结论或下一步；交给了几个帮手的，由你把它们的结果汇总成一个结论。",
            )
        return _json_result(success=True, helper=helper["name"], answer=answer)

    async def handle_browser_request(self, args: dict[str, Any], *, ctx: Any = None) -> str:
        resolved = resolve_turn_context(ctx)
        origin = str(getattr(resolved, "chat_id", "") or "")
        helper = self.helpers.helper_for_chat(origin) if origin else None
        if helper is None:
            return _json_result(success=False, error="主助理自己直接用浏览器工具就行")
        task = str(args.get("task") or "").strip()
        if not task:
            return _json_result(success=False, error="要说清楚在网页上做什么、要什么结果")
        try:
            answer = await self.browser_for(helper, task[:4000], timeout=self.ask_timeout_s)
        except asyncio.TimeoutError:
            return _json_result(success=False, error="浏览器上的事还没做完")
        except Exception as exc:
            return _json_result(success=False, error=str(exc) or type(exc).__name__)
        return _json_result(success=True, result=answer)
