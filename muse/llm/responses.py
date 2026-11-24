"""Async streaming OpenAI Responses API transport (``/v1/responses``, ChatGPT Codex, SuperGrok xAI OAuth)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

import httpx

from muse.llm.oauth import (
    codex_cloudflare_headers,
    is_official_codex_base_url,
    maybe_zstd_compress,
    resolve_codex_runtime_credentials,
    resolve_xai_oauth_runtime_credentials,
)
from muse.llm.providers import is_openai_reasoning_model, normalize_responses_url, prenormalize_provider_params
from muse.llm.types import (
    EncryptedReasoningItem,
    ReasoningDelta,
    RetryPolicy,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallDelta,
    UsageInfo,
    classify_http_error,
    host_of,
    iter_sse_json,
    post_and_stream,
    stream_with_retry,
)

_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
_MAX_CALL_ID_LEN = 64
_XAI_RESERVED_TOOLS = ("tool_search", "web_search")
_OPENAI_RESERVED_TOOLS = ("tool_search",)


def _canonical_base_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower()
        port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
        return f"{(p.scheme or 'https').lower()}://{host}{port}{(p.path or '').rstrip('/')}" if host else raw.lower()
    except Exception:
        return raw.lower()


def classify_responses_issuer(*, provider: str = "", base_url: str = "") -> str:
    """Stable issuer identifier used to stamp ``encrypted_content`` blobs."""
    prov, host = str(provider or "").strip().lower(), host_of(base_url)
    if prov == "openai-codex" or is_official_codex_base_url(base_url):
        return "codex_backend"
    if prov in ("xai", "xai-oauth") or host == "api.x.ai" or host.endswith(".x.ai"):
        return "xai_responses"
    return f"other:{_canonical_base_url(base_url)}" if base_url else "other"


def canonical_responses_model(model: str) -> str:
    name = str(model or "").strip()
    return name[:-5] if name.endswith("-900k") else name


def _clamp_call_id(call_id: str) -> str:
    raw = str(call_id or "").strip()
    if len(raw) <= _MAX_CALL_ID_LEN:
        return raw or "call_0"
    return f"call_{hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:24]}"


def _convert_content_parts(content: Any, *, role: str) -> Any:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str) and item:
            parts.append({"type": text_type, "text": item})
        elif isinstance(item, dict):
            ptype = str(item.get("type") or "").strip().lower()
            if ptype in ("text", "input_text", "output_text") and isinstance(item.get("text"), str) and item["text"]:
                parts.append({"type": text_type, "text": item["text"]})
            elif ptype in ("image_url", "input_image"):
                if role == "assistant":
                    parts.append({"type": "output_text", "text": "[Assistant image omitted during replay]"})
                else:
                    val = item.get("image_url")
                    url = str(val.get("url") or "") if isinstance(val, dict) else str(val or "")
                    detail = val.get("detail", item.get("detail")) if isinstance(val, dict) else item.get("detail")
                    if url:
                        part: dict[str, Any] = {"type": "input_image", "image_url": url}
                        if isinstance(detail, str) and detail.strip():
                            part["detail"] = detail.strip()
                        parts.append(part)
    return parts


def _matching_encrypted_items(msg: dict[str, Any], *, issuer: str, model: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for item in msg.get("codex_reasoning_items") or []:
        if not isinstance(item, dict) or not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
            continue
        item_issuer = item.get("_issuer_kind")
        if isinstance(item_issuer, str) and item_issuer.startswith("other:"):
            item_issuer = f"other:{_canonical_base_url(item_issuer[6:])}"
        if item_issuer != issuer or item.get("_issuer_model") != model:
            continue
        wire: dict[str, Any] = {"type": item.get("type") or "reasoning", "encrypted_content": item["encrypted_content"]}
        if item.get("id") and not str(item["id"]).startswith("rs_tmp_"):
            wire["id"] = str(item["id"])
        if isinstance(item.get("summary"), list):
            wire["summary"] = list(item["summary"])
        matched.append(wire)
    return matched


def _alias_responses_tools(tools: list[dict[str, Any]], reserved: tuple[str, ...]) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    taken = {str(t.get("name") or "") for t in tools if isinstance(t, dict)}
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for tool in tools:
        name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
        if name not in reserved:
            out.append(tool)
            continue
        alias, suffix = f"muse_{name}", 2
        while alias in taken:
            alias = f"muse_{name}_{suffix}"
            suffix += 1
        taken.add(alias)
        forward[name], reverse[alias] = alias, name
        out.append({**tool, "name": alias})
    return out, forward, reverse


def _convert_tools(tools: Optional[list[dict[str, Any]]], *, issuer: str, base_url: str) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" and isinstance(tool.get("function"), dict) else (tool if tool.get("type") == "function" and tool.get("name") else None)
        if fn is not None:
            params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
            converted.append({
                "type": "function", "name": str(fn.get("name") or ""), "description": str(fn.get("description") or ""),
                "parameters": params, "strict": bool(fn.get("strict", False)),
            })
    if issuer == "xai_responses":
        return _alias_responses_tools(converted, _XAI_RESERVED_TOOLS)
    if issuer == "codex_backend" or host_of(base_url) in ("api.openai.com", "chatgpt.com"):
        return _alias_responses_tools(converted, _OPENAI_RESERVED_TOOLS)
    return converted, {}, {}


def _convert_messages(messages: list[dict[str, Any]], *, issuer: str, model: str, tool_forward: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    sys_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        if role in ("system", "developer"):
            c = msg.get("content")
            text = c.strip() if isinstance(c, str) else ("\n".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("text")).strip() if isinstance(c, list) else "")
            if text:
                sys_parts.append(text)
        elif role == "tool":
            raw_out = msg.get("content")
            out_str = json.dumps(raw_out, ensure_ascii=False) if isinstance(raw_out, (dict, list)) else ("" if raw_out is None else str(raw_out))
            items.append({"type": "function_call_output", "call_id": _clamp_call_id(str(msg.get("tool_call_id") or "")), "output": out_str})
        elif role == "assistant":
            items.extend(_matching_encrypted_items(msg, issuer=issuer, model=model))
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                items.append({"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": c}]})
            elif isinstance(c, list) and (parts := _convert_content_parts(c, role="assistant")):
                items.append({"type": "message", "role": "assistant", "status": "completed", "content": parts})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                raw_name = str(fn.get("name") or "")
                args = fn.get("arguments")
                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else ((args.strip() or "{}") if isinstance(args, str) else "{}")
                items.append({"type": "function_call", "call_id": _clamp_call_id(str(tc.get("id") or "")), "name": tool_forward.get(raw_name, raw_name), "arguments": args_str})
        else:
            conv = _convert_content_parts(msg.get("content", ""), role="user")
            if isinstance(conv, str):
                items.append({"role": "user", "content": [{"type": "input_text", "text": conv}]})
            elif conv:
                items.append({"role": "user", "content": conv})
    return "\n\n".join(sys_parts).strip() or _DEFAULT_INSTRUCTIONS, items


def _extract_usage(raw: Any) -> Optional[UsageInfo]:
    if not isinstance(raw, dict):
        return None
    ind = raw.get("input_tokens_details") if isinstance(raw.get("input_tokens_details"), dict) else {}
    outd = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), dict) else {}
    in_total = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
    cached = int(ind.get("cached_tokens") or 0)
    cache_write = int(ind.get("cache_write_tokens") or ind.get("cache_creation_tokens") or 0)
    return UsageInfo(
        input_tokens=max(0, in_total - cached - cache_write),
        output_tokens=int(raw.get("output_tokens") or raw.get("completion_tokens") or 0),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        reasoning_tokens=int(outd.get("reasoning_tokens") or 0),
    )


def _maybe_encrypted_event(
    item: dict[str, Any], *, issuer: str, model: str, emitted: set[str],
) -> Optional[EncryptedReasoningItem]:
    itype, enc = str(item.get("type") or ""), item.get("encrypted_content")
    item_id = str(item.get("id") or "") or None
    if not isinstance(enc, str) or not enc or (item_id and item_id.startswith("rs_tmp_")):
        return None
    key = f"{itype}:{item_id or ''}:{enc[:32]}"
    if key in emitted:
        return None
    emitted.add(key)
    summary = item.get("summary") if isinstance(item.get("summary"), list) else None
    return EncryptedReasoningItem(item_type=itype, encrypted_content=enc, issuer=issuer, model=model, item_id=item_id, summary=summary)


class OpenAIResponsesTransport:
    """Async streaming transport for OpenAI Responses / ChatGPT Codex / SuperGrok xAI OAuth."""

    api_mode: str = "codex_responses"

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
    ) -> tuple[dict[str, Any], dict[str, str], str, str]:
        eff_max, eff_temp, cleaned_extra = prenormalize_provider_params(
            extra_body=extra_body, max_tokens=max_tokens, temperature=temperature
        )
        issuer = classify_responses_issuer(provider=self.provider, base_url=self.base_url)
        wire_model = canonical_responses_model(model)
        resp_tools, tool_forward, tool_reverse = _convert_tools(tools, issuer=issuer, base_url=self.base_url)
        instructions, input_items = _convert_messages(messages, issuer=issuer, model=wire_model, tool_forward=tool_forward)

        payload: dict[str, Any] = {
            "model": wire_model, "instructions": instructions, "input": input_items,
            "store": False, "stream": True, "include": ["reasoning.encrypted_content"],
        }
        if resp_tools:
            payload["tools"] = resp_tools
        if eff_max is not None and eff_max > 0 and issuer != "codex_backend":
            payload["max_output_tokens"] = eff_max
        if eff_temp is not None and not is_openai_reasoning_model(wire_model) and issuer != "codex_backend":
            payload["temperature"] = eff_temp
        if isinstance(reasoning_config, dict):
            eff = str(reasoning_config.get("effort") or "").strip().lower()
            disabled = reasoning_config.get("enabled") is False or eff == "none"
            if not disabled and eff in {"low", "medium", "high", "xhigh", "max"}:
                payload["reasoning"] = {"effort": "high" if eff in {"xhigh", "max"} and issuer != "codex_backend" else eff, "summary": "auto"}
            elif disabled and issuer != "codex_backend":
                payload["reasoning"] = {"effort": "none"}
        for k, v in cleaned_extra.items():
            payload.setdefault(k, v)
        return payload, tool_reverse, issuer, wire_model

    async def _refresh_oauth_token(self) -> bool:
        prov = self.provider.lower()
        fn = resolve_codex_runtime_credentials if prov == "openai-codex" else (resolve_xai_oauth_runtime_credentials if prov == "xai-oauth" else None)
        if fn is None:
            return False
        new_key = str((await asyncio.to_thread(fn, force_refresh=True)).get("api_key") or "").strip()
        if new_key:
            self.api_key = new_key
            return True
        return False

    async def stream(
        self, *, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
        reasoning_config: Optional[dict[str, Any]] = None, extra_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None, timeout: Optional[float] = None,
        retry_policy: Optional[RetryPolicy] = None, **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        del kwargs
        payload, tool_reverse, issuer, wire_model = self.build_request_payload(
            model=model, messages=messages, tools=tools, max_tokens=max_tokens,
            temperature=temperature, reasoning_config=reasoning_config, extra_body=extra_body,
        )
        url = normalize_responses_url(self.base_url)
        req_timeout = httpx.Timeout(float(timeout) if timeout and timeout > 0 else 180.0, connect=15.0)
        is_oauth_prov = self.provider.lower() in ("openai-codex", "xai-oauth")

        def _attempt(_attempt_num: int) -> AsyncIterator[StreamEvent]:
            raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            body_bytes, enc = maybe_zstd_compress(raw_bytes, base_url=self.base_url, path=urlparse(url).path)
            headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "text/event-stream", **self.default_headers}
            if issuer == "codex_backend" or is_official_codex_base_url(self.base_url):
                headers.update(codex_cloudflare_headers(self.api_key, base_url=self.base_url))
            if extra_headers:
                headers.update(extra_headers)
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            if enc:
                headers["Content-Encoding"] = enc
            return post_and_stream(
                client=self._client, url=url, headers=headers, timeout=req_timeout,
                provider=self.provider, model=wire_model, raw_content=body_bytes,
                on_json=lambda obj: self._events_from_completed(obj, tool_reverse, issuer, wire_model, emitted_calls=set(), emitted_enc=set()),
                on_sse=lambda resp: self._parse_sse(resp, tool_reverse, issuer, wire_model),
            )

        async for event in stream_with_retry(
            _attempt, policy=retry_policy, provider=self.provider, model=wire_model,
            on_auth_refresh=self._refresh_oauth_token if is_oauth_prov else None,
        ):
            yield event

    async def _events_from_completed(
        self, resp_obj: dict[str, Any], tool_reverse: dict[str, str], issuer: str, wire_model: str,
        *, emitted_calls: set[str], emitted_enc: set[str], streamed_text: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        status = str(resp_obj.get("status") or "completed").lower()
        if status == "failed":
            err = resp_obj.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise classify_http_error(500, json.dumps({"error": {"message": msg or "Responses request failed"}}), provider=self.provider, model=wire_model)
        saw_calls = bool(emitted_calls)
        for idx, item in enumerate(resp_obj.get("output") or []):
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message" and not streamed_text:
                for p in item.get("content") or []:
                    if isinstance(p, dict) and p.get("type") in ("output_text", "text") and p.get("text"):
                        yield TextDelta(text=str(p["text"]))
            elif itype in ("reasoning", "compaction"):
                if ev := _maybe_encrypted_event(item, issuer=issuer, model=wire_model, emitted=emitted_enc):
                    yield ev
            elif itype == "function_call":
                cid = str(item.get("call_id") or item.get("id") or f"call_{idx}")
                saw_calls = True
                if cid not in emitted_calls:
                    emitted_calls.add(cid)
                    raw_n = str(item.get("name") or "")
                    args = item.get("arguments")
                    yield ToolCallComplete(id=cid, name=tool_reverse.get(raw_n, raw_n), arguments=args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False), index=idx)
        usage = _extract_usage(resp_obj.get("usage"))
        if usage is not None:
            yield usage
        reason = str((resp_obj.get("incomplete_details") or {}).get("reason") or "").lower()
        finish = "tool_calls" if saw_calls else (("content_filter" if reason == "content_filter" else "length") if status == "incomplete" else "stop")
        yield StreamDone(finish_reason=finish, usage=usage)

    async def _parse_sse(
        self, resp: httpx.Response, tool_reverse: dict[str, str], issuer: str, wire_model: str,
    ) -> AsyncIterator[StreamEvent]:
        active: dict[str, dict[str, Any]] = {}
        emitted_calls: set[str] = set()
        emitted_enc: set[str] = set()
        streamed_text, finished = False, False
        async for ev_name, data in iter_sse_json(resp):
            etype = str(data.get("type") or ev_name or "")
            if etype == "response.output_text.delta" and isinstance(data.get("delta"), str) and data["delta"]:
                streamed_text = True
                yield TextDelta(text=data["delta"])
            elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta") and isinstance(data.get("delta"), str) and data["delta"]:
                yield ReasoningDelta(text=data["delta"])
            elif etype == "response.output_item.added" and isinstance(item := data.get("item"), dict) and item.get("type") == "function_call":
                iid = str(item.get("id") or item.get("call_id") or f"fc_{len(active)}")
                cid, raw_n, idx = str(item.get("call_id") or iid), str(item.get("name") or ""), int(data.get("output_index", len(active)))
                name = tool_reverse.get(raw_n, raw_n)
                active[iid] = {"call_id": cid, "name": name, "index": idx, "args": []}
                yield ToolCallDelta(index=idx, id=cid, name=name, arguments_delta="")
            elif etype == "response.function_call_arguments.delta":
                slot = active.get(str(data.get("item_id") or ""))
                delta = str(data.get("delta") or "")
                if slot is not None:
                    slot["args"].append(delta)
                    yield ToolCallDelta(index=slot["index"], id=slot["call_id"], name=slot["name"], arguments_delta=delta)
            elif etype == "response.output_item.done" and isinstance(item := data.get("item"), dict):
                itype = item.get("type")
                if itype == "function_call":
                    iid = str(item.get("id") or item.get("call_id") or "")
                    slot = active.get(iid)
                    cid = str(item.get("call_id") or (slot["call_id"] if slot else iid) or "call_0")
                    if cid not in emitted_calls:
                        emitted_calls.add(cid)
                        raw_n = str(item.get("name") or (slot["name"] if slot else ""))
                        args = item.get("arguments")
                        args_str = args if isinstance(args, str) else ("".join(slot["args"]) if slot else "{}")
                        yield ToolCallComplete(id=cid, name=tool_reverse.get(raw_n, raw_n), arguments=args_str or "{}", index=int(data.get("output_index", slot["index"] if slot else 0)))
                elif itype in ("reasoning", "compaction"):
                    if ev := _maybe_encrypted_event(item, issuer=issuer, model=wire_model, emitted=emitted_enc):
                        yield ev
            elif etype in ("response.completed", "response.incomplete"):
                finished = True
                obj = data.get("response") if isinstance(data.get("response"), dict) else data
                async for ev in self._events_from_completed(obj, tool_reverse, issuer, wire_model, emitted_calls=emitted_calls, emitted_enc=emitted_enc, streamed_text=streamed_text):
                    yield ev
            elif etype in ("response.failed", "error"):
                err = data.get("error") or (data.get("response") or {}).get("error") or data
                raise classify_http_error(500, json.dumps({"error": err if isinstance(err, dict) else {"message": str(err)}}), provider=self.provider, model=wire_model)
        if not finished:
            yield StreamDone(finish_reason="tool_calls" if emitted_calls else "stop")
