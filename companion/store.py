"""Chats and their transcripts, in the companion web app's own event format.

Each chat is a WebSocket ``chat_id``; its session key is ``websocket:<chat_id>``
(the key the web app was built around). A transcript is the list of events the
app replays to rebuild a thread: ``user_message``, ``message``, ``turn_end``,
exactly the shapes it receives live (nanobot's ``webui-thread`` projection).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SESSION_PREFIX = "websocket:"
# Event types the app's history replay understands; it rejects a whole thread over one other line.
REPLAY_EVENTS = {"user_message", "message", "file_edit", "delta", "reasoning_delta", "stream_end",
                 "reasoning_end", "context_compaction", "turn_end"}
TRANSCRIPT_SCHEMA_VERSION = 1
# What a replayed tool step keeps of a long script, a file it wrote or a result: the apps show a
# line about each step, not its code, and half of a long chat's history was scripts and results.
# The turn that may still be at work keeps everything (its document is shown as it is written).
_ARGUMENT_CHARS = 400
_RESULT_CHARS = 300


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _revision(events: list[dict[str, Any]], seq: int = 0) -> str:
    """Which copy of the chat this is: length, newest timestamp, mutation counter, and content digest."""
    last_ms = events[-1].get("created_at_ms", 0) if events else 0
    hasher = hashlib.blake2s(digest_size=4)
    for event in events:
        piece = f"{event.get('event')}|{event.get('turn_id')}|{event.get('created_at_ms')}|{event.get('text')}\n"
        hasher.update(piece.encode("utf-8", errors="replace"))
    return f"{len(events)}.{last_ms}.{seq}.{hasher.hexdigest()}"


def _latest_preview(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("event") not in ("user_message", "message"):
            continue
        if (event.get("source") or {}).get("kind") == "notice":
            continue
        text = str(event.get("text") or "").strip()
        if text:
            return text.replace("\n", " ")[:120]
    return ""


def _brief(value: Any, limit: int) -> Any:
    return value[:limit] + "…" if isinstance(value, str) and len(value) > limit else value


def _lean(event: dict[str, Any]) -> dict[str, Any]:
    tools = event.get("tool_events")
    if not isinstance(tools, list):
        return event
    lean = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") == "memory":  # 已记住 reads its result whole
            lean.append(tool)
            continue
        tool = dict(tool)
        if isinstance(tool.get("arguments"), dict):
            tool["arguments"] = {key: _brief(value, _ARGUMENT_CHARS) for key, value in tool["arguments"].items()}
        if "result" in tool:
            tool["result"] = _brief(tool["result"], _RESULT_CHARS)
        lean.append(tool)
    return {**event, "tool_events": lean}


def session_key(chat_id: str) -> str:
    return f"{SESSION_PREFIX}{chat_id}"


def chat_id_of(key: str) -> str | None:
    return key[len(SESSION_PREFIX):] if key.startswith(SESSION_PREFIX) else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._index = root / "chats.json"
        self._transcripts = root / "transcripts"
        self._lock = threading.Lock()
        self._transcripts.mkdir(parents=True, exist_ok=True)

    # -- chats -----------------------------------------------------------------

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_index(self, data: dict[str, dict[str, Any]]) -> None:
        _atomic_write_text(self._index, json.dumps(data, ensure_ascii=False, indent=2))

    def create(self) -> str:
        chat_id = str(uuid.uuid4())
        with self._lock:
            index = self._read_index()
            now = _now_iso()
            index[chat_id] = {"created_at": now, "updated_at": now, "title": "", "preview": ""}
            self._write_index(index)
        return chat_id

    def exists(self, chat_id: str) -> bool:
        return chat_id in self._read_index()

    def ensure(self, chat_id: str) -> None:
        with self._lock:
            index = self._read_index()
            if chat_id not in index:
                now = _now_iso()
                entry: dict[str, Any] = {"created_at": now, "updated_at": now, "title": "", "preview": ""}
                if chat_id.startswith("cron:"):
                    entry["cron"] = True
                index[chat_id] = entry
                self._write_index(index)

    def list(self) -> list[dict[str, Any]]:
        rows = [
            {
                "key": session_key(chat_id),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "title": meta.get("title") or "",
                "preview": meta.get("preview") or "",
                # A helper's working chat (see modules.helpers): not one of the user's side chats.
                **({"helper": meta["helper"]} if meta.get("helper") else {}),
                **({"group": True} if meta.get("group") else {}),
                **({"forked_from": meta["forked_from"]} if meta.get("forked_from") else {}),
            }
            for chat_id, meta in self._read_index().items()
            if not meta.get("desk") and not meta.get("cron") and not chat_id.startswith("cron:")
        ]
        rows.sort(key=lambda row: row["updated_at"] or "", reverse=True)
        return rows

    def clone_chat(
        self,
        src_chat_id: str,
        events: list[dict[str, Any]],
        *,
        title: Optional[str] = None,
        forked_from: Optional[dict[str, Any]] = None,
    ) -> str:
        """Atomically create a new side chat pre-populated with ``events`` cloned from ``src_chat_id``."""
        new_chat_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        stamped = [
            {**e, "chat_id": new_chat_id, "created_at_ms": e.get("created_at_ms") or now_ms}
            for e in events
        ]
        with self._lock:
            index = self._read_index()
            src_meta = index.get(src_chat_id) or {}
            clean_title = " ".join((title or "").split())[:30]
            if not clean_title:
                orig_title = str(src_meta.get("title") or "").strip()
                clean_title = f"{(orig_title or '对话')[:10]} · 分叉"
            _atomic_write_text(
                self._path(new_chat_id),
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in stamped),
            )
            now = _now_iso()
            entry: dict[str, Any] = {
                "created_at": now,
                "updated_at": now,
                "title": clean_title,
                "preview": _latest_preview(stamped),
                "rev": 1,
            }
            if forked_from is not None:
                entry["forked_from"] = dict(forked_from)
            index[new_chat_id] = entry
            self._write_index(index)
        return new_chat_id

    def replace_events(self, chat_id: str, events: list[dict[str, Any]]) -> None:
        """Atomically replace ``chat_id``'s transcript events and bump revision / ``removed_at_ms``."""
        now_ms = int(time.time() * 1000)
        stamped = [
            {**e, "chat_id": e.get("chat_id") or chat_id, "created_at_ms": e.get("created_at_ms") or now_ms}
            for e in events
        ]
        with self._lock:
            path = self._path(chat_id)
            prev_max_ms = 0
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    with contextlib.suppress(ValueError):
                        prev_max_ms = max(prev_max_ms, int(json.loads(line).get("created_at_ms") or 0))
            _atomic_write_text(
                path,
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in stamped),
            )
            index = self._read_index()
            if chat_id in index:
                meta = index[chat_id]
                meta["updated_at"] = _now_iso()
                meta["preview"] = _latest_preview(stamped)
                meta["rev"] = int(meta.get("rev") or 0) + 1
                meta["removed_at_ms"] = max(now_ms, prev_max_ms + 1)
                self._write_index(index)

    def _touch(self, chat_id: str, preview: str | None, *, bump_rev: bool = True) -> None:
        index = self._read_index()
        meta = index.setdefault(chat_id, {"created_at": _now_iso(), "title": ""})
        if chat_id.startswith("cron:"):
            meta["cron"] = True
        meta["updated_at"] = _now_iso()
        if bump_rev:
            meta["rev"] = int(meta.get("rev") or 0) + 1
        if preview:
            meta["preview"] = preview.strip().replace("\n", " ")[:120]
        self._write_index(index)

    def title(self, chat_id: str) -> str:
        return str((self._read_index().get(chat_id) or {}).get("title") or "")

    def set_title(self, chat_id: str, title: str) -> None:
        with self._lock:
            index = self._read_index()
            if chat_id in index:
                index[chat_id]["title"] = " ".join(title.split())[:30]
                self._write_index(index)

    def meta(self, chat_id: str) -> dict[str, Any]:
        return dict(self._read_index().get(chat_id) or {})

    def update_meta(self, chat_id: str, **changes: Any) -> None:
        """Set (or, with None, drop) fields of a chat's entry (``helper``: whose working chat it is)."""
        with self._lock:
            index = self._read_index()
            if chat_id not in index:
                return
            for key, value in changes.items():
                if value is None:
                    index[chat_id].pop(key, None)
                else:
                    index[chat_id][key] = value
            self._write_index(index)

    def delete(self, chat_id: str) -> None:
        """A side chat the user removed: its entry and transcript go (the agent's own session stays)."""
        with self._lock:
            index = self._read_index()
            index.pop(chat_id, None)
            self._write_index(index)
            path = self._path(chat_id)
            if path.is_file():
                path.unlink()

    # -- transcripts -----------------------------------------------------------

    def _path(self, chat_id: str) -> Path:
        if not chat_id or "/" in chat_id or "\\" in chat_id or ".." in chat_id:
            raise ValueError("invalid chat id")
        return self._transcripts / f"{chat_id}.jsonl"

    def append(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Persist one replayable event (stamped with ``created_at_ms``) and return it."""
        stamped = {**event, "created_at_ms": event.get("created_at_ms") or int(time.time() * 1000)}
        with self._lock:
            with self._path(chat_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stamped, ensure_ascii=False) + "\n")
            if event.get("event") in ("user_message", "message"):
                is_notice = (event.get("source") or {}).get("kind") == "notice"
                self._touch(chat_id, None if is_notice else event.get("text"))
            else:
                index = self._read_index()
                if chat_id in index:
                    index[chat_id]["rev"] = int(index[chat_id].get("rev") or 0) + 1
                    self._write_index(index)
        return stamped

    def remove_events(self, chat_id: str, gone: Any) -> int:
        """Take the events ``gone(event)`` picks out of a chat's transcript (one message the user
        deleted); how many went."""
        with self._lock:
            path = self._path(chat_id)
            if not path.is_file():
                return 0
            events: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            keep = [e for e in events if not gone(e)]
            removed = len(events) - len(keep)
            if removed:
                _atomic_write_text(path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep))
                index = self._read_index()
                if chat_id in index:
                    meta = index[chat_id]
                    meta["updated_at"] = _now_iso()
                    meta["preview"] = _latest_preview(keep)
                    meta["rev"] = int(meta.get("rev") or 0) + 1
                    max_ms = max((int(e.get("created_at_ms") or 0) for e in events), default=0)
                    meta["removed_at_ms"] = max(int(time.time() * 1000), max_ms + 1)
                    self._write_index(index)
            return removed

    def events(self, chat_id: str) -> list[dict[str, Any]]:
        path = self._path(chat_id)
        if not path.is_file():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def thread_payload(self, chat_id: str, known: Optional[int] = None, last: Optional[int] = None) -> dict[str, Any]:
        """The chat's events. With ``known``/``last`` (how many events a phone already keeps, and the
        newest one's ``created_at_ms``): when its copy still matches, only the events after it
        (``incremental``); otherwise everything, so a deleted or changed message is never missed."""
        meta = self._read_index().get(chat_id) or {}
        seq = int(meta.get("rev") or 0)
        removed_at_ms = int(meta.get("removed_at_ms") or 0)
        # Activity saved outside any turn (``turn_id: null``) fails the app's check for the whole thread.
        events = [e for e in self.events(chat_id) if e.get("event") in REPLAY_EVENTS and isinstance(e.get("chat_id"), str)
                  and ("turn_id" not in e or isinstance(e["turn_id"], str))]
        completed = [e["turn_id"] for e in events if e.get("event") == "turn_end" and e.get("turn_id")]
        newest = next((e.get("turn_id") for e in reversed(events) if e.get("turn_id")), None)
        events = [e if e.get("turn_id") == newest else _lean(e) for e in events]
        rev = _revision(events, seq)
        if (
            known is not None
            and last is not None
            and 0 < known <= len(events)
            and events[known - 1].get("created_at_ms") == last
            and last >= removed_at_ms
        ):
            return {
                "schemaVersion": TRANSCRIPT_SCHEMA_VERSION,
                "sessionKey": session_key(chat_id),
                "projection": "events",
                "events": events[known:],
                "incremental": True,
                "completed_turn_ids": completed,
                "revision": rev,
            }
        return {
            "schemaVersion": TRANSCRIPT_SCHEMA_VERSION,
            "sessionKey": session_key(chat_id),
            "projection": "events",
            "events": events,
            "completed_turn_ids": completed,
            "revision": rev,
        }
