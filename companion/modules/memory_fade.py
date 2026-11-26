"""Letting memory fade, as a person's does: what has not come up for a while is shortened, then forgotten.

Each entry knows when it last came up (``memory_tidy.seen_at``): when it was learned or corrected,
when the user talked about it again (``memory_learn``), when it was put back. Once a day:

- an entry about what the user is up to (近况与计划) that has not come up for ``_DAYS`` [0] days is
  shortened to its gist; after [1] days it is removed. Interests (喜好与习惯) and the companion's own
  notes (MEMORY.md) take longer;
- who the user is (基本信息、身边的人、相处方式) and lines the user wrote never fade;
- every change goes into the change log, where the 记忆 page can undo it (and the line starts afresh).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any, Optional

from .memory import _LOCK, _TARGETS, _entries, _store, is_pinned
from .memory_tidy import (_CHANGES_KEPT, _META_LOCK, _failed, _only_source_words, _read_meta, _write_meta,
                          entry_key, seen_at)

logger = logging.getLogger(__name__)

_DAY_S = 24 * 3600
# (shortened after, forgotten after) days without coming up, by section; "笔记" is MEMORY.md.
_DAYS = {"近况与计划": (21, 45), "喜好与习惯": (45, 90), "": (30, 60), "笔记": (30, 60)}
# Who the user is: kept until a conversation says otherwise (memory_learn's update/remove).
_KEPT = ("基本信息", "身边的人", "相处方式")
_EVERY_S = 20 * 3600
_MAX_PER_PASS = 8
_SHORT_ENOUGH = 24
_SECTION = re.compile(r"^【([^】]+)】")
_RUNNING = threading.Lock()

_PROMPT = """下面是你对用户的一些记忆，都很久没再提起了。把每条压缩成更短的一句，只留最要紧的一点（以后用户再提起时你还能想起来就行）。
规则：只能用原句里已有的字词，不加新内容，不改意思；每条至少短三分之一。
只输出一个 JSON：{"short":[{"id":编号,"text":"……"}]}"""


def _policy(target: str, entry: str) -> Optional[tuple[int, int]]:
    if target == "memory":
        return _DAYS["笔记"]
    match = _SECTION.match(entry)
    section = match.group(1) if match else ""
    return None if section in _KEPT else _DAYS.get(section, _DAYS[""])


def plan(entries: dict[str, list[str]], meta: dict[str, Any], now: float) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, int]]]:
    """``(to shorten, to forget)`` as ``(target, entry, idle days)``, the longest idle first. An entry
    never seen before starts its clock now (recorded in ``meta``)."""
    protected = set(meta["user_written"])
    faded = set(meta["faded"])
    target_files = {target: file for file, (target, _) in _TARGETS.items()}
    shorten, forget = [], []
    for target, items in entries.items():
        file = target_files.get(target, "")
        for entry in items:
            since = seen_at(meta, entry)
            if since is None:
                meta["seen_at"][entry_key(entry)] = since = now
            policy = _policy(target, entry)
            if policy is None or entry in protected or (file and is_pinned(file, entry)):
                continue
            idle = int((now - since) // _DAY_S)
            if idle >= policy[1]:
                forget.append((target, entry, idle))
            elif idle >= policy[0] and entry_key(entry) not in faded:
                shorten.append((target, entry, idle))
    order = lambda item: -item[2]  # noqa: E731
    return sorted(shorten, key=order)[:_MAX_PER_PASS], sorted(forget, key=order)[:_MAX_PER_PASS]


def shortened(entry: str, text: Any) -> Optional[str]:
    """The model's shorter line for ``entry`` when it is allowed: its section kept, at most two
    thirds as long, only words of the original."""
    match = _SECTION.match(entry)
    prefix = match.group(0) if match else ""
    body = entry[len(prefix):]
    short = _SECTION.sub("", " ".join(str(text or "").split()))
    if len(short) < 4 or len(short) * 3 > len(body) * 2 or not _only_source_words(short, [body]):
        return None
    return prefix + short


def _ask_short_checked(entries: list[str]) -> tuple[dict[int, Any], bool]:
    try:
        from .llm import ask_json

        lines = "\n".join(f"{i}. {' '.join(e.split())}" for i, e in enumerate(entries, 1))
        proposal = ask_json(_PROMPT, lines, timeout=90)
    except Exception as exc:  # no model now: they are shortened at the next pass
        logger.info("companion: memory fading has no model (%s)", type(exc).__name__)
        return {}, False
    return {item["id"]: item.get("text") for item in proposal.get("short") or []
            if isinstance(item, dict) and isinstance(item.get("id"), int)}, True


def _ask_short(entries: list[str]) -> dict[int, Any]:
    answers, _ = _ask_short_checked(entries)
    return answers


def fade(force: bool = False) -> dict[str, Any]:
    """Once a day (or when ``force``): shorten and forget what has not come up for long."""
    if not _RUNNING.acquire(blocking=False):
        return {"faded": 0, "forgot": 0, "running": True}
    try:
        now = time.time()
        with _META_LOCK:
            meta = _read_meta()
            if not force and now - meta["fade_pass_at"] < _EVERY_S:
                return {"faded": 0, "forgot": 0}
            store = _store()
            entries = {target: [e for e in _entries(store, target) if e.strip()] for target, _ in _TARGETS.values()}
            to_shorten, to_forget = plan(entries, meta, now)
            _write_meta(meta)  # the clocks started for lines seen for the first time
        files = {target: file for file, (target, _) in _TARGETS.items()}
        long_ones = [(t, e, d) for t, e, d in to_shorten if len(_SECTION.sub("", e)) > _SHORT_ENOUGH]
        if long_ones:
            if _ask_short is not _DEFAULT_ASK_SHORT:
                # Preserve compatibility with tests that monkeypatch ``memory_fade._ask_short``.
                answers, short_ok = _ask_short([e for _, e, _ in long_ones]), True
            else:
                answers, short_ok = _ask_short_checked([e for _, e, _ in long_ones])
        else:
            answers, short_ok = {}, True
        applied, newly_faded = [], []
        for index, (target, entry, idle) in enumerate(long_ones, 1):
            short = shortened(entry, answers.get(index))
            if short is None:
                continue
            with _LOCK:
                error = _failed(_store().apply_batch(target, [
                    {"action": "replace", "old_text": entry, "matched_entry": entry, "content": short}]))
            if error:
                logger.info("companion: memory fading skipped a line: %s", error)
                continue
            applied.append({"id": uuid.uuid4().hex[:10], "at": now, "file": files[target], "kind": "fade",
                            "before": [entry], "after": short, "why": f"{idle} 天没再提起，记个大概"})
            newly_faded.append((entry, short))
        # Short lines need no shortening: they only count as faded, and are forgotten in time.
        newly_faded += [(e, e) for _, e, _ in to_shorten if len(_SECTION.sub("", e)) <= _SHORT_ENOUGH]
        for target, entry, idle in to_forget:
            with _LOCK:
                error = _failed(_store().apply_batch(target, [
                    {"action": "remove", "old_text": entry, "matched_entry": entry}]))
            if error:
                logger.info("companion: memory fading could not forget a line: %s", error)
                continue
            applied.append({"id": uuid.uuid4().hex[:10], "at": now, "file": files[target], "kind": "forget",
                            "before": [entry], "after": None, "why": f"{idle} 天没再提起"})
        with _META_LOCK:
            meta = _read_meta()
            faded = set(meta["faded"])
            for before, after in newly_faded:
                # The shorter line keeps the old one's clock: it fades on towards being forgotten.
                meta["seen_at"][entry_key(after)] = seen_at(meta, before) or now
                faded.add(entry_key(after))
            live = {entry_key(e) for items in (
                [e for e in _entries(_store(), target) if e.strip()] for target, _ in _TARGETS.values()) for e in items}
            meta["seen_at"] = {k: v for k, v in meta["seen_at"].items() if k in live}
            meta["faded"] = sorted(faded & live)
            if short_ok or applied or newly_faded:
                meta["fade_pass_at"] = now
            meta["changes"] = (applied[::-1] + meta["changes"])[:_CHANGES_KEPT]
            _write_meta(meta)
        faded_count = sum(1 for c in applied if c["kind"] == "fade")
        if applied:
            logger.info("companion: memory faded %d line(s), forgot %d", faded_count, len(applied) - faded_count)
        return {"faded": faded_count, "forgot": len(applied) - faded_count}
    finally:
        _RUNNING.release()


_DEFAULT_ASK_SHORT = _ask_short


def freshness(meta: dict[str, Any], entry: str, now: float) -> tuple[int, bool]:
    """Days since ``entry`` last came up, and whether it is fading (for the feed's topics)."""
    since = seen_at(meta, entry)
    return (int((now - since) // _DAY_S) if since else 0), entry_key(entry) in set(meta["faded"])
