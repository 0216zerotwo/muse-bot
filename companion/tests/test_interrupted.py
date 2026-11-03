"""A task the last stop cut off (the server's window closed mid-task): the chat says so, with 接着做."""

import asyncio
import json
import unittest

from _loader import load

NOW = 1_800_000_000_000
STARTED = NOW - 60_000  # this server came up a minute ago


def asked(turn, at, text="帮我比价 MacBook Air 和 Pro"):
    return {"event": "user_message", "text": text, "starts_turn": True, "turn_id": turn, "created_at_ms": at}


def step(turn, at):
    return {"event": "message", "text": "", "kind": "tool_hint", "turn_id": turn, "created_at_ms": at}


def answer(turn, at, text="Air 13 起价 ¥9999。"):
    return {"event": "message", "text": text, "turn_id": turn, "turn_phase": "answer", "created_at_ms": at}


class CutOffTest(unittest.TestCase):
    def setUp(self):
        _, (self.interrupted,) = load("interrupted")

    def cut(self, events):
        return self.interrupted.cut_off(events, NOW, STARTED)

    def test_a_task_stopped_while_at_work_is_cut_off(self):
        self.assertEqual(self.cut([asked("t1", NOW - 600_000), step("t1", NOW - 590_000)]),
                         {"turn_id": "t1", "text": "帮我比价 MacBook Air 和 Pro"})
        self.assertEqual(self.cut([asked("t1", NOW - 600_000)])["turn_id"], "t1")  # stopped before its first step
        # It answered, then went on working: still cut off.
        self.assertIsNotNone(self.cut([asked("t1", NOW - 600_000), answer("t1", NOW - 590_000, "正在查，查到就告诉你。"),
                                       step("t1", NOW - 580_000)]))

    def test_answered_ended_old_or_taken_up_again_is_not(self):
        self.assertIsNone(self.cut([asked("t1", NOW - 600_000), step("t1", NOW - 590_000), answer("t1", NOW - 580_000)]))
        self.assertIsNone(self.cut([asked("t1", NOW - 600_000),
                                    {"event": "turn_end", "turn_id": "t1", "created_at_ms": NOW - 500_000}]))
        self.assertIsNone(self.cut([asked("t1", NOW - 7 * 3600_000)]))
        self.assertIsNone(self.cut([asked("t1", NOW - 600_000), step("t1", STARTED + 5_000)]))  # Hermes resumed it
        self.assertIsNone(self.cut([]))

    def test_the_message_names_the_task_and_asks_for_it_again(self):
        text = self.interrupted.message("帮我把这几天 A 股和美股大盘的情况对比做成文档，要有图表")
        self.assertIn("「帮我把这几天 A 股和美股大盘的情况对比做成文档…」没做完", text)
        card = json.loads(text.split("```card\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(card["items"][0]["label"], "接着做")
        self.assertEqual(card["items"][0]["send"], "接着做刚才被打断的事：帮我把这几天 A 股和美股大盘的情况对比做成文档，要有图表")


class _Store:
    def __init__(self, chats, index):
        self.chats, self.index = chats, index

    def _read_index(self):
        return self.index

    def events(self, chat_id):
        return self.chats.get(chat_id, [])


class _Adapter:
    def __init__(self, store, turns=()):
        self.store, self._turns, self.sent = store, {t: "x" for t in turns}, []

    async def _emit(self, chat_id, event, *, persist=False):
        self.sent.append((chat_id, event["event"], event.get("turn_id"), persist))


class AnnounceTest(unittest.TestCase):
    def test_each_cut_off_chat_is_told_once_and_its_turn_closed(self):
        _, (interrupted,) = load("interrupted")
        interrupted._SETTLE_S = 0
        now = interrupted.time.time() * 1000
        cut = [asked("t1", now - 600_000), step("t1", now - 590_000)]
        store = _Store({"main": cut, "busy": cut, "helper": cut, "done": [asked("t2", now - 600_000), answer("t2", now - 500_000)]},
                       {"main": {}, "busy": {}, "helper": {"helper": "h1"}, "done": {}})
        adapter = _Adapter(store, turns=["busy"])  # already at work again
        asyncio.run(interrupted.announce(adapter, int(now)))
        self.assertEqual(adapter.sent, [("main", "turn_end", "t1", True), ("main", "message", None, True)])


if __name__ == "__main__":
    unittest.main()
