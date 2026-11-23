"""Memory tidy/fade retry resilience, learn mtime skip & force lookback, and user_written cleanup."""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from _loader import chat_store, load, temp_dir


class FakeDiskMemoryStore:
    def __init__(self, mem_dir: Path):
        self.mem_dir = mem_dir
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, list[str]] = {"user": [], "memory": []}

    def load_from_disk(self) -> None:
        pass

    def _entries_for(self, target: str) -> list[str]:
        return list(self.data.get(target, []))

    def add(self, target: str, content: str) -> dict[str, object]:
        self.data.setdefault(target, []).append(content)
        (self.mem_dir / ("USER.md" if target == "user" else "MEMORY.md")).write_text(
            "\n".join(self.data[target]), encoding="utf-8"
        )
        return {"success": True}

    def remove(self, target: str, line: str, matched_entry: str | None = None) -> dict[str, object]:
        items = self.data.setdefault(target, [])
        key = matched_entry or line
        if key not in items:
            return {"success": False, "error": "not found"}
        items.remove(key)
        (self.mem_dir / ("USER.md" if target == "user" else "MEMORY.md")).write_text(
            "\n".join(items), encoding="utf-8"
        )
        return {"success": True}

    def apply_batch(self, target: str, ops: list[dict[str, object]]) -> dict[str, object]:
        items = self.data.setdefault(target, [])
        for op in ops:
            action = op.get("action")
            if action == "add":
                items.append(str(op["content"]))
            elif action == "remove":
                old = str(op.get("matched_entry") or op.get("old_text") or "")
                if old in items:
                    items.remove(old)
            elif action == "replace":
                old = str(op.get("matched_entry") or op.get("old_text") or "")
                new = str(op["content"])
                if old in items:
                    items[items.index(old)] = new
        (self.mem_dir / ("USER.md" if target == "user" else "MEMORY.md")).write_text(
            "\n".join(items), encoding="utf-8"
        )
        return {"success": True}


class MemoryOpsTest(unittest.TestCase):
    def setUp(self):
        self.folder, (
            self.memory,
            self.tidy,
            self.fade,
            self.learn,
            self.llm,
        ) = load("memory", "memory_tidy", "memory_fade", "memory_learn", "llm")
        self.mem_dir = self.folder / "memories"
        self.disk_store = FakeDiskMemoryStore(self.mem_dir)

        tools_pkg = types.ModuleType("tools")
        mem_tool = types.ModuleType("tools.memory_tool")
        mem_tool.load_on_disk_store = lambda: self.disk_store
        mem_tool.get_memory_dir = lambda: self.mem_dir
        tools_pkg.memory_tool = mem_tool
        self.enterContext(mock.patch.dict(sys.modules, {"tools": tools_pkg, "tools.memory_tool": mem_tool}))

    def test_tidy_keeps_due_state_when_llm_fails_then_succeeds_on_retry(self):
        for i in range(self.tidy.NEW_ENTRIES_DUE):
            self.disk_store.add("user", f"【喜好与习惯】喜欢喝手冲咖啡 {i}")
        self.disk_store.data["user"][0] = "【喜好与习惯】平时喜欢喝浅烘手冲咖啡"
        self.disk_store.data["user"][1] = "【喜好与习惯】平时喜欢喝手冲咖啡"

        self.assertTrue(self.tidy.due())

        def broken_propose(_entries, _protected):
            raise RuntimeError("llm unavailable")

        with mock.patch.object(self.tidy, "_propose", side_effect=broken_propose):
            res = self.tidy.tidy()
        self.assertEqual(res["changed"], 0)
        meta_after_fail = self.tidy._read_meta()
        self.assertEqual(meta_after_fail["last_tidy_at"], 0)
        self.assertEqual(meta_after_fail["seen"], [])
        self.assertTrue(self.tidy.due())

        def working_propose(_entries, _protected):
            return {"merge": [{"ids": [1, 2], "text": "【喜好与习惯】平时喜欢喝浅烘手冲咖啡"}], "drop": []}

        with mock.patch.object(self.tidy, "_propose", side_effect=working_propose):
            res_ok = self.tidy.tidy()
        self.assertEqual(res_ok["changed"], 1)
        meta_after_ok = self.tidy._read_meta()
        self.assertGreater(meta_after_ok["last_tidy_at"], 0)
        self.assertFalse(self.tidy.due())

    def test_fade_does_not_advance_fade_pass_at_when_llm_fails_with_zero_changes(self):
        long_entry = "【近况与计划】最近一直在对比几家云服务器的线路延迟和月付价格准备迁移部署"
        self.disk_store.add("user", long_entry)
        meta = self.tidy._read_meta()
        meta["seen_at"][self.tidy.entry_key(long_entry)] = time.time() - 25 * 86400
        self.tidy._write_meta(meta)

        with mock.patch.object(self.llm, "ask_json", side_effect=RuntimeError("llm down")):
            res_fail = self.fade.fade()
        self.assertEqual(res_fail, {"faded": 0, "forgot": 0})
        self.assertEqual(self.tidy._read_meta()["fade_pass_at"], 0)

        # Retry immediately WITHOUT force=True succeeds once LLM recovers!
        with mock.patch.object(
            self.llm,
            "ask_json",
            return_value={"short": [{"id": 1, "text": "对比几家云服务器的线路延迟"}]},
        ):
            res_ok = self.fade.fade()
        self.assertEqual(res_ok["faded"], 1)
        self.assertGreater(self.tidy._read_meta()["fade_pass_at"], 0)
        self.assertIn("【近况与计划】对比几家云服务器的线路延迟", self.disk_store._entries_for("user"))

    def test_learn_skips_untouched_transcripts_and_honors_force_lookback(self):
        store = chat_store(temp_dir("companion-learn-"))
        old_chat = store.create()
        active_chat = store.create()

        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 10 * 86400 * 1000
        store.append(old_chat, {"event": "user_message", "text": "很久以前的话", "created_at_ms": old_ms})
        old_path = store._path(old_chat)
        old_sec = old_ms / 1000.0
        os.utime(old_path, (old_sec, old_sec))
        idx = store._read_index()
        idx[old_chat]["updated_at"] = "2020-01-01T00:00:00+00:00"
        store._write_index(idx)

        recent_ms = now_ms - 60_000
        store.append(active_chat, {"event": "user_message", "text": "我周末喜欢去西湖骑车", "created_at_ms": recent_ms})

        # Set learned_until_ms to recent_ms so normal hourly learn finds nothing new,
        # while old_chat has mtime far older than cutoff.
        meta = self.tidy._read_meta()
        meta["learned_until_ms"] = recent_ms
        self.tidy._write_meta(meta)

        read_chats: list[str] = []
        real_events = store.events

        def spy_events(chat_id: str):
            read_chats.append(chat_id)
            return real_events(chat_id)

        with mock.patch.object(store, "events", side_effect=spy_events):
            # Non-forced pass: active_chat has no message > recent_ms, and old_chat is skipped without reading JSONL!
            no_force = self.learn.learn(store, force=False)
            self.assertEqual(no_force, {"learned": 0})
            self.assertNotIn(old_chat, read_chats)

            # Forced pass (「现在更新」): falls back to 24h lookback and learns from active_chat while still skipping old_chat!
            read_chats.clear()
            with mock.patch.object(
                self.learn,
                "_propose",
                return_value={"add": [{"section": "喜好与习惯", "text": "周末喜欢去西湖骑车"}], "update": [], "remove": []},
            ):
                forced = self.learn.learn(store, force=True)
            self.assertEqual(forced["learned"], 1)
            self.assertIn(active_chat, read_chats)
            self.assertNotIn(old_chat, read_chats)
            self.assertIn("【喜好与习惯】周末喜欢去西湖骑车", self.disk_store._entries_for("user"))

    def test_forget_memory_and_write_memory_doc_clean_user_written_protection(self):
        self.memory.remember("我不吃香菜")
        self.memory.remember("周末早起跑步")
        self.assertEqual(set(self.tidy._read_meta()["user_written"]), {"我不吃香菜", "周末早起跑步"})

        # 1) forget_memory removes deleted line from user_written.
        self.memory.forget_memory("USER.md", "我不吃香菜")
        self.assertEqual(self.tidy._read_meta()["user_written"], ["周末早起跑步"])

        # 2) write_memory_doc removes lines deleted from the markdown document from user_written,
        # while marking newly added lines as user_written.
        self.memory.write_memory_doc("## 喜好与习惯\n\n每天喝两杯水\n")
        self.assertEqual(self.tidy._read_meta()["user_written"], ["【喜好与习惯】每天喝两杯水"])
        self.memory.write_memory_doc("")
        self.assertEqual(self.tidy._read_meta()["user_written"], [])


if __name__ == "__main__":
    unittest.main()
