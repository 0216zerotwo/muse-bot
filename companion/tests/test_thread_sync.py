"""A phone that keeps a chat itself asks only for what is new; a copy that no longer matches gets it all."""

import tempfile
import unittest
from pathlib import Path

from _loader import chat_store, temp_dir


class ThreadSyncTest(unittest.TestCase):
    def setUp(self):
        self.store = chat_store(temp_dir("companion-thread-"))
        self.chat = self.store.create()
        for n, text in enumerate(["早", "早上好", "今天下雨吗", "下午有雨"]):
            self.store.append(self.chat, {"event": "user_message" if n % 2 == 0 else "message", "chat_id": self.chat,
                                          "text": text, "turn_id": f"t{n // 2}", "created_at_ms": 1000 + n})

    def test_a_matching_copy_gets_only_what_came_after(self):
        full = self.store.thread_payload(self.chat)
        self.assertNotIn("incremental", full)
        kept = full["events"][:2]
        newer = self.store.thread_payload(self.chat, known=2, last=kept[-1]["created_at_ms"])
        self.assertTrue(newer["incremental"])
        self.assertEqual([e["text"] for e in newer["events"]], ["今天下雨吗", "下午有雨"])
        self.assertEqual(newer["completed_turn_ids"], full["completed_turn_ids"])

    def test_a_copy_that_no_longer_matches_gets_everything(self):
        # Its last event isn't where the phone thinks (a message was deleted, say).
        stale = self.store.thread_payload(self.chat, known=2, last=999)
        self.assertNotIn("incremental", stale)
        self.assertEqual(len(stale["events"]), 4)
        # More than the chat has, or nothing known: everything too.
        self.assertEqual(len(self.store.thread_payload(self.chat, known=9, last=1003)["events"]), 4)
        self.assertEqual(len(self.store.thread_payload(self.chat, known=0, last=0)["events"]), 4)

    def test_older_steps_come_without_their_long_scripts_and_results(self):
        script = "print('x')\n" * 400
        step = {"name": "execute_code", "phase": "end", "arguments": {"code": script}, "result": "ok " * 500}
        memory = {"name": "memory", "phase": "end", "arguments": {"target": "user", "content": "x"}, "result": "r" * 900}
        self.store.append(self.chat, {"event": "message", "kind": "tool_hint", "chat_id": self.chat, "turn_id": "t0",
                                      "text": "", "created_at_ms": 1001, "tool_events": [step, memory]})
        self.store.append(self.chat, {"event": "message", "kind": "tool_hint", "chat_id": self.chat, "turn_id": "t1",
                                      "text": "", "created_at_ms": 1004, "tool_events": [step]})
        events = self.store.thread_payload(self.chat)["events"]
        older, newest = events[-2]["tool_events"], events[-1]["tool_events"]
        self.assertLessEqual(len(older[0]["arguments"]["code"]), 401)
        self.assertTrue(older[0]["result"].endswith("…"))
        self.assertEqual(older[1]["result"], "r" * 900)  # 已记住 reads a memory step's result whole
        self.assertEqual(newest[0]["arguments"]["code"], script)  # the turn that may still be at work
        # The stored chat keeps everything.
        self.assertEqual(self.store.events(self.chat)[-2]["tool_events"][0]["arguments"]["code"], script)

    def test_its_revision_changes_when_a_message_is_swapped_for_another(self):
        before = self.store.thread_payload(self.chat)["revision"]
        self.store.append(self.chat, {"event": "message", "chat_id": self.chat, "text": "补一句", "turn_id": "t1",
                                      "created_at_ms": 2000})
        self.assertNotEqual(self.store.thread_payload(self.chat)["revision"], before)

    def test_removing_messages_refreshes_preview_bumps_revision_and_forces_full_sync(self):
        self.assertEqual(self.store.list()[0]["preview"], "下午有雨")
        rev_before = self.store.thread_payload(self.chat)["revision"]

        # Remove the tail reply: preview refreshes to the previous message, revision changes,
        # and an older phone snapshot (known=2, last=1001) gets a full refresh instead of incremental.
        removed = self.store.remove_events(self.chat, lambda e: e.get("text") == "下午有雨")
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.list()[0]["preview"], "今天下雨吗")
        synced = self.store.thread_payload(self.chat, known=2, last=1001)
        self.assertNotIn("incremental", synced)
        self.assertNotEqual(synced["revision"], rev_before)

        # Removing all remaining messages clears preview to "".
        self.store.remove_events(self.chat, lambda _e: True)
        self.assertEqual(self.store.list()[0]["preview"], "")


if __name__ == "__main__":
    unittest.main()
