"""查找聊天记录: the words in any of the user's chats, newest first, cards and helpers' work left out."""

import tempfile
import unittest
from pathlib import Path

from _loader import chat_store, load


class ChatSearchTest(unittest.TestCase):
    def setUp(self):
        _, (self.search,) = load("chat_search")
        self.store = chat_store(Path(tempfile.mkdtemp(prefix="companion-search-")))
        self.main, self.side, self.helper = self.store.create(), self.store.create(), self.store.create()
        self.store.set_title(self.side, "买电脑")
        index = self.store._read_index()
        index[self.helper]["helper"] = "研究员"
        self.store._write_index(index)

    def add(self, chat, event, text, at, **extra):
        self.store.append(chat, {"event": event, "chat_id": chat, "text": text, "turn_id": f"t{at}", "created_at_ms": at, **extra})

    def test_words_are_found_in_every_chat_newest_first(self):
        self.add(self.main, "user_message", "记住：我不吃香菜", 1000)
        self.add(self.main, "message", "好，以后推荐菜都避开**香菜**。", 2000)
        self.add(self.side, "user_message", "1w预算，MacBook Air 高配还是 Pro 低配", 3000)
        self.add(self.helper, "message", "香菜的资料", 4000)  # a helper's working chat
        self.add(self.main, "message", "", 5000, kind="tool_hint")
        self.add(self.main, "message", "已记住", 6000, source={"kind": "notice"})
        found = self.search.search(self.store, " 香菜 ", main_chat=self.main)
        self.assertEqual([(h["role"], h["chat_title"]) for h in found["hits"]], [("assistant", "主要聊天"), ("user", "主要聊天")])
        self.assertEqual(found["hits"][0]["text"], "好，以后推荐菜都避开香菜。")
        mac = self.search.search(self.store, "macbook", main_chat=self.main)["hits"]
        self.assertEqual((mac[0]["chat_id"], mac[0]["chat_title"]), (self.side, "买电脑"))
        self.assertEqual(self.search.search(self.store, "  ")["hits"], [])

    def test_cards_are_not_searched_and_long_text_is_cut_around_the_match(self):
        card = '看这里：\n```ui\n<div class="香菜">x</div>\n```'
        self.add(self.main, "message", card, 1000)
        self.assertEqual(self.search.search(self.store, "香菜")["hits"], [])
        self.add(self.main, "message", "开头" * 40 + "关键词" + "结尾" * 40, 2000)
        text = self.search.search(self.store, "关键词")["hits"][0]["text"]
        self.assertTrue(text.startswith("…") and text.endswith("…") and "关键词" in text)
        self.assertLess(len(text), 70)


if __name__ == "__main__":
    unittest.main()
