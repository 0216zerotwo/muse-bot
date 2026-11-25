"""Tasks a restart cut off: the chat says which one, with 接着做.

Hermes takes a turn up again by itself after a clean restart (it marks the session before it stops).
Closing the server's window, a crash or a power cut leave no mark, and the task the user was waiting
on just stopped: they found out only by asking. A little after start-up, each chat whose last task
never ended (and is recent) gets a message naming it and a 接着做 button, unless Hermes or the user
has already taken the chat up again. The cut turn is closed, so the apps stop showing it at work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FRESH_S = 6 * 3600
_NATIVE_SETTLE_S = 3


def settle_seconds() -> int:
    return _NATIVE_SETTLE_S


def cut_off(events: list[dict[str, Any]], now_ms: int, started_ms: int) -> Optional[dict[str, Any]]:
    """The chat's last task when it was still at work (asked, or using tools, not yet answered),
    never ended, is recent, and nothing has happened in the chat since this server started:
    ``{"turn_id", "text"}``."""
    asked: Optional[dict[str, Any]] = None
    answered = False
    ended: set[str] = set()
    for event in events:
        if (event.get("created_at_ms") or 0) >= started_ms:
            return None  # taken up again already (Hermes's resume, or the user)
        kind = event.get("event")
        if kind == "user_message" and event.get("starts_turn") and event.get("turn_id"):
            asked, answered = event, False
        elif kind == "turn_end" and event.get("turn_id"):
            ended.add(str(event["turn_id"]))
        elif kind == "message" and asked is not None and event.get("turn_id") == asked["turn_id"]:
            # Its answer came (only the turn's end was lost); a step after it means it went on working.
            answered = event.get("kind") != "tool_hint" and bool(event.get("text"))
    if asked is None or answered or str(asked["turn_id"]) in ended:
        return None
    if now_ms - (asked.get("created_at_ms") or 0) > _FRESH_S * 1000:
        return None
    return {"turn_id": str(asked["turn_id"]), "text": " ".join(str(asked.get("text") or "").split())}


def message(task: str) -> str:
    """What the chat says: the task in a few words, and a button that asks for it again."""
    short = task if len(task) <= 24 else task[:24] + "…"
    named = f"「{short}」" if short else "刚才的事"
    card = {"type": "buttons", "items": [{"label": "接着做", "primary": True,
                                          "send": "接着做刚才被打断的事" + (f"：{task[:300]}" if task else "")}]}
    return f"服务器刚才重启了，{named}没做完。要接着做，点一下就行。\n\n```card\n{json.dumps(card, ensure_ascii=False)}\n```"


async def announce(adapter: Any, started_ms: int) -> None:
    """After start-up has settled: tell each chat whose task the last stop cut off."""
    await asyncio.sleep(settle_seconds())
    try:
        chats = [chat_id for chat_id, meta in adapter.store._read_index().items()
                 if not meta.get("desk") and not meta.get("helper") and not meta.get("cron") and not chat_id.startswith("cron:")]
    except Exception:
        logger.exception("companion: could not list the chats to look for cut-off tasks")
        return
    now = int(time.time() * 1000)
    for chat_id in chats:
        if chat_id in adapter._turns:
            continue
        try:
            cut = cut_off(adapter.store.events(chat_id), now, started_ms)
        except Exception:
            logger.exception("companion: could not read chat %s", chat_id)
            continue
        if cut is None:
            continue
        logger.info("companion: a restart cut off a task in chat %s", chat_id)
        await adapter._emit(chat_id, {"event": "turn_end", "turn_id": cut["turn_id"], "turn_phase": "complete"}, persist=True)
        await adapter._emit(chat_id, {"event": "message", "text": message(cut["text"])}, persist=True)
