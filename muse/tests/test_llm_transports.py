"""Comprehensive unit tests for muse.llm providers and the 3 async streaming transports."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import unittest
from typing import Any
from unittest import mock

import httpx

from muse.llm.anthropic_messages import (
    ANTHROPIC_MAX_IMAGE_BYTES,
    ANTHROPIC_MAX_IMAGE_DIM,
    AnthropicMessagesTransport,
    prepare_anthropic_image_sync,
    sniff_image_mime,
)
from muse.llm.chat_completions import (
    MistralToolIdMapper,
    OpenAIChatTransport,
    sanitize_moonshot_tools,
    to_mistral_tool_call_id,
)
from muse.llm.providers import (
    _is_anthropic_endpoint,
    normalize_anthropic_messages_url,
    prenormalize_provider_params,
    resolve_runtime_provider,
)
from muse.llm.responses import OpenAIResponsesTransport
from muse.llm.types import (
    EncryptedReasoningItem,
    LLMRequestError,
    MessageCarrierEvent,
    ReasoningDelta,
    RetryPolicy,
    RetryStatusEvent,
    StreamDone,
    TextDelta,
    ThinkingBlockComplete,
    ToolCallComplete,
    UsageInfo,
    collect_stream,
)


def _fake_jwt(payload: dict[str, Any]) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


def _sse_response(lines: list[str], status_code: int = 200) -> httpx.Response:
    raw = ("\n".join(lines) + "\n\n").encode("utf-8")
    return httpx.Response(
        status_code,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        content=raw,
    )


class ProviderResolutionAndPrenormTest(unittest.TestCase):
    def test_prenormalize_pops_max_tokens_and_temperature_before_reasoning_rules(self):
        cfg = {
            "providers": {
                "m-o3": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "model": "o3-mini",
                    "extra_body": {"max_tokens": 4096, "temperature": 0.2, "seed": 42},
                }
            }
        }
        rt = resolve_runtime_provider(requested="m-o3", target_model="o3-mini", config=cfg)
        self.assertEqual(rt["max_tokens"], 4096)
        self.assertEqual(rt["temperature"], 0.2)
        self.assertEqual(rt["extra_body"], {"seed": 42})
        # Ensure original config dict was not mutated.
        self.assertIn("max_tokens", cfg["providers"]["m-o3"]["extra_body"])

        transport = OpenAIChatTransport(base_url=rt["base_url"], api_key=rt["api_key"], provider="m-o3")
        payload, _, _ = transport.build_request_payload(
            model="o3-mini",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"max_tokens": 4096, "temperature": 0.2, "seed": 42},
        )
        self.assertEqual(payload.get("max_completion_tokens"), 4096)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload.get("seed"), 42)

        # Verify gpt-5* also strips temperature and uses max_completion_tokens.
        payload5, _, _ = transport.build_request_payload(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"max_tokens": 2048, "temperature": 0.7},
        )
        self.assertEqual(payload5.get("max_completion_tokens"), 2048)
        self.assertNotIn("max_tokens", payload5)
        self.assertNotIn("temperature", payload5)

    def test_implicit_anthropic_routing_and_url_dedup(self):
        self.assertTrue(_is_anthropic_endpoint("https://api.anthropic.com/v1"))
        self.assertTrue(_is_anthropic_endpoint("https://api.anthropic.com"))
        self.assertFalse(_is_anthropic_endpoint("https://api.anthropic.com/v1/openai"))
        self.assertFalse(_is_anthropic_endpoint("https://api.anthropic.com.evil.test/v1"))
        self.assertTrue(_is_anthropic_endpoint("https://api.minimaxi.com/anthropic"))
        self.assertTrue(_is_anthropic_endpoint("https://open.bigmodel.cn/api/anthropic"))
        self.assertTrue(_is_anthropic_endpoint("https://api.deepseek.com/anthropic"))
        self.assertTrue(_is_anthropic_endpoint("https://api.moonshot.cn/anthropic"))
        self.assertTrue(_is_anthropic_endpoint("https://custom.relay.example/v1", api_mode="anthropic"))
        self.assertFalse(_is_anthropic_endpoint("https://api.openai.com/v1"))

        self.assertEqual(normalize_anthropic_messages_url("https://api.anthropic.com"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(normalize_anthropic_messages_url("https://api.anthropic.com/v1"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(normalize_anthropic_messages_url("https://api.anthropic.com/v1/messages"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(normalize_anthropic_messages_url("https://api.minimaxi.com/anthropic"), "https://api.minimaxi.com/anthropic/v1/messages")
        self.assertEqual(normalize_anthropic_messages_url("https://api.minimaxi.com/anthropic/v1/messages"), "https://api.minimaxi.com/anthropic/v1/messages")

        cfg = {
            "providers": {
                "m-claude": {
                    "base_url": "https://api.anthropic.com/v1",
                    "api_key": "sk-ant-key",
                    "model": "claude-sonnet-4-6",
                    "extra_body": {"max_tokens": 8192, "temperature": 0.1},
                }
            }
        }
        rt = resolve_runtime_provider(requested="m-claude", target_model="claude-sonnet-4-6", config=cfg)
        self.assertEqual(rt["api_mode"], "anthropic_messages")
        self.assertEqual(rt["max_tokens"], 8192)
        self.assertEqual(rt["temperature"], 0.1)


class OpenAIChatTransportTest(unittest.TestCase):
    def test_gemini_thought_signature_capture_replay_strip_and_thinking_budget(self):
        captured_requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(json.loads(request.content.decode("utf-8")))
            return _sse_response(
                [
                    'data: {"choices":[{"index":0,"delta":{"extra_content":{"google":{"thought_signature":"sig_msg_1"}},"tool_calls":[{"index":0,"id":"tc_gem_1","type":"function","extra_content":{"google":{"thought_signature":"sig_tc_1"}},"function":{"name":"lookup","arguments":"{\\"q\\":"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hi\\"}"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":100,"completion_tokens":20}}',
                    "data: [DONE]",
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                gem = OpenAIChatTransport(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    api_key="gk",
                    provider="google",
                    client=client,
                )
                events = [
                    ev
                    async for ev in gem.stream(
                        model="gemini-3-flash-preview",
                        messages=[{"role": "user", "content": "title please"}],
                        max_tokens=20,
                        reasoning_config={"enabled": False},
                    )
                ]
                return events

        events = asyncio.run(run())
        # Verify Gemini 3 thinking_config + safe token floor (20 -> >=512 so title isn't starved).
        req_body = captured_requests[0]
        self.assertEqual(
            req_body["extra_body"]["google"]["thinking_config"],
            {"include_thoughts": False, "thinking_level": "low"},
        )
        self.assertGreaterEqual(req_body["max_tokens"], 512)

        # Verify Gemini 2.5 gets thinking_budget=0 and keeps max_tokens=20.
        gem_t = OpenAIChatTransport(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="gk",
            provider="google",
        )
        p25, _, _ = gem_t.build_request_payload(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
            reasoning_config={"enabled": False},
        )
        self.assertEqual(
            p25["extra_body"]["google"]["thinking_config"],
            {"include_thoughts": False, "thinking_budget": 0},
        )
        self.assertEqual(p25["max_tokens"], 20)

        # Verify gemma-* is excluded from thinking_config injection.
        pgemma, _, _ = gem_t.build_request_payload(
            model="gemma-3-27b-it",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=20,
            reasoning_config={"enabled": False},
        )
        self.assertNotIn("extra_body", pgemma)
        self.assertNotIn("thinking_config", pgemma)

        # Verify captured thought_signature events.
        tc_ev = next(e for e in events if isinstance(e, ToolCallComplete))
        self.assertEqual(tc_ev.thought_signature, "sig_tc_1")
        self.assertEqual(tc_ev.extra_content, {"google": {"thought_signature": "sig_tc_1"}})
        carrier_ev = next(e for e in events if isinstance(e, MessageCarrierEvent))
        self.assertEqual(carrier_ev.extra_content, {"google": {"thought_signature": "sig_msg_1"}})

        # Replay to Gemini -> preserves signatures; replay to strict OpenAI -> strips them!
        history = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "extra_content": {"google": {"thought_signature": "sig_msg_1"}},
                "tool_calls": [
                    {
                        "id": "tc_gem_1",
                        "type": "function",
                        "extra_content": {"google": {"thought_signature": "sig_tc_1"}},
                        "function": {"name": "lookup", "arguments": '{"q":"hi"}', "thought_signature": "sig_tc_1"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_gem_1", "content": "ok"},
        ]
        gem_replay, _, _ = gem_t.build_request_payload(model="gemini-3-flash-preview", messages=history)
        self.assertEqual(gem_replay["messages"][1]["extra_content"], {"google": {"thought_signature": "sig_msg_1"}})
        self.assertEqual(
            gem_replay["messages"][1]["tool_calls"][0]["extra_content"],
            {"google": {"thought_signature": "sig_tc_1"}},
        )

        strict_t = OpenAIChatTransport(base_url="https://api.groq.com/openai/v1", api_key="k", provider="groq")
        strict_replay, _, _ = strict_t.build_request_payload(model="llama-3.3-70b", messages=history)
        self.assertNotIn("extra_content", strict_replay["messages"][1])
        self.assertNotIn("extra_content", strict_replay["messages"][1]["tool_calls"][0])
        self.assertNotIn("thought_signature", strict_replay["messages"][1]["tool_calls"][0]["function"])

    def test_kimi_thinking_disabled_reasoning_content_and_schema_sanitization(self):
        kimi = OpenAIChatTransport(base_url="https://api.moonshot.cn/v1", api_key="mk", provider="moonshot")
        raw_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search web",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "nullable": True},
                            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                            "mode": {"type": "string", "enum": ["fast", "", None]},
                            "nested": {"properties": {"flag": {"type": "boolean"}}},
                        },
                        "required": ["query", "nonexistent_field"],
                    },
                },
            }
        ]
        history = [
            {"role": "user", "content": "find docs"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        payload, _, _ = kimi.build_request_payload(
            model="kimi-k2.5",
            messages=history,
            tools=raw_tools,
            reasoning_config={"enabled": False},
        )
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        # Missing reasoning_content on multi-step tool turn filled with default "".
        self.assertEqual(payload["messages"][1]["reasoning_content"], "")
        params = payload["tools"][0]["function"]["parameters"]
        self.assertEqual(params["required"], ["query"])
        self.assertNotIn("nullable", params["properties"]["query"])
        self.assertEqual(params["properties"]["limit"], {"type": "integer"})
        self.assertEqual(params["properties"]["mode"]["enum"], ["fast"])
        self.assertEqual(params["properties"]["nested"]["type"], "object")
        self.assertEqual(params["properties"]["nested"]["required"], [])

    def test_mistral_deterministic_9char_tool_id_bidirectional_mapping(self):
        mapper = MistralToolIdMapper()
        orig = "call_very_long_openai_id_1234567890"
        wire = mapper.to_wire(orig)
        self.assertRegex(wire, r"^[A-Za-z0-9]{9}$")
        self.assertEqual(wire, to_mistral_tool_call_id(orig))
        self.assertEqual(mapper.from_wire(wire), orig)
        # Already valid 9-char stays untouched.
        self.assertEqual(mapper.to_wire("AbCd12349"), "AbCd12349")

        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            captured.append(body)
            mapped_id = body["messages"][1]["tool_calls"][0]["id"]
            return _sse_response(
                [
                    f'data: {{"choices":[{{"index":0,"delta":{{"tool_calls":[{{"index":0,"id":"{mapped_id}","type":"function","function":{{"name":"ping","arguments":"{{}}"}}}}]}}}}]}}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                    "data: [DONE]",
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                mistral = OpenAIChatTransport(base_url="https://api.mistral.ai/v1", api_key="k", provider="mistral", client=client)
                return await collect_stream(
                    mistral.stream(
                        model="mistral-large-latest",
                        messages=[
                            {"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "", "tool_calls": [{"id": orig, "type": "function", "function": {"name": "ping", "arguments": "{}"}}]},
                            {"role": "tool", "tool_call_id": orig, "name": "ping", "content": "pong"},
                        ],
                    )
                )

        resp = asyncio.run(run())
        wire_tc_id = captured[0]["messages"][1]["tool_calls"][0]["id"]
        wire_tr_id = captured[0]["messages"][2]["tool_call_id"]
        self.assertEqual(wire_tc_id, wire_tr_id)
        self.assertRegex(wire_tc_id, r"^[A-Za-z0-9]{9}$")
        self.assertNotIn("name", captured[0]["messages"][2])
        self.assertEqual(resp.choices[0].message.tool_calls[0].id, orig)

    def test_xai_tool_search_alias_and_deepseek_cache_hit_usage(self):
        captured: list[dict[str, Any]] = []

        def xai_handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content.decode("utf-8")))
            return _sse_response(
                [
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"tc1","type":"function","function":{"name":"muse_tool_search","arguments":"{\\"q\\":\\"x\\"}"}}]}}]}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":50,"completion_tokens":10,"prompt_cache_hit_tokens":40}}',
                    "data: [DONE]",
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(xai_handler)) as client:
                xai = OpenAIChatTransport(base_url="https://api.x.ai/v1", api_key="xk", provider="xai", client=client)
                return await collect_stream(
                    xai.stream(
                        model="grok-3",
                        messages=[{"role": "user", "content": "find tool"}],
                        tools=[{"type": "function", "function": {"name": "tool_search", "parameters": {"type": "object"}}}],
                    )
                )

        resp = asyncio.run(run())
        self.assertEqual(captured[0]["tools"][0]["function"]["name"], "muse_tool_search")
        self.assertEqual(resp.choices[0].message.tool_calls[0].function.name, "tool_search")
        self.assertEqual(resp.usage.cache_read_tokens, 40)
        self.assertEqual(resp.usage.input_tokens, 10)  # uncached remainder for muse.db SUM(input+cache)
        self.assertEqual(resp.usage.prompt_tokens, 50)

    def test_xai_alias_collision_and_openrouter_gemini_gating(self):
        xai = OpenAIChatTransport(base_url="https://api.x.ai/v1", api_key="k", provider="xai")
        tools = [
            {"type": "function", "function": {"name": "muse_tool_search", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "tool_search", "parameters": {"type": "object"}}},
        ]
        payload, rev, _ = xai.build_request_payload(
            model="grok-4", messages=[{"role": "user", "content": "hi"}], tools=tools,
        )
        self.assertEqual([t["function"]["name"] for t in payload["tools"]], ["muse_tool_search", "muse_tool_search_2"])
        self.assertEqual(rev, {"muse_tool_search_2": "tool_search"})

        or_t = OpenAIChatTransport(base_url="https://openrouter.ai/api/v1", api_key="k", provider="openrouter")
        msgs = [
            {"role": "user", "content": "check"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "SIG_OR"}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        or_payload, _, _ = or_t.build_request_payload(
            model="google/gemini-2.5-flash",
            messages=msgs,
            max_tokens=20,
            reasoning_config={"enabled": False},
        )
        self.assertNotIn("thinking_config", or_payload)
        self.assertNotIn("extra_body", or_payload)
        self.assertEqual(or_payload["reasoning"], {"enabled": False, "effort": "none"})
        self.assertEqual(or_payload["messages"][1]["tool_calls"][0]["extra_content"], {"google": {"thought_signature": "SIG_OR"}})


class OpenAIResponsesTransportTest(unittest.TestCase):
    def test_codex_jwt_headers_zstd_encrypted_reasoning_isolation_and_xai_aliases(self):
        jwt_token = _fake_jwt(
            {
                "exp": 9999999999,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct_123",
                    "chatgpt_data_residency": "us",
                },
            }
        )
        seen_headers: list[dict[str, str]] = []
        seen_bodies: list[dict[str, Any]] = []

        def codex_handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            raw = request.content
            if request.headers.get("content-encoding") == "zstd":
                from compression import zstd

                raw = zstd.decompress(raw)
            seen_bodies.append(json.loads(raw.decode("utf-8")))
            return _sse_response(
                [
                    "event: response.reasoning_summary_text.delta",
                    'data: {"type":"response.reasoning_summary_text.delta","delta":"thinking..."}',
                    "event: response.output_text.delta",
                    'data: {"type":"response.output_text.delta","delta":"Hello from Codex"}',
                    "event: response.output_item.done",
                    'data: {"type":"response.output_item.done","item":{"type":"reasoning","id":"rs_1","encrypted_content":"ENC_BLOB_A","summary":[{"type":"summary_text","text":"thinking..."}]}}',
                    "event: response.completed",
                    'data: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":120,"output_tokens":30,"input_tokens_details":{"cached_tokens":80},"output_tokens_details":{"reasoning_tokens":15}}}}',
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(codex_handler)) as client:
                transport = OpenAIResponsesTransport(
                    base_url="https://chatgpt.com/backend-api/codex",
                    api_key=jwt_token,
                    provider="openai-codex",
                    client=client,
                )
                # Turn 1: mix one matching encrypted item and one foreign-model item.
                history = [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "step 1"},
                    {
                        "role": "assistant",
                        "content": "prev",
                        "codex_reasoning_items": [
                            {"type": "reasoning", "id": "rs_ok", "encrypted_content": "MATCHING", "_issuer_kind": "codex_backend", "_issuer_model": "gpt-5.4"},
                            {"type": "reasoning", "id": "rs_bad_model", "encrypted_content": "OTHER_MODEL", "_issuer_kind": "codex_backend", "_issuer_model": "gpt-5.5"},
                            {"type": "reasoning", "id": "rs_bad_issuer", "encrypted_content": "OTHER_ISSUER", "_issuer_kind": "xai_responses", "_issuer_model": "gpt-5.4"},
                        ],
                    },
                    {"role": "user", "content": "step 2"},
                ]
                events = [ev async for ev in transport.stream(model="gpt-5.4", messages=history)]
                return events

        events = asyncio.run(run())
        hdrs = {k.lower(): v for k, v in seen_headers[0].items()}
        self.assertEqual(hdrs.get("chatgpt-account-id"), "acct_123")
        self.assertEqual(hdrs.get("x-openai-internal-codex-residency"), "us")
        # Python 3.14 has compression.zstd -> compressed automatically on official Codex endpoint.
        try:
            from compression import zstd as _zstd  # noqa: F401

            self.assertEqual(hdrs.get("content-encoding"), "zstd")
        except ImportError:
            pass

        # Only the exact matching (issuer, model) encrypted item was replayed!
        replayed_enc = [i["encrypted_content"] for i in seen_bodies[0]["input"] if i.get("type") == "reasoning"]
        self.assertEqual(replayed_enc, ["MATCHING"])

        enc_ev = next(e for e in events if isinstance(e, EncryptedReasoningItem))
        self.assertEqual(enc_ev.issuer, "codex_backend")
        self.assertEqual(enc_ev.model, "gpt-5.4")
        self.assertEqual(enc_ev.encrypted_content, "ENC_BLOB_A")
        usage_ev = next(e for e in events if isinstance(e, UsageInfo))
        self.assertEqual((usage_ev.input_tokens, usage_ev.prompt_tokens, usage_ev.cache_read_tokens, usage_ev.reasoning_tokens), (40, 120, 80, 15))

        # Verify xAI Responses aliases both tool_search and web_search.
        xai_resp = OpenAIResponsesTransport(base_url="https://api.x.ai/v1", api_key="xk", provider="xai-oauth")
        xai_payload, rev, issuer, _ = xai_resp.build_request_payload(
            model="grok-4",
            messages=[{"role": "user", "content": "search"}],
            tools=[
                {"type": "function", "function": {"name": "tool_search", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
            ],
        )
        self.assertEqual(issuer, "xai_responses")
        wire_names = [t["name"] for t in xai_payload["tools"]]
        self.assertEqual(wire_names, ["muse_tool_search", "muse_web_search"])
        self.assertEqual(rev, {"muse_tool_search": "tool_search", "muse_web_search": "web_search"})

    def test_responses_401_oauth_refresh_with_zero_retries_and_xai_unalias_sse(self):
        from unittest import mock

        seen_auths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auths.append(request.headers.get("authorization", ""))
            if len(seen_auths) == 1:
                return httpx.Response(401, json={"error": {"message": "expired token"}})
            return _sse_response(
                [
                    "event: response.output_item.done",
                    'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_w1","name":"muse_web_search","arguments":"{\\"q\\":\\"news\\"}"}}',
                    "event: response.completed",
                    'data: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":10,"output_tokens":5}}}',
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                t = OpenAIResponsesTransport(
                    base_url="https://api.x.ai/v1",
                    api_key="old_tok",
                    provider="xai-oauth",
                    client=client,
                )
                with mock.patch(
                    "muse.llm.responses.resolve_xai_oauth_runtime_credentials",
                    return_value={"api_key": "new_tok"},
                ):
                    return await collect_stream(
                        t.stream(
                            model="grok-4",
                            messages=[{"role": "user", "content": "search"}],
                            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}],
                            retry_policy=RetryPolicy(max_retries=0, base_delay=0.0, jitter=0.0),
                        )
                    )

        resp = asyncio.run(run())
        self.assertEqual(seen_auths, ["Bearer old_tok", "Bearer new_tok"])
        self.assertEqual(resp.choices[0].message.tool_calls[0].function.name, "web_search")

    def test_codex_zstd_graceful_fallback_when_unavailable(self):
        from unittest import mock
        from muse.llm.oauth import maybe_zstd_compress

        raw = b'{"hello":"world"}'
        with mock.patch.dict("sys.modules", {"compression": None, "compression.zstd": None}):
            out, enc = maybe_zstd_compress(raw, base_url="https://chatgpt.com/backend-api/codex", path="/responses")
        self.assertEqual(out, raw)
        self.assertIsNone(enc)


class AnthropicMessagesTransportTest(unittest.TestCase):
    def test_alternating_merge_thinking_replay_three_tier_cache_and_sse(self):
        captured: list[tuple[dict[str, str], dict[str, Any]]] = []

        def ant_handler(request: httpx.Request) -> httpx.Response:
            captured.append((dict(request.headers), json.loads(request.content.decode("utf-8"))))
            return _sse_response(
                [
                    "event: message_start",
                    'data: {"type":"message_start","message":{"usage":{"input_tokens":500,"cache_read_input_tokens":300,"cache_creation_input_tokens":100}}}',
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"deep thought"}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig_xyz"}}',
                    "event: content_block_stop",
                    'data: {"type":"content_block_stop","index":0}',
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":1,"content_block":{"type":"redacted_thinking","data":"REDACT_DATA"}}',
                    "event: content_block_stop",
                    'data: {"type":"content_block_stop","index":1}',
                    "event: content_block_start",
                    'data: {"type":"content_block_start","index":2,"content_block":{"type":"text","text":""}}',
                    "event: content_block_delta",
                    'data: {"type":"content_block_delta","index":2,"delta":{"type":"text_delta","text":"Answer!"}}',
                    "event: content_block_stop",
                    'data: {"type":"content_block_stop","index":2}',
                    "event: message_delta",
                    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}',
                    "event: message_stop",
                    'data: {"type":"message_stop"}',
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(ant_handler)) as client:
                ant = AnthropicMessagesTransport(
                    base_url="https://api.anthropic.com/v1",
                    api_key="sk-ant-123",
                    provider="anthropic",
                    client=client,
                )
                messages = [
                    {"role": "system", "content": "System prompt"},
                    {"role": "user", "content": "u1"},
                    {"role": "user", "content": "u2"},  # consecutive user -> must merge!
                    {
                        "role": "assistant",
                        "content": "a1",
                        "reasoning_details": [
                            {"type": "thinking", "thinking": "prev", "signature": "sig_prev"},
                            {"type": "redacted_thinking", "data": "red_prev"},
                        ],
                    },
                    {"role": "assistant", "content": "a2"},  # consecutive assistant -> must merge!
                    {"role": "user", "content": "u3"},
                ]
                tools = [
                    {"type": "function", "function": {"name": "get_time", "description": "now", "parameters": {"type": "object", "properties": {}}}}
                ]
                return await collect_stream(
                    ant.stream(
                        model="claude-sonnet-4-6",
                        messages=messages,
                        tools=tools,
                        max_tokens=1024,
                        reasoning_config={"enabled": True, "effort": "medium"},
                    )
                )

        resp = asyncio.run(run())
        hdrs, body = captured[0]
        hdrs_l = {k.lower(): v for k, v in hdrs.items()}
        self.assertEqual(hdrs_l.get("x-api-key"), "sk-ant-123")
        self.assertNotIn("authorization", hdrs_l)

        # Strict alternation: user -> assistant -> user!
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        # Replayed signed thinking + redacted_thinking preserved, no cache_control on thinking!
        asst_blocks = body["messages"][1]["content"]
        self.assertEqual(asst_blocks[0]["type"], "thinking")
        self.assertEqual(asst_blocks[0]["signature"], "sig_prev")
        self.assertNotIn("cache_control", asst_blocks[0])
        self.assertEqual(asst_blocks[1], {"type": "redacted_thinking", "data": "red_prev"})

        # 3-tier cache_control: Tier 1 system, Tier 2 tools[-1], Tier 3 recent history turns (<= 4 total).
        self.assertEqual(body["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][-2]["content"][-1]["cache_control"], {"type": "ephemeral"})

        # Verify SSE capture of thinking + signature + redacted_thinking + usage.
        self.assertEqual(resp.choices[0].message.content, "Answer!")
        self.assertEqual(resp.choices[0].message.reasoning_content, "deep thought")
        self.assertEqual(
            resp.choices[0].message.reasoning_details,
            [
                {"type": "thinking", "thinking": "deep thought", "signature": "sig_xyz"},
                {"type": "redacted_thinking", "data": "REDACT_DATA"},
            ],
        )
        self.assertEqual((resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_tokens, resp.usage.cache_write_tokens), (500, 42, 300, 100))

    def test_anthropic_image_resize_over_8000px_and_non_native_transcode(self):
        from PIL import Image

        # 1) Oversized dimension (>8000px) -> downscaled <= 8000px.
        wide = Image.new("RGB", (8200, 200), color=(20, 120, 220))
        buf = io.BytesIO()
        wide.save(buf, format="PNG")
        out_bytes, out_mime = prepare_anthropic_image_sync(buf.getvalue(), "image/png")
        with Image.open(io.BytesIO(out_bytes)) as resized:
            self.assertLessEqual(resized.size[0], ANTHROPIC_MAX_IMAGE_DIM)
            self.assertLessEqual(resized.size[1], ANTHROPIC_MAX_IMAGE_DIM)
            self.assertLessEqual(len(out_bytes), ANTHROPIC_MAX_IMAGE_BYTES)
        self.assertIn(out_mime, ("image/png", "image/jpeg"))

        # 2) BMP (non-native) -> transcoded to PNG/JPEG.
        bmp_img = Image.new("RGB", (64, 64), color=(255, 0, 0))
        bmp_buf = io.BytesIO()
        bmp_img.save(bmp_buf, format="BMP")
        self.assertEqual(sniff_image_mime(bmp_buf.getvalue()), "image/bmp")
        tr_bytes, tr_mime = prepare_anthropic_image_sync(bmp_buf.getvalue(), "image/bmp")
        self.assertIn(tr_mime, ("image/png", "image/jpeg"))
        self.assertEqual(sniff_image_mime(tr_bytes), tr_mime)

        # 3) HEIC/AVIF magic sniffing + HEIC transcode via pillow-heif when available.
        self.assertEqual(sniff_image_mime(b"\x00\x00\x00\x18ftypheic"), "image/heic")
        self.assertEqual(sniff_image_mime(b"\x00\x00\x00\x18ftypavif"), "image/avif")
        try:
            import pillow_heif

            heif_file = pillow_heif.from_pillow(Image.new("RGB", (40, 40), color=(0, 200, 100)))
            heic_buf = io.BytesIO()
            heif_file.save(heic_buf, format="HEIF")
            heic_raw = heic_buf.getvalue()
            conv_bytes, conv_mime = prepare_anthropic_image_sync(heic_raw, "image/heic")
            self.assertIn(conv_mime, ("image/png", "image/jpeg"))
            self.assertEqual(sniff_image_mime(conv_bytes), conv_mime)
        except ImportError:
            pass

        # 4) Oversized byte budget (>5MB simulated via smaller max_bytes param) -> shrunk under limit.
        import os

        noisy = Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
        nbuf = io.BytesIO()
        noisy.save(nbuf, format="PNG")
        noisy_raw = nbuf.getvalue()
        self.assertGreater(len(noisy_raw), 100_000)
        shrunk_bytes, shrunk_mime = prepare_anthropic_image_sync(
            noisy_raw, "image/png", max_bytes=80_000, target_bytes=75_000
        )
        self.assertLessEqual(len(shrunk_bytes), 80_000)
        self.assertEqual(shrunk_mime, "image/jpeg")

    def test_minimax_bearer_auth_opus47_temp_strip_and_async_image_prep(self):
        from PIL import Image

        # MiniMax uses Authorization: Bearer (never x-api-key).
        mm = AnthropicMessagesTransport(base_url="https://api.minimax.io/anthropic", api_key="mm-key", provider="minimax")
        hdrs = {k.lower(): v for k, v in mm.build_headers().items()}
        self.assertEqual(hdrs.get("authorization"), "Bearer mm-key")
        self.assertNotIn("x-api-key", hdrs)

        # Async build_request_payload runs threadpool BMP->PNG/JPEG transcode & strips temperature on claude-opus-4-7
        # while preserving interleaved thinking+tool_use replay order!
        bmp_buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(10, 20, 30)).save(bmp_buf, format="BMP")
        b64_bmp = base64.b64encode(bmp_buf.getvalue()).decode("ascii")

        ant = AnthropicMessagesTransport(base_url="https://api.anthropic.com", api_key="sk-ant", provider="anthropic")
        payload = asyncio.run(
            ant.build_request_payload(
                model="claude-opus-4-7",
                messages=[
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/bmp;base64,{b64_bmp}"}}]},
                    {
                        "role": "assistant",
                        "anthropic_content_blocks": [
                            {"type": "thinking", "thinking": "step1", "signature": "sig_1"},
                            {"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {"k": "v"}},
                        ],
                    },
                    {"role": "tool", "tool_call_id": "tu_1", "content": "result"},
                ],
                extra_body={"max_tokens": 2048, "temperature": 0.4},
            )
        )
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertNotIn("temperature", payload)
        # BMP image was transcoded off the event loop to PNG/JPEG!
        img_src = payload["messages"][0]["content"][0]["source"]
        self.assertIn(img_src["media_type"], ("image/png", "image/jpeg"))
        # Interleaved thinking + tool_use preserved in exact order!
        asst = payload["messages"][1]["content"]
        self.assertEqual([b["type"] for b in asst], ["thinking", "tool_use"])
        self.assertEqual(asst[0]["signature"], "sig_1")


class RetryAndErrorKindTest(unittest.TestCase):
    def test_retry_status_retrying_and_recovered_events(self):
        attempts = {"n": 0}

        def flaky_handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})
            if attempts["n"] == 2:
                return httpx.Response(503, json={"error": {"message": "server overloaded"}})
            return _sse_response(
                [
                    'data: {"choices":[{"index":0,"delta":{"content":"recovered!"}}]}',
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
                    "data: [DONE]",
                ]
            )

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(flaky_handler)) as client:
                t = OpenAIChatTransport(base_url="https://api.openai.com/v1", api_key="k", client=client)
                return [
                    ev
                    async for ev in t.stream(
                        model="gpt-4o",
                        messages=[{"role": "user", "content": "hi"}],
                        retry_policy=RetryPolicy(max_retries=3, base_delay=0.0, jitter=0.0),
                    )
                ]

        events = asyncio.run(run())
        retries = [e for e in events if isinstance(e, RetryStatusEvent)]
        self.assertEqual([r.state for r in retries], ["retrying", "retrying", "recovered"])
        self.assertEqual(retries[0].error_kind, "rate_limit")
        self.assertEqual(retries[1].error_kind, "server")
        self.assertTrue(any(isinstance(e, TextDelta) and e.text == "recovered!" for e in events))

    def test_exact_five_failure_error_kinds(self):
        cases = [
            ("billing", lambda _r: httpx.Response(402, json={"error": {"message": "Insufficient credits"}})),
            ("rate_limit", lambda _r: httpx.Response(429, json={"error": {"message": "Too many requests"}})),
            ("server", lambda _r: httpx.Response(500, json={"error": {"message": "Internal error"}})),
            ("timeout", lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("read timed out", request=r))),
            ("connection", lambda r: (_ for _ in ()).throw(httpx.ConnectError("connection refused", request=r))),
        ]

        async def check(handler):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                t = OpenAIChatTransport(base_url="https://api.openai.com/v1", api_key="k", client=client)
                async for _ in t.stream(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "hi"}],
                    retry_policy=RetryPolicy(max_retries=1, base_delay=0.0, jitter=0.0),
                ):
                    pass

        for expected_kind, handler in cases:
            with self.subTest(kind=expected_kind):
                with self.assertRaises(LLMRequestError) as ctx:
                    asyncio.run(check(handler))
                self.assertEqual(ctx.exception.failure_error_kind, expected_kind)
                self.assertEqual(ctx.exception.error_kind, expected_kind)


if __name__ == "__main__":
    unittest.main()
