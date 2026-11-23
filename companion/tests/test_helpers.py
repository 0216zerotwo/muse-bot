import unittest

from _loader import chat_store, load


class FakeAdapter:
    def __init__(self, store):
        self.store = store
        self.notices = []

    async def post_notice(self, chat_id, text):
        self.notices.append((chat_id, text))


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.helpers,) = load("helpers")
        self.store = chat_store(self.folder)
        # Never the real scheduled jobs.
        self.dropped = []
        self.helpers._drop_jobs = self.dropped.append

    def make(self, name, **values):
        return self.helpers.create(self.store, {"name": name, "title": f"{name}的事", **values})

    def test_habits_replace_and_cap(self):
        row = self.make("行程")
        first = self.helpers.add_habit(row["id"], "每天最多四个地方", "chat")
        self.helpers.add_habit(row["id"], "每天最多三个地方", "learned", replaces=first["id"])
        habits = self.helpers.habits_of(self.helpers.find("行程"))
        self.assertEqual([h["text"] for h in habits], ["每天最多三个地方"])
        for i in range(self.helpers.MAX_HABITS + 5):
            self.helpers.add_habit(row["id"], f"习惯{i}", "you")
        self.assertEqual(len(self.helpers.habits_of(self.helpers.find("行程"))), self.helpers.MAX_HABITS)
        self.assertIsNone(self.helpers.add_habit(row["id"], "   ", "you"))

    def test_habits_reach_the_helper_and_the_companion(self):
        row = self.make("行程", instructions="排行程")
        self.helpers.add_habit(row["id"], "别排太紧", "chat")
        self.assertIn("别排太紧", self.helpers.context_for(row["chat_id"]))
        main = self.store.create()
        self.assertIn("「行程」：别排太紧", self.helpers.context_for(main, "帮我排个周末"))

    def test_deleting_a_helper_takes_its_chat_jobs_and_empty_groups(self):
        a, b = self.make("记账"), self.make("比价")
        group = self.helpers.create_group(self.store, "小组", [a["id"], b["id"]])
        self.helpers.delete(self.store, a["id"])
        self.assertEqual(self.dropped, [a["chat_id"]])
        self.assertFalse(self.store.exists(a["chat_id"]))
        self.assertEqual(self.helpers._groups()[0]["members"], [b["id"]])  # one left: the group stays
        self.helpers.delete(self.store, b["id"])
        self.assertEqual(self.helpers._groups(), [])
        self.assertFalse(self.store.exists(group["chat_id"]))

    def test_list_preview_skips_grey_lines(self):
        row = self.make("翻译")
        self.store.append(row["chat_id"], {"event": "user_message", "text": "good night"})
        self.store.append(row["chat_id"], {"event": "message", "text": "晚安。", "turn_phase": "answer"})
        self.store.append(row["chat_id"], {"event": "message", "text": "记住了：要简短", "source": {"kind": "notice"}})
        self.assertEqual(self.helpers._last(self.store, row["chat_id"], "翻译")["text"], "晚安。")
        self.store.append(row["chat_id"], {"event": "message", "text": "我叫**鱼教练**，用 `打卡` 记一笔", "turn_phase": "answer"})
        self.assertEqual(self.helpers._last(self.store, row["chat_id"], "翻译")["text"], "我叫鱼教练，用 打卡 记一笔")

    def test_a_helper_added_from_a_template_is_that_default(self):
        template = self.helpers.TEMPLATES[0]
        row = self.helpers.create(self.store, {k: template[k] for k in ("name", "emoji", "title", "instructions")})
        self.assertEqual(row.get("preset"), template["name"])
        self.assertTrue(self.helpers.is_default(row))

    def test_a_name_is_enough_to_start(self):
        self.assertEqual(self.helpers.create_blank(self.store, "邮件分拣")["name"], "邮件分拣")
        # A sentence about it is no name: it names itself once set up.
        self.assertEqual(self.helpers.create_blank(self.store, "帮我盯着快递，到了告诉我")["name"], "新帮手")
        row = self.helpers.find("邮件分拣")
        self.assertIn("直接推断你的职责", self.helpers.context_for(row["chat_id"]))

    def test_a_helper_hands_work_on_but_never_back_up_the_chain(self):
        import asyncio
        import json
        import threading

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        def stop_loop():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()

        self.addCleanup(stop_loop)
        asked, posted = [], []

        class Adapter(FakeAdapter):
            def helper_origin(self, chat_id):
                return None

            async def ask_helper(self, origin, chat_id, task, timeout):
                asked.append((origin, chat_id, task))
                return "做好了"

            async def post_as_helper(self, chat_id, helper, text):
                posted.append((chat_id, helper["name"], text))

        self.helpers._adapter, self.helpers._loop = Adapter(self.store), loop
        self.helpers._current_chat = lambda kwargs: kwargs.get("chat")
        boss, books, trips = (self.make(n, instructions="做事") for n in ("管家", "记账", "行程"))
        self.assertIn("「记账」", self.helpers.context_for(boss["chat_id"]))  # it knows whom it can ask

        result = json.loads(self.helpers._handle_ask({"helper": "记账", "task": "算下这周的开销"}, chat=boss["chat_id"]))
        self.assertTrue(result["success"])
        self.assertEqual(asked, [(boss["chat_id"], books["chat_id"], "算下这周的开销")])
        self.assertEqual(posted, [(boss["chat_id"], "记账", "做好了")])  # shown as 记账's own message
        self.assertEqual(self.helpers._asking, {})

        # 记账 working for 管家 can't hand it back to 管家 (each would wait on the other).
        self.helpers._asking[books["chat_id"]] = boss["chat_id"]
        back = json.loads(self.helpers._handle_ask({"helper": "管家", "task": "x"}, chat=books["chat_id"]))
        self.assertFalse(back["success"])
        # ...but may ask a third one.
        self.assertTrue(json.loads(self.helpers._handle_ask({"helper": "行程", "task": "y"}, chat=books["chat_id"]))["success"])

    def test_browser_desks_are_fresh_hidden_chats(self):
        self.helpers._adapter = FakeAdapter(self.store)
        first, second = self.helpers.new_desk(self.store), self.helpers.new_desk(self.store)
        self.assertNotEqual(first, second)
        self.assertTrue(self.helpers.is_desk(first))
        self.assertFalse(any(row["key"].endswith(first) for row in self.store.list()))
        self.assertFalse(self.helpers.is_desk(self.store.create()))


class JobChatTest(unittest.TestCase):
    def test_a_job_made_in_a_helpers_chat_is_known_as_its_either_way(self):
        _, (automations,) = load("automations")
        self.assertEqual(automations.job_chat({"deliver": "companion:c1"}), "c1")
        # As the scheduling tool saves a job set up in a chat: "origin", the chat kept beside it.
        self.assertEqual(automations.job_chat({"deliver": "origin", "origin": {"platform": "companion", "chat_id": "c2"}}), "c2")
        self.assertIsNone(automations.job_chat({"deliver": "origin", "origin": {"platform": "telegram", "chat_id": "c3"}}))
        self.assertIsNone(automations.job_chat({"deliver": "local"}))


class HelperTriggersTest(unittest.TestCase):
    def test_a_trigger_set_up_by_a_helper_is_its_own(self):
        import sys
        import types
        from unittest import mock

        folder, (helpers, triggers) = load("helpers", "triggers")
        store = chat_store(folder)
        helpers._drop_jobs = lambda chat_id: None
        # A stand-in for Hermes's scheduled jobs: never the real ones.
        jobs = []
        cron = types.ModuleType("cron.jobs")
        cron.create_job = lambda **job: jobs.append(job) or {"id": f"j{len(jobs)}"}
        cron.remove_job = lambda job_id: jobs.append({"removed": job_id})
        package = types.ModuleType("cron")
        package.jobs = cron
        self.enterContext(mock.patch.dict(sys.modules, {"cron": package, "cron.jobs": cron}))
        triggers.read_companion_state = lambda: {"chat_id": "main"}

        mail = helpers.create(store, {"name": "邮件分拣", "title": "分邮件"})
        own = triggers.add_trigger("page", "价格", "降价了告诉我", url="https://example.com", owner=mail["id"])
        triggers.add_trigger("page", "新闻", "有大事告诉我", url="https://example.org")
        self.assertEqual([j["deliver"] for j in jobs], [f"companion:{mail['chat_id']}", "companion:main"])
        self.assertEqual(triggers.public(own)["owner_name"], "邮件分拣")

        helpers.delete(store, mail["id"])
        self.assertEqual([t["name"] for t in triggers.list_triggers()], ["新闻"])


if __name__ == "__main__":
    unittest.main()
