"""Learning about the user from what was said, every hour (as Muse keeps its Memory.md).

The chat model may save a memory mid-turn, but it rarely does (and a weak one gets the tool call
wrong), so this does it on its own: once an hour, when something new was said in any chat, the
model reads the new part of the conversations next to the current memory and proposes what to
add, correct or drop. This code checks the proposal and applies it through Hermes's memory store
(so the size limit and the safety scan still apply):

- only what the user said or plainly showed about themselves, the people around them, their plans
  and how they like to be talked to; no guesses, no secrets (passwords, ID or card numbers);
- every entry sits in one of ``SECTIONS`` (a 【section】 prefix), which the 记忆 page shows as headings;
- entries the user wrote themselves are never changed or dropped;
- at most ``_MAX_CHANGES`` changes in one pass.

It also notes which entries (and which of the companion's own notes) came up again, so what the
user stops talking about can fade (``memory_fade``): the model says which, and an entry whose own
words turn up in what the user said counts too.

Each change goes into the memory change log (``memory_tidy``), where the app can undo it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from .llm import ask_json
from .memory import _LOCK, _store
from .memory_tidy import _CHANGES_KEPT, _META_LOCK, _read_meta, _write_meta, mark_seen

logger = logging.getLogger(__name__)

SECTIONS = ("基本信息", "喜好与习惯", "身边的人", "近况与计划", "相处方式")
_SECTION = re.compile(r"^【(" + "|".join(SECTIONS) + r")】")
_MAX_CHANGES = 12
_MAX_CONVERSATION_CHARS = 9_000
_MAX_ENTRY_CHARS = 200
_SECRET = re.compile(r"(密码|password|验证码|\b\d{15,19}\b|身份证号?\s*[:：]?\s*\d)", re.I)
_RUNNING = threading.Lock()

_PROMPT = """你是一个私人助理，在维护你对用户的长期记忆（Memory.md）。下面先是现在的记忆（每行前面是编号），再是你自己的笔记（编号前有 N），然后是你和用户最近的对话。
根据对话，提出对记忆的修改：

能记的：用户亲口说的或明确表现出来的
- 基本信息：称呼、所在城市、时区、工作/学业、常用语言等
- 喜好与习惯：兴趣、口味、作息、常去的地方、讨厌的东西
- 身边的人：家人、朋友、同事——名字、关系、和他们相关的要紧事（如生日、在忙什么）
- 近况与计划：最近在忙的事、打算、要去的地方、截止日期（相对时间换成具体日期，今天是 {today}）
- 相处方式：用户希望你怎么说话、怎么做事（比如"回答短一点"、"别用表情"）

规则：
- 用户花心思问的话题也说明了什么：反复比较一样要买的东西（在考虑买它）、追问某类行情（在关注它）、让你帮忙记录某件事（在做这件事）——这些算兴趣或近况，可以记
- 不猜测、不推断性格、不记一次性的随口小事（比如"问了今天天气"）、不记你自己说的话；标了"功能测试"的消息是在测试，忽略
- 绝不记密码、验证码、证件号、银行卡号
- 已经记着的，有新说法就 update 那一条；过时的（计划已经过去、被推翻的）remove
- 标了【用户写的】的条目不能 update 或 remove
- 每条一句话，简洁具体，用中文
- 没有值得记的就全部返回空列表
- 另外：这段对话里用户又谈到、仍然成立的记忆（不需要修改的也算），把编号放进 touched；谈到的笔记，把 N 后面的数字放进 touched_notes（笔记只用来标 touched_notes，不能 add/update/remove 它们）。用户不再谈的事会慢慢淡忘，所以只放真的谈到了的

只输出一个 JSON：
{{"add":[{{"section":"喜好与习惯","text":"……"}}],"update":[{{"id":编号,"text":"……"}}],"remove":[{{"id":编号,"why":"……"}}],"touched":[编号],"touched_notes":[编号]}}
section 只能是：{sections}"""


# ── what is new since the last pass ───────────────────────────────────────────────────────────────

def _chat_untouched_since(chat_store: Any, chat_id: str, meta: dict[str, Any], since_ms: int) -> bool:
    """Fast skip for transcripts untouched since ``since_ms`` without parsing their JSONL lines."""
    if since_ms <= 0:
        return False
    cutoff_s = (since_ms - 2000) / 1000.0
    get_path = getattr(chat_store, "_path", None)
    if not callable(get_path):
        return False
    try:
        st = get_path(chat_id).stat()
    except OSError:
        return True
    if st.st_mtime >= cutoff_s:
        return False
    raw_updated = meta.get("updated_at")
    if isinstance(raw_updated, str) and raw_updated:
        try:
            if datetime.fromisoformat(raw_updated).timestamp() >= cutoff_s:
                return False
        except ValueError:
            return False
    return True


def _conversation(chat_store: Any, since_ms: int) -> tuple[str, int]:
    """The user's messages and the replies since ``since_ms`` in every chat, oldest first, as text
    (the newest part when it is long), and the newest time seen."""
    lines: list[tuple[int, str]] = []
    newest = since_ms
    for chat_id, meta in list(chat_store._read_index().items()):
        if meta.get("helper") or meta.get("desk"):
            continue  # a helper's (or the browser desk's) working chat: the main agent's instructions, not the user's words
        if _chat_untouched_since(chat_store, chat_id, meta, since_ms):
            continue
        for event in chat_store.events(chat_id):
            at = int(event.get("created_at_ms") or 0)
            text = " ".join(str(event.get("text") or "").split())
            if at <= since_ms or not text:
                continue
            if event.get("event") == "user_message":
                lines.append((at, f"用户：{text[:1500]}"))
            elif event.get("event") == "message" and event.get("turn_phase") == "answer":
                lines.append((at, f"你：{text[:600]}"))
            else:
                continue
            newest = max(newest, at)
    if not any(line.startswith("用户：") for _, line in lines):
        return "", newest
    lines.sort()
    text = "\n".join(line for _, line in lines)
    return text[-_MAX_CONVERSATION_CHARS:], newest


# ── checking and applying a proposal ──────────────────────────────────────────────────────────────

def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())[:_MAX_ENTRY_CHARS]


def _plan(entries: list[str], proposal: dict[str, Any], protected: set[str]) -> list[dict[str, Any]]:
    """The allowed changes: ``{"kind", "before": [old] or [], "after": new or None}``."""
    changes: list[dict[str, Any]] = []
    used: set[int] = set()

    def usable(index: Any) -> bool:
        return isinstance(index, int) and 1 <= index <= len(entries) and index not in used \
            and entries[index - 1] not in protected

    for item in proposal.get("update") or []:
        if not isinstance(item, dict) or not usable(item.get("id")):
            continue
        old = entries[item["id"] - 1]
        section = (_SECTION.match(old) or [None, "近况与计划"])[1]
        body = _SECTION.sub("", _clean(item.get("text")))
        if not body or _SECRET.search(body) or body == _SECTION.sub("", old):
            continue
        used.add(item["id"])
        changes.append({"kind": "learn-update", "before": [old], "after": f"【{section}】{body}"})
    for item in proposal.get("remove") or []:
        if not isinstance(item, dict) or not usable(item.get("id")):
            continue
        used.add(item["id"])
        changes.append({"kind": "learn-remove", "before": [entries[item["id"] - 1]], "after": None,
                        "why": _clean(item.get("why"))[:80]})
    known = {_SECTION.sub("", e) for e in entries}
    for item in proposal.get("add") or []:
        if not isinstance(item, dict) or item.get("section") not in SECTIONS:
            continue
        body = _SECTION.sub("", _clean(item.get("text")))
        if not body or body in known or _SECRET.search(body):
            continue
        known.add(body)
        changes.append({"kind": "learn-add", "before": [], "after": f"【{item['section']}】{body}"})
    return changes[:_MAX_CHANGES]


_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.+-]{2,}")
_CJK = re.compile(r"[㐀-鿿]+")


def _tokens(text: str) -> set[str]:
    """Words of three or more letters, and each pair of neighbouring Chinese characters."""
    words = {w.lower() for w in _LATIN.findall(text)}
    pairs = {run[i:i + 2] for run in _CJK.findall(text) for i in range(len(run) - 1)}
    return words | pairs


def mentioned(entries: list[str], said: str) -> list[str]:
    """The entries whose own words turn up in what the user said: at least three of them, and at
    least a quarter of the entry's (one shared word like 最近 is not the same topic)."""
    heard = _tokens(said)
    found = []
    for entry in entries:
        own = _tokens(_SECTION.sub("", entry))
        hits = len(own & heard)
        if own and hits >= 3 and hits * 4 >= len(own):
            found.append(entry)
    return found


def _picked(ids: Any, items: list[str]) -> list[str]:
    return [items[i - 1] for i in ids if isinstance(i, int) and 1 <= i <= len(items)] if isinstance(ids, list) else []


def _ops(change: dict[str, Any]) -> list[dict[str, Any]]:
    before, after = change["before"], change["after"]
    if not before:
        return [{"action": "add", "content": after}]
    if after is None:
        return [{"action": "remove", "old_text": before[0], "matched_entry": before[0]}]
    return [{"action": "replace", "old_text": before[0], "matched_entry": before[0], "content": after}]


def _propose(memory_lines: str, conversation: str) -> dict[str, Any]:
    prompt = _PROMPT.format(today=datetime.now().strftime("%Y-%m-%d（%a）"), sections="、".join(SECTIONS))
    return ask_json(prompt, f"现在的记忆：\n{memory_lines or '（还没有）'}\n\n最近的对话：\n{conversation}")


def learn(chat_store: Any, force: bool = False) -> dict[str, Any]:
    """One pass over what was said since the last one. ``force``: also when nothing new arrived
    within the hour (the app's 现在更新). Returns how many changes were made."""
    if not _RUNNING.acquire(blocking=False):
        return {"learned": 0, "running": True}
    try:
        meta = _read_meta()
        saved_since = int(meta.get("learned_until_ms") or 0)
        since = saved_since or int((time.time() - 7 * 86400) * 1000)
        conversation, newest = _conversation(chat_store, since)
        if not conversation and force:
            # 「现在更新」: re-read recent conversations even when nothing newer than learned_until_ms arrived.
            for window_s in (24 * 3600, 7 * 86400):
                lookback = max(0, int((time.time() - window_s) * 1000))
                conversation, lookback_newest = _conversation(chat_store, lookback)
                if conversation:
                    newest = max(saved_since, lookback_newest)
                    break
        if not conversation:
            return {"learned": 0}
        store = _store()
        entries = [e for e in store._entries_for("user") if e.strip()]
        notes = [e for e in store._entries_for("memory") if e.strip()]
        protected = set(meta["user_written"])
        lines = "\n".join(f"{i}. {'【用户写的】' if e in protected else ''}{' '.join(e.split())}"
                          for i, e in enumerate(entries, 1))
        if notes:
            lines += "\n\n你自己的笔记：\n" + "\n".join(f"N{i}. {' '.join(e.split())}" for i, e in enumerate(notes, 1))
        try:
            proposal = _propose(lines, conversation)
        except Exception as exc:  # no model right now: the same conversation is read next time
            logger.info("companion: memory learning has no model (%s)", type(exc).__name__)
            return {"learned": 0, "error": type(exc).__name__}
        applied = []
        for change in _plan(entries, proposal, protected):
            with _LOCK:
                result = _store().apply_batch("user", _ops(change))
            if result.get("success") is False or result.get("error"):
                logger.info("companion: memory learning skipped a change: %s", result.get("error") or result)
                continue
            applied.append({"id": uuid.uuid4().hex[:10], "at": time.time(), "file": "USER.md", **change})
        said = "\n".join(line for line in conversation.split("\n") if line.startswith("用户："))
        came_up = {*_picked(proposal.get("touched"), entries), *_picked(proposal.get("touched_notes"), notes),
                   *mentioned(entries + notes, said), *(c["after"] for c in applied if c["after"])}
        with _META_LOCK:
            meta = _read_meta()
            meta["learned_until_ms"] = newest
            meta["learned_at"] = time.time()
            meta["changes"] = (applied[::-1] + meta["changes"])[:_CHANGES_KEPT]
            mark_seen(meta, came_up, time.time())
            _write_meta(meta)
        if applied:
            logger.info("companion: learned %d memory change(s) from recent chats", len(applied))
        return {"learned": len(applied)}
    finally:
        _RUNNING.release()


def last_learned() -> Optional[float]:
    return _read_meta().get("learned_at") or None
