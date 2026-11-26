"""Conversation branching (Fork Side Chat) and any-turn Edit & Resubmit.

Keeps ``ChatStore`` (``.jsonl`` + ``chats.json``) and ``MuseDB`` (``sessions`` + ``messages``)
strictly synchronized in atomic SQLite transactions without modifying ``muse/db.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
import uuid
from typing import Any, Optional


class ChatBusyError(RuntimeError):
    """Raised when attempting to edit/truncate a chat while a turn is actively running."""

    status = 409

    def __init__(self, message: str = "正在回复中，请先停止当前回复再修改") -> None:
        super().__init__(message)
        self.message = message


class InvalidBranchInputError(ValueError):
    """Raised on malformed or empty branch/edit request parameters (HTTP/WS 400)."""

    status = 400


class BranchTargetNotFoundError(ValueError):
    """Raised when a chat or target ``turn_id`` cannot be found (HTTP/WS 404)."""

    status = 404


_BG_TASKS: set[asyncio.Task[Any]] = set()


def _resolve_engine(adapter: Any) -> Any:
    return getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)


def _resolve_actor(adapter: Any, chat_id: str) -> Any:
    engine = _resolve_engine(adapter)
    actors = getattr(engine, "_actors", None) if engine is not None else None
    return actors.get(chat_id) if isinstance(actors, dict) else None


def _active_turn_id(adapter: Any, chat_id: str) -> Optional[str]:
    turns = getattr(adapter, "_turns", None)
    if isinstance(turns, dict) and turns.get(chat_id):
        return str(turns[chat_id])
    actor = _resolve_actor(adapter, chat_id)
    if actor is not None:
        if getattr(actor, "active_turn_id", None):
            return str(actor.active_turn_id)
        if getattr(actor, "_phase", "idle") in ("llm", "tools"):
            return "__active__"
    return None


@contextlib.contextmanager
def _borrow_db(adapter: Any):
    """Yield an active ``MuseDB`` instance; close on exit only if opened transiently here."""
    engine = _resolve_engine(adapter)
    if engine is not None:
        ensure_fn = getattr(engine, "_ensure_db", None)
        if callable(ensure_fn):
            yield ensure_fn()
            return
        if getattr(engine, "_db", None) is not None:
            yield engine._db
            return
    if getattr(adapter, "db", None) is not None:
        yield adapter.db
        return
    from muse.db import MuseDB

    temp_db = MuseDB()
    try:
        yield temp_db
    finally:
        temp_db.close()


def _json_loads_dict(raw: Any) -> dict[str, Any]:
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sanitize_cloned_extra(raw_extra: Any) -> Optional[str]:
    """Strip ``legacy_key`` when cloning rows so ``idx_messages_legacy_key`` never collides."""
    if not raw_extra:
        return None
    if not isinstance(raw_extra, str):
        return None
    parsed = _json_loads_dict(raw_extra)
    if not parsed:
        return raw_extra
    if "legacy_key" in parsed:
        retired = {k: v for k, v in parsed.items() if k != "legacy_key"}
        retired["forked_legacy_key"] = parsed["legacy_key"]
        return _json_dumps(retired)
    return raw_extra


def _clone_muse_db_session(
    adapter: Any,
    src_chat_id: str,
    new_chat_id: str,
    *,
    turn_id: Optional[str],
    kept_turn_ids: set[str],
    store_truncated: bool,
    exclude_turn_id: Optional[str] = None,
) -> None:
    engine = _resolve_engine(adapter)
    assembler = getattr(engine, "_assembler", None) if engine is not None else None

    with _borrow_db(adapter) as db:
        write_tx = getattr(db, "_write_tx", None)
        if not callable(write_tx):
            return
        with write_tx() as conn:
            src_sess = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND active = 1 ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (src_chat_id,),
            ).fetchone()
            if src_sess is None:
                return

            src_sid = str(src_sess["id"])
            source = str(src_sess["source"] or "companion")
            model = src_sess["model"]
            src_meta = _json_loads_dict(src_sess["metadata_json"])

            soul_snapshot = src_meta.get("soul_snapshot")
            if not isinstance(soul_snapshot, str) and assembler is not None:
                cached_snaps = getattr(assembler, "_soul_snapshots", None)
                if isinstance(cached_snaps, dict) and isinstance(cached_snaps.get(src_sid), str):
                    soul_snapshot = cached_snaps[src_sid]

            new_meta: dict[str, Any] = {
                "forked_from": {
                    "chat_id": src_chat_id,
                    "session_id": src_sid,
                    "turn_id": turn_id or "",
                }
            }
            if isinstance(soul_snapshot, str):
                new_meta["soul_snapshot"] = soul_snapshot

            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
                (src_sid,),
            ).fetchall()

            db_truncated = False
            if turn_id:
                last_idx = None
                for i, r in enumerate(rows):
                    if r["turn_id"] == turn_id:
                        last_idx = i
                if last_idx is not None:
                    msgs_to_clone = rows[: last_idx + 1]
                    db_truncated = (last_idx + 1) < len(rows)
                elif kept_turn_ids:
                    fallback_idx = None
                    for i, r in enumerate(rows):
                        if r["turn_id"] in kept_turn_ids:
                            fallback_idx = i
                    msgs_to_clone = rows[: fallback_idx + 1] if fallback_idx is not None else []
                    db_truncated = len(msgs_to_clone) < len(rows)
                else:
                    msgs_to_clone = []
                    db_truncated = bool(rows)
            elif store_truncated:
                if kept_turn_ids:
                    fallback_idx = None
                    for i, r in enumerate(rows):
                        if r["turn_id"] in kept_turn_ids:
                            fallback_idx = i
                    msgs_to_clone = rows[: fallback_idx + 1] if fallback_idx is not None else []
                elif exclude_turn_id:
                    msgs_to_clone = [r for r in rows if r["turn_id"] != exclude_turn_id]
                else:
                    msgs_to_clone = []
                db_truncated = len(msgs_to_clone) < len(rows)
            else:
                msgs_to_clone = list(rows)

            is_truncated = store_truncated or db_truncated
            new_checkpoint = None if is_truncated else src_sess["summary_checkpoint"]
            new_sid = uuid.uuid4().hex
            now_s = time.time()

            conn.execute(
                "INSERT INTO sessions (id, chat_id, source, model, started_at, ended_at, active, "
                "summary_checkpoint, metadata_json) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)",
                (new_sid, new_chat_id, source, model, now_s, new_checkpoint, _json_dumps(new_meta)),
            )

            for r in msgs_to_clone:
                conn.execute(
                    "INSERT INTO messages (session_id, chat_id, turn_id, role, content, "
                    "tool_calls_json, tool_call_id, tool_name, extra_json, active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        new_sid,
                        new_chat_id,
                        r["turn_id"],
                        r["role"],
                        r["content"],
                        r["tool_calls_json"],
                        r["tool_call_id"],
                        r["tool_name"],
                        _sanitize_cloned_extra(r["extra_json"]),
                        float(r["created_at"] or now_s),
                    ),
                )

            if assembler is not None and isinstance(soul_snapshot, str):
                cached_snaps = getattr(assembler, "_soul_snapshots", None)
                if isinstance(cached_snaps, dict):
                    cached_snaps[new_sid] = soul_snapshot

        heal_fn = getattr(db, "heal_orphan_tool_calls", None)
        if callable(heal_fn):
            with contextlib.suppress(Exception):
                heal_fn(new_sid)


def fork_chat(
    adapter: Any,
    chat_id: str,
    *,
    turn_id: str | None = None,
    title: str | None = None,
    before_user_index: int | None = None,
) -> dict[str, Any]:
    """Fork ``chat_id`` (optionally up through ``turn_id``) into a new side chat."""
    if not chat_id or not adapter.store.exists(chat_id):
        raise BranchTargetNotFoundError("找不到这个对话")

    all_events = adapter.store.events(chat_id)
    eff_turn = (turn_id or "").strip() or None
    active_tid = _active_turn_id(adapter, chat_id)
    store_truncated = False

    if eff_turn:
        last_idx = None
        for idx, ev in enumerate(all_events):
            if ev.get("turn_id") == eff_turn:
                last_idx = idx
        if last_idx is None:
            raise BranchTargetNotFoundError("找不到这条消息")
        sliced_events = all_events[: last_idx + 1]
        store_truncated = (last_idx + 1) < len(all_events)
    elif before_user_index is not None and before_user_index >= 0:
        user_positions = [i for i, ev in enumerate(all_events) if ev.get("event") == "user_message"]
        if before_user_index < len(user_positions):
            cutoff = user_positions[before_user_index]
            sliced_events = all_events[:cutoff]
            store_truncated = True
        elif active_tid:
            sliced_events = [ev for ev in all_events if ev.get("turn_id") != active_tid]
            store_truncated = len(sliced_events) < len(all_events)
        else:
            sliced_events = list(all_events)
    elif active_tid:
        sliced_events = [ev for ev in all_events if ev.get("turn_id") != active_tid]
        store_truncated = len(sliced_events) < len(all_events)
    else:
        sliced_events = list(all_events)

    forked_from = {"chat_id": chat_id, "turn_id": eff_turn or ""}
    new_chat_id = adapter.store.clone_chat(
        chat_id,
        sliced_events,
        title=title,
        forked_from=forked_from,
    )

    kept_turn_ids = {str(e.get("turn_id")) for e in sliced_events if e.get("turn_id")}
    _clone_muse_db_session(
        adapter,
        chat_id,
        new_chat_id,
        turn_id=eff_turn,
        kept_turn_ids=kept_turn_ids,
        store_truncated=store_truncated,
        exclude_turn_id=active_tid if not eff_turn else None,
    )

    return {
        "sessions": adapter.store.list(),
        "chat_id": new_chat_id,
        "key": f"websocket:{new_chat_id}",
    }


def _truncate_muse_db_for_edit(
    adapter: Any,
    chat_id: str,
    target_turn_id: str,
    kept_turn_ids: set[str],
) -> None:
    with _borrow_db(adapter) as db:
        write_tx = getattr(db, "_write_tx", None)
        if not callable(write_tx):
            return
        sid_to_heal: Optional[str] = None
        with write_tx() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND active = 1 ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            if sess is None:
                return
            sid = str(sess["id"])
            sid_to_heal = sid
            prior_inactive_row = conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 0",
                (sid,),
            ).fetchone()
            prior_inactive_max = (
                int(prior_inactive_row[0]) if prior_inactive_row and prior_inactive_row[0] is not None else None
            )

            rows = conn.execute(
                "SELECT id, turn_id, role FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
                (sid,),
            ).fetchall()

            target_row = next((r for r in rows if r["turn_id"] == target_turn_id), None)
            clear_checkpoint = False
            if target_row is not None:
                target_id = int(target_row["id"])
                conn.execute(
                    "UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?",
                    (sid, target_id),
                )
                remaining = [r for r in rows if int(r["id"]) < target_id]
                if sess["summary_checkpoint"]:
                    if (
                        not remaining
                        or not kept_turn_ids
                        or (prior_inactive_max is not None and prior_inactive_max >= target_id)
                        or (prior_inactive_max is not None and prior_inactive_max > int(remaining[-1]["id"]))
                    ):
                        clear_checkpoint = True
            else:
                # Target turn may have been compacted earlier or absent from MuseDB:
                # drop any active rows belonging to turns after kept_turn_ids.
                unkept_row = next(
                    (r for r in rows if r["turn_id"] is not None and str(r["turn_id"]) not in kept_turn_ids),
                    None,
                )
                if unkept_row is not None:
                    conn.execute(
                        "UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?",
                        (sid, int(unkept_row["id"])),
                    )
                elif not kept_turn_ids and rows:
                    conn.execute(
                        "UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1",
                        (sid,),
                    )
                if sess["summary_checkpoint"]:
                    inactive_hit = conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? AND turn_id = ? LIMIT 1",
                        (sid, target_turn_id),
                    ).fetchone()
                    if inactive_hit is not None or not kept_turn_ids or unkept_row is not None:
                        clear_checkpoint = True

            if clear_checkpoint:
                conn.execute("UPDATE sessions SET summary_checkpoint = NULL WHERE id = ?", (sid,))

        if sid_to_heal:
            heal_fn = getattr(db, "heal_orphan_tool_calls", None)
            if callable(heal_fn):
                with contextlib.suppress(Exception):
                    heal_fn(sid_to_heal)


async def _cleanup_pending_actor_state(actor: Any) -> None:
    if actor is None:
        return
    if getattr(actor, "_pending_confirm", None) is not None:
        actor._pending_confirm = None
    if getattr(actor, "_pending_clarify", None) is not None:
        cancel_clarify = getattr(actor, "_cancel_pending_clarify", None)
        if callable(cancel_clarify):
            with contextlib.suppress(Exception):
                await cancel_clarify("edit_message")
        else:
            pending = actor._pending_clarify
            actor._pending_clarify = None
            fut = getattr(pending, "future", None)
            if fut is not None and not fut.done():
                fut.cancel()
    if getattr(actor, "_goal_barrier_task", None) is not None or getattr(actor, "_phase", "idle") in (
        "goal_eval",
        "goal_wait",
    ):
        cancel_goal = getattr(actor, "_cancel_goal_eval_or_wait", None)
        if callable(cancel_goal):
            with contextlib.suppress(Exception):
                await cancel_goal()


async def edit_and_resubmit(
    adapter: Any,
    chat_id: str,
    *,
    turn_id: str,
    content: str,
) -> dict[str, Any]:
    """Truncate ``chat_id`` from ``turn_id`` (inclusive) in both stores and resubmit ``content``."""
    if not chat_id or not adapter.store.exists(chat_id):
        raise BranchTargetNotFoundError("找不到这个对话")
    eff_turn = str(turn_id or "").strip()
    if not eff_turn:
        raise InvalidBranchInputError("缺少要修改的消息标识")
    clean_content = str(content or "").strip()
    if not clean_content:
        raise InvalidBranchInputError("修改后的消息内容不能为空")

    if _active_turn_id(adapter, chat_id) is not None:
        raise ChatBusyError("正在回复中，请先停止当前回复再修改")

    all_events = adapter.store.events(chat_id)
    first_idx = next(
        (
            i
            for i, ev in enumerate(all_events)
            if ev.get("turn_id") == eff_turn and ev.get("event") == "user_message"
        ),
        None,
    )
    if first_idx is None:
        raise BranchTargetNotFoundError("找不到要修改的消息")

    actor = _resolve_actor(adapter, chat_id)
    await _cleanup_pending_actor_state(actor)

    kept_events = all_events[:first_idx]
    kept_turn_ids = {str(e.get("turn_id")) for e in kept_events if e.get("turn_id")}

    await asyncio.to_thread(adapter.store.replace_events, chat_id, kept_events)
    await asyncio.to_thread(_truncate_muse_db_for_edit, adapter, chat_id, eff_turn, kept_turn_ids)

    new_turn_id = uuid.uuid4().hex
    submit_fn = getattr(adapter, "submit", None)
    if callable(submit_fn):
        res = submit_fn(chat_id, clean_content, new_turn_id)
        if inspect.isawaitable(res):
            task = asyncio.create_task(res)
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)
            await asyncio.sleep(0)
    else:
        await asyncio.to_thread(
            adapter.store.append,
            chat_id,
            {
                "event": "user_message",
                "chat_id": chat_id,
                "text": clean_content,
                "turn_id": new_turn_id,
                "starts_turn": True,
                "turn_phase": "user",
            },
        )

    return {
        "sessions": adapter.store.list(),
        "chat_id": chat_id,
        "turn_id": new_turn_id,
    }
