"""Tidying what the companion remembers: merge duplicates, drop what is outdated, every change undoable.

Hermes's memory only grows (until it hits its size limit). After every eight new
entries, or weekly, the model proposes merges and drops; this code decides what
is allowed, so a bad proposal cannot rewrite memory:

- entries the user wrote (「记住」 in the app) are never merged or dropped;
- a merged entry may only use wording from the entries it replaces;
- at most a fifth of the entries (at least two) change in one pass.

Each change is logged with the text it replaced, and the memory page can undo it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .fs_atomic import atomic_write_json
from .memory import _LOCK, _TARGETS, _entries, _store, is_pinned
from .paths import get_webui_dir

logger = logging.getLogger(__name__)

NEW_ENTRIES_DUE = 8
DAYS_DUE = 7
_DAY_S = 24 * 3600
_CHANGES_KEPT = 50
_META_LOCK = threading.Lock()
_RUNNING = threading.Lock()

_PROMPT = """你在整理一个私人助理对用户的长期记忆。下面每行是一条记忆，前面是编号。
找出可以合并的重复或高度重叠的条目，以及已经过时（被更新的条目推翻）或没有用处的条目。

规则：
- 合并：只合并说的是同一件事的条目；合并后的文字只能用原条目里已有的词句，不能补充、推测或改写含义。
- 删除：只删明显过时（后面有更新的说法）或重复的条目；拿不准就不动。
- 标了【用户写的】的条目不能合并也不能删除。
- 大多数时候不需要改动，没有就返回空列表。

只输出一个 JSON：{"merge":[{"ids":[编号,…],"text":"合并后的一条"}],"drop":[{"id":编号,"why":"原因"}]}"""


def _meta_path() -> Path:
    return get_webui_dir() / "memory-tidy.json"


def _read_meta() -> dict[str, Any]:
    try:
        data = json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("user_written", [])
    data.setdefault("seen", [])
    data.setdefault("last_tidy_at", 0)
    data.setdefault("changes", [])
    # When each entry last came up (by ``entry_key``), and which have been shortened (memory_fade).
    data.setdefault("seen_at", {})
    data.setdefault("faded", [])
    data.setdefault("fade_pass_at", 0)
    return data


def entry_key(entry: str) -> str:
    return hashlib.sha1(" ".join(entry.split()).encode("utf-8")).hexdigest()[:12]


def mark_seen(meta: dict[str, Any], entries: Any, at: float) -> None:
    """These entries came up now (learned, corrected, talked about again, put back): their clock
    starts over and a shortened one counts as fresh again."""
    faded = set(meta["faded"])
    for entry in entries:
        key = entry_key(entry)
        meta["seen_at"][key] = max(at, meta["seen_at"].get(key, 0))
        faded.discard(key)
    meta["faded"] = sorted(faded)


def seen_at(meta: dict[str, Any], entry: str) -> Optional[float]:
    """When ``entry`` last came up: its own record, else the last logged change that wrote it."""
    key = entry_key(entry)
    if key in meta["seen_at"]:
        return meta["seen_at"][key]
    return next((c["at"] for c in meta["changes"] if c.get("after") == entry and not c.get("undone")), None)


def _write_meta(data: dict[str, Any]) -> None:
    atomic_write_json(_meta_path(), data)


def note_user_written(entry: str) -> None:
    """The user added this line themselves: tidying never touches it."""
    with _META_LOCK:
        meta = _read_meta()
        if entry not in meta["user_written"]:
            meta["user_written"].append(entry)
            _write_meta(meta)


def drop_user_written(entries: Any) -> None:
    """Forget protection flags for entries the user deleted."""
    gone = {str(e) for e in entries if e}
    if not gone:
        return
    with _META_LOCK:
        meta = _read_meta()
        kept = [e for e in meta["user_written"] if e not in gone and e.strip() not in gone]
        if len(kept) != len(meta["user_written"]):
            meta["user_written"] = kept
            _write_meta(meta)


def _all_entries() -> dict[str, list[str]]:
    store = _store()
    return {target: [e for e in _entries(store, target) if e.strip()] for target, _ in _TARGETS.values()}


def due() -> bool:
    meta = _read_meta()
    entries = [e for items in _all_entries().values() for e in items]
    if len(entries) < 2:
        return False
    seen = set(meta["seen"])
    new = sum(1 for entry in entries if entry not in seen)
    if not meta["last_tidy_at"]:
        return new >= NEW_ENTRIES_DUE
    return new >= NEW_ENTRIES_DUE or (new > 0 and time.time() - meta["last_tidy_at"] >= DAYS_DUE * _DAY_S)


# ── checking a proposal ───────────────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[A-Za-z0-9]+|[㐀-鿿]")


def _only_source_words(text: str, sources: list[str]) -> bool:
    """Every word (Latin) or character (CJK) of ``text`` already appears in ``sources``."""
    pool = {token.lower() for source in sources for token in _WORD.findall(source)}
    return all(token.lower() in pool for token in _WORD.findall(text))


def _plan(
    entries: list[str],
    proposal: dict[str, Any],
    protected: set[str],
    *,
    file: Optional[str] = None,
) -> list[dict[str, Any]]:
    """The allowed changes, in order, within this pass's budget."""
    budget = max(2, len(entries) // 5)
    used: set[int] = set()
    changes: list[dict[str, Any]] = []
    check_files = (file,) if file else tuple(_TARGETS)

    def usable(index: Any) -> bool:
        if not (isinstance(index, int) and 1 <= index <= len(entries) and index not in used):
            return False
        entry = entries[index - 1]
        if entry in protected:
            return False
        return not any(is_pinned(f, entry) for f in check_files)

    for merge in proposal.get("merge") or []:
        if not isinstance(merge, dict):
            continue
        ids, text = merge.get("ids"), " ".join(str(merge.get("text") or "").split())
        if not isinstance(ids, list) or len(set(ids)) < 2 or not all(usable(i) for i in ids):
            continue
        ids = sorted(set(ids))
        before = [entries[i - 1] for i in ids]
        if not text or len(text) > sum(len(b) for b in before) or not _only_source_words(text, before):
            continue
        if len(used) + len(ids) > budget:
            break
        used.update(ids)
        changes.append({"kind": "merge", "before": before, "after": text})
    for drop in proposal.get("drop") or []:
        index = drop.get("id") if isinstance(drop, dict) else None
        if not usable(index) or len(used) + 1 > budget:
            continue
        used.add(index)
        changes.append({"kind": "drop", "before": [entries[index - 1]], "after": None,
                        "why": str(drop.get("why") or "")[:80]})
    return changes


def _ops(change: dict[str, Any]) -> list[dict[str, Any]]:
    before, after = change["before"], change["after"]
    if after is None:
        return [{"action": "remove", "old_text": before[0], "matched_entry": before[0]}]
    first, rest = before[0], before[1:]
    return [{"action": "replace", "old_text": first, "matched_entry": first, "content": after},
            *({"action": "remove", "old_text": entry, "matched_entry": entry} for entry in rest)]


def _failed(result: dict[str, Any]) -> Optional[str]:
    if result.get("success") is False or result.get("error"):
        return str(result.get("error") or result.get("message") or "没有改成")
    return None


# ── one pass ──────────────────────────────────────────────────────────────────────────────────────

def _propose(entries: list[str], protected: set[str]) -> dict[str, Any]:
    from .llm import ask

    lines = "\n".join(f"{i}. {'【用户写的】' if e in protected else ''}{' '.join(e.split())}"
                      for i, e in enumerate(entries, 1))
    raw = ask(_PROMPT, lines, timeout=90)
    match = re.search(r"\{[\s\S]*\}", raw)
    try:
        proposal = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return proposal if isinstance(proposal, dict) else {}


def tidy(force: bool = False) -> dict[str, Any]:
    """Run one pass when due (or when ``force``); returns how many changes were applied."""
    if not _RUNNING.acquire(blocking=False):
        return {"changed": 0, "running": True}
    try:
        if not force and not due():
            return {"changed": 0}
        protected = set(_read_meta()["user_written"])
        applied = []
        failed_targets: set[str] = set()
        for file, (target, _) in _TARGETS.items():
            entries = _all_entries()[target]
            if len(entries) < 2:
                continue
            file_protected = protected | {e for e in entries if is_pinned(file, e)}
            try:
                proposal = _propose(entries, file_protected)
            except Exception as exc:  # no model right now: try again at the next due time
                logger.info("companion: memory tidy has no model (%s)", type(exc).__name__)
                failed_targets.add(target)
                continue
            for change in _plan(entries, proposal, file_protected, file=file):
                with _LOCK:
                    error = _failed(_store().apply_batch(target, _ops(change)))
                if error:
                    logger.info("companion: memory tidy skipped a change: %s", error)
                    continue
                applied.append({"id": uuid.uuid4().hex[:10], "at": time.time(), "file": file, **change})
        if failed_targets and not applied:
            return {"changed": 0}
        with _META_LOCK:
            meta = _read_meta()
            current_by_target = _all_entries()
            prev_seen = set(meta["seen"])
            if not failed_targets:
                meta["last_tidy_at"] = time.time()
                meta["seen"] = [e for items in current_by_target.values() for e in items]
            else:
                seen_out: list[str] = []
                for target, items in current_by_target.items():
                    if target in failed_targets:
                        seen_out.extend(e for e in items if e in prev_seen)
                    else:
                        seen_out.extend(items)
                meta["seen"] = seen_out
            for change in applied:
                if change["after"] is not None:
                    # A merged line came up when the newest of the lines it replaces did.
                    times = [t for t in (seen_at(meta, entry) for entry in change["before"]) if t]
                    if times:
                        mark_seen(meta, [change["after"]], max(times))
            meta["changes"] = (applied[::-1] + meta["changes"])[:_CHANGES_KEPT]
            _write_meta(meta)
        if applied:
            logger.info("companion: memory tidy applied %d change(s)", len(applied))
        return {"changed": len(applied)}
    finally:
        _RUNNING.release()


def maybe_tidy_soon() -> None:
    """After a turn: tidy in the background when due (cheap check, never blocks the reply)."""

    def run() -> None:
        try:
            tidy()
        except Exception:
            logger.exception("companion: memory tidy failed")

    asyncio.get_running_loop().run_in_executor(None, run)


def recent_changes() -> list[dict[str, Any]]:
    return _read_meta()["changes"][:20]


def undo(change_id: str) -> None:
    """Put back what a change replaced (and remove the merged line it wrote)."""
    with _META_LOCK:
        meta = _read_meta()
        change = next((c for c in meta["changes"] if c.get("id") == change_id), None)
        if change is None or change.get("undone"):
            raise ValueError("这条改动已经撤销过了")
        target = _TARGETS[change["file"]][0]
        with _LOCK:
            current = _entries(_store(), target)
            ops = []
            if change["after"] is not None and change["after"] in current:
                ops.append({"action": "remove", "old_text": change["after"], "matched_entry": change["after"]})
            ops += [{"action": "add", "content": entry} for entry in change["before"] if entry not in current]
            error = _failed(_store().apply_batch(target, ops)) if ops else None
        if error:
            raise ValueError(error)
        change["undone"] = True
        # The restored lines are not new: they should not make the next pass due.
        meta["seen"] = list({*meta["seen"], *change["before"]})
        # Put back on purpose: they start afresh instead of fading again tomorrow.
        mark_seen(meta, change["before"], time.time())
        _write_meta(meta)
