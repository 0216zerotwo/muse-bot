"""Async streaming OpenAI Chat Completions transport with full provider parity (Gemini/Kimi/Mistral/xAI/DeepSeek)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import string
from typing import Any, AsyncIterator, Optional

import httpx

from muse.llm.providers import (
    is_openai_reasoning_model,
    normalize_chat_completions_url,
    prenormalize_provider_params,
)
from muse.llm.types import (
    MessageCarrierEvent,
    ReasoningDelta,
    RetryPolicy,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallDelta,
    UsageInfo,
    host_matches,
    iter_sse_json,
    post_and_stream,
    stream_with_retry,
)

logger = logging.getLogger(__name__)

_GEMINI_HOSTS = ("generativelanguage.googleapis.com", "aiplatform.googleapis.com")
_GEMINI_PROVIDERS = frozenset({"gemini", "google", "vertex"})
_MISTRAL_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9]{9}$")
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_MISTRAL_MARKERS = ("mistral", "magistral", "mixtral", "devstral", "codestral", "ministral", "pixtral", "voxtral")
_XAI_TOOL_SEARCH_ALIAS = "muse_tool_search"
_GEMINI_VERSION_RE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?")
_GEMINI_3_HIDDEN_THOUGHT_TOKEN_FLOOR = 512
_GEMINI_ENABLED_THOUGHT_TOKEN_FLOOR = 4096
_STRIP_MSG_KEYS = frozenset({
    "id", "codex_reasoning_items", "codex_message_items", "tool_name", "effect_disposition", "timestamp",
    "platform_message_id", "message_id", "api_content", "anthropic_content_blocks", "bedrock_content_blocks",
})
_STRIP_TC_KEYS = frozenset({"call_id", "response_item_id"})
_SCHEMA_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_LIST_KEYS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_NODE_KEYS = frozenset({"items", "contains", "not", "additionalProperties", "propertyNames", "if", "then", "else"})
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})
_ENUM_SAMPLE_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"))


# ── Provider detection & Gemini thinking helpers ─────────────────────────────


def is_gemini_openai_compat_url(base_url: Any) -> bool:
    norm = str(base_url or "").strip().rstrip("/").lower()
    return bool(norm) and host_matches(norm, "generativelanguage.googleapis.com") and norm.endswith("/openai")


def is_gemini_signature_route(model: Any, base_url: Any = None, provider: Any = None) -> bool:
    m, p = str(model or "").strip().lower(), str(provider or "").strip().lower()
    return "gemini" in m or "gemma" in m or p in _GEMINI_PROVIDERS or any(host_matches(base_url, h) for h in _GEMINI_HOSTS)


def _bare_gemini_model(model: Any) -> str:
    return str(model or "").strip().lower().removeprefix("google/")


def _gemini_version(model: Any) -> tuple[int, int]:
    match = _GEMINI_VERSION_RE.match(_bare_gemini_model(model))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)


def is_gemini_thinking_model(model: Any) -> bool:
    """True for ``gemini-*`` models (excluding ``gemma-*`` which HTTP 400 on thinking_config)."""
    bare = _bare_gemini_model(model)
    return bare.startswith("gemini") and not bare.startswith("gemma")


def build_gemini_thinking_config(model: str, reasoning_config: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(reasoning_config, dict) or not is_gemini_thinking_model(model):
        return None
    bare = _bare_gemini_model(model)
    ver = _gemini_version(bare)
    uses_level = ver >= (3, 0) or bare.endswith("-latest")
    effort = str(reasoning_config.get("effort") or "medium").strip().lower()
    if reasoning_config.get("enabled") is False or effort == "none":
        cfg: dict[str, Any] = {"include_thoughts": False}
        if bare.startswith("gemini-2.5") or (ver[0] == 2 and ver[1] >= 5):
            cfg["thinking_budget"] = 0
        elif uses_level:
            cfg["thinking_level"] = "low"
        return cfg
    cfg = {"include_thoughts": True}
    if uses_level:
        if "pro" in bare and ver < (4, 0):
            cfg["thinking_level"] = "high" if effort in {"high", "xhigh", "max", "ultra"} else "low"
        elif effort in {"minimal", "low"}:
            cfg["thinking_level"] = "low"
        elif effort in {"high", "xhigh", "max", "ultra"}:
            cfg["thinking_level"] = "high"
        else:
            cfg["thinking_level"] = "medium"
    return cfg


def apply_gemini_token_floor(model: str, thinking_config: Optional[dict[str, Any]], max_tokens: Optional[int]) -> Optional[int]:
    """Prevent Gemini 3+ hidden thoughts at ``thinking_level: "low"`` from starving small output caps."""
    del model
    if max_tokens is None or not thinking_config or thinking_config.get("thinking_budget") == 0:
        return max_tokens
    if thinking_config.get("include_thoughts") is True:
        return max(max_tokens, _GEMINI_ENABLED_THOUGHT_TOKEN_FLOOR)
    return max(max_tokens, _GEMINI_3_HIDDEN_THOUGHT_TOKEN_FLOOR) if "thinking_level" in thinking_config else max_tokens


def is_moonshot_route(model: Any, base_url: Any = None, provider: Any = None) -> bool:
    p = str(provider or "").strip().lower()
    if "moonshot" in p or "kimi" in p or any(host_matches(base_url, h) for h in ("moonshot.cn", "moonshot.ai", "kimi.com")):
        return True
    bare = str(model or "").strip().lower()
    tail = bare.rsplit("/", 1)[-1] if bare else ""
    return bool(bare) and (tail == "kimi" or tail.startswith("kimi-") or tail == "k3" or tail.startswith(("k3.", "k3-")) or "moonshot" in bare or "/kimi" in bare)


def is_mistral_route(model: Any, base_url: Any = None, provider: Any = None) -> bool:
    p, m = str(provider or "").strip().lower(), str(model or "").strip().lower()
    return p == "mistral" or host_matches(base_url, "mistral.ai") or any(k in m for k in _MISTRAL_MARKERS)


def is_xai_route(model: Any, base_url: Any = None, provider: Any = None) -> bool:
    p, m = str(provider or "").strip().lower(), str(model or "").strip().lower()
    return p in {"xai", "xai-oauth"} or host_matches(base_url, "x.ai") or m.startswith(("grok-", "x-ai/grok-"))


def _is_deepseek_route(model: Any, base_url: Any = None, provider: Any = None) -> bool:
    p, m = str(provider or "").strip().lower(), str(model or "").strip().lower()
    return p == "deepseek" or host_matches(base_url, "deepseek.com") or "deepseek" in m


# ── Moonshot schema sanitizer, Mistral ID mapper, xAI aliases, thought_sig ───


def _fill_missing_type(node: dict[str, Any]) -> dict[str, Any]:
    nt = node.get("type")
    if isinstance(nt, list):
        return {**node, "type": next((t for t in nt if isinstance(t, str) and t not in {"", "null"}), "string")}
    if "type" in node and nt not in {None, ""}:
        return node
    if any(k in node for k in ("properties", "required", "additionalProperties")):
        inferred = "object"
    elif "items" in node or "prefixItems" in node:
        inferred = "array"
    elif isinstance(node.get("enum"), list) and node["enum"]:
        inferred = next((t for cls, t in _ENUM_SAMPLE_TYPES if isinstance(node["enum"][0], cls)), "string")
    elif any(k in node for k in ("if", "then", "else")):
        return node
    else:
        inferred = "string"
    return {**node, "type": inferred}


def _ensure_required_array(node: dict[str, Any]) -> dict[str, Any]:
    props, req = node.get("properties"), node.get("required")
    node["required"] = [r for r in req if r in props] if (isinstance(req, list) and isinstance(props, dict)) else (list(req) if isinstance(req, list) else [])
    return node


def _repair_moonshot_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_repair_moonshot_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    repaired = {
        k: ({sk: _repair_moonshot_schema(sv) for sk, sv in v.items()} if k in _SCHEMA_MAP_KEYS and isinstance(v, dict)
            else (_repair_moonshot_schema(v) if (k in _SCHEMA_LIST_KEYS and isinstance(v, list)) or (k in _SCHEMA_NODE_KEYS and isinstance(v, dict)) else v))
        for k, v in node.items()
    }
    if isinstance(repaired.get("anyOf"), list):
        repaired.pop("type", None)
        non_null = [b for b in repaired["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) < len(repaired["anyOf"]):
            repaired = {**{k: v for k, v in repaired.items() if k != "anyOf"}, **non_null[0]}
        else:
            if non_null and len(non_null) < len(repaired["anyOf"]):
                repaired["anyOf"] = non_null
            repaired.pop("nullable", None)
            return repaired
    repaired.pop("nullable", None)
    if "$ref" not in repaired:
        repaired = _fill_missing_type(repaired)
    if isinstance(repaired.get("enum"), list) and repaired.get("type") in _SCALAR_TYPES:
        cleaned = [v for v in repaired["enum"] if v is not None and v != ""]
        if cleaned:
            repaired["enum"] = cleaned
        else:
            repaired.pop("enum", None)
    return _ensure_required_array(repaired) if repaired.get("type") == "object" else repaired


def sanitize_moonshot_tool_parameters(parameters: Any) -> dict[str, Any]:
    repaired = _repair_moonshot_schema(copy.deepcopy(parameters)) if isinstance(parameters, dict) else {}
    if not isinstance(repaired, dict):
        repaired = {}
    repaired["type"] = "object"
    repaired.setdefault("properties", {})
    return _ensure_required_array(repaired)


def sanitize_moonshot_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**t, "function": {**t["function"], "parameters": sanitize_moonshot_tool_parameters(t["function"].get("parameters"))}}
        if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
        for t in tools
    ]


def to_mistral_tool_call_id(raw_id: str) -> str:
    """Deterministic 9-char Base62 id matching ``^[A-Za-z0-9]{9}$``."""
    key = str(raw_id or "").strip()
    if _MISTRAL_TOOL_ID_RE.fullmatch(key):
        return key
    num = int.from_bytes(hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()[:8], "big")
    chars = []
    for _ in range(9):
        num, rem = divmod(num, 62)
        chars.append(_BASE62[rem])
    return "".join(chars)


class MistralToolIdMapper:
    """Bidirectional deterministic mapper between canonical tool_call IDs and Mistral 9-char IDs."""

    def __init__(self) -> None:
        self._to_wire: dict[str, str] = {}
        self._from_wire: dict[str, str] = {}

    def to_wire(self, original_id: str) -> str:
        raw = str(original_id or "").strip()
        if not raw or raw in self._to_wire:
            return self._to_wire.get(raw, raw)
        mapped, nonce = to_mistral_tool_call_id(raw), 1
        while mapped in self._from_wire and self._from_wire[mapped] != raw:
            mapped = to_mistral_tool_call_id(f"{raw}#{nonce}")
            nonce += 1
        self._to_wire[raw], self._from_wire[mapped] = mapped, raw
        return mapped

    def from_wire(self, wire_id: str) -> str:
        return self._from_wire.get(wire_id, wire_id)


def alias_xai_chat_tools(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    taken = {(t.get("function") or {}).get("name") for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or fn.get("name") != "tool_search":
            out.append(tool)
            continue
        alias, suffix = _XAI_TOOL_SEARCH_ALIAS, 2
        while alias in taken:
            alias = f"{_XAI_TOOL_SEARCH_ALIAS}_{suffix}"
            suffix += 1
        taken.add(alias)
        forward["tool_search"], reverse[alias] = alias, "tool_search"
        out.append({**tool, "function": {**fn, "name": alias}})
    return out, forward, reverse


def extract_thought_signature(extra_content: Any) -> Optional[str]:
    if not isinstance(extra_content, dict):
        return None
    google = extra_content.get("google")
    sig = google.get("thought_signature") if isinstance(google, dict) else extra_content.get("thought_signature")
    return sig.strip() if isinstance(sig, str) and sig.strip() else None


def _norm_sig(extra_content: Any) -> Optional[dict[str, Any]]:
    sig = extract_thought_signature(extra_content)
    return {"google": {"thought_signature": sig}} if sig else None


def _flatten_text_only_content(content: Any) -> Any:
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content):
        return "\n".join(str(p.get("text") or "") for p in content if p.get("text"))
    return content


def _sanitize_chat_messages(
    messages: list[dict[str, Any]], *, keep_gemini_sig: bool, is_kimi: bool, keep_reasoning_content: bool,
    mistral_mapper: Optional[MistralToolIdMapper], tool_forward: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role", "user")
        msg = {k: v for k, v in raw.items() if k not in _STRIP_MSG_KEYS and not (isinstance(k, str) and k.startswith("_"))}
        if "content" in msg:
            msg["content"] = _flatten_text_only_content(msg["content"])
        if role == "tool":
            msg.pop("name", None)
            if mistral_mapper is not None and msg.get("tool_call_id"):
                msg["tool_call_id"] = mistral_mapper.to_wire(str(msg["tool_call_id"]))
        elif role == "assistant":
            norm_extra = _norm_sig(msg.get("extra_content")) if keep_gemini_sig else None
            if norm_extra:
                msg["extra_content"] = norm_extra
            else:
                msg.pop("extra_content", None)
            if not keep_reasoning_content:
                msg.pop("reasoning_content", None)
            msg.pop("reasoning", None)
            msg.pop("reasoning_details", None)
            tcs = msg.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                cleaned_tcs: list[dict[str, Any]] = []
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    tc_c = {k: v for k, v in tc.items() if k not in _STRIP_TC_KEYS}
                    if isinstance(tc_c.get("function"), dict):
                        fn_c = dict(tc_c["function"])
                        if not keep_gemini_sig:
                            fn_c.pop("thought_signature", None)
                        if fn_c.get("name") in tool_forward:
                            fn_c["name"] = tool_forward[fn_c["name"]]
                        tc_c["function"] = fn_c
                    norm_tc = _norm_sig(tc_c.get("extra_content")) if keep_gemini_sig else None
                    if norm_tc:
                        tc_c["extra_content"] = norm_tc
                    else:
                        tc_c.pop("extra_content", None)
                    if mistral_mapper is not None and tc_c.get("id"):
                        tc_c["id"] = mistral_mapper.to_wire(str(tc_c["id"]))
                    cleaned_tcs.append(tc_c)
                if cleaned_tcs:
                    msg["tool_calls"] = cleaned_tcs
                    if is_kimi and "reasoning_content" not in msg:
                        msg["reasoning_content"] = ""
                else:
                    msg.pop("tool_calls", None)
            else:
                msg.pop("tool_calls", None)
        out.append(msg)
    return out


def _extract_chat_usage(raw: Any) -> Optional[UsageInfo]:
    if not isinstance(raw, dict):
        return None
    pd = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    cd = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    prompt_total = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    cached = int(
        pd.get("cached_tokens") or raw.get("cache_read_input_tokens")
        or raw.get("prompt_cache_hit_tokens") or raw.get("cached_tokens") or 0
    )
    cache_write = int(
        pd.get("cache_write_tokens") or pd.get("cache_creation_input_tokens")
        or raw.get("cache_creation_input_tokens") or raw.get("cache_write_tokens") or 0
    )
    return UsageInfo(
        input_tokens=max(0, prompt_total - cached - cache_write),
        output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=int(cd.get("reasoning_tokens") or 0),
    )


# ── OpenAIChatTransport ──────────────────────────────────────────────────────


class OpenAIChatTransport:
    """Async streaming transport for OpenAI-compatible ``/chat/completions`` endpoints."""

    api_mode: str = "chat_completions"

    def __init__(
        self, *, base_url: str, api_key: str, provider: str = "",
        default_headers: Optional[dict[str, str]] = None, client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = str(base_url or "https://api.openai.com/v1").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.provider = str(provider or "").strip()
        self.default_headers = dict(default_headers or {})
        self._client = client

    def build_request_payload(
        self, *, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
        reasoning_config: Optional[dict[str, Any]] = None, extra_body: Optional[dict[str, Any]] = None,
        stream: bool = True, stream_options: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], dict[str, str], Optional[MistralToolIdMapper]]:
        eff_max, eff_temp, cleaned_extra = prenormalize_provider_params(
            extra_body=extra_body, max_tokens=max_tokens, temperature=temperature
        )
        is_kimi = is_moonshot_route(model, self.base_url, self.provider)
        is_mistral = is_mistral_route(model, self.base_url, self.provider)
        is_xai = is_xai_route(model, self.base_url, self.provider)
        is_deepseek = _is_deepseek_route(model, self.base_url, self.provider)
        keep_gemini_sig = is_gemini_signature_route(model, self.base_url, self.provider)
        is_gemini_compat = is_gemini_openai_compat_url(self.base_url)

        wire_tools = copy.deepcopy(tools) if tools else None
        tool_forward: dict[str, str] = {}
        tool_reverse: dict[str, str] = {}
        if wire_tools:
            if is_kimi:
                wire_tools = sanitize_moonshot_tools(wire_tools)
            if is_xai:
                wire_tools, tool_forward, tool_reverse = alias_xai_chat_tools(wire_tools)

        mistral_mapper = MistralToolIdMapper() if is_mistral else None
        wire_messages = _sanitize_chat_messages(
            messages, keep_gemini_sig=keep_gemini_sig, is_kimi=is_kimi,
            keep_reasoning_content=(is_kimi or is_deepseek),
            mistral_mapper=mistral_mapper, tool_forward=tool_forward,
        )

        is_google_ep = is_gemini_compat or any(host_matches(self.base_url, h) for h in _GEMINI_HOSTS) or self.provider.lower() in _GEMINI_PROVIDERS
        is_openrouter = self.provider.lower() == "openrouter" or host_matches(self.base_url, "openrouter.ai")
        gemini_thinking: Optional[dict[str, Any]] = None
        if is_google_ep and is_gemini_thinking_model(model):
            gemini_thinking = build_gemini_thinking_config(model, reasoning_config)
            if gemini_thinking is not None:
                eff_max = apply_gemini_token_floor(model, gemini_thinking, eff_max)

        payload: dict[str, Any] = {"model": model, "messages": wire_messages}
        if stream:
            payload["stream"] = True
            payload["stream_options"] = dict(stream_options) if isinstance(stream_options, dict) else {"include_usage": True}

        if is_openai_reasoning_model(model):
            if eff_max is not None:
                payload["max_completion_tokens"] = eff_max
            if isinstance(reasoning_config, dict):
                eff = str(reasoning_config.get("effort") or "").strip().lower()
                off = reasoning_config.get("enabled") is False or eff == "none"
                if off:
                    if str(model or "").strip().lower().removeprefix("openai/").startswith("gpt-5"):
                        payload["reasoning_effort"] = "minimal"
                elif eff in {"minimal", "low", "medium", "high", "xhigh", "max", "adaptive"}:
                    payload["reasoning_effort"] = (
                        "high" if eff in {"xhigh", "max"} else ("medium" if eff == "adaptive" else eff)
                    )
        else:
            if eff_max is not None:
                payload["max_tokens"] = eff_max
            if eff_temp is not None:
                payload["temperature"] = eff_temp

        if wire_tools:
            payload["tools"] = wire_tools
        if is_kimi and isinstance(reasoning_config, dict):
            off = reasoning_config.get("enabled") is False or str(reasoning_config.get("effort") or "").strip().lower() == "none"
            cleaned_extra.setdefault("thinking", {"type": "disabled" if off else "enabled"})
        elif is_openrouter and isinstance(reasoning_config, dict):
            eff = str(reasoning_config.get("effort") or "medium").strip().lower()
            off = reasoning_config.get("enabled") is False or eff == "none"
            or_eff = "high" if eff in {"xhigh", "max"} else ("medium" if eff == "adaptive" else eff)
            cleaned_extra.setdefault("reasoning", {"enabled": not off, "effort": "none" if off else or_eff})
        if gemini_thinking is not None:
            if is_gemini_compat:
                nested = dict(cleaned_extra.get("extra_body") or {})
                google_block = {**(nested.get("google") or {}), "thinking_config": gemini_thinking}
                cleaned_extra["extra_body"] = {**nested, "google": google_block}
            else:
                cleaned_extra.setdefault("thinking_config", gemini_thinking)

        for k, v in cleaned_extra.items():
            payload.setdefault(k, v)
        return payload, tool_reverse, mistral_mapper

    async def stream(
        self, *, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
        reasoning_config: Optional[dict[str, Any]] = None, extra_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None, timeout: Optional[float] = None,
        retry_policy: Optional[RetryPolicy] = None, **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        payload, tool_reverse, mistral_mapper = self.build_request_payload(
            model=model, messages=messages, tools=tools, max_tokens=max_tokens, temperature=temperature,
            reasoning_config=reasoning_config, extra_body=extra_body, stream=True, stream_options=kwargs.get("stream_options"),
        )
        url = normalize_chat_completions_url(self.base_url)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **self.default_headers, **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req_timeout = httpx.Timeout(float(timeout) if timeout and timeout > 0 else 120.0, connect=15.0)

        def _attempt(_attempt_num: int) -> AsyncIterator[StreamEvent]:
            return post_and_stream(
                client=self._client, url=url, headers=headers, timeout=req_timeout,
                provider=self.provider, model=model, json_body=payload,
                on_json=lambda data: self._events_from_json(data, tool_reverse, mistral_mapper),
                on_sse=lambda resp: self._parse_sse(resp, tool_reverse, mistral_mapper),
            )

        async for event in stream_with_retry(_attempt, policy=retry_policy, provider=self.provider, model=model):
            yield event

    async def _events_from_json(
        self, data: dict[str, Any], tool_reverse: dict[str, str], mistral_mapper: Optional[MistralToolIdMapper],
    ) -> AsyncIterator[StreamEvent]:
        choice = (data.get("choices") or [{}])[0]
        msg, finish = choice.get("message") or {}, str(choice.get("finish_reason") or "stop").lower()
        if isinstance(r := (msg.get("reasoning_content") or msg.get("reasoning")), str) and r:
            yield ReasoningDelta(text=r)
        if isinstance(c := msg.get("content"), str) and c:
            yield TextDelta(text=c)
        if msg_sig := _norm_sig(msg.get("extra_content")):
            yield MessageCarrierEvent(extra_content=msg_sig, finish_reason=finish)
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = tool_reverse.get(str(fn.get("name") or ""), str(fn.get("name") or ""))
            tc_id = mistral_mapper.from_wire(str(tc.get("id") or f"call_{idx}")) if mistral_mapper else str(tc.get("id") or f"call_{idx}")
            args = fn.get("arguments")
            args_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            extra = _norm_sig(tc.get("extra_content"))
            sig = extract_thought_signature(extra) or (str(fn["thought_signature"]) if fn.get("thought_signature") else None)
            if sig and not extra:
                extra = {"google": {"thought_signature": sig}}
            yield ToolCallComplete(id=tc_id, name=name, arguments=args_str or "{}", index=idx, extra_content=extra, thought_signature=sig)
        usage = _extract_chat_usage(data.get("usage"))
        if usage is not None:
            yield usage
        yield StreamDone(finish_reason=finish, usage=usage)

    async def _parse_sse(
        self, resp: httpx.Response, tool_reverse: dict[str, str], mistral_mapper: Optional[MistralToolIdMapper],
    ) -> AsyncIterator[StreamEvent]:
        tool_acc: dict[int, dict[str, Any]] = {}
        id_to_idx: dict[str, int] = {}
        last_implicit_idx: Optional[int] = None
        msg_extra: Optional[dict[str, Any]] = None
        finish_reason, usage = "stop", None
        async for _, chunk in iter_sse_json(resp):
            if chunk.get("usage"):
                usage = _extract_chat_usage(chunk["usage"]) or usage
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"]).lower()
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("extra_content"):
                msg_extra = _norm_sig(delta["extra_content"]) or msg_extra
            if isinstance(rc := (delta.get("reasoning_content") or delta.get("reasoning")), str) and rc:
                yield ReasoningDelta(text=rc)
            if isinstance(tc_text := delta.get("content"), str) and tc_text:
                yield TextDelta(text=tc_text)
            for tc_d in delta.get("tool_calls") or []:
                if not isinstance(tc_d, dict):
                    continue
                fn = tc_d.get("function") if isinstance(tc_d.get("function"), dict) else {}
                raw_id = str(tc_d["id"]) if tc_d.get("id") else ""
                mapped_id = (mistral_mapper.from_wire(raw_id) if mistral_mapper else raw_id) if raw_id else ""
                raw_idx = tc_d.get("index")
                if mapped_id and mapped_id in id_to_idx:
                    idx = id_to_idx[mapped_id]
                elif raw_idx is not None:
                    cand_idx = int(raw_idx)
                    existing = tool_acc.get(cand_idx)
                    if existing and mapped_id and existing["id"] and existing["id"] != mapped_id:
                        idx = max(tool_acc.keys(), default=-1) + 1
                    else:
                        idx = cand_idx
                elif mapped_id:
                    if 0 in tool_acc and not tool_acc[0]["id"] and not tool_acc[0]["name"] and not tool_acc[0]["arguments"]:
                        idx = 0
                    else:
                        idx = max(tool_acc.keys(), default=-1) + 1
                elif fn.get("name") and last_implicit_idx is not None and tool_acc.get(last_implicit_idx, {}).get("name"):
                    idx = max(tool_acc.keys(), default=-1) + 1
                elif last_implicit_idx is not None:
                    idx = last_implicit_idx
                else:
                    idx = 0
                if raw_idx is None:
                    last_implicit_idx = idx
                if mapped_id:
                    id_to_idx[mapped_id] = idx
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": [], "extra_content": None, "thought_signature": None})
                if mapped_id:
                    slot["id"] = mapped_id
                if fn.get("name"):
                    slot["name"] = tool_reverse.get(str(fn["name"]), str(fn["name"]))
                arg_delta = fn.get("arguments")
                if isinstance(arg_delta, str) and arg_delta:
                    slot["arguments"].append(arg_delta)
                if tc_d.get("extra_content") and (norm := _norm_sig(tc_d["extra_content"])):
                    slot["extra_content"], slot["thought_signature"] = norm, extract_thought_signature(norm)
                if fn.get("thought_signature") and not slot["thought_signature"]:
                    sig = str(fn["thought_signature"]).strip()
                    if sig:
                        slot["thought_signature"], slot["extra_content"] = sig, {"google": {"thought_signature": sig}}
                yield ToolCallDelta(
                    index=idx, id=slot["id"] or None, name=slot["name"] or None,
                    arguments_delta=arg_delta if isinstance(arg_delta, str) else "", extra_content=slot["extra_content"],
                )
        for idx in sorted(tool_acc):
            s = tool_acc[idx]
            if s["name"]:
                yield ToolCallComplete(
                    id=s["id"] or f"call_{idx}", name=s["name"], arguments="".join(s["arguments"]) or "{}",
                    index=idx, extra_content=s["extra_content"], thought_signature=s["thought_signature"],
                )
        if msg_extra is not None:
            yield MessageCarrierEvent(extra_content=msg_extra, finish_reason=finish_reason)
        if usage is not None:
            yield usage
        yield StreamDone(finish_reason=finish_reason, usage=usage)
