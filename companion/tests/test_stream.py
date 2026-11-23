"""How a reply reaches the app while it streams: one bubble per segment, rewritten in place, and
Hermes's status lines and scheduled messages kept apart from the reply."""

import asyncio
import importlib
import importlib.util
import sys
import unittest

from _loader import ROOT, chat_store, ensure_hermes_stubs, temp_dir


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


class TurnLifecycleAndSteeringTest(unittest.TestCase):
    def setUp(self):
        self.module = _adapter_module()
        a = object.__new__(self.module.CompanionAdapter)
        a.platform = "companion"
        a.store = chat_store(temp_dir("companion-steer-"))
        a._subs = {}
        a._turns = {}
        a._pending_turns = {}
        a._turn_output = set()
        a._drafts = {}
        a._last_reply = {}
        a._helper_waits = {}
        a._internal_locks = {}
        a._turn_idle = {}
        a._cards = lambda text: text
        a.companion_name = lambda: "伙伴"
        a.push_goal_state = lambda chat_id, socket=None: asyncio.sleep(0)
        self.handled = []

        async def handle_message(event):
            self.handled.append(event)

        a.handle_message = handle_message
        self.addCleanup(setattr, self.module, "_busy_heads", self.module._busy_heads)
        self.module._busy_heads = lambda: ("好，按你刚说的调整", "收到，手上这件做完就回你")
        self.addCleanup(setattr, self.module.router, "prefetch", self.module.router.prefetch)
        self.module.router.prefetch = lambda *args, **kwargs: None
        self.addCleanup(setattr, self.module.memory_tidy, "maybe_tidy_soon", self.module.memory_tidy.maybe_tidy_soon)
        self.module.memory_tidy.maybe_tidy_soon = lambda: None
        self.addCleanup(setattr, self.module.computer, "close_after_task", self.module.computer.close_after_task)
        self.module.computer.close_after_task = lambda chat_id: None
        self.adapter = a
        self.chat = a.store.create()
        self.addCleanup(
            setattr,
            self.module.companion_state,
            "read_companion_state",
            self.module.companion_state.read_companion_state,
        )
        self.module.companion_state.read_companion_state = lambda: {"chat_id": self.chat}

    def test_mid_turn_steered_message_never_clobbers_active_turn_or_emits_premature_turn_end(self):
        a, c = self.adapter, self.chat

        async def scenario():
            await a.submit(c, "帮我查周末天气", "t1")
            ev_t1 = self.handled[-1]
            await a.on_processing_start(ev_t1)
            await a.send_draft(c, 1, "周六晴，")

            # Mid-turn follow-up t2 arrives while t1 is streaming.
            await a.submit(c, "顺便看下周日", "t2")
            ev_t2 = self.handled[-1]
            self.assertEqual(a._turns[c], "t1")
            await a.send(c, "好，按你刚说的调整。")
            await a.on_processing_complete(ev_t2, "success")

            # Active turn t1 and its open draft must remain untouched; no premature turn_end!
            self.assertEqual(a._turns.get(c), "t1")
            self.assertIn(c, a._drafts)
            self.assertEqual([e for e in a.store.events(c) if e.get("event") == "turn_end"], [])

            # Now t1 finishes normally.
            await a.send(c, "周六晴，周日多云。")
            await a.on_processing_complete(ev_t1, "success")
            self.assertNotIn(c, a._turns)
            turn_ends = [e["turn_id"] for e in a.store.events(c) if e.get("event") == "turn_end"]
            self.assertEqual(turn_ends, ["t1"])

        asyncio.run(scenario())

    def test_in_band_queued_followup_handles_streaming_and_nonstreaming_real_hook_order(self):
        a, c = self.adapter, self.chat

        async def scenario():
            # 1) Streaming t2 with real run_turn.py order: inner complete(t2) BEFORE final send(t2), then outer complete(t1).
            await a.submit(c, "第一问", "t1")
            ev_t1 = self.handled[-1]
            await a.on_processing_start(ev_t1)
            await a.submit(c, "第二问", "t2")
            ev_t2 = self.handled[-1]
            await a.send(c, "收到，手上这件做完就回你。")
            await a.on_processing_complete(ev_t2, "success")  # intake outer hook (ignored)
            await a.send(c, "第一问的回答")

            await a.on_processing_start(ev_t2)
            self.assertEqual(a._turns.get(c), "t2")
            await a.send_draft(c, 2, "第二问草稿")
            # run_turn.py:1987 fires inner complete(t2) BEFORE base.py:4600 calls send()!
            await a.on_processing_complete(ev_t2, "success")
            self.assertEqual(a._turns.get(c), "t2")
            self.assertIn(c, a._drafts)
            await a.send(c, "第二问草稿完整版")
            # base.py:4614 fires outer complete(t1) at chain end.
            await a.on_processing_complete(ev_t1, "success")
            self.assertNotIn(c, a._turns)

            answers = [(e.get("turn_id"), e.get("text")) for e in a.store.events(c)
                       if e.get("event") == "message" and e.get("turn_phase") == "answer"]
            self.assertEqual(answers, [("t1", "第一问的回答"), ("t2", "第二问草稿完整版")])
            ends = [e["turn_id"] for e in a.store.events(c) if e.get("event") == "turn_end"]
            self.assertEqual(ends, ["t1", "t2"])

            # 2) Non-streaming tool-less t4 with real run_turn.py order: inner complete(t4) BEFORE send(t4), then outer complete(t3).
            await a.submit(c, "第三问", "t3")
            ev_t3 = self.handled[-1]
            await a.on_processing_start(ev_t3)
            await a.submit(c, "第四问", "t4")
            ev_t4 = self.handled[-1]
            await a.send(c, "第三问的回答")

            await a.on_processing_start(ev_t4)
            await a.on_processing_complete(ev_t4, "success")  # inner hook before any output!
            self.assertEqual(a._turns.get(c), "t4")
            await a.send(c, "第四问直接回答")
            await a.on_processing_complete(ev_t3, "success")  # outer chain root finishes t4 cleanly!
            self.assertNotIn(c, a._turns)

            ends_after = [e["turn_id"] for e in a.store.events(c) if e.get("event") == "turn_end"]
            self.assertEqual(ends_after, ["t1", "t2", "t3", "t4"])

        asyncio.run(scenario())

    def test_start_turn_exception_cleans_up_and_unblocks_submit_internal(self):
        a, c = self.adapter, self.chat

        async def scenario():
            async def failing_handle(_event):
                raise RuntimeError("gateway dispatch failed")

            a.handle_message = failing_handle
            with self.assertRaises(RuntimeError):
                await a.submit(c, "会报错的请求", "t-err")
            self.assertNotIn(c, a._turns)

            # Subsequent submit_internal does not deadlock!
            a.handle_message = lambda ev: asyncio.sleep(0)
            turn_id = await asyncio.wait_for(a.submit_internal(c, "【系统通知】恢复正常"), timeout=1.0)
            self.assertTrue(turn_id)

        asyncio.run(scenario())

    def test_submit_internal_waits_for_active_turn_to_finish(self):
        a, c = self.adapter, self.chat

        async def scenario():
            await a.submit(c, "正在忙的事", "t1")
            ev_t1 = self.handled[-1]
            await a.on_processing_start(ev_t1)
            await a.send_draft(c, 1, "处理中")

            internal_task = asyncio.create_task(a.submit_internal(c, "【系统通知】审批通过"))
            await asyncio.sleep(0.05)
            self.assertFalse(internal_task.done())
            self.assertEqual(a._turns.get(c), "t1")

            await a.send(c, "处理完毕")
            await a.on_processing_complete(ev_t1, "success")
            internal_turn_id = await asyncio.wait_for(internal_task, timeout=1.0)
            self.assertTrue(internal_turn_id)
            self.assertEqual(a._turns.get(c), internal_turn_id)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
