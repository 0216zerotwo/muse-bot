"""重新生成: the last answer leaves the chat and the question is asked again under the same turn;
anything else is refused with the chat untouched."""

import asyncio
import unittest

from _loader import temp_dir
from muse.app import NativeCompanionAdapter
from muse.db import MuseDB, close_all
from muse.engine import ChatTurnEngine
from muse.llm.types import StreamDone, TextDelta
from muse.runtime import close_all_shared_dbs


class RegenerateTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("companion-regen-")
        self.db = MuseDB(self.home / "muse.db")
        self.adapter = NativeCompanionAdapter(home=self.home, start_background_services=False)
        self.asked: list[str] = []

        async def fake_stream(*, messages, **_):
            user_msgs = [m for m in messages if m.get("role") == "user"]
            self.asked.append(str(user_msgs[-1].get("content") or "") if user_msgs else "")
            yield TextDelta("重新生成的回答")
            yield StreamDone()

        self.engine = ChatTurnEngine(self.adapter, db=self.db, stream_llm=fake_stream)
        self.adapter._engine = self.engine
        self.chat = self.adapter.store.create()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        close_all_shared_dbs()
        close_all()

    def add(self, event, turn, text="", **extra):
        self.adapter.store.append(
            self.chat,
            {
                "event": event,
                "chat_id": self.chat,
                "turn_id": turn,
                "text": text,
                **({"starts_turn": True} if event == "user_message" else {}),
                **extra,
            },
        )
        sid = self.db.get_or_create_active_session(self.chat, source="companion")["id"]
        if event == "user_message":
            self.db.append_message(sid, self.chat, "user", text, turn_id=turn)
        elif event == "message" and extra.get("kind") != "tool_hint":
            self.db.append_message(sid, self.chat, "assistant", text, turn_id=turn)

    def regenerate(self, turn, *, wait_done: bool = True):
        async def go():
            await self.adapter.regenerate(self.chat, turn)
            if wait_done:
                await self.engine.get_actor(self.chat).wait_idle(timeout=2.0)

        asyncio.run(go())

    def test_the_last_answer_goes_and_the_question_is_asked_again(self):
        self.add("user_message", "t1", "早")
        self.add("message", "t1", "早上好", turn_phase="answer")
        self.add("turn_end", "t1")
        self.add("user_message", "t2", "周六去哪玩")
        self.add("message", "t2", kind="tool_hint", turn_phase="activity")
        self.add("message", "t2", "去西湖骑车吧", turn_phase="answer")
        self.add("turn_end", "t2")

        self.regenerate("t2")
        events = self.adapter.store.events(self.chat)
        t2_answers = [e["text"] for e in events if e.get("turn_id") == "t2" and e.get("event") == "message"]
        self.assertEqual(t2_answers, ["重新生成的回答"])
        self.assertEqual([e["turn_id"] for e in events if e.get("event") == "user_message"], ["t1", "t2"])
        self.assertTrue(any("周六去哪玩" in q for q in self.asked))

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
        self.add("message", "t4", "（一个确认卡片的结果）")
        with self.assertRaises(ValueError):
            self.regenerate("t3")
        actor = self.engine.get_actor(self.chat)
        actor.active_turn_id = "t9"
        actor._phase = "llm"
        try:
            with self.assertRaises(ValueError):
                self.regenerate("t3")
        finally:
            actor.active_turn_id = None
            actor._phase = "idle"
        self.assertEqual(len(self.adapter.store.events(self.chat)), 6)
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
