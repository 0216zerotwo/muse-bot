"""重新生成: the last answer leaves the chat and the question is asked again (Hermes's /retry), under
the same turn; anything else is refused with the chat untouched."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from _loader import chat_store
from test_stream import _adapter_module


class RegenerateTest(unittest.TestCase):
    def setUp(self):
        module = _adapter_module()
        adapter = object.__new__(module.CompanionAdapter)
        adapter._turns = {}
        adapter.store = chat_store(Path(tempfile.mkdtemp(prefix="companion-regen-")))
        self.started = []

        async def start(chat_id, text, turn_id, files=None):
            self.started.append((chat_id, text, turn_id))

        adapter._start_turn = start
        self.adapter, self.chat = adapter, adapter.store.create()

    def add(self, event, turn, text="", **extra):
        self.adapter.store.append(self.chat, {"event": event, "chat_id": self.chat, "turn_id": turn, "text": text,
                                              **({"starts_turn": True} if event == "user_message" else {}), **extra})

    def regenerate(self, turn):
        async def go():
            await self.adapter.regenerate(self.chat, turn)
            await asyncio.sleep(0)  # the turn starts in the background
        asyncio.run(go())

    def test_the_last_answer_goes_and_the_question_is_asked_again(self):
        self.add("user_message", "t1", "早")
        self.add("message", "t1", "早上好")
        self.add("turn_end", "t1")
        self.add("user_message", "t2", "周六去哪玩")
        self.add("message", "t2", kind="tool_hint")
        self.add("message", "t2", "去西湖骑车吧")
        self.add("turn_end", "t2")
        self.regenerate("t2")
        kept = [(e["event"], e["turn_id"]) for e in self.adapter.store.events(self.chat)]
        self.assertEqual(kept, [("user_message", "t1"), ("message", "t1"), ("turn_end", "t1"), ("user_message", "t2")])
        self.assertEqual(self.started, [(self.chat, "/retry", "t2")])
        self.assertEqual(self.adapter._turns[self.chat], "t2")

    def test_only_the_last_plain_question_while_idle(self):
        self.add("user_message", "t1", "早")
        self.add("message", "t1", "早上好")
        self.add("user_message", "t2", "看看这张图", media_urls=[{"url": "/api/media/x/y", "name": "a.png"}])
        self.add("message", "t2", "是一只猫")
        for turn, message in (("t1", "只能重新生成最后一条回复"), ("t2", "带图片或文件的消息暂时不能重新生成")):
            with self.assertRaises(ValueError) as caught:
                self.regenerate(turn)
            self.assertEqual(str(caught.exception), message)
        self.add("user_message", "t3", "再来")
        self.add("message", "t4", "（一个确认卡片的结果）")  # another turn after it: /retry would ask that instead
        with self.assertRaises(ValueError):
            self.regenerate("t3")
        self.adapter._turns[self.chat] = "t9"
        with self.assertRaises(ValueError):
            self.regenerate("t3")
        self.assertEqual(len(self.adapter.store.events(self.chat)), 6)  # untouched
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
