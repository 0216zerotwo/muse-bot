"""Comprehensive unit tests for `muse.db` (schema, FTS5 trigram/CJK, crash healing, turn retry/undo, and zero-mutation legacy migrator)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from muse import db as muse_db
from muse.db import MuseDB, close_all, sync_from_legacy_state_db
from muse.paths import muse_home_override


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


class MuseDBCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="muse-db-test 中文 "))
        self.db_path = self.tmp_root / "muse.db"

    def tearDown(self) -> None:
        close_all()
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_schema_pragmas_tables_indexes_and_windows_handle_release(self) -> None:
        with MuseDB(self.db_path) as db:
            conn = db._require_conn()
            jmode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            self.assertEqual(jmode, "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL = 1
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            for expected in (
                "sessions",
                "messages",
                "messages_fts",
                "session_model_usage",
                "session_goals",
                "sync_meta",
            ):
                self.assertIn(expected, tables)

            indexes = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
            self.assertIn("idx_sessions_chat_active_started", indexes)

            # Virtual alias columns on session_model_usage (`provider`, `base_url`, `requests`) + exact Hermes columns.
            db.record_usage(
                "sess-1",
                "deepseek-chat",
                provider="deepseek",
                base_url="https://api.deepseek.com",
                input_tokens=1000,
                output_tokens=250,
                cache_read_tokens=400,
                requests=2,
            )
            alias_row = conn.execute(
                "SELECT provider, base_url, requests, billing_provider, billing_base_url, api_call_count "
                "FROM session_model_usage WHERE session_id = 'sess-1'"
            ).fetchone()
            self.assertEqual(tuple(alias_row), ("deepseek", "https://api.deepseek.com", 2, "deepseek", "https://api.deepseek.com", 2))

        # Handle must be fully released on Windows so unlink succeeds immediately!
        os.unlink(self.db_path)
        self.assertFalse(self.db_path.exists())

    def test_close_all_releases_open_instances(self) -> None:
        db = MuseDB(self.db_path)
        db.create_session("main")
        close_all()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.get_active_session("main")
        os.unlink(self.db_path)
        self.assertFalse(self.db_path.exists())

    def test_posix_0600_permission_helper(self) -> None:
        target = self.tmp_root / "perm.db"
        target.write_bytes(b"sqlite")
        wal = self.tmp_root / "perm.db-wal"
        wal.write_bytes(b"wal")
        chmod_calls: list[tuple[str, int]] = []

        with (
            mock.patch.object(muse_db.os, "name", "posix"),
            mock.patch.object(muse_db.os, "chmod", side_effect=lambda p, m: chmod_calls.append((os.fspath(p), m))),
        ):
            muse_db._secure_db_files(target, create_main=False)

        self.assertEqual(chmod_calls, [(os.fspath(target), 0o600), (os.fspath(wal), 0o600)])

    def test_multithreaded_concurrent_writes_and_fts_reads(self) -> None:
        import concurrent.futures

        with MuseDB(self.db_path) as db:
            sid = db.create_session("main")

            def worker(idx: int) -> int:
                mid = db.append_message(sid, "main", "user", f"并发写入压力测试条目编号 {idx} 数据一致性校验", turn_id=f"t-{idx}")
                db.record_usage(sid, "gpt-5", provider="openai", input_tokens=10, output_tokens=5, requests=1)
                hits = db.search_messages("并发写入压力测试")
                return mid if hits else 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(worker, range(32)))

            self.assertEqual(len([r for r in results if r > 0]), 32)
            self.assertEqual(len(db.get_messages(sid)), 32)
            u_rows = db.query_usage_since(0.0)
            self.assertEqual(u_rows[0][4], 32)
            self.assertEqual(u_rows[0][5], 320)

    def test_sessions_lifecycle_and_silent_renew(self) -> None:
        with MuseDB(self.db_path) as db:
            s1 = db.create_session("main", model="gpt-5", metadata={"persona": "v1"})
            active = db.get_active_session("main")
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active["id"], s1)
            self.assertEqual(active["metadata"], {"persona": "v1"})

            s2 = db.renew_session("main", model="claude-sonnet-4-6", metadata={"persona": "v2"})
            self.assertNotEqual(s1, s2)
            old = db.get_session(s1)
            cur = db.get_active_session("main")
            assert old is not None and cur is not None
            self.assertFalse(old["active"])
            self.assertIsNotNone(old["ended_at"])
            self.assertEqual(cur["id"], s2)
            self.assertTrue(cur["active"])

    def test_extra_json_reasoning_signatures_multimodal_and_surrogates(self) -> None:
        with MuseDB(self.db_path) as db:
            sid = db.create_session("main")
            # Multimodal content + lone surrogate in scraped text
            m1 = db.append_message(
                sid,
                "main",
                "user",
                [
                    {"type": "text", "text": "帮我分析这张架构图 \ud83d"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
                turn_id="t1",
            )
            m2 = db.append_message(
                sid,
                "main",
                "assistant",
                "这是三层异步内核。",
                turn_id="t1",
                tool_calls=[
                    {
                        "id": "call_gem_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "sig-xyz"}},
                    }
                ],
                extra={
                    "reasoning_content": "先检查入口路由...",
                    "thought_signature": "sig-msg-level",
                    "thinking_blocks": [
                        {"type": "thinking", "thinking": "deep check", "signature": "ant-sig-1"},
                        {"type": "redacted_thinking", "data": "enc-blob"},
                    ],
                },
            )
            self.assertGreater(m2, m1)
            msgs = db.get_messages(sid)
            self.assertEqual(len(msgs), 2)
            self.assertIn("帮我分析这张架构图", msgs[0]["content"])
            self.assertEqual(len(msgs[0]["extra"]["content_parts"]), 2)
            self.assertEqual(msgs[1]["extra"]["reasoning_content"], "先检查入口路由...")
            self.assertEqual(msgs[1]["extra"]["thought_signature"], "sig-msg-level")
            self.assertEqual(len(msgs[1]["extra"]["thinking_blocks"]), 2)
            self.assertEqual(
                msgs[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
                "sig-xyz",
            )

    def test_fts5_trigram_cjk_like_fallback_and_soft_delete_sync(self) -> None:
        with MuseDB(self.db_path) as db:
            self.assertEqual(db.fts_tokenizer, "trigram")
            s_main = db.create_session("main", model="deepseek-chat")
            s_side = db.create_session("side-1", model="qwen-max")

            m1 = db.append_message(s_main, "main", "user", "今天中午做了低碳水饮食记录分析，热量 520 千卡", turn_id="t1")
            db.append_message(s_main, "main", "assistant", "养了一只橘猫，名字叫团子，特别粘人", turn_id="t1")
            db.append_message(s_side, "side-1", "user", "副会话里的饮食记录与运动周报 100%_ok", turn_id="t2")

            # 1) >= 3 CJK chars -> FTS5 trigram match
            hits_3plus = db.search_messages("饮食记录")
            self.assertEqual(len(hits_3plus), 2)

            # Scoped to chat_id="main" + trailing wildcard stripped cleanly
            hits_main = db.search_messages("饮食记录*", chat_id="main")
            self.assertEqual(len(hits_main), 1)
            self.assertEqual(hits_main[0]["id"], m1)

            # 2) 1-2 char CJK -> LIKE fallback
            hits_1char = db.search_messages("猫")
            self.assertEqual(len(hits_1char), 1)
            self.assertIn("橘猫", hits_1char[0]["content"])

            hits_2char = db.search_messages("热量", chat_id="main")
            self.assertEqual(len(hits_2char), 1)

            # 3) LIKE wildcard escaping (`100%_ok` must NOT match `1000Xok`)
            db.append_message(s_side, "side-1", "assistant", "1000Xok should not match literal percent-underscore")
            hits_escaped = db.search_messages("100%_ok")
            self.assertEqual(len(hits_escaped), 1)
            self.assertIn("100%_ok", hits_escaped[0]["content"])

            # 4) Soft-delete / compaction sync removes deactivated messages from FTS5 immediately!
            db.compact_session_messages(s_main, upto_message_id=m1, summary_checkpoint="[SUMMARY]: 用户记录了午餐热量")
            self.assertEqual(db.search_messages("饮食记录", chat_id="main"), [])
            sess = db.get_session(s_main)
            assert sess is not None
            self.assertEqual(sess["summary_checkpoint"], "[SUMMARY]: 用户记录了午餐热量")

    def test_fts5_unicode61_fallback_when_trigram_unavailable(self) -> None:
        fallback_path = self.tmp_root / "fallback_unicode61.db"
        real_ensure = MuseDB._ensure_fts_table

        def simulated_no_trigram(conn: sqlite3.Connection) -> str:
            orig_exec = conn.execute

            class Wrapper:
                def execute(self, sql: str, *args: object):
                    if "tokenize='trigram'" in sql:
                        raise sqlite3.OperationalError("no such tokenizer: trigram")
                    return orig_exec(sql, *args)

            return real_ensure(Wrapper())  # type: ignore[arg-type]

        with mock.patch.object(MuseDB, "_ensure_fts_table", staticmethod(simulated_no_trigram)):
            with MuseDB(fallback_path) as db:
                self.assertEqual(db.fts_tokenizer, "unicode61")
                sid = db.create_session("main")
                db.append_message(sid, "main", "user", "Fallback database migration verification 数据库迁移测试")
                self.assertEqual(len(db.search_messages("migration")), 1)
                self.assertEqual(len(db.search_messages("数据库迁移")), 1)
                self.assertEqual(len(db.search_messages("迁移")), 1)

    def test_heal_orphan_tool_calls_tail_and_mid_history(self) -> None:
        with MuseDB(self.db_path) as db:
            sid = db.create_session("main")
            # Mid-history orphan followed by user message, plus a tail partial orphan!
            db.append_message(sid, "main", "user", "第一步", turn_id="t1")
            db.append_message(
                sid,
                "main",
                "assistant",
                "",
                turn_id="t1",
                tool_calls=[{"id": "call_mid_1", "function": {"name": "web_search", "arguments": "{}"}}],
            )
            # Crash happened before call_mid_1 finished; user later sent t2 which crashed mid-batch too:
            db.append_message(sid, "main", "user", "还在吗？", turn_id="t2")
            db.append_message(
                sid,
                "main",
                "assistant",
                "",
                turn_id="t2",
                tool_calls=[
                    {"id": "call_tail_ok", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "call_tail_orphan", "function": {"name": "terminal", "arguments": "{}"}},
                ],
            )
            db.append_message(
                sid,
                "main",
                "tool",
                "file contents",
                turn_id="t2",
                tool_call_id="call_tail_ok",
                tool_name="read_file",
            )

            healed = db.heal_orphan_tool_calls(sid)
            self.assertEqual(healed, 2)
            # Idempotent on second call
            self.assertEqual(db.heal_orphan_tool_calls(sid), 0)

            msgs = db.get_messages(sid)
            roles_and_ids = [(m["role"], m.get("tool_call_id"), m["content"]) for m in msgs]
            self.assertEqual(
                roles_and_ids,
                [
                    ("user", None, "第一步"),
                    ("assistant", None, ""),
                    ("tool", "call_mid_1", "[Interrupted before completion]"),
                    ("user", None, "还在吗？"),
                    ("assistant", None, ""),
                    ("tool", "call_tail_ok", "file contents"),
                    ("tool", "call_tail_orphan", "[Interrupted before completion]"),
                ],
            )

    def test_rollback_turn_for_retry_preserves_clarify_card_and_tool_result(self) -> None:
        with MuseDB(self.db_path) as db:
            sid = db.create_session("main")
            # turn_A: user asks -> assistant emits clarify tool call
            db.append_message(sid, "main", "user", "帮我订明天去上海的票", turn_id="turn_A")
            db.append_message(
                sid,
                "main",
                "assistant",
                "",
                turn_id="turn_A",
                tool_calls=[{"id": "call_clarify_1", "function": {"name": "clarify", "arguments": '{"question":"哪个车站？"}'}}],
            )
            placeholder_id = db.append_message(
                sid,
                "main",
                "tool",
                "[Awaiting user response]",
                turn_id="turn_A",
                tool_call_id="call_clarify_1",
                tool_name="clarify",
            )

            # User clicks option in turn_B -> placeholder updated with choice and bound to turn_B, then assistant runs
            db.update_message(placeholder_id, content="上海虹桥", turn_id="turn_B")
            db.append_message(
                sid,
                "main",
                "assistant",
                "",
                turn_id="turn_B",
                tool_calls=[{"id": "call_search_9", "function": {"name": "web_search", "arguments": "{}"}}],
            )
            db.append_message(
                sid,
                "main",
                "tool",
                "G101 08:00",
                turn_id="turn_B",
                tool_call_id="call_search_9",
                tool_name="web_search",
            )
            db.append_message(sid, "main", "assistant", "为您找到独特的联程方案 G101。", turn_id="turn_B")

            # Now user clicks /retry on turn_B!
            removed = db.rollback_turn_for_retry(sid, "turn_B")
            self.assertEqual(removed, 3)

            remaining = db.get_messages(sid)
            self.assertEqual(len(remaining), 3)
            self.assertEqual(remaining[0]["turn_id"], "turn_A")
            self.assertEqual(remaining[0]["role"], "user")
            self.assertEqual(remaining[1]["turn_id"], "turn_A")
            self.assertEqual(remaining[1]["role"], "assistant")
            self.assertEqual(remaining[2]["turn_id"], "turn_B")
            self.assertEqual(remaining[2]["role"], "tool")
            self.assertEqual(remaining[2]["content"], "上海虹桥")
            # Retried assistant text must be purged from FTS5 too!
            self.assertEqual(db.search_messages("独特的联程方案"), [])

    def test_undo_last_exchange(self) -> None:
        with MuseDB(self.db_path) as db:
            sid = db.create_session("main")
            db.append_message(sid, "main", "user", "第一轮问题", turn_id="t1")
            db.append_message(sid, "main", "assistant", "第一轮回答", turn_id="t1")
            db.append_message(sid, "main", "user", "第二轮问题误发", turn_id="t2")
            db.append_message(sid, "main", "assistant", "第二轮回答撤回词", turn_id="t2")

            undone = db.undo_last_exchange(sid)
            self.assertEqual(undone, 2)
            msgs = db.get_messages(sid)
            self.assertEqual([m["content"] for m in msgs], ["第一轮问题", "第一轮回答"])
            self.assertEqual(db.search_messages("撤回词"), [])


class LegacyStateDbMigratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="muse-mig-home-"))
        self.state_db = self.home / "state.db"
        self.muse_db = self.home / "muse.db"

    def tearDown(self) -> None:
        close_all()
        shutil.rmtree(self.home, ignore_errors=True)

    def _init_legacy_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                chat_id TEXT,
                session_key TEXT,
                model TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                last_activity_at REAL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                platform_message_id TEXT,
                reasoning_content TEXT,
                reasoning TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                finish_reason TEXT,
                message_uid TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                billing_provider TEXT NOT NULL DEFAULT '',
                billing_base_url TEXT NOT NULL DEFAULT '',
                billing_mode TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                actual_cost_usd REAL NOT NULL DEFAULT 0.0,
                first_seen REAL,
                last_seen REAL,
                PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
            );
            CREATE TABLE IF NOT EXISTS state_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )

    def test_absent_state_db_never_creates_muse_db(self) -> None:
        with muse_home_override(self.home):
            res = sync_from_legacy_state_db()
        self.assertEqual(res["synced"], 0)
        self.assertFalse(self.muse_db.exists())

    def test_zero_byte_mutation_sha256_idempotency_and_roundtrip_merge(self) -> None:
        t0 = time.time() - 600.0
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            self._init_legacy_schema(conn)
            # Older superseded session s_old + latest session s1 (ended_at set by graceful gateway shutdown 'agent_close')
            conn.execute(
                "INSERT INTO sessions VALUES ('s_old', 'companion', 'main', 'companion:main', 'gpt-4o', ?, ?, 'session_reset', ?)",
                (t0 - 100, t0 - 50, t0 - 50),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_old', 'user', '旧对话不应混入当前活跃会话', ?)",
                (t0 - 90,),
            )
            conn.execute(
                "INSERT INTO sessions VALUES ('s1', 'companion', 'main', 'companion:main', 'deepseek-chat', ?, ?, 'agent_close', ?)",
                (t0, t0 + 20, t0 + 20),
            )
            conn.execute(
                "INSERT INTO sessions VALUES ('s_cron', 'cron', '', 'cron:daily', 'deepseek-chat', ?, ?, 'agent_close', ?)",
                (t0 + 5, t0 + 10, t0 + 10),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, platform_message_id) VALUES (?, ?, ?, ?, ?)",
                ("s1", "user", "\x00json:" + json.dumps([{"type": "text", "text": "帮我盯紧本周部署任务"}]), t0 + 1, "turn-legacy-1"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, platform_message_id, reasoning_content) VALUES (?, ?, ?, ?, ?, ?)",
                ("s1", "assistant", "好的，已建立部署清单。", t0 + 2, "turn-legacy-1", "正在梳理步骤..."),
            )
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('s1', 'deepseek-chat', 'deepseek', 'https://api.deepseek.com', '', '', 2, 1000, 300, 200, 0, 50, 0.01, 0.0, ?, ?)",
                (t0 + 1, t0 + 20),
            )
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('s_cron', 'deepseek-chat', 'deepseek', 'https://api.deepseek.com', '', '', 1, 500, 100, 0, 0, 0, 0.005, 0.0, ?, ?)",
                (t0 + 5, t0 + 10),
            )
            # Stale goal on s_old vs active goal on latest s1
            conn.execute(
                "INSERT INTO state_meta VALUES ('goal:s_old', ?)",
                (json.dumps({"goal": "旧目标", "status": "active", "created_at": t0 - 90}),),
            )
            conn.execute(
                "INSERT INTO state_meta VALUES ('goal:s1', ?)",
                (
                    json.dumps(
                        {
                            "goal": "完成 v2.0 灰度上线",
                            "status": "active",
                            "turns_used": 2,
                            "max_turns": 15,
                            "created_at": t0 + 2,
                            "last_turn_at": t0 + 15,
                            "last_verdict": "continue",
                            "last_reason": "已完成数据库层校验",
                            "gates": [{"command": "pytest"}],
                        }
                    ),
                ),
            )
            conn.commit()

        sha_before = _sha256(self.state_db)
        sidecars_before = sorted(p.name for p in self.home.glob("state.db*"))

        # Step 1: Initial v1.x -> v2.0 sync
        res1 = sync_from_legacy_state_db(self.home)
        self.assertEqual(_sha256(self.state_db), sha_before)
        self.assertEqual(sorted(p.name for p in self.home.glob("state.db*")), sidecars_before)
        self.assertEqual(res1["synced"], 1)
        self.assertEqual(res1["messages"], 2)
        self.assertEqual(res1["usage"], 2)
        self.assertEqual(res1["goals"], 1)

        # Watermark skip when unchanged + idempotency when forced
        self.assertEqual(sync_from_legacy_state_db(self.home)["synced"], 0)
        forced = sync_from_legacy_state_db(self.home, force=True)
        self.assertEqual(forced["messages"], 0)

        # Step 2: Native v2.0 writes new turn & usage onto s1
        with MuseDB(self.muse_db) as db:
            active_sess = db.get_active_session("main")
            assert active_sess is not None
            self.assertEqual(active_sess["id"], "s1")
            msgs = db.get_messages("s1")
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["content"], "帮我盯紧本周部署任务")
            self.assertEqual(msgs[1]["extra"]["reasoning_content"], "正在梳理步骤...")

            banner = db.get_goal_banner_state("main")
            self.assertEqual(
                banner,
                {
                    "active": True,
                    "status": "active",
                    "objective": "完成 v2.0 灰度上线",
                    "ui_summary": "已完成数据库层校验",
                },
            )

            db.append_message("s1", "main", "user", "v2.0 原生新增消息", turn_id="turn-v2-1", created_at=t0 + 100)
            db.record_usage(
                "s1",
                "deepseek-chat",
                billing_provider="deepseek",
                billing_base_url="https://api.deepseek.com",
                input_tokens=800,
                output_tokens=200,
                requests=1,
                seen_at=t0 + 100,
            )

        # Step 3: Simulate rollback v2.0 -> v1.x where Hermes writes incremental usage, message & clears goal!
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, platform_message_id) VALUES (?, ?, ?, ?, ?)",
                ("s1", "user", "回退 v1.x 期间产生的新消息", t0 + 200, "turn-v1-rb"),
            )
            # Hermes cumulative row advances from (calls=2, in=1000, out=300) -> (calls=3, in=1500, out=450)
            conn.execute(
                "UPDATE session_model_usage SET api_call_count = 3, input_tokens = 1500, output_tokens = 450, last_seen = ? "
                "WHERE session_id = 's1'",
                (t0 + 200,),
            )
            # Hermes clear_goal() mutates status -> 'cleared' WITHOUT bumping last_turn_at!
            cleared_goal = {
                "goal": "完成 v2.0 灰度上线",
                "status": "cleared",
                "turns_used": 2,
                "max_turns": 15,
                "created_at": t0 + 2,
                "last_turn_at": t0 + 15,
            }
            conn.execute("UPDATE state_meta SET value = ? WHERE key = 'goal:s1'", (json.dumps(cleared_goal),))
            conn.commit()

        sha_v1_after_rb = _sha256(self.state_db)

        # Step 4: Upgrade back to v2.0 -> incremental merge without double-counting or loss!
        res2 = sync_from_legacy_state_db(self.home)
        self.assertEqual(_sha256(self.state_db), sha_v1_after_rb)
        self.assertEqual(res2["synced"], 1)
        self.assertEqual(res2["messages"], 1)

        with MuseDB(self.muse_db) as db:
            all_msgs = db.get_messages("s1")
            self.assertEqual(len(all_msgs), 4)
            self.assertEqual(
                [m["content"] for m in all_msgs],
                [
                    "帮我盯紧本周部署任务",
                    "好的，已建立部署清单。",
                    "v2.0 原生新增消息",
                    "回退 v1.x 期间产生的新消息",
                ],
            )
            # Usage check: 1000 (v1 initial) + 800 (v2 native) + 500 (v1 rollback delta) = 2300 input_tokens (+ 200 cache_read = 2500 total in)
            u_rows = db.query_usage_since(t0 - 10)
            by_src = {r[3]: r for r in u_rows}
            comp_u = by_src["companion"]
            # (model, base_url, task, source, calls, in+cache_r+cache_w, out, cache_r)
            self.assertEqual(comp_u[4], 4)  # 2 + 1 + 1 calls
            self.assertEqual(comp_u[5], 2300 + 200)  # 2300 input + 200 cache_read
            self.assertEqual(comp_u[6], 300 + 200 + 150)  # 650 output
            self.assertEqual(by_src["cron"][4], 1)

            # Cleared goal during v1.x rollback must reflect as inactive in v2.0!
            self.assertEqual(db.get_goal_banner_state("main"), {"active": False})

    def test_crashed_wal_recovery_without_shm_leaves_source_files_byte_identical(self) -> None:
        now = time.time()
        conn = sqlite3.connect(str(self.state_db), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        self._init_legacy_schema(conn)

        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO sessions VALUES ('s_wal', 'companion', 'chat-wal', 'companion:chat-wal', 'claude-sonnet-4-6', ?, NULL, NULL, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_wal', 'user', '崩溃前仅写入 WAL 尚未 checkpoint 的关键消息', ?)",
            (now + 1,),
        )
        conn.execute(
            "INSERT INTO state_meta VALUES ('goal:s_wal', ?)",
            (
                json.dumps(
                    {
                        "goal": "恢复 WAL 数据",
                        "status": "paused",
                        "paused_reason": "等待人工确认网关",
                        "created_at": now,
                    }
                ),
            ),
        )
        conn.commit()

        wal_path = self.home / "state.db-wal"
        shm_path = self.home / "state.db-shm"
        self.assertTrue(wal_path.is_file())
        self.assertGreater(wal_path.stat().st_size, 0)

        # Snapshot raw main DB + uncheckpointed WAL bytes into a clean crash dir WITHOUT -shm
        crash_home = Path(tempfile.mkdtemp(prefix="muse-crashed-wal-"))
        try:
            crashed_db = crash_home / "state.db"
            crashed_wal = crash_home / "state.db-wal"
            shutil.copyfile(self.state_db, crashed_db)
            shutil.copyfile(wal_path, crashed_wal)
            conn.close()
            if shm_path.exists():
                pass
            self.assertFalse((crash_home / "state.db-shm").exists())

            db_sha_before = _sha256(crashed_db)
            wal_sha_before = _sha256(crashed_wal)

            res = sync_from_legacy_state_db(crash_home)
            self.assertEqual(res["synced"], 1)
            self.assertEqual(res["messages"], 1)
            self.assertEqual(res["goals"], 1)

            # ZERO byte changes to crashed state.db or state.db-wal, and NO state.db-shm created!
            self.assertEqual(_sha256(crashed_db), db_sha_before)
            self.assertEqual(_sha256(crashed_wal), wal_sha_before)
            self.assertFalse((crash_home / "state.db-shm").exists())

            with MuseDB(crash_home / "muse.db") as mdb:
                sess = mdb.get_active_session("chat-wal")
                assert sess is not None
                self.assertEqual(sess["id"], "s_wal")
                hits = mdb.search_messages("尚未 checkpoint")
                self.assertEqual(len(hits), 1)
                self.assertEqual(
                    mdb.get_goal_banner_state("chat-wal"),
                    {
                        "active": True,
                        "status": "blocked",
                        "objective": "恢复 WAL 数据",
                        "ui_summary": "等待人工确认网关",
                    },
                )
        finally:
            close_all()
            shutil.rmtree(crash_home, ignore_errors=True)

    def test_newer_native_goals_and_usage_are_not_overwritten_by_stale_legacy(self) -> None:
        now = time.time()
        with MuseDB(self.muse_db) as db:
            db.upsert_goal(
                "main",
                status="active",
                objective="v2.0 最新原生目标",
                updated_at=now + 500,
                state={"last_reason": "原生推进中"},
            )
            db.record_usage("s1", "gpt-5", billing_provider="openai", input_tokens=9000, output_tokens=1000, requests=5, seen_at=now + 500)

        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            self._init_legacy_schema(conn)
            conn.execute("INSERT INTO sessions VALUES ('s1', 'companion', 'main', 'companion:main', 'gpt-5', ?, NULL, NULL, ?)", (now - 500, now - 500))
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('s1', 'gpt-5', 'openai', '', '', '', 1, 100, 20, 0, 0, 0, 0.0, 0.0, ?, ?)",
                (now - 500, now - 400),
            )
            conn.execute(
                "INSERT INTO state_meta VALUES ('goal:s1', ?)",
                (json.dumps({"goal": "过期的旧目标", "status": "active", "created_at": now - 500, "last_turn_at": now - 400}),),
            )
            conn.commit()

        sync_from_legacy_state_db(self.home)
        with MuseDB(self.muse_db) as db:
            self.assertEqual(db.get_goal_banner_state("main")["objective"], "v2.0 最新原生目标")
            u_rows = db.query_usage_since(0.0)
            self.assertEqual(u_rows[0][5], 9000)

    def test_minimal_synthetic_state_db_and_corrupt_file_resilience(self) -> None:
        now = time.time()
        # Minimal 2-table schema matching companion/tests/test_models_config.py
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, started_at REAL)")
            conn.execute(
                "CREATE TABLE session_model_usage ("
                "session_id TEXT, model TEXT, billing_base_url TEXT, task TEXT, api_call_count INT, "
                "input_tokens INT, cache_read_tokens INT, cache_write_tokens INT, output_tokens INT, last_seen REAL)"
            )
            conn.execute("INSERT INTO sessions VALUES ('s_min', 'companion', 'main', ?)", (now,))
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('s_min', 'deepseek-chat', 'https://api.deepseek.com', '', 2, 1000, 200, 0, 300, ?)",
                (now,),
            )
            conn.commit()

        sha_min = _sha256(self.state_db)
        res = sync_from_legacy_state_db(self.home)
        self.assertEqual(_sha256(self.state_db), sha_min)
        self.assertEqual(res["usage"], 1)
        with MuseDB(self.muse_db) as db:
            rows = db.query_usage_since(now - 10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], 2)
            self.assertEqual(rows[0][5], 1200)

        # Corrupt non-SQLite file must degrade gracefully without altering bytes
        bad_home = Path(tempfile.mkdtemp(prefix="muse-bad-legacy-"))
        try:
            bad_db = bad_home / "state.db"
            bad_db.write_bytes(b"corrupt-non-sqlite-header-payload" * 4)
            bad_sha = _sha256(bad_db)
            res_bad = sync_from_legacy_state_db(bad_home)
            self.assertEqual(res_bad["synced"], 0)
            self.assertEqual(_sha256(bad_db), bad_sha)
        finally:
            close_all()
            shutil.rmtree(bad_home, ignore_errors=True)

    def test_multiprocess_concurrent_writes_and_roundtrip_sync_race(self) -> None:
        now = time.time()
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            self._init_legacy_schema(conn)
            conn.execute("INSERT INTO sessions VALUES ('s_mp', 'companion', 'main', 'companion:main', 'gpt-5', ?, NULL, NULL, ?)", (now, now))
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('s_mp', 'gpt-5', 'openai', '', '', '', 2, 1000, 200, 0, 0, 0, 0.0, 0.0, ?, ?)",
                (now, now + 1),
            )
            conn.commit()

        # Initial sync + native increment (+400) + v1.x rollback increment (+300 -> 1300 in state.db)
        sync_from_legacy_state_db(self.home)
        with MuseDB(self.muse_db) as db:
            db.record_usage("s_mp", "gpt-5", billing_provider="openai", input_tokens=400, output_tokens=50, requests=1, seen_at=now + 10)
        with contextlib.closing(sqlite3.connect(self.state_db)) as conn:
            conn.execute(
                "UPDATE session_model_usage SET api_call_count = 3, input_tokens = 1300, output_tokens = 260, last_seen = ? WHERE session_id = 's_mp'",
                (now + 50,),
            )
            conn.commit()

        sha_before = _sha256(self.state_db)
        repo_root = str(Path(__file__).resolve().parents[2])
        worker_code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from pathlib import Path; from muse.db import MuseDB, sync_from_legacy_state_db; "
            "home = Path(sys.argv[2]); wid = int(sys.argv[3]); "
            "sync_from_legacy_state_db(home); "
            "db = MuseDB(home / 'muse.db'); "
            "[db.append_message('s_mp', 'main', 'user', f'proc-{wid}-{k}') for k in range(5)]; "
            "[db.record_usage('s_mp', 'gpt-5', billing_provider='openai', input_tokens=10, requests=1) for _ in range(5)]; "
            "db.close()"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", worker_code, repo_root, str(self.home), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(6)
        ]
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, msg=f"worker failed: {err}\n{out}")

        self.assertEqual(_sha256(self.state_db), sha_before)
        with MuseDB(self.muse_db) as db:
            self.assertEqual(len(db.get_messages("s_mp")), 30)  # 6 procs * 5 messages
            u_rows = db.query_usage_since(0.0)
            # 1000 (initial) + 400 (native) + 300 (v1 delta synced ONCE across 6 racing procs!) + 6*5*10 (300) = 2000!
            self.assertEqual(u_rows[0][5], 2000)
            # 2 + 1 + 1 (delta) + 30 = 34 calls!
            self.assertEqual(u_rows[0][4], 34)


if __name__ == "__main__":
    unittest.main()
