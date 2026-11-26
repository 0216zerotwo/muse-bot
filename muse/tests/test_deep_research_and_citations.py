"""Unit tests for turn-scoped citations, concurrent SSRF-safe `web_extract`, `deep_research`, and turn passthrough."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from companion.store import ChatStore, _lean
from muse.context import TurnExecutionContext, get_turn_context, run_in_turn_thread, turn_context_scope
from muse.db import MuseDB, close_all
from muse.llm.types import StreamDone, TextDelta, ToolCallComplete
from muse.streaming import ChatSink, DraftSegmentStreamer
from muse.tools import create_default_tool_registry
from muse.tools.citations import (
    MAX_TURN_CITATIONS,
    get_turn_citations,
    record_citations,
    reset_turn_citations,
    start_turn_citations,
    turn_citations_scope,
)
from muse.tools.web import deep_research_handler, web_extract_handler, web_search_handler
from muse.turn_actor import ChatTurnActor


class _StoreSink(ChatSink):
    def __init__(self, store: ChatStore) -> None:
        self.store = store
        self.live: list[dict[str, Any]] = []
        self._active: dict[str, str | None] = {}

    async def emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
        body = {**event, "chat_id": chat_id}
        if persist:
            body = self.store.append(chat_id, body)
        self.live.append(dict(body))
        return body

    def append_store(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.store.append(chat_id, {**event, "chat_id": chat_id})

    def store_events(self, chat_id: str) -> list[dict[str, Any]]:
        return self.store.events(chat_id)

    def ensure_chat(self, chat_id: str) -> None:
        self.store.ensure(chat_id)

    def set_active_turn(self, chat_id: str, turn_id: str | None) -> None:
        self._active[chat_id] = turn_id

    def get_active_turn(self, chat_id: str) -> str | None:
        return self._active.get(chat_id)


class CitationsCollectorTest(unittest.TestCase):
    def test_dedup_secret_scrubbing_knowledge_cap_and_thread_propagation(self) -> None:
        # Outside any active turn, recording is a safe no-op.
        self.assertEqual(record_citations([{"title": "Noop", "url": "https://example.com"}]), [])
        self.assertEqual(get_turn_citations(), [])

        tok = start_turn_citations()
        try:
            recorded = record_citations([
                {
                    "title": "",
                    "url": "https://user:pass@www.example.com/docs/?api_key=sk-secret-123&lang=zh#top",
                    "snippet": "",
                },
                {
                    "title": "Official Docs",
                    "url": "https://WWW.EXAMPLE.COM/docs?api_key=sk-secret-123&lang=zh#other",
                    "snippet": "Check https://api.example.com?token=secret-tok for details",
                },
                {
                    "title": "Q4 规划 · 摘要",
                    "url": "#knowledge:doc_42",
                    "domain": "知识库 · 工作",
                    "snippet": "内部知识库摘录",
                },
                {"title": "Bad", "url": "javascript:alert(1)", "snippet": "nope"},
                {"title": "BadFile", "url": "file:///etc/passwd", "snippet": "nope"},
            ])
            self.assertEqual(len(recorded), 3)
            items = get_turn_citations()
            self.assertEqual(len(items), 2)

            first = items[0]
            self.assertEqual(first["id"], 1)
            self.assertEqual(first["title"], "Official Docs")
            self.assertEqual(first["domain"], "example.com")
            self.assertNotIn("sk-secret-123", first["url"])
            self.assertNotIn("user:pass", first["url"])
            self.assertIn("[REDACTED]", first["url"])
            self.assertNotIn("secret-tok", first["snippet"])
            self.assertIn("[REDACTED]", first["snippet"])

            second = items[1]
            self.assertEqual(second["id"], 2)
            self.assertEqual(second["url"], "#knowledge:doc_42")
            self.assertEqual(second["domain"], "知识库 · 工作")

            # Verify worker-thread propagation via run_in_turn_thread + ThreadPoolExecutor
            async def _from_thread() -> None:
                await run_in_turn_thread(
                    record_citations,
                    [{"title": "Async Worker Hit", "url": "https://docs.python.org/3/", "snippet": "stdlib"}],
                )

            asyncio.run(_from_thread())

            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [
                    pool.submit(
                        contextvars.copy_context().run,
                        record_citations,
                        [{"title": f"Page {i}", "url": f"https://site-{i}.example.org/p", "snippet": f"S{i}"}],
                    )
                    for i in range(20)
                ]
                for f in futs:
                    f.result()

            final = get_turn_citations()
            self.assertEqual(len(final), MAX_TURN_CITATIONS)
            self.assertEqual([c["id"] for c in final], list(range(1, MAX_TURN_CITATIONS + 1)))
            self.assertTrue(any(c["url"] == "https://docs.python.org/3/" for c in final))
        finally:
            reset_turn_citations(tok)

        self.assertEqual(get_turn_citations(), [])


class ConcurrentWebExtractAndDeepResearchTest(unittest.TestCase):
    def test_concurrent_web_extract_preserves_order_context_and_per_hop_ssrf(self) -> None:
        barrier = threading.Barrier(3, timeout=3.0)
        observed_chat_ids: list[str] = []
        obs_lock = threading.Lock()

        def mock_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            ctx = get_turn_context()
            with obs_lock:
                observed_chat_ids.append(ctx.chat_id if ctx else "")
            if path.endswith("/hop1"):
                return httpx.Response(302, headers={"Location": "https://93.184.216.34/hop2"})
            if path.endswith("/hop2"):
                return httpx.Response(302, headers={"Location": "http://127.0.0.1:8766/secret"})
            if path.endswith(("/p1", "/p2", "/p3")):
                barrier.wait()
                slug = path.rsplit("/", 1)[-1]
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text=f"<html><head><title>Title {slug}</title></head><body><p>Body {slug}</p></body></html>",
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        orig_client = httpx.Client
        tctx = TurnExecutionContext(chat_id="chat-concurrent-1", session_id="s1", turn_id="t1")

        with (
            turn_context_scope(tctx),
            turn_citations_scope(),
            mock.patch.object(httpx, "Client", side_effect=lambda **kw: orig_client(transport=transport, **kw)),
        ):
            raw = web_extract_handler(
                {
                    "urls": [
                        "https://93.184.216.34/p1",
                        "https://93.184.216.34/hop1",
                        "http://169.254.169.254/latest/meta-data/",
                        "https://93.184.216.34/p2",
                        "https://93.184.216.34/p3?token=leak_me",
                    ]
                }
            )
            citations = get_turn_citations()

        parsed = json.loads(raw)
        self.assertTrue(parsed["success"])
        self.assertEqual(len(parsed["results"]), 5)
        self.assertEqual(parsed["results"][0]["title"], "Title p1")
        self.assertIn("SSRF", parsed["results"][1]["error"])
        self.assertIn("Blocked", parsed["results"][2]["error"])
        self.assertEqual(parsed["results"][3]["title"], "Title p2")
        self.assertEqual(parsed["results"][4]["title"], "Title p3")

        # Worker threads inherited TurnExecutionContext via copy_context().run
        self.assertTrue(observed_chat_ids)
        self.assertTrue(all(cid == "chat-concurrent-1" for cid in observed_chat_ids))

        # Only the 3 safe succeeded pages were recorded as citations, with secret scrubbed.
        self.assertEqual([c["id"] for c in citations], [1, 2, 3])
        self.assertEqual([c["title"] for c in citations], ["Title p1", "Title p2", "Title p3"])
        self.assertNotIn("leak_me", citations[2]["url"])
        self.assertIn("[REDACTED]", citations[2]["url"])

    def test_web_search_records_citations_and_preserves_envelope(self) -> None:
        fake_rows = [
            {"href": "https://93.184.216.34/a", "title": "Alpha", "body": "Alpha summary"},
            {"href": "https://93.184.216.34/b?api_key=xyz", "title": "Beta", "body": "Beta summary"},
        ]
        with (
            turn_citations_scope(),
            mock.patch("ddgs.DDGS") as mock_ddgs,
            mock.patch.dict(os.environ, {"TAVILY_API_KEY": "", "BRAVE_API_KEY": ""}),
        ):
            mock_ddgs.return_value.__enter__.return_value.text.return_value = fake_rows
            raw = web_search_handler({"query": "alpha beta", "max_results": 5})
            citations = get_turn_citations()

        parsed = json.loads(raw)
        self.assertTrue(parsed["success"])
        self.assertEqual(len(parsed["data"]["web"]), 2)
        self.assertEqual([c["id"] for c in citations], [1, 2])
        self.assertEqual(citations[0]["title"], "Alpha")
        self.assertNotIn("xyz", citations[1]["url"])

    def test_deep_research_parallel_dedup_ssrf_and_numbered_citations(self) -> None:
        reg = create_default_tool_registry()
        self.assertIn("deep_research", reg.get_tool_names_for_toolset("web"))

        search_map = {
            "python async": [
                {"url": "http://127.0.0.1:8080/admin", "title": "Unsafe Local", "description": "bad"},
                {"url": "https://93.184.216.34/shared", "title": "Shared Guide", "description": "Consensus doc"},
                {"url": "https://93.184.216.34/redirect-bad", "title": "Evil Redirect", "description": "ssrf"},
                {"url": "https://93.184.216.34/only-main", "title": "Main Only", "description": "Main snippet"},
            ],
            "asyncio tasks": [
                {"url": "https://93.184.216.34/shared/", "title": "Shared Guide Dup", "description": "Consensus doc"},
                {"url": "https://93.184.216.34/sub-doc", "title": "Task Group", "description": "Sub snippet"},
            ],
            "contextvars": [
                {"url": "https://93.184.216.34/ctx-doc", "title": "ContextVars", "description": "Ctx snippet"},
            ],
        }
        seen_queries: list[str] = []
        q_lock = threading.Lock()

        def fake_run_search(q: str, limit: int, **_: Any) -> tuple[str, list[dict[str, str]]]:
            with q_lock:
                seen_queries.append(q)
            if q == "failing sub":
                raise RuntimeError("transient search outage")
            return "ddgs", list(search_map.get(q, []))[:limit]

        def mock_http(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if u.endswith("/redirect-bad"):
                return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
            if u.endswith("/shared"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<html><head><title>Shared Deep Article</title></head><body><p>Detailed shared findings.</p></body></html>",
                )
            if u.endswith("/only-main"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<html><head><title>Main Page</title></head><body><p>Main body details.</p></body></html>",
                )
            if u.endswith("/sub-doc"):
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><head><title>Extra</title></head><body><p>Extra body</p></body></html>",
            )

        transport = httpx.MockTransport(mock_http)
        orig_client = httpx.Client

        with (
            turn_citations_scope(),
            mock.patch("muse.tools.web._run_search_query", side_effect=fake_run_search),
            mock.patch.object(httpx, "Client", side_effect=lambda **kw: orig_client(transport=transport, **kw)),
        ):
            raw = deep_research_handler(
                {
                    "query": "python async",
                    "sub_queries": ["asyncio tasks", "contextvars", "failing sub", "ignored fourth"],
                    "max_sources": 4,
                }
            )
            citations = get_turn_citations()

        parsed = json.loads(raw)
        self.assertTrue(parsed["success"])
        # Max 3 sub_queries accepted (+1 main = 4 total)
        self.assertEqual(len(parsed["queries"]), 4)
        self.assertNotIn("ignored fourth", seen_queries)
        # Unsafe loopback URL and SSRF redirect were both dropped
        urls = [s["url"] for s in parsed["sources"]]
        self.assertFalse(any("127.0.0.1" in u or "redirect-bad" in u for u in urls))
        self.assertGreaterEqual(parsed["skipped_unsafe"], 2)
        # Consensus URL (/shared) ranked first as [1]
        self.assertEqual(parsed["sources"][0]["id"], 1)
        self.assertEqual(parsed["sources"][0]["url"], "https://93.184.216.34/shared")
        self.assertEqual(parsed["sources"][0]["title"], "Shared Deep Article")
        self.assertTrue(parsed["sources"][0]["extracted"])
        # 403 page degraded gracefully to search snippet
        sub_entry = next(s for s in parsed["sources"] if s["url"].endswith("/sub-doc"))
        self.assertFalse(sub_entry["extracted"])
        self.assertEqual(sub_entry["excerpt"], "Sub snippet")
        # Digest numbering matches recorded turn citations
        self.assertIn("[1] Shared Deep Article", parsed["digest"])
        self.assertEqual([c["id"] for c in citations], [s["id"] for s in parsed["sources"]])
        self.assertEqual([c["url"] for c in citations], [s["url"] for s in parsed["sources"]])


class TurnCitationPassthroughTest(unittest.TestCase):
    def tearDown(self) -> None:
        close_all()

    def test_turn_actor_attaches_sources_to_answer_event_db_extra_and_lean_preserves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = MuseDB(root / "muse.db")
            store = ChatStore(root / "chats")
            chat_id = store.create()
            sink = _StoreSink(store)
            reg = create_default_tool_registry()

            async def scenario() -> None:
                call_idx = 0

                async def fake_stream(*, messages: list[dict[str, Any]], **_: Any):
                    nonlocal call_idx
                    call_idx += 1
                    if call_idx == 1:
                        yield TextDelta("我先帮你查一下。")
                        yield ToolCallComplete(
                            id="tc-search",
                            name="web_search",
                            arguments=json.dumps({"query": "muse citations", "max_results": 2}),
                        )
                        yield StreamDone(finish_reason="tool_calls")
                    elif call_idx == 2:
                        yield TextDelta("根据检索结果 [1]，方案可行。")
                        yield StreamDone(finish_reason="stop")
                    else:
                        yield TextDelta("好的，不用谢！")
                        yield StreamDone(finish_reason="stop")

                async def exec_tool(tc: dict[str, Any], ctx: TurnExecutionContext) -> str:
                    fn = tc["function"]
                    return await reg.dispatch(fn["name"], fn["arguments"], ctx=ctx)

                actor = ChatTurnActor(chat_id, db=db, sink=sink, stream_llm=fake_stream, execute_tool=exec_tool)

                fake_rows = [
                    {"href": "https://93.184.216.34/spec", "title": "Citation Spec", "body": "Spec overview"},
                ]
                with (
                    mock.patch("ddgs.DDGS") as mock_ddgs,
                    mock.patch.dict(os.environ, {"TAVILY_API_KEY": "", "BRAVE_API_KEY": ""}),
                ):
                    mock_ddgs.return_value.__enter__.return_value.text.return_value = fake_rows
                    await actor.submit("查一下引用规范", "turn-cited")
                    await actor.wait_idle(timeout=3.0)

                # Second turn without web tools must NOT leak turn-cited sources
                await actor.submit("谢谢", "turn-plain")
                await actor.wait_idle(timeout=3.0)

            try:
                asyncio.run(scenario())
                events = store.events(chat_id)
                t1_msgs = [e for e in events if e.get("event") == "message" and e.get("turn_id") == "turn-cited"]
                self.assertEqual(len(t1_msgs), 2)
                # Pre-tool segment has no sources; final answer has sources
                self.assertNotIn("sources", t1_msgs[0])
                self.assertEqual(t1_msgs[1]["turn_phase"], "answer")
                self.assertEqual(len(t1_msgs[1]["sources"]), 1)
                self.assertEqual(t1_msgs[1]["sources"][0]["id"], 1)
                self.assertEqual(t1_msgs[1]["sources"][0]["url"], "https://93.184.216.34/spec")

                # Live stream_end for final answer also carried sources
                stream_ends_with_src = [
                    f for f in sink.live if f.get("event") == "stream_end" and f.get("turn_id") == "turn-cited" and f.get("sources")
                ]
                self.assertEqual(len(stream_ends_with_src), 1)

                # Turn 2 had no sources
                t2_msgs = [e for e in events if e.get("event") == "message" and e.get("turn_id") == "turn-plain"]
                self.assertEqual(len(t2_msgs), 1)
                self.assertNotIn("sources", t2_msgs[0])

                # MuseDB assistant row persisted sources in extra_json
                sess = db.get_active_session(chat_id, source="companion")
                self.assertIsNotNone(sess)
                db_rows = db.get_messages(sess["id"], active_only=True)
                cited_db_row = next(
                    r for r in db_rows if r["role"] == "assistant" and r["turn_id"] == "turn-cited" and not r.get("tool_calls")
                )
                self.assertEqual(cited_db_row["extra"]["sources"][0]["title"], "Citation Spec")

                # ChatStore._lean() and thread_payload() keep top-level sources intact even with tool_events
                heavy = {
                    **t1_msgs[1],
                    "tool_events": [{"name": "web_search", "arguments": {"q": "x" * 900}, "result": "r" * 900}],
                }
                leaned = _lean(heavy)
                self.assertEqual(leaned["sources"], t1_msgs[1]["sources"])
                payload = store.thread_payload(chat_id)
                replayed = next(
                    e for e in payload["events"] if e.get("turn_id") == "turn-cited" and e.get("sources")
                )
                self.assertEqual(replayed["sources"], t1_msgs[1]["sources"])
            finally:
                db.close()

    def test_non_streamed_close_segment_attaches_sources_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(Path(tmp) / "chats")
            chat_id = store.create()
            sink = _StoreSink(store)

            async def run() -> None:
                with turn_citations_scope():
                    record_citations([{"title": "Direct Doc", "url": "https://93.184.216.34/doc", "snippet": "ok"}])
                    streamer = DraftSegmentStreamer(sink, chat_id, "t-direct")
                    await streamer.close_segment(final_raw="直接回答 [1]", for_tools=False)

            asyncio.run(run())
            ev = store.events(chat_id)[0]
            self.assertEqual(ev["event"], "message")
            self.assertEqual(ev["turn_phase"], "answer")
            self.assertEqual(ev["sources"][0]["id"], 1)
            self.assertEqual(ev["sources"][0]["title"], "Direct Doc")


if __name__ == "__main__":
    unittest.main()
