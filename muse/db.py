"""SQLite WAL state store (`muse.db`) and copy-on-read `state.db` incremental migrator."""

from __future__ import annotations

import contextlib, hashlib, json, logging, os, random, re, shutil, sqlite3, stat, tempfile, threading, time, uuid, weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .paths import get_legacy_state_db_path, get_muse_db_path, get_muse_home

logger = logging.getLogger(__name__)

_LEGACY_CONTENT_JSON_PREFIX = "\x00json:"
_LEGACY_SYNC_META_KEY = "hermes_state_db"
_LEGACY_USAGE_SNAP_KEY = "hermes_usage_snapshot"
_LEGACY_GOAL_SNAP_KEY = "hermes_goal_snapshot"
_INTERRUPTED_TOOL_RESULT = "[Interrupted before completion]"
_UNSET: Any = object()

_FTS_OPERATORS = frozenset({"AND", "OR", "NOT"})
_BOUNDARY_END_REASONS = frozenset({
    "session_reset", "session_switch", "idle", "daily",
    "suspended", "resume_pending_expired", "new_session",
})
_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
    (0x3000, 0x303F), (0x3040, 0x309F), (0x30A0, 0x30FF), (0xAC00, 0xD7AF),
)

_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'companion',
    model TEXT, started_at REAL NOT NULL, ended_at REAL, active INTEGER NOT NULL DEFAULT 1,
    summary_checkpoint TEXT, metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat_active_started ON sessions(chat_id, active, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL, turn_id TEXT, role TEXT NOT NULL, content TEXT, tool_calls_json TEXT,
    tool_call_id TEXT, tool_name TEXT, extra_json TEXT, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session_active_id ON messages(session_id, active, id ASC);
CREATE INDEX IF NOT EXISTS idx_messages_chat_turn ON messages(chat_id, turn_id, id ASC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_legacy_key ON messages(json_extract(extra_json, '$.legacy_key'))
    WHERE extra_json IS NOT NULL AND json_extract(extra_json, '$.legacy_key') IS NOT NULL;
CREATE TABLE IF NOT EXISTS session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0, actual_cost_usd REAL NOT NULL DEFAULT 0.0,
    first_seen REAL NOT NULL DEFAULT 0.0, last_seen REAL NOT NULL DEFAULT 0.0,
    provider TEXT GENERATED ALWAYS AS (billing_provider) VIRTUAL,
    base_url TEXT GENERATED ALWAYS AS (billing_base_url) VIRTUAL,
    requests INTEGER GENERATED ALWAYS AS (api_call_count) VIRTUAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_last_seen ON session_model_usage(last_seen);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_session ON session_model_usage(session_id);
CREATE TABLE IF NOT EXISTS session_goals (
    chat_id TEXT PRIMARY KEY, session_key TEXT, status TEXT NOT NULL, objective TEXT, gate TEXT,
    iteration INTEGER NOT NULL DEFAULT 0, max_iterations INTEGER NOT NULL DEFAULT 25,
    updated_at REAL NOT NULL, state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
"""

_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages WHEN new.active = 1 AND COALESCE(new.content, '') != ''
BEGIN INSERT INTO messages_fts(rowid, content, session_id, chat_id, message_id) VALUES (new.id, new.content, new.session_id, new.chat_id, new.id); END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
BEGIN DELETE FROM messages_fts WHERE rowid = old.id; END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF active, content, session_id, chat_id ON messages
BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content, session_id, chat_id, message_id)
    SELECT new.id, new.content, new.session_id, new.chat_id, new.id WHERE new.active = 1 AND COALESCE(new.content, '') != '';
END;
"""

_USAGE_COLS = (
    "session_id, model, billing_provider, billing_base_url, billing_mode, task, source, "
    "api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "reasoning_tokens, estimated_cost_usd, actual_cost_usd, first_seen, last_seen"
)
_USAGE_PK = "session_id, model, billing_provider, billing_base_url, billing_mode, task"
_USAGE_SUMMARY_SQL = (
    "SELECT u.model, u.billing_base_url, u.task, COALESCE(s.source, NULLIF(u.source, '')), "
    "SUM(u.api_call_count), SUM(u.input_tokens + u.cache_read_tokens + u.cache_write_tokens), "
    "SUM(u.output_tokens), SUM(u.cache_read_tokens) "
    "FROM session_model_usage u LEFT JOIN sessions s ON s.id = u.session_id "
    "WHERE u.last_seen >= ? GROUP BY u.model, u.billing_base_url, u.task, COALESCE(s.source, NULLIF(u.source, ''))"
)

_REGISTRY_LOCK = threading.RLock()
_OPEN_INSTANCES: weakref.WeakSet[MuseDB] = weakref.WeakSet()
_SYNC_LOCKS: dict[str, threading.RLock] = {}


def _sync_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _REGISTRY_LOCK:
        return _SYNC_LOCKS.setdefault(key, threading.RLock())


def _scrub_str(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")

def _json_dumps(value: Any) -> str:
    return _scrub_str(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) or "{}"

def _json_loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default

def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _has_cjk(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)

def _secure_db_files(db_path: Path, *, create_main: bool = False) -> None:
    """Ensure 0600 permissions on muse.db and its WAL/SHM sidecars on POSIX without dropping fcntl locks."""
    if os.name == "nt":
        return
    if create_main:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            os.close(os.open(db_path, flags, 0o600))
        except FileExistsError:
            pass
        except OSError:
            return
    for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm")):
        with contextlib.suppress(OSError):
            st = os.lstat(candidate)
            if not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode):
                os.chmod(candidate, 0o600)

def _extract_tool_call_meta(tc: Any) -> tuple[str | None, str | None]:
    if not isinstance(tc, dict):
        return None, None
    cid = tc.get("id") or tc.get("tool_call_id")
    fn = tc.get("function")
    name = fn.get("name") if isinstance(fn, dict) else tc.get("name")
    return (str(cid) if cid else None, str(name) if name else None)

def _probe_legacy_file_signature(state_db_path: Path) -> dict[str, Any] | None:
    """Pure byte/stat observation of state.db (+ state.db-wal). NEVER opens via SQLite or writes anything."""
    try:
        st_main = os.stat(state_db_path)
        if not stat.S_ISREG(st_main.st_mode):
            return None
        with state_db_path.open("rb") as fh:
            hdr = fh.read(100)
            header_hex = hdr[24:28].hex() + ":" + hdr[92:96].hex() if len(hdr) >= 96 else hdr.hex()
    except OSError:
        return None
    wal_path = state_db_path.with_name(f"{state_db_path.name}-wal")
    wal_mtime_ns, wal_size, wal_digest = 0, 0, ""
    with contextlib.suppress(OSError):
        st_wal = os.stat(wal_path)
        if stat.S_ISREG(st_wal.st_mode):
            wal_mtime_ns, wal_size = st_wal.st_mtime_ns, st_wal.st_size
            if wal_size > 0:
                h = hashlib.blake2b(digest_size=16)
                with wal_path.open("rb") as wfh:
                    h.update(wfh.read(4 * 1024 * 1024))
                    if wal_size > 4 * 1024 * 1024:
                        wfh.seek(max(0, wal_size - 65536))
                        h.update(wfh.read(65536))
                wal_digest = h.hexdigest()
    return {"mtime_ns": st_main.st_mtime_ns, "size": st_main.st_size, "header_sig": header_hex,
            "wal_mtime_ns": wal_mtime_ns, "wal_size": wal_size, "wal_digest": wal_digest}

def _decode_legacy_content(raw_content: Any) -> tuple[str | None, Any]:
    if raw_content is None or not isinstance(raw_content, str):
        return (None if raw_content is None else _scrub_str(str(raw_content))), None
    if not raw_content.startswith(_LEGACY_CONTENT_JSON_PREFIX):
        return _scrub_str(raw_content), None
    decoded = _json_loads(raw_content[len(_LEGACY_CONTENT_JSON_PREFIX):], default=None)
    if isinstance(decoded, str):
        return _scrub_str(decoded), None
    if isinstance(decoded, list):
        return _scrub_str("\n".join(str(p.get("text", "")) for p in decoded if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))), decoded
    return (_scrub_str(_json_dumps(decoded)), decoded) if decoded is not None else (_scrub_str(raw_content), None)

def _build_snippet(content: str, needle: str, width: int = 120) -> str:
    clean = (content or "").replace("\r", " ").replace("\n", " ")
    idx = clean.lower().find(needle.lower()) if (clean and needle) else -1
    start = max(0, idx - 40) if idx >= 0 else 0
    return clean[start : start + width]


class MuseDB:
    """Thread-safe SQLite WAL database for Muse Native runtime."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path).expanduser() if db_path is not None else get_muse_db_path()
        self._lock = threading.RLock()
        self._closed = False
        self.fts_tokenizer = "trigram"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _secure_db_files(self.db_path, create_main=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            self._configure_and_init(conn)
        except BaseException:
            with contextlib.suppress(Exception):
                conn.close()
            raise

        self._conn: sqlite3.Connection | None = conn
        _secure_db_files(self.db_path, create_main=False)
        with _REGISTRY_LOCK:
            _OPEN_INSTANCES.add(self)

    def _configure_and_init(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA busy_timeout=5000")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    break
                time.sleep(random.uniform(0.01, 0.05))
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._tx_on(conn):
            for stmt in _split_sql_statements(_CORE_SCHEMA_SQL):
                conn.execute(stmt)
            self.fts_tokenizer = self._ensure_fts_table(conn)
            for stmt in _split_sql_statements(_FTS_TRIGGERS_SQL):
                conn.execute(stmt)

    @staticmethod
    def _ensure_fts_table(conn: sqlite3.Connection) -> str:
        existing = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'").fetchone()
        if existing is not None:
            return "unicode61" if "unicode61" in (existing["sql"] or "").lower() else "trigram"
        fts_cols = "content, session_id UNINDEXED, chat_id UNINDEXED, message_id UNINDEXED"
        try:
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5({fts_cols}, tokenize='trigram')")
            return "trigram"
        except sqlite3.OperationalError as exc:
            if "trigram" not in str(exc).lower() and "tokenizer" not in str(exc).lower():
                raise
            logger.warning("SQLite lacks FTS5 trigram tokenizer (%s); falling back to unicode61", type(exc).__name__)
            conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5({fts_cols}, tokenize='unicode61')")
            return "unicode61"

    @staticmethod
    @contextmanager
    def _tx_on(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        deadline = time.monotonic() + 8.0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(random.uniform(0.01, 0.06))
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _require_conn(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise sqlite3.ProgrammingError(f"MuseDB connection is closed: {self.db_path}")
        return self._conn

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._tx_on(self._require_conn()) as conn:
            yield conn

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn, self._conn = self._conn, None
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
        with _REGISTRY_LOCK:
            _OPEN_INSTANCES.discard(self)

    def __enter__(self) -> MuseDB:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ── Sessions ──────────────────────────────────────────────────────────

    def create_session(
        self, chat_id: str, *, session_id: str | None = None, source: str = "companion",
        model: str | None = None, started_at: float | None = None, summary_checkpoint: str | None = None,
        metadata: dict[str, Any] | None = None, deactivate_existing: bool = True,
    ) -> str:
        sid = session_id or uuid.uuid4().hex
        now = float(started_at if started_at is not None else time.time())
        meta_json = _json_dumps(metadata) if metadata is not None else None
        with self._write_tx() as conn:
            if deactivate_existing and chat_id:
                conn.execute(
                    "UPDATE sessions SET active = 0, ended_at = COALESCE(ended_at, ?) "
                    "WHERE chat_id = ? AND source = ? AND active = 1 AND id != ?",
                    (now, chat_id, source, sid),
                )
            conn.execute(
                "INSERT INTO sessions (id, chat_id, source, model, started_at, ended_at, active, "
                "summary_checkpoint, metadata_json) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "chat_id = CASE WHEN sessions.chat_id = '' THEN excluded.chat_id ELSE sessions.chat_id END, "
                "source = excluded.source, model = COALESCE(excluded.model, sessions.model), active = 1, ended_at = NULL, "
                "summary_checkpoint = COALESCE(excluded.summary_checkpoint, sessions.summary_checkpoint), "
                "metadata_json = COALESCE(excluded.metadata_json, sessions.metadata_json)",
                (sid, chat_id, source, model, now, _scrub_str(summary_checkpoint), meta_json),
            )
        return sid

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_conn().execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._decode_session_row(row) if row is not None else None

    def get_active_session(self, chat_id: str, *, source: str = "companion") -> dict[str, Any] | None:
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND source = ? AND active = 1 "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (chat_id, source),
            ).fetchone()
        return self._decode_session_row(row) if row is not None else None

    def get_or_create_active_session(self, chat_id: str, *, source: str = "companion", model: str | None = None) -> dict[str, Any]:
        existing = self.get_active_session(chat_id, source=source)
        if existing is not None:
            return existing
        sid = self.create_session(chat_id, source=source, model=model)
        created = self.get_session(sid)
        assert created is not None
        return created

    def renew_session(self, chat_id: str, *, source: str = "companion", model: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        return self.create_session(chat_id, source=source, model=model, metadata=metadata, deactivate_existing=True)

    def update_session(
        self, session_id: str, *, model: str | None = _UNSET, ended_at: float | None = _UNSET,
        active: bool | int = _UNSET, summary_checkpoint: str | None = _UNSET, metadata: dict[str, Any] | None = _UNSET,
    ) -> None:
        pairs = [
            ("model", model), ("ended_at", ended_at),
            ("active", (1 if active else 0) if active is not _UNSET else _UNSET),
            ("summary_checkpoint", _scrub_str(summary_checkpoint) if summary_checkpoint is not _UNSET else _UNSET),
            ("metadata_json", (_json_dumps(metadata) if metadata is not None else None) if metadata is not _UNSET else _UNSET),
        ]
        sets, params = [f"{c} = ?" for c, v in pairs if v is not _UNSET], [v for _, v in pairs if v is not _UNSET]
        if sets:
            with self._write_tx() as conn:
                conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", [*params, session_id])

    @staticmethod
    def _decode_session_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["active"] = bool(data.get("active"))
        data["metadata"] = _json_loads(data.get("metadata_json"), default={})
        return data

    # ── Messages, FTS5 Search, Compaction & Crash/Turn Helpers ────────────

    def append_message(
        self, session_id: str, chat_id: str, role: str, content: str | list[dict[str, Any]] | None = None,
        *, turn_id: str | None = None, tool_calls: list[dict[str, Any]] | str | None = None,
        tool_call_id: str | None = None, tool_name: str | None = None,
        extra: dict[str, Any] | str | None = None, active: bool = True, created_at: float | None = None,
    ) -> int:
        now = float(created_at if created_at is not None else time.time())
        extra_dict = dict(_json_loads(extra, default={})) if isinstance(extra, str) else dict(extra or {})
        if isinstance(content, list):
            texts = [str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
            text_content = _scrub_str("\n".join(texts))
            extra_dict.setdefault("content_parts", content)
        else:
            text_content = _scrub_str(content)

        tc_json = (_scrub_str(tool_calls) if tool_calls.strip() else None) if isinstance(tool_calls, str) else (_json_dumps(tool_calls) if tool_calls else None)
        extra_json = _json_dumps(extra_dict) if extra_dict else None
        with self._write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO messages (session_id, chat_id, turn_id, role, content, "
                "tool_calls_json, tool_call_id, tool_name, extra_json, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, chat_id, turn_id, role, text_content, tc_json, tool_call_id, _scrub_str(tool_name), extra_json, 1 if active else 0, now),
            )
            return int(cur.lastrowid or 0)

    def update_message(
        self, message_id: int, *, content: str | None = _UNSET, turn_id: str | None = _UNSET,
        extra: dict[str, Any] | None = _UNSET, active: bool | int = _UNSET,
    ) -> None:
        pairs = [
            ("content", _scrub_str(content) if content is not _UNSET else _UNSET),
            ("turn_id", turn_id),
            ("extra_json", (_json_dumps(extra) if extra else None) if extra is not _UNSET else _UNSET),
            ("active", (1 if active else 0) if active is not _UNSET else _UNSET),
        ]
        sets, params = [f"{c} = ?" for c, v in pairs if v is not _UNSET], [v for _, v in pairs if v is not _UNSET]
        if sets:
            with self._write_tx() as conn:
                conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", [*params, int(message_id)])

    def get_messages(self, session_id: str, *, active_only: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
        clause = "AND active = 1 " if active_only else ""
        with self._lock:
            conn = self._require_conn()
            if limit is not None and limit > 0:
                rows = conn.execute(
                    f"SELECT * FROM (SELECT * FROM messages WHERE session_id = ? {clause}ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (session_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM messages WHERE session_id = ? {clause}ORDER BY id ASC", (session_id,)).fetchall()
        return [self._decode_message_row(r) for r in rows]

    @staticmethod
    def _decode_message_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["active"] = bool(data.get("active"))
        data["tool_calls"] = _json_loads(data.get("tool_calls_json"), default=[]) if data.get("tool_calls_json") else []
        extra = _json_loads(data.get("extra_json"), default={}) if data.get("extra_json") else {}
        if isinstance(extra, dict):
            for rk in ("reasoning_details", "codex_reasoning_items"):
                if isinstance(extra.get(rk), str) and isinstance(parsed := _json_loads(extra[rk], default=None), list):
                    extra[rk] = parsed
        data["extra"] = extra
        return data

    def deactivate_messages(self, message_ids: Sequence[int]) -> int:
        ids = [int(mid) for mid in message_ids]
        if not ids:
            return 0
        changed = 0
        with self._write_tx() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                changed += conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({','.join('?' * len(chunk))}) AND active = 1", chunk).rowcount
        return changed

    def compact_session_messages(self, session_id: str, *, upto_message_id: int, summary_checkpoint: str) -> int:
        with self._write_tx() as conn:
            cur = conn.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id <= ?", (session_id, int(upto_message_id)))
            conn.execute("UPDATE sessions SET summary_checkpoint = ? WHERE id = ?", (_scrub_str(summary_checkpoint), session_id))
            return int(cur.rowcount)

    def heal_orphan_tool_calls(self, session_id: str) -> int:
        """Append synthetic `[Interrupted before completion]` tool results for dangling assistant.tool_calls."""
        with self._write_tx() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC", (session_id,)).fetchall()
            plan: list[tuple[str, dict[str, Any]]] = []
            healed, needs_resequence, idx, total = 0, False, 0, len(rows)
            while idx < total:
                row = dict(rows[idx])
                plan.append(("keep", row))
                tcs = _json_loads(row["tool_calls_json"], default=[]) if (row["role"] == "assistant" and row.get("tool_calls_json")) else []
                expected: list[tuple[str, str | None]] = []
                seen: set[str] = set()
                for tc in tcs if isinstance(tcs, list) else []:
                    cid, tname = _extract_tool_call_meta(tc)
                    if cid and cid not in seen:
                        seen.add(cid)
                        expected.append((cid, tname))
                if not expected:
                    idx += 1
                    continue
                answered: set[str] = set()
                j = idx + 1
                while j < total and rows[j]["role"] == "tool":
                    t_row = dict(rows[j])
                    plan.append(("keep", t_row))
                    if t_row.get("tool_call_id"):
                        answered.add(str(t_row["tool_call_id"]))
                    j += 1
                missing = [(cid, tname) for cid, tname in expected if cid not in answered]
                if missing:
                    needs_resequence = needs_resequence or (j < total)
                    base_ts = float(rows[j - 1]["created_at"] or time.time())
                    for cid, tname in missing:
                        healed += 1
                        plan.append(("insert", {
                            "session_id": session_id, "chat_id": row["chat_id"], "turn_id": row["turn_id"],
                            "role": "tool", "content": _INTERRUPTED_TOOL_RESULT, "tool_calls_json": None,
                            "tool_call_id": cid, "tool_name": tname,
                            "extra_json": _json_dumps({"synthetic_healed": True}), "created_at": base_ts,
                        }))
                idx = j

            if healed == 0:
                return 0
            if not needs_resequence:
                for kind, item in plan:
                    if kind == "insert":
                        self._insert_raw_message(conn, item)
                return healed

            first_pos = next(i for i, (kind, _) in enumerate(plan) if kind == "insert")
            tail_ids = [int(item["id"]) for kind, item in plan[first_pos:] if kind == "keep"]
            if tail_ids:
                conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({','.join('?' * len(tail_ids))})", tail_ids)
            for _, item in plan[first_pos:]:
                extra_obj = _json_loads(item.get("extra_json"), default={}) if "id" in item else {}
                if isinstance(extra_obj, dict) and "legacy_key" in extra_obj:
                    retired = {k: v for k, v in extra_obj.items() if k != "legacy_key"}
                    retired["retired_legacy_key"] = extra_obj["legacy_key"]
                    conn.execute("UPDATE messages SET extra_json = ? WHERE id = ?", (_json_dumps(retired), int(item["id"])))
                self._insert_raw_message(conn, item)
            return healed

    @staticmethod
    def _insert_raw_message(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO messages (session_id, chat_id, turn_id, role, content, tool_calls_json, tool_call_id, tool_name, extra_json, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (item["session_id"], item["chat_id"], item.get("turn_id"), item["role"], item.get("content"), item.get("tool_calls_json"), item.get("tool_call_id"), item.get("tool_name"), item.get("extra_json"), float(item.get("created_at") or time.time())),
        )

    def rollback_turn_for_retry(self, session_id: str, turn_id: str) -> int:
        """Soft-invalidate assistant and subsequent records after initial user/clarify input in `turn_id`."""
        if not turn_id:
            return 0
        with self._write_tx() as conn:
            rows = conn.execute(
                "SELECT id, turn_id, role, tool_calls_json, tool_call_id, tool_name "
                "FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            first_assistant = next((r for r in rows if r["turn_id"] == turn_id and r["role"] == "assistant"), None)
            if first_assistant is None:
                return 0
            cutoff_id = int(first_assistant["id"])

            to_deactivate: set[int] = set()
            invalidated_calls: set[str] = set()
            for r in rows:
                rid = int(r["id"])
                if rid >= cutoff_id and (
                    r["turn_id"] == turn_id or (r["role"] == "tool" and r["tool_call_id"] and str(r["tool_call_id"]) in invalidated_calls)
                ):
                    to_deactivate.add(rid)
                    if r["role"] == "assistant" and r["tool_calls_json"]:
                        for tc in _json_loads(r["tool_calls_json"], default=[]) or []:
                            cid, _ = _extract_tool_call_meta(tc)
                            if cid:
                                invalidated_calls.add(cid)
            if not to_deactivate:
                return 0
            sids = sorted(to_deactivate)
            return int(conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({','.join('?' * len(sids))}) AND active = 1", sids).rowcount)

    def undo_last_exchange(self, session_id: str, turn_id: str | None = None) -> int:
        """Soft-delete the latest exchange (or `turn_id`), keeping `turn_A` intact when undoing a `clarify` resumption."""
        with self._write_tx() as conn:
            rows = conn.execute("SELECT id, turn_id, role, tool_name, extra_json FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC", (session_id,)).fetchall()
            if not rows:
                return 0
            eff_turn = (turn_id or "").strip() or None
            tail_tid = str(rows[-1]["turn_id"] or "").strip() or None
            real_u = lambda r: r["role"] == "user" and not (isinstance(ex := _json_loads(r["extra_json"], default={}), dict) and ex.get("clarify_attachment"))
            if eff_turn is None and tail_tid:
                tail_rows = [r for r in rows if r["turn_id"] == tail_tid]
                f_ex = _json_loads(tail_rows[0]["extra_json"], default={}) if tail_rows else {}
                if tail_rows and not any(real_u(r) for r in tail_rows) and tail_rows[0]["role"] == "tool" and (tail_rows[0]["tool_name"] == "clarify" or (isinstance(f_ex, dict) and "clarify_id" in f_ex)):
                    eff_turn = tail_tid
            if eff_turn and (turn_rows := [r for r in rows if r["turn_id"] == eff_turn]):
                first = turn_rows[0]
                f_ex = _json_loads(first["extra_json"], default={})
                if not any(real_u(r) for r in turn_rows) and first["role"] == "tool" and (first["tool_name"] == "clarify" or (isinstance(f_ex, dict) and "clarify_id" in f_ex)):
                    cid = int(first["id"])
                    parent_tid = next((r["turn_id"] for r in reversed(rows) if int(r["id"]) < cid and r["role"] == "assistant"), None)
                    restored = {**(f_ex if isinstance(f_ex, dict) else {}), "awaiting_user": False, "undone": True}
                    conn.execute("UPDATE messages SET content = ?, turn_id = ?, extra_json = ? WHERE id = ?", (_INTERRUPTED_TOOL_RESULT, parent_tid, _json_dumps(restored), cid))
                    deact = [int(r["id"]) for r in rows if int(r["id"]) != cid and (r["turn_id"] == eff_turn or (eff_turn == tail_tid and int(r["id"]) > cid))]
                    if deact:
                        conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({','.join('?' * len(deact))}) AND active = 1", deact)
                    return len(deact) + 1
                if eff_turn == tail_tid:
                    return int(conn.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?", (session_id, int(first["id"]))).rowcount)
                tids = [int(r["id"]) for r in turn_rows]
                return int(conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({','.join('?' * len(tids))}) AND active = 1", tids).rowcount)
            last_user_id = next((int(r["id"]) for r in reversed(rows) if r["role"] == "user"), None)
            if last_user_id is not None:
                return int(conn.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?", (session_id, last_user_id)).rowcount)
            if tail_tid:
                return int(conn.execute("UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND turn_id = ?", (session_id, tail_tid)).rowcount)
            return int(conn.execute("UPDATE messages SET active = 0 WHERE id = ? AND active = 1", (int(rows[-1]["id"]),)).rowcount)

    def search_messages(
        self, query: str, *, chat_id: str | None = None, session_id: str | None = None,
        roles: Sequence[str] | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search active messages using FTS5 trigram (>=3 chars) + LIKE fallback (1-2 char CJK/short terms)."""
        cleaned = (query or "").strip()[:2048]
        if limit <= 0 or not cleaned:
            return []

        terms: list[str] = []
        for match in _TOKEN_RE.finditer(cleaned):
            phrase, bare = match.group(1), match.group(2)
            tok = (phrase if phrase is not None else bare or "").strip()
            if not tok or (phrase is None and tok.upper() in _FTS_OPERATORS):
                continue
            if phrase is None and (_has_cjk(tok) or (tok.endswith("*") and len(tok) > 1)):
                tok = tok.rstrip("*") or tok
            if tok:
                terms.append(tok)
        if not terms:
            return []

        fts_terms, like_terms = [], []
        for tok in terms:
            use_fts = (len(tok) >= 3) if self.fts_tokenizer == "trigram" else (not _has_cjk(tok))
            (fts_terms if use_fts else like_terms).append(tok)

        where, params = ["m.active = 1"], []
        if chat_id is not None:
            where.append("m.chat_id = ?")
            params.append(chat_id)
        if session_id is not None:
            where.append("m.session_id = ?")
            params.append(session_id)
        if roles:
            rlist = list(roles)
            where.append(f"m.role IN ({','.join('?' * len(rlist))})")
            params.extend(rlist)
        for lterm in like_terms:
            where.append("COALESCE(m.content, '') LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(lterm)}%")

        cols = "m.id, m.session_id, m.chat_id, m.turn_id, m.role, m.content, m.created_at, s.source, s.model, s.started_at AS session_started"
        with self._lock:
            conn = self._require_conn()
            rows: list[sqlite3.Row] = []
            if fts_terms:
                match_expr = " ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
                sql = (
                    f"SELECT {cols} FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                    f"JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ? AND {' AND '.join(where)} "
                    "ORDER BY bm25(messages_fts), m.created_at DESC, m.id DESC LIMIT ?"
                )
                try:
                    rows = conn.execute(sql, [match_expr, *params, int(limit)]).fetchall()
                except sqlite3.OperationalError:
                    for t in fts_terms:
                        where.append("COALESCE(m.content, '') LIKE ? ESCAPE '\\'")
                        params.append(f"%{_escape_like(t)}%")
                    fts_terms = []
            if not fts_terms:
                sql = f"SELECT {cols} FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {' AND '.join(where)} ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
                rows = conn.execute(sql, [*params, int(limit)]).fetchall()

        first_term = terms[0]
        return [
            {
                "id": int(r["id"]), "message_id": int(r["id"]), "session_id": r["session_id"],
                "chat_id": r["chat_id"], "turn_id": r["turn_id"], "role": r["role"],
                "content": r["content"] or "", "snippet": _build_snippet(r["content"] or "", first_term),
                "created_at": float(r["created_at"]), "source": r["source"],
                "model": r["model"], "session_started": float(r["session_started"]),
            }
            for r in rows
        ]

    # ── Token & Model Usage Accounting ────────────────────────────────────

    def record_usage(
        self, session_id: str, model: str, *, provider: str = "", billing_provider: str | None = None,
        base_url: str = "", billing_base_url: str | None = None, billing_mode: str = "",
        task: str = "", source: str = "companion", chat_id: str = "",
        input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
        cache_write_tokens: int = 0, reasoning_tokens: int = 0,
        requests: int | None = None, api_call_count: int | None = None,
        estimated_cost_usd: float = 0.0, actual_cost_usd: float = 0.0, seen_at: float | None = None,
    ) -> None:
        now = float(seen_at if seen_at is not None else time.time())
        eff_provider = billing_provider if billing_provider is not None else provider
        eff_base_url = billing_base_url if billing_base_url is not None else base_url
        calls = int(api_call_count if api_call_count is not None else (requests if requests is not None else 1))
        sid = session_id or "__system__"
        vals = (
            sid, model or "unknown", eff_provider, eff_base_url, billing_mode, task, source or "",
            calls, int(input_tokens or 0), int(output_tokens or 0), int(cache_read_tokens or 0),
            int(cache_write_tokens or 0), int(reasoning_tokens or 0),
            float(estimated_cost_usd or 0.0), float(actual_cost_usd or 0.0), now, now,
        )
        sum_cols = ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd")
        inc_sql = ", ".join(f"{c} = session_model_usage.{c} + excluded.{c}" for c in sum_cols)
        with self._write_tx() as conn:
            conn.execute("INSERT OR IGNORE INTO sessions (id, chat_id, source, model, started_at, active) VALUES (?, ?, ?, ?, ?, 0)", (sid, chat_id, source or "companion", model, now))
            conn.execute(
                f"INSERT INTO session_model_usage ({_USAGE_COLS}) VALUES ({','.join('?' * 17)}) "
                f"ON CONFLICT({_USAGE_PK}) DO UPDATE SET {inc_sql}, "
                "source = CASE WHEN session_model_usage.source = '' THEN excluded.source ELSE session_model_usage.source END, "
                "first_seen = MIN(session_model_usage.first_seen, excluded.first_seen), "
                "last_seen = MAX(session_model_usage.last_seen, excluded.last_seen)",
                vals,
            )

    def query_usage_since(self, since: float) -> list[tuple[Any, ...]]:
        with self._lock:
            return [tuple(r) for r in self._require_conn().execute(_USAGE_SUMMARY_SQL, (float(since),)).fetchall()]

    # ── Session Goals (`/goal` Banner & Controller State) ─────────────────

    def upsert_goal(
        self, chat_id: str, *, status: str, objective: str, session_key: str | None = None,
        gate: str | None = None, iteration: int = 0, max_iterations: int = 25,
        state: dict[str, Any] | str | None = None, updated_at: float | None = None,
    ) -> None:
        now = float(updated_at if updated_at is not None else time.time())
        state_json = (_scrub_str(state) or "{}") if isinstance(state, str) else _json_dumps({"goal": objective, "status": status, **(state or {})})
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO session_goals (chat_id, session_key, status, objective, gate, iteration, max_iterations, updated_at, state_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "session_key = COALESCE(excluded.session_key, session_goals.session_key), status = excluded.status, "
                "objective = excluded.objective, gate = excluded.gate, iteration = excluded.iteration, "
                "max_iterations = excluded.max_iterations, updated_at = excluded.updated_at, state_json = excluded.state_json",
                (chat_id, session_key, status, _scrub_str(objective) or "", _scrub_str(gate), int(iteration), int(max_iterations), now, state_json),
            )

    def get_goal(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_conn().execute("SELECT * FROM session_goals WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["state"] = _json_loads(data.get("state_json"), default={})
        return data

    def get_goal_banner_state(self, chat_id: str) -> dict[str, Any]:
        """Exact wire format expected by `companion.modules.goals.goal_state(chat_id)`."""
        goal_row = self.get_goal(chat_id)
        status = str((goal_row or {}).get("status") or "")
        if goal_row is None or status not in ("active", "paused", "blocked"):
            return {"active": False}
        state = goal_row.get("state") if isinstance(goal_row.get("state"), dict) else {}
        blocked = status in ("paused", "blocked") or state.get("last_verdict") == "blocked"
        summary = state.get("ui_summary")
        if summary is None:
            summary = state.get("paused_reason") if status == "paused" else state.get("last_reason")
        objective = goal_row.get("objective") if goal_row.get("objective") is not None else str(state.get("goal") or "")
        return {
            "active": True, "status": "blocked" if blocked else "active",
            "objective": objective, "ui_summary": str(summary or "").strip()[:200] or None,
        }

    def clear_goal(self, chat_id: str) -> bool:
        existing = self.get_goal(chat_id)
        if existing is None:
            return False
        self.upsert_goal(
            chat_id, status="cleared", objective=str(existing.get("objective") or ""),
            session_key=existing.get("session_key"), gate=existing.get("gate"),
            iteration=int(existing.get("iteration") or 0), max_iterations=int(existing.get("max_iterations") or 25),
            state={**dict(existing.get("state") or {}), "status": "cleared"},
        )
        return True

    # ── Sync Metadata & Copy-on-Read Legacy State DB Migrator ─────────────

    def get_sync_meta(self, key: str) -> Any | None:
        with self._lock:
            row = self._require_conn().execute("SELECT value_json FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return _json_loads(row["value_json"], default=None) if row is not None else None

    def set_sync_meta(self, key: str, value: Any, *, updated_at: float | None = None) -> None:
        with self._write_tx() as conn:
            self._set_sync_meta_on_conn(conn, key, value, float(updated_at if updated_at is not None else time.time()))

    @staticmethod
    def _set_sync_meta_on_conn(conn: sqlite3.Connection, key: str, value: Any, now: float) -> None:
        conn.execute(
            "INSERT INTO sync_meta (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, _json_dumps(value), now),
        )

    def sync_from_legacy_state_db(self, state_db_path: Path | str | None = None, *, force: bool = False) -> dict[str, int]:
        """Copy-on-read watermark incremental sync from legacy Hermes `state.db`."""
        counts = {"sessions": 0, "messages": 0, "usage": 0, "goals": 0, "synced": 0}
        src_path = Path(state_db_path).expanduser() if state_db_path is not None else self.db_path.with_name("state.db")
        sig = _probe_legacy_file_signature(src_path)
        if sig is None or sig["size"] == 0:
            return counts

        with _sync_lock_for(self.db_path):
            prev_wm = self.get_sync_meta(_LEGACY_SYNC_META_KEY)
            if not force and isinstance(prev_wm, dict) and all(prev_wm.get(k) == sig[k] for k in sig):
                return counts

            legacy_mtime_s = max(sig["mtime_ns"], sig["wal_mtime_ns"]) / 1_000_000_000.0
            with tempfile.TemporaryDirectory(prefix="muse-legacy-sync-") as tmp_dir_str:
                copied_db = Path(tmp_dir_str) / "state.db"
                try:
                    shutil.copyfile(src_path, copied_db)
                    src_wal = src_path.with_name(f"{src_path.name}-wal")
                    if src_wal.is_file():
                        shutil.copyfile(src_wal, Path(tmp_dir_str) / "state.db-wal")
                except OSError as exc:
                    logger.warning("Failed to copy legacy state.db for read-only sync: %s", type(exc).__name__)
                    return counts
                try:
                    with contextlib.closing(sqlite3.connect(str(copied_db), timeout=5.0)) as legacy_conn:
                        legacy_conn.row_factory = sqlite3.Row
                        extracted = _extract_legacy_snapshot(legacy_conn, legacy_mtime_s)
                except sqlite3.Error as exc:
                    logger.warning("Skipping unreadable legacy state.db copy: %s", type(exc).__name__)
                    return counts

            now = time.time()
            sum_cols = ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd")
            delta_set_sql = ", ".join(f"{c} = session_model_usage.{c} + ?" for c in sum_cols)
            repl_set_sql = ", ".join(f"{c} = excluded.{c}" for c in sum_cols)

            with self._write_tx() as conn:
                # Re-check watermark and snapshots under BEGIN IMMEDIATE to prevent cross-process TOCTOU double-deltas.
                wm_row = conn.execute("SELECT value_json FROM sync_meta WHERE key = ?", (_LEGACY_SYNC_META_KEY,)).fetchone()
                cur_wm = _json_loads(wm_row["value_json"], default=None) if wm_row is not None else None
                if not force and isinstance(cur_wm, dict) and all(cur_wm.get(k) == sig[k] for k in sig):
                    return counts
                us_row = conn.execute("SELECT value_json FROM sync_meta WHERE key = ?", (_LEGACY_USAGE_SNAP_KEY,)).fetchone()
                gs_row = conn.execute("SELECT value_json FROM sync_meta WHERE key = ?", (_LEGACY_GOAL_SNAP_KEY,)).fetchone()
                usage_snap: dict[str, list[Any]] = (_json_loads(us_row["value_json"], default={}) if us_row is not None else {}) or {}
                goal_snap: dict[str, str] = (_json_loads(gs_row["value_json"], default={}) if gs_row is not None else {}) or {}
                for srow in extracted["all_sessions"]:
                    conn.execute(
                        "INSERT INTO sessions (id, chat_id, source, model, started_at, ended_at, active) VALUES (?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(id) DO UPDATE SET source = excluded.source, chat_id = CASE WHEN sessions.chat_id = '' THEN excluded.chat_id ELSE sessions.chat_id END, "
                        "model = COALESCE(sessions.model, excluded.model), ended_at = COALESCE(excluded.ended_at, sessions.ended_at)",
                        (srow["id"], srow["chat_id"], srow["source"], srow["model"], srow["started_at"], srow["ended_at"]),
                    )
                for comp in extracted["latest_companion_sessions"]:
                    sid, cid, recency = comp["id"], comp["chat_id"], comp["effective_recency"]
                    cur_active = conn.execute("SELECT id, started_at FROM sessions WHERE chat_id = ? AND source = 'companion' AND active = 1 ORDER BY started_at DESC LIMIT 1", (cid,)).fetchone()
                    cur_max_ts = 0.0
                    if cur_active is not None:
                        mrow = conn.execute("SELECT MAX(created_at) FROM messages WHERE session_id = ? AND active = 1", (cur_active["id"],)).fetchone()
                        cur_max_ts = max(float(cur_active["started_at"] or 0.0), float(mrow[0] or 0.0))
                    should_activate = (comp.get("end_reason") not in _BOUNDARY_END_REASONS) and (cur_active is None or cur_active["id"] == sid or recency > cur_max_ts)
                    if should_activate:
                        conn.execute("UPDATE sessions SET active = 0, ended_at = COALESCE(ended_at, ?) WHERE chat_id = ? AND source = 'companion' AND active = 1 AND id != ?", (now, cid, sid))
                    act_flag, eff_ended = (1, None) if should_activate else (0, comp["ended_at"])
                    counts["sessions"] += max(0, conn.execute(
                        "INSERT INTO sessions (id, chat_id, source, model, started_at, ended_at, active, metadata_json) VALUES (?, ?, 'companion', ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id, model = COALESCE(excluded.model, sessions.model), ended_at = excluded.ended_at, active = CASE WHEN ? = 1 THEN 1 ELSE sessions.active END",
                        (sid, cid, comp["model"], comp["started_at"], eff_ended, act_flag, _json_dumps({"legacy_imported": True}), act_flag),
                    ).rowcount)
                    for msg_vals in comp["messages"]:
                        counts["messages"] += max(0, conn.execute(
                            "INSERT OR IGNORE INTO messages (session_id, chat_id, turn_id, role, content, tool_calls_json, tool_call_id, tool_name, extra_json, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (sid, cid, *msg_vals),
                        ).rowcount)
                for pk_str, base_vals, counters, lseen in extracted["usage_rows"]:
                    prev = usage_snap.get(pk_str)
                    if prev is not None:
                        if lseen <= float(prev[8]):
                            continue
                        deltas = tuple(max(0, counters[i] - prev[i]) for i in range(8))
                        cur_u = conn.execute(
                            f"INSERT INTO session_model_usage ({_USAGE_COLS}) VALUES ({','.join('?' * 17)}) ON CONFLICT({_USAGE_PK}) DO UPDATE SET {delta_set_sql}, "
                            "first_seen = MIN(session_model_usage.first_seen, excluded.first_seen), last_seen = MAX(session_model_usage.last_seen, excluded.last_seen)",
                            (*base_vals, *deltas),
                        )
                    else:
                        cur_u = conn.execute(
                            f"INSERT INTO session_model_usage ({_USAGE_COLS}) VALUES ({','.join('?' * 17)}) ON CONFLICT({_USAGE_PK}) DO UPDATE SET {repl_set_sql}, "
                            "source = CASE WHEN session_model_usage.source = '' THEN excluded.source ELSE session_model_usage.source END, "
                            "first_seen = MIN(session_model_usage.first_seen, excluded.first_seen), last_seen = excluded.last_seen WHERE excluded.last_seen > session_model_usage.last_seen",
                            base_vals,
                        )
                    counts["usage"] += max(0, cur_u.rowcount)
                    usage_snap[pk_str] = [*counters, lseen]
                for grow in extracted["goals"]:
                    gkey, raw_val = grow["meta_key"], grow["state_json"]
                    prev_raw = goal_snap.get(gkey)
                    eff_updated_at = max(grow["updated_at"], legacy_mtime_s) if (prev_raw is not None and prev_raw != raw_val) else grow["updated_at"]
                    goal_snap[gkey] = raw_val
                    if conn.execute("SELECT 1 FROM session_goals WHERE chat_id = ?", (grow["chat_id"],)).fetchone() is None and grow["status"] not in ("active", "paused", "blocked"):
                        continue
                    counts["goals"] += max(0, conn.execute(
                        "INSERT INTO session_goals (chat_id, session_key, status, objective, gate, iteration, max_iterations, updated_at, state_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET session_key = COALESCE(excluded.session_key, session_goals.session_key), status = excluded.status, objective = excluded.objective, "
                        "gate = excluded.gate, iteration = excluded.iteration, max_iterations = excluded.max_iterations, updated_at = excluded.updated_at, state_json = excluded.state_json "
                        "WHERE excluded.updated_at > session_goals.updated_at",
                        (grow["chat_id"], grow["session_key"], grow["status"], grow["objective"], grow["gate"], grow["iteration"], grow["max_iterations"], eff_updated_at, raw_val),
                    ).rowcount)

                watermark = {**sig, "max_started_at": extracted["max_started_at"], "max_last_seen": extracted["max_last_seen"]}
                self._set_sync_meta_on_conn(conn, _LEGACY_SYNC_META_KEY, watermark, now)
                if len(usage_snap) <= 20_000:
                    self._set_sync_meta_on_conn(conn, _LEGACY_USAGE_SNAP_KEY, usage_snap, now)
                if len(goal_snap) <= 5_000:
                    self._set_sync_meta_on_conn(conn, _LEGACY_GOAL_SNAP_KEY, goal_snap, now)

            counts["synced"] = 1
            return counts


def _split_sql_statements(script: str) -> list[str]:
    stmts, buf = [], ""
    for line in script.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        stmts.append(buf.strip())
    return stmts


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _extract_legacy_snapshot(conn: sqlite3.Connection, fallback_mtime: float) -> dict[str, Any]:
    tables = {str(r["name"]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    all_sessions, session_by_id, latest_by_chat, max_started_at = [], {}, {}, 0.0
    if "sessions" in tables:
        scols = _table_columns(conn, "sessions")
        sel = ["id", "source" if "source" in scols else "'companion' AS source", "chat_id" if "chat_id" in scols else "'' AS chat_id",
               "started_at" if "started_at" in scols else "0.0 AS started_at",
               *(c if c in scols else f"NULL AS {c}" for c in ("session_key", "model", "ended_at", "end_reason", "last_activity_at"))]
        for r in conn.execute(f"SELECT {', '.join(sel)} FROM sessions ORDER BY COALESCE(started_at, 0) DESC, rowid DESC").fetchall():
            sid = str(r["id"] or "").strip()
            if not sid:
                continue
            started, source = float(r["started_at"] or fallback_mtime), str(r["source"] or "companion")
            max_started_at = max(max_started_at, started)
            skey = str(r["session_key"] or "").strip() or None
            cid = str(r["chat_id"] or "").strip() or (skey.rsplit(":", 1)[-1] if source == "companion" and skey else "")
            entry = {"id": sid, "source": source, "chat_id": cid, "session_key": skey, "model": r["model"], "started_at": started,
                     "ended_at": float(r["ended_at"]) if r["ended_at"] is not None else None,
                     "end_reason": str(r["end_reason"] or "").strip() or None, "last_activity_at": float(r["last_activity_at"] or 0.0)}
            all_sessions.append(entry)
            session_by_id[sid] = entry
            if source == "companion" and cid and cid not in latest_by_chat:
                latest_by_chat[cid] = entry

    mcols = _table_columns(conn, "messages") if "messages" in tables else set()
    latest_companion_sessions, extra_fields = [], ("reasoning_content", "reasoning", "reasoning_details", "codex_reasoning_items", "finish_reason", "message_uid")
    for comp in latest_by_chat.values():
        sid, msgs, max_msg_ts = comp["id"], [], 0.0
        if {"session_id", "role"}.issubset(mcols):
            active_pred = " AND COALESCE(active, 1) = 1" if "active" in mcols else ""
            msel = ["id" if "id" in mcols else "rowid AS id", "role",
                    *(c if c in mcols else f"NULL AS {c}" for c in ("content", "tool_calls", "tool_call_id", "tool_name", "platform_message_id", *extra_fields)),
                    "timestamp" if "timestamp" in mcols else f"{comp['started_at']!r} AS timestamp"]
            for mr in conn.execute(f"SELECT {', '.join(msel)} FROM messages WHERE session_id = ?{active_pred} ORDER BY id ASC", (sid,)).fetchall():
                ts = float(mr["timestamp"] if mr["timestamp"] is not None else comp["started_at"])
                max_msg_ts = max(max_msg_ts, ts)
                text_content, parts = _decode_legacy_content(mr["content"])
                extra_obj: dict[str, Any] = {"legacy_key": f"state_db:{sid}:{mr['id']}", **({"content_parts": parts} if parts is not None else {})}
                for k in extra_fields:
                    if mr[k] not in (None, ""):
                        if k in ("reasoning_details", "codex_reasoning_items") and isinstance(mr[k], str):
                            parsed_blk = _json_loads(_scrub_str(mr[k]), default=None)
                            extra_obj[k] = parsed_blk if isinstance(parsed_blk, list) else _scrub_str(mr[k])
                        else:
                            extra_obj[k] = _scrub_str(mr[k]) if isinstance(mr[k], str) else mr[k]
                tc_raw = mr["tool_calls"]
                msgs.append((str(mr["platform_message_id"]).strip() if mr["platform_message_id"] else None, str(mr["role"] or "user"), text_content,
                             _scrub_str(tc_raw) if isinstance(tc_raw, str) and tc_raw.strip() else None, str(mr["tool_call_id"]) if mr["tool_call_id"] else None,
                             _scrub_str(str(mr["tool_name"])) if mr["tool_name"] else None, _json_dumps(extra_obj), ts))
        latest_companion_sessions.append({**comp, "effective_recency": max(comp["started_at"], comp["last_activity_at"], max_msg_ts), "messages": msgs})

    usage_rows, max_last_seen = [], 0.0
    if "session_model_usage" in tables:
        ucols = _table_columns(conn, "session_model_usage")
        usel = ["session_id", "model" if "model" in ucols else "'unknown' AS model",
                *(c if c in ucols else f"'' AS {c}" for c in ("billing_provider", "billing_base_url", "billing_mode", "task")),
                *(c if c in ucols else f"0 AS {c}" for c in ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")),
                *(c if c in ucols else f"0.0 AS {c}" for c in ("estimated_cost_usd", "actual_cost_usd")),
                *(c if c in ucols else f"NULL AS {c}" for c in ("first_seen", "last_seen"))]
        for ur in conn.execute(f"SELECT {', '.join(usel)} FROM session_model_usage").fetchall():
            usid = str(ur["session_id"] or "").strip()
            if not usid:
                continue
            sess_info = session_by_id.get(usid)
            if sess_info is None:
                sess_info = {"id": usid, "source": "companion", "chat_id": "", "session_key": None, "model": ur["model"],
                             "started_at": float(ur["first_seen"] or ur["last_seen"] or fallback_mtime), "ended_at": None, "end_reason": None, "last_activity_at": 0.0}
                all_sessions.append(sess_info)
                session_by_id[usid] = sess_info
            lseen = float(ur["last_seen"] if ur["last_seen"] is not None else (ur["first_seen"] if ur["first_seen"] is not None else sess_info["started_at"]))
            fseen = float(ur["first_seen"] if ur["first_seen"] is not None else lseen)
            max_last_seen = max(max_last_seen, lseen)
            route = (usid, str(ur["model"] or "unknown"), str(ur["billing_provider"] or ""), str(ur["billing_base_url"] or ""), str(ur["billing_mode"] or ""), str(ur["task"] or ""))
            counters = (int(ur["api_call_count"] or 0), int(ur["input_tokens"] or 0), int(ur["output_tokens"] or 0), int(ur["cache_read_tokens"] or 0),
                        int(ur["cache_write_tokens"] or 0), int(ur["reasoning_tokens"] or 0), float(ur["estimated_cost_usd"] or 0.0), float(ur["actual_cost_usd"] or 0.0))
            usage_rows.append(("|".join(route), (*route, sess_info["source"], *counters, fseen, lseen), counters, lseen))

    goals_by_chat: dict[str, dict[str, Any]] = {}
    if "state_meta" in tables and {"key", "value"}.issubset(_table_columns(conn, "state_meta")):
        for gr in conn.execute("SELECT key, value FROM state_meta WHERE key LIKE 'goal:%' AND value IS NOT NULL").fetchall():
            key_str, raw_json = str(gr["key"] or ""), str(gr["value"] or "").strip()
            gsid = key_str[5:].strip()
            parsed = _json_loads(raw_json, default=None) if (gsid and raw_json) else None
            if not isinstance(parsed, dict):
                continue
            sess = session_by_id.get(gsid)
            if sess is not None:
                if sess["source"] != "companion" or not sess["chat_id"] or latest_by_chat.get(sess["chat_id"], {}).get("id") != gsid:
                    continue
                cid, skey, sess_started = sess["chat_id"], sess["session_key"], sess["started_at"]
            else:
                cid, skey, sess_started = str(parsed.get("chat_id") or gsid), None, 0.0
            explicit_ts = max(float(parsed.get(k) or 0.0) for k in ("updated_at", "last_turn_at", "created_at"))
            gates_val = parsed.get("gates")
            gate_str = _json_dumps(gates_val) if (isinstance(gates_val, list) and gates_val) else (parsed["gate"].strip() if isinstance(parsed.get("gate"), str) and parsed["gate"].strip() else None)
            entry = {"meta_key": key_str, "chat_id": cid, "session_key": skey, "status": str(parsed.get("status") or "active"),
                     "objective": _scrub_str(str(parsed.get("goal") or parsed.get("objective") or "")) or "", "gate": gate_str,
                     "iteration": int(parsed.get("turns_used") or parsed.get("iteration") or 0), "max_iterations": int(parsed.get("max_turns") or parsed.get("max_iterations") or 25),
                     "updated_at": explicit_ts if explicit_ts > 0.0 else (sess_started or fallback_mtime), "state_json": _scrub_str(raw_json) or "{}"}
            if cid not in goals_by_chat or entry["updated_at"] >= goals_by_chat[cid]["updated_at"]:
                goals_by_chat[cid] = entry

    return {"all_sessions": all_sessions, "latest_companion_sessions": latest_companion_sessions,
            "usage_rows": usage_rows, "goals": list(goals_by_chat.values()), "max_started_at": max_started_at, "max_last_seen": max_last_seen}


def sync_from_legacy_state_db(
    home: Path | str | None = None, *, muse_db_path: Path | str | None = None,
    state_db_path: Path | str | None = None, force: bool = False,
) -> dict[str, int]:
    """Module-level watermark incremental migrator (never creates empty muse.db if state.db is absent)."""
    base_home = Path(home).expanduser() if home is not None else get_muse_home()
    src = Path(state_db_path).expanduser() if state_db_path is not None else get_legacy_state_db_path(base_home)
    if not src.is_file():
        return {"sessions": 0, "messages": 0, "usage": 0, "goals": 0, "synced": 0}
    dst = Path(muse_db_path).expanduser() if muse_db_path is not None else get_muse_db_path(base_home)
    with MuseDB(dst) as db:
        return db.sync_from_legacy_state_db(src, force=force)


def close_all() -> None:
    """Close all open `MuseDB` connections in this process (ensuring Windows file handles release)."""
    import sys

    rt = sys.modules.get("muse.runtime")
    if rt is not None and callable(getattr(rt, "close_all_shared_dbs", None)):
        rt.close_all_shared_dbs()
    with _REGISTRY_LOCK:
        instances = list(_OPEN_INSTANCES)
    for db in instances:
        db.close()


__all__ = ["MuseDB", "close_all", "sync_from_legacy_state_db"]
