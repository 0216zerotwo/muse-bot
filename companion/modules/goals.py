"""The goal banner: what the companion is working towards in a chat (``/goal``)."""

from __future__ import annotations

import sqlite3
from typing import Any, Optional


def _session_id(chat_id: str) -> Optional[str]:
    from muse.db import sync_from_legacy_state_db
    from muse.paths import get_muse_db_path
    from muse.runtime import use_shared_db

    try:
        sync_from_legacy_state_db()
        muse_path = get_muse_db_path()
        if not muse_path.is_file():
            return None
        with use_shared_db(db_path=muse_path) as db:
            sess = db.get_active_session(chat_id)
            return sess["id"] if sess else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def goal_state(chat_id: str) -> dict[str, Any]:
    """``{"active", "status", "objective", "ui_summary"}`` as the app's banner reads it."""
    from muse.db import sync_from_legacy_state_db
    from muse.paths import get_muse_db_path
    from muse.runtime import use_shared_db

    try:
        sync_from_legacy_state_db()
        muse_path = get_muse_db_path()
        if not muse_path.is_file():
            return {"active": False}
        with use_shared_db(db_path=muse_path) as db:
            return db.get_goal_banner_state(chat_id)
    except (sqlite3.Error, OSError, ValueError):
        return {"active": False}
