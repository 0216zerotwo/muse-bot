"""How a reply reaches the app while it streams: one bubble per segment, rewritten in place, and
notices / scheduled messages / mid-turn steering kept apart from the active reply."""

import asyncio
import unittest
from unittest.mock import patch

from _loader import temp_dir
from muse.app import NativeCompanionAdapter
from muse.db import MuseDB, close_all
from muse.engine import ChatTurnEngine
from muse.llm.types import StreamDone, TextDelta, ToolCallComplete
from muse.paths import muse_home_override
from muse.runtime import close_all_shared_dbs
from muse.streaming import DraftSegmentStreamer


class _RecordingSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, payload):
        self.frames.append(dict(payload))


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("companion-stream-")
        self.adapter = NativeCompanionAdapter(home=self.home, start_background_services=False)
        self.chat = self.adapter.store.create()
        self.sock = _RecordingSocket()
        self.adapter.attach(self.chat, self.sock)
        self.pushed = []
        self.adapter.schedule_push = lambda *a, **k: self.pushed.append((a, k))
        self.addCleanup(close_all_shared_dbs)

    def run_(self, coro):
        return asyncio.run(coro)

    def bubbles(self):
        shown: dict[str, str] = {}
        for e in self.sock.frames:
            sid = e.get("stream_id")
            if e["event"] == "delta":
                shown[sid] = shown.get(sid, "") + e["text"]
            elif e["event"] == "stream_end" and "text" in e:
                shown[sid] = e["text"]
            elif e["event"] == "message" and (e.get("source") or {}).get("kind") != "notice":
                shown[f"message{len(shown)}"] = e["text"]
        return list(shown.values())

    def test_a_rewritten_draft_stays_one_bubble(self):
        streamer = DraftSegmentStreamer(self.adapter, self.chat, "t1")
        self.run_(streamer.push_full("我找到了：modelId 是 fe50"))
        self.run_(streamer.push_full("我找到了：它的 modelId 是 fe50cdf3"))
        self.run_(streamer.push_full("我找到了：它的 modelId 是 fe50cdf304"))
        self.run_(streamer.close_segment(final_raw="我找到了：它的 modelId 是 fe50cdf3040449"))
        self.assertEqual(self.bubbles(), ["我找到了：它的 modelId 是 fe50cdf3040449"])
        self.assertEqual(len(self.adapter.store.events(self.chat)), 1)

    def test_a_final_reply_unlike_the_stream_replaces_it(self):
        streamer = DraftSegmentStreamer(self.adapter, self.chat, "t1")
        self.run_(streamer.push_full("我先看看 Fish Audio 上"))
        self.run_(streamer.close_segment(final_raw="找到了：modelId 是 fe50cdf3040449"))
        self.assertEqual(self.bubbles(), ["找到了：modelId 是 fe50cdf3040449"])

    def test_each_segment_after_a_tool_call_is_its_own_bubble(self):
        streamer = DraftSegmentStreamer(self.adapter, self.chat, "t1")
        self.run_(streamer.push_full("我打开浏览器看看。"))
        self.run_(streamer.close_segment(final_raw="我打开浏览器看看。", for_tools=True))
        self.run_(streamer.push_full("找到了：fe50"))
        self.run_(streamer.close_segment(final_raw="找到了：fe50cdf3", for_tools=False))
        self.assertEqual(self.bubbles(), ["我打开浏览器看看。", "找到了：fe50cdf3"])

    def test_notices_and_proactive_scheduled_messages_never_close_open_draft(self):
        async def scenario():
            with MuseDB(self.home / "muse.db") as db:
                engine = ChatTurnEngine(self.adapter, db=db, stream_llm=lambda **_: (_ for _ in ()))
                self.adapter._engine = engine
                self.adapter.source_for_job = lambda _job: {"kind": "checkin", "label": "主动关心"}
                streamer = DraftSegmentStreamer(self.adapter, self.chat, "t1")
                await streamer.push_full("正在打开页面")
                await self.adapter.post_notice(self.chat, "好，按你刚说的调整。")
                await engine.deliver_proactive(self.chat, "明天停班停课了", job_id="j1")
                self.assertIsNotNone(streamer.stream_id)
                await streamer.close_segment(final_raw="正在打开页面：完成")

        self.run_(scenario())
        proactive = next(
            f for f in self.sock.frames if f["event"] == "message" and (f.get("source") or {}).get("label") == "主动关心"
        )
        self.assertNotIn("turn_id", proactive)
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("正在打开页面：完成", self.bubbles())


class TurnLifecycleAndSteeringTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir("companion-steer-")
        self.db = MuseDB(self.home / "muse.db")
        self.adapter = NativeCompanionAdapter(home=self.home, start_background_services=False)
        self.chat = self.adapter.store.create()
        self.sock = _RecordingSocket()
        self.adapter.attach(self.chat, self.sock)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        close_all_shared_dbs()
        close_all()

    def test_mid_turn_steered_and_queued_followup_preserve_turn_boundaries_and_submit_internal(self):
        async def scenario():
            tool_gate = asyncio.Event()
            release_tool = asyncio.Event()
            step = 0

            async def fake_stream(*, messages, **_):
                nonlocal step
                step += 1
                if step == 1:
                    yield TextDelta("周六晴，")
                    yield ToolCallComplete(id="tc-1", name="check_sun", arguments="{}")
                    yield StreamDone(finish_reason="tool_calls")
                elif step == 2:
                    yield TextDelta("周日多云。")
                    yield StreamDone()
                else:
                    last = str(messages[-1].get("content") or "")
                    yield TextDelta(f"后续：{last[:12]}")
                    yield StreamDone()

            async def fake_tool(_tc, _ctx):
                tool_gate.set()
                await release_tool.wait()
                return '{"ok": true}'

            with muse_home_override(self.home), patch("companion.modules.router.prefetch", lambda *a, **k: None):
                engine = ChatTurnEngine(self.adapter, db=self.db, stream_llm=fake_stream, execute_tool=fake_tool)
                self.adapter._engine = engine
                await self.adapter.submit(self.chat, "帮我查周末天气", "t1")
                await asyncio.wait_for(tool_gate.wait(), timeout=2.0)

                # Steered message during tool phase does NOT clobber active turn t1
                await self.adapter.submit(self.chat, "顺便看下周日", "t2")
                self.assertEqual(self.adapter.get_active_turn(self.chat), "t1")
                self.assertEqual([e for e in self.adapter.store.events(self.chat) if e.get("event") == "turn_end"], [])

                # submit_internal queues cleanly behind active t1
                internal_tid = await self.adapter.submit_internal(self.chat, "【系统通知】审批通过")
                self.assertTrue(internal_tid)
                self.assertEqual(self.adapter.get_active_turn(self.chat), "t1")

                release_tool.set()
                await engine.get_actor(self.chat).wait_idle(timeout=3.0)
                self.assertIsNone(self.adapter.get_active_turn(self.chat))

                turn_ends = [e["turn_id"] for e in self.adapter.store.events(self.chat) if e.get("event") == "turn_end"]
                self.assertEqual(turn_ends, ["t1", internal_tid])
                await self.adapter.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
