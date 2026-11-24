"""The goal banner: what the companion is working towards in a chat (``/goal``)."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Optional


def _session_id(chat_id: str) -> Optional[str]:
    from muse.paths import active_engine_name, get_legacy_state_db_path, get_muse_db_path

    if active_engine_name() == "native":
        from muse.db import MuseDB, sync_from_legacy_state_db

        try:
            sync_from_legacy_state_db()
            muse_path = get_muse_db_path()
            if not muse_path.is_file():
                return None
            with MuseDB(muse_path) as db:
                sess = db.get_active_session(chat_id)
                return sess["id"] if sess else None
        except (sqlite3.Error, OSError, ValueError):
            return None

    path = get_legacy_state_db_path()
    if not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            row = db.execute(
                "SELECT id FROM sessions WHERE source = 'companion' AND chat_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    return row[0] if row else None


def goal_state(chat_id: str) -> dict[str, Any]:
    """``{"active", "status", "objective", "ui_summary"}`` as the app's banner reads it."""
    from muse.paths import active_engine_name, get_muse_db_path

    if active_engine_name() == "native":
        from muse.db import MuseDB, sync_from_legacy_state_db

        try:
            sync_from_legacy_state_db()
            muse_path = get_muse_db_path()
            if not muse_path.is_file():
                return {"active": False}
            with MuseDB(muse_path) as db:
                return db.get_goal_banner_state(chat_id)
        except (sqlite3.Error, OSError, ValueError):
            return {"active": False}

    from hermes_cli.goals import load_goal

    session_id = _session_id(chat_id)
    goal = load_goal(session_id) if session_id else None
    if goal is None or goal.status not in ("active", "paused"):
        return {"active": False}
    blocked = goal.status == "paused" or goal.last_verdict == "blocked"
    summary = goal.paused_reason if goal.status == "paused" else goal.last_reason
    return {
        "active": True,
        "status": "blocked" if blocked else "active",
        "objective": goal.goal,
        "ui_summary": (summary or "").strip()[:200] or None,
    }
