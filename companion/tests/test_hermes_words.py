import json
import unittest

from _loader import load


class HermesWordsTest(unittest.TestCase):
    def setUp(self):
        _, (self.words,) = load("hermes_words")

    def card(self, text):
        return json.loads(text.split("```card")[1].split("```")[0])

    def test_a_paused_conversation_offers_buttons_not_commands(self):
        notice = ("⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted, and only "
                  "you know whether to send it again. Your message is saved. Send /discard to drop the lost turn")
        text = self.words.in_our_words(notice)
        self.assertNotIn("Hermes", text)
        self.assertEqual([item["send"] for item in self.card(text)["items"]], ["/discard", "/new"])
        self.assertEqual(self.words.in_our_words("⏳ Hermes is restarting — your message is saved"), "我在重启，你这条记下了，回来就回你。")
        self.assertEqual(self.words.in_our_words("今天天气不错"), "今天天气不错")

    def test_confirming_a_new_conversation(self):
        text = self.words.confirm_text("/new")
        self.assertTrue(text.startswith("要开个新对话吗？"))
        self.assertEqual([item["send"] for item in self.card(text)["items"]], ["/approve", "/cancel"])
        self.assertIsNone(self.words.confirm_text("/reload-mcp"))


if __name__ == "__main__":
    unittest.main()
