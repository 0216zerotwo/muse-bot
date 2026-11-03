"""Hermes's own notices that its locale files don't cover, said as the companion would.

A conversation paused across a restart, a message saved while it restarts, a "are you sure" for
/new: Hermes writes these in English, names itself, and asks for commands (/discard, /approve)
that mean nothing on a phone. Here they become a sentence and buttons that send those commands.
"""

from __future__ import annotations

import json
from typing import Optional


def _with_buttons(text: str, items: list[dict[str, object]]) -> str:
    card = {"type": "buttons", "items": items}
    return f"{text}\n\n```card\n{json.dumps(card, ensure_ascii=False)}\n```"


_PAUSED_LOST_TURN = "⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted"
_PAUSED = "⏸️ This conversation is paused:"
_RESTARTING = "⏳ Hermes is restarting"
_BUSY_SAVED = "⏳ Your message is saved and will be answered when the work running in this conversation finishes"


def in_our_words(text: str) -> str:
    """The notice in the companion's words, or the text as it was."""
    head = text.lstrip()
    if head.startswith(_PAUSED_LOST_TURN):
        return _with_buttons(
            "服务器重启时，上一件事没做完就断了，这段对话先停在这里。你刚发的消息已经记下了。",
            [{"label": "放下那件事，接着聊", "send": "/discard", "primary": True},
             {"label": "开个新对话", "send": "/new"}],
        )
    if head.startswith(_PAUSED):
        return _with_buttons("这段对话暂时没法继续，你刚发的消息已经记下了。开个新对话就能接着聊。",
                             [{"label": "开个新对话", "send": "/new", "primary": True}])
    if head.startswith(_RESTARTING):
        return "我在重启，你这条记下了，回来就回你。"
    if head.startswith(_BUSY_SAVED):
        return "收到，手上这件做完就回你。"
    return text


# What each command Hermes asks about does, in a line.
_CONFIRMS = {
    "new": "要开个新对话吗？现在这段聊天会收起来，我记得的关于你的事都还在。",
    "reset": "要开个新对话吗？现在这段聊天会收起来，我记得的关于你的事都还在。",
    "undo": "要撤回上一轮吗？你上一条消息和我的回复都会删掉。",
}


def confirm_text(title: str) -> Optional[str]:
    """A yes-or-no card for the command Hermes wants confirmed (``/new``…), or None for one this
    doesn't know (Hermes's own prompt is sent then)."""
    command = (title or "").strip().lstrip("/").split(" ")[0].lower()
    ask = _CONFIRMS.get(command)
    if ask is None:
        return None
    # Hermes takes /approve and /cancel typed back as the answer.
    return _with_buttons(ask, [{"label": "好", "send": "/approve", "primary": True}, {"label": "算了", "send": "/cancel"}])
