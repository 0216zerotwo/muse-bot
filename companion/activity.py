"""What the agent is doing, shown in the app while it works.

Hermes calls the plugin's ``pre_tool_call`` / ``post_tool_call`` hooks around every
tool call (on the agent's thread). These turn them into the companion's
``tool_hint`` activity events (nanobot's wire shape: ``tool_events`` with
``phase`` start/end), which drive the "正在…" line, the pet's animation and
the 任务 page's activity log. Hooks only observe: they never block a call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from .approvals import approval_action
from .modules import computer, creations, egress, permissions, secrets_guard

if TYPE_CHECKING:
    from .adapter import CompanionAdapter

logger = logging.getLogger(__name__)

_RESULT_LIMIT = 2000
_MAX_SESSION_CACHE = 1024
_adapter: Optional["CompanionAdapter"] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
# Hermes session id -> companion chat id (bounded LRU-style dict)
_chat_by_session: dict[str, str] = {}


def bind(adapter: "CompanionAdapter", loop: asyncio.AbstractEventLoop) -> None:
    global _adapter, _loop
    _adapter, _loop = adapter, loop


def _chat_for_session(session_id: str) -> Optional[str]:
    """The companion chat a session belongs to."""
    from muse.context import get_turn_context
    from muse.paths import get_muse_db_path

    ctx = get_turn_context()
    if ctx is not None and ctx.chat_id and (not session_id or ctx.session_id == session_id):
        return ctx.chat_id
    if session_id.startswith("companion:"):
        return session_id.split(":", 1)[1] or None
    cached_native = _chat_by_session.get(session_id)
    if cached_native:
        return cached_native
    db_path = get_muse_db_path()
    if session_id and db_path.is_file():
        with contextlib.suppress(Exception):
            from muse.runtime import use_shared_db

            with use_shared_db(db_path=db_path) as mdb:
                row = mdb.get_session(session_id)
                if row and row.get("chat_id"):
                    cid = str(row["chat_id"])
                    if len(_chat_by_session) >= _MAX_SESSION_CACHE:
                        _chat_by_session.pop(next(iter(_chat_by_session)), None)
                    _chat_by_session[session_id] = cid
                    return cid
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _RESULT_LIMIT else text[:_RESULT_LIMIT] + "…"


def _emit(phase: str, tool_name: str, args: Any, *, session_id: str = "", tool_call_id: str = "",
          result: Any = None, error: Any = None) -> None:
    adapter, loop = _adapter, _loop
    if adapter is None or loop is None or not session_id:
        if os.environ.get("COMPANION_DEBUG_FRAMES"):
            logger.warning("companion: tool %s %s without session (adapter=%s)", tool_name, phase, adapter is not None)
        return
    chat_id = _chat_for_session(str(session_id))
    if not chat_id:
        if os.environ.get("COMPANION_DEBUG_FRAMES"):
            logger.warning("companion: no chat for session %r (tool %s %s)", session_id, tool_name, phase)
        return
    tool_event = {
        "version": 1,
        "phase": phase,
        "call_id": str(tool_call_id or f"{tool_name}-{id(args)}"),
        "name": tool_name,
        "arguments": args if isinstance(args, dict) else {},
        "result": _text(result) if phase == "end" else None,
        "error": _text(error),
        "files": [],
        "embeds": [],
    }
    asyncio.run_coroutine_threadsafe(adapter.emit_activity(chat_id, tool_event), loop)


def on_pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[dict[str, str]]:
    session_id, call_id = kwargs.get("session_id", ""), kwargs.get("tool_call_id", "")
    _emit("start", tool_name, args, session_id=session_id, tool_call_id=call_id)
    secret = secrets_guard.check(tool_name, args)
    if secret:
        return {"action": "block", "message": secret}
    session_chat = _chat_for_session(str(session_id)) if session_id else None
    is_browser = computer.is_browser_tool(tool_name)
    if is_browser:
        # The browser is the companion's: a helper asks it with browser_request instead.
        from .modules.helpers import helper_for_chat

        if helper_for_chat(session_chat):
            return {"action": "block", "message": (
                "帮手不能直接用浏览器。用 browser_request 把要在网页上做的事交给主助理，它做完会把结果告诉你。")}
        if computer.holding():
            return {"action": "block", "message": (
                "用户正在亲自操作浏览器，等用户说好了再继续。现在不要操作浏览器，可以先做别的或者等一等。")}
        computer.ensure_browser()
    base_action = approval_action(tool_name, args)
    # After reading private data, sending to a site needs the user too (see modules/egress) —
    # always evaluated even when ``base_action`` is set so ``browser_submit=allow`` cannot bypass ``send_private``.
    egress_target = egress.check(session_id, tool_name, args, chat_id=session_chat)
    host = (egress_target or {}).get("host")
    base_kind = permissions.kind_of(tool_name, args, base_action, is_browser)
    base_level = permissions.level_of(base_kind) if base_kind else permissions.ALLOW
    if base_level == permissions.DENY:
        return {"action": "block", "message": permissions.denied_message(base_kind)}
    egress_level = permissions.level_of("send_private") if egress_target else permissions.ALLOW
    if egress_level == permissions.DENY:
        return {"action": "block", "message": permissions.denied_message("send_private")}

    if base_action == "goal_plan":  # a plan change is always the user's call
        action, kind = "goal_plan", base_kind
    else:
        base_needs_ask = bool(base_kind and base_level == permissions.ASK and (base_action or base_kind != "run_command"))
        egress_needs_ask = bool(egress_target and egress_level == permissions.ASK)
        if not base_needs_ask and not egress_needs_ask:
            return None
        if base_needs_ask:
            action = base_action or base_kind
            kind = base_kind
        else:
            action = "send_private"
            kind = "send_private"

    if not action:
        return None
    if _adapter is not None:
        base_covered = (not base_action and action == "send_private") or _adapter.approvals.granted(action, args, host)
        egress_covered = (not egress_target or egress_level == permissions.ALLOW
                          or _adapter.approvals.granted("send_private", args, host))
        if base_covered and egress_covered and action != "goal_plan":
            return None
    # Acting in the user's name: hold the call as an approval card instead of running it. Calls
    # from outside a companion chat (scheduled jobs) are shown in the main companion chat.
    chat_id = session_chat or _home_chat()
    if chat_id is None or _adapter is None:
        return {"action": "block", "message": "这个操作需要用户确认，但现在没法让用户确认，所以没有执行。"}
    facing = _adapter.facing_chat(chat_id)
    message = _adapter.approvals.defer(facing, call_id, tool_name, args, action, egress_target, kind=kind)
    if facing != chat_id:  # handed work: the card is where the user waits, and the companion takes it from there
        message += "（确认卡片已放到用户正在看的聊天里，用户同意后主助理会在那边接着做。先把做到哪一步写出来。）"
    return {"action": "block", "message": message}


def _record_media(tool_name: str, args: Any, result: Any) -> None:
    """用量: a picture or a clip a chat's tools made (the length and resolution asked, else the
    tools' defaults)."""
    if tool_name not in ("image_generate", "video_generate") or not isinstance(result, str):
        return
    try:
        made = json.loads(result)
    except ValueError:
        return
    if not isinstance(made, dict) or not made.get("success"):
        return
    from .modules.models import record_media

    args = args if isinstance(args, dict) else {}
    if tool_name == "image_generate":
        record_media("image")
    else:
        seconds = args.get("duration") if isinstance(args.get("duration"), (int, float)) else 6
        record_media("video", seconds=float(seconds), resolution=str(args.get("resolution") or "720p"))


def _home_chat() -> Optional[str]:
    from .modules.companion_state import read_companion_state

    chat_id = read_companion_state().get("chat_id")
    return str(chat_id) if chat_id else None


def on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **kwargs: Any) -> None:
    error = kwargs.get("error_message") if kwargs.get("status") == "error" else None
    session_id = kwargs.get("session_id")
    session_chat = _chat_for_session(str(session_id)) if session_id else None
    if computer.is_browser_tool(tool_name) and kwargs.get("status") not in ("error", "blocked"):
        computer.note_action(tool_name, args, session_chat)
    if kwargs.get("status") not in ("error", "blocked"):
        egress.note(session_id, tool_name, chat_id=session_chat)
        # Images, videos and audio a generation tool made go into 创意.
        creations.on_generation_result(tool_name, args, result, _loop)
        _record_media(tool_name, args, result)
    if kwargs.get("status") == "blocked" and kwargs.get("error_message"):
        # The app recognizes a held call by the block message itself.
        result = kwargs["error_message"]
    _emit("end", tool_name, args, session_id=kwargs.get("session_id", ""),
          tool_call_id=kwargs.get("tool_call_id", ""), result=result, error=error)
