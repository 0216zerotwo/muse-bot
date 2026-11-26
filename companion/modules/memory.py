"""What the companion remembers, as items the user can review, remove or add (ported from nanobot).

Long-term memory is Hermes's own: ``memories/USER.md`` (about the user) and
``memories/MEMORY.md`` (the companion's notes), one entry per item. The agent
reads them at the start of each conversation and keeps them with its memory
tool; this page goes through the same store, so the size limits and the safety
scan on new entries apply to what the user adds here too.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .fs_atomic import atomic_write_json
from .paths import get_webui_dir

# file -> (Hermes memory target, section title in the app)
_TARGETS = {"USER.md": ("user", "关于你"), "MEMORY.md": ("memory", "我记下的")}
_MAX_REMEMBER_CHARS = 500
_LOCK = threading.Lock()
_PIN_LOCK = threading.RLock()


def _pins_path() -> Path:
    return get_webui_dir() / "memory_pins.json"


def _norm_pin_entry(entry: str) -> str:
    return " ".join(str(entry or "").split())


def _read_pins() -> dict[str, list[str]]:
    try:
        raw = json.loads(_pins_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, list[str]] = {}
    for file in _TARGETS:
        items = raw.get(file)
        if isinstance(items, list):
            out[file] = [str(x) for x in items if isinstance(x, str) and x.strip()]
        else:
            out[file] = []
    return out


def _write_pins(data: dict[str, list[str]]) -> None:
    atomic_write_json(_pins_path(), {file: data.get(file, []) for file in _TARGETS})


def is_pinned(file: str, entry: str) -> bool:
    """True when ``entry`` in ``file`` is pinned by the user and protected from tidy/fade."""
    if file not in _TARGETS or not isinstance(entry, str) or not entry.strip():
        return False
    norm = _norm_pin_entry(entry)
    stripped = entry.strip()
    with _PIN_LOCK:
        saved = _read_pins().get(file, [])
    return any(item == entry or item.strip() == stripped or _norm_pin_entry(item) == norm for item in saved)


def _drop_pins(file: str, entries: list[str]) -> None:
    if file not in _TARGETS:
        return
    gone_raw = {str(e) for e in entries if e}
    gone_norm = {_norm_pin_entry(e) for e in gone_raw if e.strip()}
    if not gone_norm:
        return
    with _PIN_LOCK:
        pins = _read_pins()
        current = pins.get(file, [])
        kept = [
            item
            for item in current
            if item not in gone_raw and item.strip() not in gone_raw and _norm_pin_entry(item) not in gone_norm
        ]
        if len(kept) != len(current):
            pins[file] = kept
            _write_pins(pins)


def pin_memory(file: str, line: str, pinned: bool | None = None) -> dict[str, Any]:
    """Pin, unpin, or toggle ``line`` in ``file``, persisted atomically in ``memory_pins.json``."""
    if file not in _TARGETS:
        raise ValueError("不支持这个记忆文件")
    if not isinstance(line, str) or not line.strip():
        raise ValueError("缺少记忆条目内容")
    target = _TARGETS[file][0]
    norm_line = _norm_pin_entry(line)
    with _LOCK:
        entries = _entries(_store(), target)
    canonical = next(
        (
            e
            for e in entries
            if e == line or e.strip() == line.strip() or _norm_pin_entry(e) == norm_line
        ),
        None,
    )
    if canonical is None:
        if pinned is False and is_pinned(file, line):
            _drop_pins(file, [line, line.strip()])
            return {**read_memory(), "pinned": False}
        raise ValueError("这条记忆已经不在了，刷新一下再试")

    with _PIN_LOCK:
        pins = _read_pins()
        current = pins.get(file, [])
        already = any(
            item == canonical or item.strip() == canonical.strip() or _norm_pin_entry(item) == norm_line
            for item in current
        )
        next_pinned = (not already) if pinned is None else bool(pinned)
        filtered = [
            item
            for item in current
            if item != canonical and item.strip() != canonical.strip() and _norm_pin_entry(item) != norm_line
        ]
        if next_pinned:
            filtered.append(canonical)
        pins[file] = filtered
        _write_pins(pins)
    return {**read_memory(), "pinned": next_pinned}


def _store():
    from muse.tools.memory_store import load_on_disk_store

    store = load_on_disk_store()
    store.load_from_disk()
    return store


def _entries(store: Any, target: str) -> list[str]:
    if callable(getattr(store, "entries_for", None)):
        return list(store.entries_for(target))
    return list(store._entries_for(target))


def _item_id(file: str, entry: str) -> str:
    return hashlib.sha1(f"{file}\n{entry}".encode()).hexdigest()[:12]


def read_memory() -> dict[str, Any]:
    store = _store()
    sections = []
    for file, (target, title) in _TARGETS.items():
        items = [
            {
                "id": _item_id(file, entry),
                "file": file,
                "line": entry,
                "text": " ".join(entry.split()),
                "pinned": is_pinned(file, entry),
            }
            for entry in _entries(store, target) if entry.strip()
        ]
        if items:
            sections.append({"file": file, "title": title, "items": items})
    from .memory_learn import last_learned
    from .memory_tidy import recent_changes

    return {"sections": sections, "changes": recent_changes(), "learned_at": last_learned()}


def _failed(result: dict[str, Any]) -> str | None:
    if result.get("success") is False or result.get("error"):
        return str(result.get("error") or result.get("message") or "没有改成")
    return None


def forget_memory(file: str, line: str) -> dict[str, Any]:
    """Remove one exact remembered entry. Raises ``ValueError`` if it is gone."""
    if file not in _TARGETS:
        raise ValueError("不支持这个记忆文件")
    target = _TARGETS[file][0]
    with _LOCK:
        store = _store()
        if line not in _entries(store, target):
            raise ValueError("这条记忆已经不在了，刷新一下再试")
        error = _failed(store.remove(target, line, matched_entry=line))
    if error:
        raise ValueError(error)
    from .memory_tidy import drop_user_written

    drop_user_written([line, line.strip()])
    _drop_pins(file, [line, line.strip()])
    return read_memory()


def remember(text: str) -> dict[str, Any]:
    """Keep something the user explicitly asked the companion to remember (about them)."""
    fact = " ".join(text.split())
    if not fact or len(fact) > _MAX_REMEMBER_CHARS:
        raise ValueError(f"记忆内容要在 1~{_MAX_REMEMBER_CHARS} 字之间")
    with _LOCK:
        error = _failed(_store().add("user", fact))
    if error:
        raise ValueError(error)
    from .memory_tidy import note_user_written

    note_user_written(fact)
    return read_memory()


def tidy_now() -> dict[str, Any]:
    from .memory_tidy import tidy

    return {**read_memory(), "tidied": tidy(force=True)}


def undo_change(change_id: str) -> dict[str, Any]:
    from .memory_tidy import undo

    undo(change_id)
    return read_memory()


# ── MEMORY.md as one document ─────────────────────────────────────────────────────────────────────
# The app edits memory as text, as Muse's Memory.md: what it knows about the user under a heading
# per section (an entry's 【section】 prefix, see memory_learn.SECTIONS), then its own notes; one
# entry per paragraph. Saving turns the text back into entries and applies the difference through
# Hermes's store, so its size limits and the safety scan on new entries still apply.

_OTHER = "其他"  # about the user, without a section


def _sections() -> tuple[str, ...]:
    from .memory_learn import SECTIONS

    return SECTIONS


def _split(entry: str) -> tuple[str, str]:
    """(section, text) of a USER.md entry."""
    for section in _sections():
        prefix = f"【{section}】"
        if entry.startswith(prefix):
            return section, entry[len(prefix):].strip()
    return _OTHER, entry


def read_memory_doc() -> dict[str, Any]:
    store = _store()
    groups: dict[str, list[str]] = {}
    for entry in _entries(store, "user"):
        if entry.strip():
            section, text = _split(entry.strip())
            groups.setdefault(section, []).append(text)
    parts = [f"## {section}\n\n" + "\n\n".join(groups[section])
             for section in (*_sections(), _OTHER) if groups.get(section)]
    notes = [entry.strip() for entry in _entries(store, "memory") if entry.strip()]
    if notes:
        parts.append(f"## {_TARGETS['MEMORY.md'][1]}\n\n" + "\n\n".join(notes))
    if not parts:
        parts = [f"## {section}\n" for section in _sections()]
    return {"text": "\n\n".join(parts).strip() + "\n", "updated_at": _updated_at()}


def _updated_at() -> int | None:
    """When USER.md or MEMORY.md last changed (ms), for the card's date."""
    from muse.tools.memory_store import get_memory_dir

    times = []
    for name in _TARGETS:
        try:
            times.append((get_memory_dir() / name).stat().st_mtime)
        except OSError:
            pass
    return int(max(times) * 1000) if times else None


def _parse_doc(text: str) -> dict[str, list[str]]:
    """``{file: [entry, …]}``; text before the first heading is about the user."""
    sections: dict[str, list[str]] = {file: [] for file in _TARGETS}
    known = set(_sections())
    current = ("USER.md", "")
    block: list[str] = []

    def flush() -> None:
        entry = "\n".join(block).strip()
        if entry.startswith("- "):
            entry = entry[2:].strip()
        file, prefix = current
        if entry and not (prefix and entry.startswith(prefix)):
            entry = prefix + entry
        if entry and entry not in sections[file]:
            sections[file].append(entry)
        block.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        heading = line.strip()
        if heading.startswith("## "):
            flush()
            title = heading[3:].strip()
            if title == _TARGETS["MEMORY.md"][1]:
                current = ("MEMORY.md", "")
            elif title in known:
                current = ("USER.md", f"【{title}】")
            else:
                current = ("USER.md", "")
        elif not heading:
            flush()
        else:
            block.append(line.rstrip())
    flush()
    return sections


def write_memory_doc(text: str) -> dict[str, Any]:
    """Apply the edited document: removed entries are forgotten, new ones remembered."""
    from .memory_tidy import drop_user_written, note_user_written

    wanted = _parse_doc(text)
    with _LOCK:
        store = _store()
        for file, (target, _) in _TARGETS.items():
            current = [entry for entry in _entries(store, target) if entry.strip()]
            removed = [entry for entry in current if entry.strip() not in wanted[file]]
            ops = [{"action": "remove", "old_text": entry, "matched_entry": entry} for entry in removed]
            existing = {entry.strip() for entry in current}
            added = [entry for entry in wanted[file] if entry not in existing]
            ops += [{"action": "add", "content": entry} for entry in added]
            if not ops:
                continue
            error = _failed(store.apply_batch(target, ops))
            if error:
                raise ValueError(f"{_TARGETS[file][1]}：{error}")
            if removed:
                drop_user_written([*removed, *(e.strip() for e in removed)])
                _drop_pins(file, [*removed, *(e.strip() for e in removed)])
            for entry in added:
                note_user_written(entry)
    return read_memory_doc()
