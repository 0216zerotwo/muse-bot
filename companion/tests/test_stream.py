"""How a reply reaches the app while it streams: one bubble per segment, rewritten in place, and
Hermes's status lines and scheduled messages kept apart from the reply."""

import asyncio
import importlib
import importlib.util
import sys
import unittest

from _loader import ROOT, ensure_hermes_stubs


def _adapter_module():
    ensure_hermes_stubs()
    name = "companion_stream_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        sys.modules[name] = importlib.util.module_from_spec(spec)
    return importlib.import_module(f"{name}.adapter")


class StreamTest(unittest.TestCase):
    def setUp(self):
        module = _adapter_module()
        adapter = object.__new__(module.CompanionAdapter)  # no server, no data: just the relay
        adapter._drafts, adapter._turns, adapter._last_reply = {}, {"c": "t1"}, {}
        self.events, self.kept = [], []

        async def emit(chat_id, body, persist=False):
            self.events.append(body)

        adapter._emit = emit
        adapter.store = type("Store", (), {"append": lambda _, chat, body: self.kept.append(body)})()
        adapter._cards = lambda text: text
        adapter.companion_name = lambda: "伙伴"
        self.adapter, self.module = adapter, module
        self.pushed = []
        self.addCleanup(setattr, module.push, "schedule_push", module.push.schedule_push)
        module.push.schedule_push = lambda *a, **k: self.pushed.append(a)

    def run_(self, coro):
        return asyncio.run(coro)

    def bubbles(self):
        """What the app shows: per stream, its text (deltas appended, a ``text`` replacing it)."""
        shown: dict[str, str] = {}
        for e in self.events:
            sid = e.get("stream_id")
            if e["event"] == "delta":
                shown[sid] = shown.get(sid, "") + e["text"]
            elif e["event"] == "stream_end" and "text" in e:
                shown[sid] = e["text"]
            elif e["event"] == "message":
                shown[f"message{len(shown)}"] = e["text"]
        return list(shown.values())

    def test_a_rewritten_draft_stays_one_bubble(self):
        a = self.adapter
        self.run_(a.send_draft("c", 7, "我找到了：modelId 是 fe50"))
        self.run_(a.send_draft("c", 7, "我找到了：它的 modelId 是 fe50cdf3"))  # an earlier part changed
        self.run_(a.send_draft("c", 7, "我找到了：它的 modelId 是 fe50cdf304"))
        self.run_(a.send("c", "我找到了：它的 modelId 是 fe50cdf3040449"))
        self.assertEqual(self.bubbles(), ["我找到了：它的 modelId 是 fe50cdf3040449"])
        self.assertEqual(len(self.kept), 1)  # kept once, for replay

    def test_a_final_reply_unlike_the_stream_replaces_it(self):
        a = self.adapter
        self.run_(a.send_draft("c", 7, "我先看看 Fish Audio 上"))
        self.run_(a.send("c", "找到了：modelId 是 fe50cdf3040449"))
        self.assertEqual(self.bubbles(), ["找到了：modelId 是 fe50cdf3040449"])

    def test_each_segment_after_a_tool_call_is_its_own_bubble(self):
        a = self.adapter
        self.run_(a.send_draft("c", 7, "我打开浏览器看看。"))
        self.run_(a.send_draft("c", 8, "找到了：fe50"))
        self.run_(a.send("c", "找到了：fe50cdf3"))
        self.assertEqual(self.bubbles(), ["我打开浏览器看看。", "找到了：fe50cdf3"])

    def test_status_lines_are_not_the_reply(self):
        a = self.adapter
        notices = []

        async def post_notice(chat_id, text):
            notices.append(text)

        a.post_notice = post_notice
        self.run_(a.send_draft("c", 7, "正在找"))
        interim = {"_interim_send": True}
        self.run_(a.send("c", "⏳ 正在处理——3 分钟 — 第 10/20 轮迭代", metadata=interim))
        self.run_(a.send("c", "这一步花的时间比较久", metadata=interim))
        self.assertEqual(notices, ["这一步花的时间比较久"])  # the heartbeat is dropped
        self.assertIn("c", a._drafts)  # the reply being streamed stays open
        self.run_(a.send("c", "正在找的结果：找到了"))
        self.assertEqual(self.bubbles(), ["正在找的结果：找到了"])

    def test_a_message_sent_while_it_works_is_answered_with_a_grey_line(self):
        a = self.adapter
        notices = []

        async def post_notice(chat_id, text):
            notices.append(text)

        a.post_notice = post_notice
        self.addCleanup(setattr, self.module, "_busy_heads", self.module._busy_heads)
        self.module._busy_heads = lambda: ("好，按你刚说的调整", "收到，手上这件做完就回你")
        self.run_(a.send_draft("c", 7, "正在打开苹果官网"))
        # Hermes appends its one-time English tip to the first such answer.
        tip = "💡 First-time tip — I redirected the current run using your message. This notice won't appear again."
        self.run_(a.send("c", "好，按你刚说的调整。\n\n" + tip))
        self.assertEqual(notices, ["好，按你刚说的调整。"])
        self.assertIn("c", a._drafts)  # the reply being streamed stays open
        self.run_(a.send("c", tip))  # a tip on its own: nothing at all
        self.assertEqual(notices, ["好，按你刚说的调整。"])
        self.run_(a.send("c", "官网价：¥9,999"))
        self.assertEqual(self.bubbles(), ["官网价：¥9,999"])

    def test_a_scheduled_message_during_a_turn_is_its_own(self):
        a = self.adapter
        self.addCleanup(setattr, self.module.routines, "source_for_job", self.module.routines.source_for_job)
        self.module.routines.source_for_job = lambda job: {"kind": "checkin", "label": "主动关心"}
        self.run_(a.send_draft("c", 7, "登录成功了"))
        self.run_(a.send("c", "明天台北停班停课了", metadata={"job_id": "j1"}))
        message = next(e for e in self.events if e["event"] == "message")
        self.assertNotIn("turn_id", message)
        self.assertEqual(message["source"]["label"], "主动关心")
        self.assertIn("c", a._drafts)  # the user's reply goes on streaming
        self.assertEqual(len(self.pushed), 1)


if __name__ == "__main__":
    unittest.main()
