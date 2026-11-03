"""Helpers getting to know the user, every hour: what the user keeps asking for in each helper's line
of work becomes one of its habits (``helpers.add_habit``), which it then follows by default.

A helper records a habit itself when the user states one outright (``helper_habit``). This pass
catches the rest: preferences the user shows again and again, or says in the main chat or a group
rather than to the helper (planning a trip with the companion and saying twice "别排太紧" teaches
行程). Once an hour, when the user said something new anywhere, the model reads the new part of the
conversations next to each helper's job and current habits and proposes additions, rewrites and
removals; this code checks them:

- only lasting preferences about how that helper's kind of work should be done — said with "以后 /
  每次 / 总是", or shown at least twice; no one-off requests, no guesses, no secrets;
- at most ``_MAX_CHANGES`` changes in one pass; habits the user typed on the profile are never touched.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from . import helpers
from .llm import ask_json

logger = logging.getLogger(__name__)

_MAX_CHANGES = 8
_MAX_CONVERSATION_CHARS = 8_000
_SECRET = re.compile(r"(密码|password|验证码|\b\d{15,19}\b|身份证号?\s*[:：]?\s*\d)", re.I)
_RUNNING = threading.Lock()

_PROMPT = """用户有几个帮手，每个帮手负责一类事。下面是帮手的名单（职责、它已经摸清的用户习惯，习惯前是编号），然后是用户最近在各个聊天里说的话。
找出用户在某个帮手负责的那类事上的长期习惯和偏好，让帮手以后默认照着做。

能记的：
- 用户明确说“以后/每次/总是/一直/别再”的偏好，比如“行程以后别排太紧”
- 同一类偏好用户表达了两次或以上（哪怕措辞不同），比如两次嫌行程太赶
- 要具体：写清怎么做，比如“每天最多安排三个地方，中间留出休息时间”

不能记的：一次性的要求（“这次预算五千”）、你的猜测、和帮手职责无关的事、密码验证码证件号；标了“功能测试”的话忽略。

已有的习惯有了新说法就 update 那一条；被用户推翻的 remove。标了【用户写的】的不能动。没有就返回空列表。

只输出一个 JSON：
{"changes":[{"helper":"帮手名","op":"add","text":"……"},{"helper":"帮手名","op":"update","id":编号,"text":"……"},{"helper":"帮手名","op":"remove","id":编号}]}"""


def _conversation(chat_store: Any, since_ms: int) -> tuple[str, int]:
    """What the user said in every chat since ``since_ms`` (with which chat it was), oldest first."""
    by_chat = {h["chat_id"]: h["name"] for h in helpers._read()}
    groups = {g["chat_id"]: g["name"] for g in helpers._groups()}
    lines: list[tuple[int, str]] = []
    newest = since_ms
    for chat_id, meta in list(chat_store._read_index().items()):
        if meta.get("desk"):
            continue
        where = (f"和「{by_chat[chat_id]}」单聊" if chat_id in by_chat else f"群聊「{groups[chat_id]}」" if chat_id in groups
                 else "和主助理聊")
        for event in chat_store.events(chat_id):
            at = int(event.get("created_at_ms") or 0)
            if at <= since_ms or event.get("event") != "user_message":
                continue
            if (event.get("provenance") or {}).get("session_message"):
                continue  # work the companion handed a helper: the user's own words are in the other chat
            text = " ".join(str(event.get("text") or "").split())
            if text and not text.startswith("【系统通知】"):
                lines.append((at, f"[{where}] {text[:600]}"))
                newest = max(newest, at)
    lines.sort()
    return "\n".join(line for _, line in lines)[-_MAX_CONVERSATION_CHARS:], newest


def _roster(rows: list[dict[str, Any]]) -> str:
    out = []
    for helper in rows:
        habits = helpers.habits_of(helper)
        out.append(f"「{helper['name']}」：{helper.get('title') or '（没写职责）'}\n" + (
            "\n".join(f"  {i}. {'【用户写的】' if h.get('by') == 'you' else ''}{h['text']}" for i, h in enumerate(habits, 1))
            or "  （还没有习惯）"))
    return "\n".join(out)


def _propose(roster: str, conversation: str) -> list[dict[str, Any]]:
    changes = ask_json(_PROMPT, f"帮手：\n{roster}\n\n用户最近说的话：\n{conversation}", max_tokens=1500).get("changes")
    return [c for c in changes if isinstance(c, dict)] if isinstance(changes, list) else []


def apply(rows: list[dict[str, Any]], changes: list[dict[str, Any]]) -> int:
    """Checks the proposed changes and makes the allowed ones; how many were made."""
    made = 0
    for change in changes:
        if made >= _MAX_CHANGES:
            break
        helper = next((h for h in rows if h["name"] == change.get("helper")), None)
        if helper is None:
            continue
        habits = helpers.habits_of(helper)
        index = change.get("id")
        target = habits[index - 1] if isinstance(index, int) and 1 <= index <= len(habits) else None
        if target is not None and target.get("by") == "you":
            continue
        text = " ".join(str(change.get("text") or "").split())
        op = change.get("op")
        if op in ("add", "update") and (not text or _SECRET.search(text) or any(h["text"] == text for h in habits)):
            continue
        if op == "add":
            helpers.add_habit(helper["id"], text, "learned")
            helpers.notice(helper, f"记住了：{text}")
        elif op == "update" and target is not None:
            helpers.add_habit(helper["id"], text, "learned", replaces=target["id"])
            helpers.notice(helper, f"改记成：{text}")
        elif op == "remove" and target is not None:
            helpers.remove_habit(helper["id"], target["id"])
            helpers.notice(helper, f"不再记：{target['text']}")
        else:
            continue
        made += 1
    return made


def learn(chat_store: Any) -> dict[str, Any]:
    """One pass over what the user said since the last one."""
    if not _RUNNING.acquire(blocking=False):
        return {"learned": 0, "running": True}
    try:
        rows = [h for h in helpers._read() if not h.get("setup")]
        if not rows:
            return {"learned": 0}
        since = int(helpers._load().get("habits_learned_until_ms") or 0) or int((time.time() - 7 * 86400) * 1000)
        conversation, newest = _conversation(chat_store, since)
        if not conversation:
            return {"learned": 0}
        try:
            changes = _propose(_roster(rows), conversation)
        except Exception as exc:  # no model right now: the same conversation is read next time
            logger.info("companion: helper learning has no model (%s)", type(exc).__name__)
            return {"learned": 0, "error": type(exc).__name__}
        made = apply(helpers._read(), changes)
        helpers.set_learned_until(newest)
        if made:
            logger.info("companion: helpers learned %d habit change(s)", made)
        return {"learned": made}
    finally:
        _RUNNING.release()
