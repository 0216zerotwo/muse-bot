"""Async streaming Anthropic Messages API transport (``/v1/messages`` and compatible endpoints)."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import logging
import re
from contextlib import suppress
from typing import Any, AsyncIterator, Optional

import httpx

from muse.llm.providers import normalize_anthropic_messages_url, prenormalize_provider_params
from muse.llm.types import (
    ReasoningDelta,
    RetryPolicy,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingBlockComplete,
    ToolCallComplete,
    ToolCallDelta,
    UsageInfo,
    classify_http_error,
    host_of,
    iter_sse_json,
    post_and_stream,
    stream_with_retry,
)

logger = logging.getLogger(__name__)

ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MAX_IMAGE_BYTES = 5 * 1024 * 1024
ANTHROPIC_TARGET_IMAGE_BYTES = int(4.5 * 1024 * 1024)
ANTHROPIC_MAX_IMAGE_DIM = 8000
_NATIVE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
_CACHEABLE_TYPES = frozenset({"text", "tool_use", "tool_result", "image"})
_EMPTY_PLACEHOLDER = "(empty)"
_STOP_REASON_MAP = {
    "end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length",
    "stop_sequence": "stop", "refusal": "content_filter", "model_context_window_exceeded": "length",
}
_THINKING_BUDGETS = {"low": 4000, "medium": 8000, "high": 16000, "xhigh": 32000, "max": 32000}
_LEGACY_MANUAL_CLAUDE = (
    "claude-3", "claude-opus-4-0", "claude-opus-4.0", "claude-opus-4-1", "claude-opus-4.1",
    "claude-sonnet-4-0", "claude-sonnet-4.0", "claude-opus-4-2025", "claude-sonnet-4-2025",
    "claude-opus-4-5", "claude-opus-4.5", "claude-sonnet-4-5", "claude-sonnet-4.5", "claude-haiku-4-5", "claude-haiku-4.5",
)
_NO_XHIGH_CLAUDE = ("claude-opus-4-6", "claude-opus-4.6", "claude-sonnet-4-6", "claude-sonnet-4.6")
_FTYP_BRANDS = {
    **dict.fromkeys((b"avif", b"avis"), "image/avif"),
    **dict.fromkeys((b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"), "image/heic"),
}
_MAGIC_SIGNATURES: tuple[tuple[tuple[tuple[int, bytes], ...], str], ...] = (
    (((0, b"\x89PNG\r\n\x1a\n"),), "image/png"), (((0, b"\xff\xd8\xff"),), "image/jpeg"),
    (((0, b"GIF87a"),), "image/gif"), (((0, b"GIF89a"),), "image/gif"),
    (((0, b"RIFF"), (8, b"WEBP")), "image/webp"), (((0, b"BM"),), "image/bmp"),
    *((((4, b"ftyp"), (8, brand)), mime) for brand, mime in _FTYP_BRANDS.items()),
    (((0, b"II*\x00"),), "image/tiff"), (((0, b"MM\x00*"),), "image/tiff"), (((0, b"\x00\x00\x01\x00"),), "image/x-icon"),
)


def _is_minimax_url(base_url: str) -> bool:
    return host_of(base_url).endswith(("minimax.io", "minimaxi.com"))


def _compat_kind(base_url: str, model: str) -> Optional[str]:
    host, m = host_of(base_url), (model or "").lower()
    if host.endswith(("moonshot.cn", "moonshot.ai", "kimi.com")) or "kimi" in m or "moonshot" in m:
        return "kimi"
    return "deepseek" if (host.endswith("deepseek.com") or "deepseek" in m) else None


def _is_claude(model: str) -> bool:
    return "claude" in (model or "").lower()


def _supports_adaptive(model: str) -> bool:
    m = (model or "").lower()
    return "kimi" in m or "moonshot" in m or (_is_claude(m) and not any(t in m for t in _LEGACY_MANUAL_CLAUDE))


def _forbids_sampling(model: str) -> bool:
    m = (model or "").lower()
    return _is_claude(m) and not any(t in m for t in (_NO_XHIGH_CLAUDE + _LEGACY_MANUAL_CLAUDE))


# ── Image sniff / resize / transcode (run inside threadpool) ─────────────────


def sniff_image_mime(raw: bytes) -> Optional[str]:
    if not raw:
        return None
    for checks, mime in _MAGIC_SIGNATURES:
        if all(raw[off : off + len(sig)] == sig for off, sig in checks):
            return mime
    return None


def prepare_anthropic_image_sync(
    raw_bytes: bytes, declared_mime: str = "image/jpeg", *,
    max_bytes: int = ANTHROPIC_MAX_IMAGE_BYTES, target_bytes: int = ANTHROPIC_TARGET_IMAGE_BYTES,
    max_dim: int = ANTHROPIC_MAX_IMAGE_DIM,
) -> tuple[bytes, str]:
    """Ensure image is PNG/JPEG/GIF/WEBP, <=8000px per side, <=5MB (call via ``asyncio.to_thread``)."""
    mime = sniff_image_mime(raw_bytes) or (declared_mime or "image/jpeg").lower().strip()
    mime = "image/jpeg" if mime == "image/jpg" else mime
    try:
        from PIL import Image
    except ImportError:
        return raw_bytes, mime
    with suppress(Exception):
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            w, h = img.size
            if mime in _NATIVE_IMAGE_MIMES and len(raw_bytes) <= max_bytes and w <= max_dim and h <= max_dim:
                return raw_bytes, mime
            work = img.copy()
    except Exception as exc:
        logger.debug("anthropic image prep: Pillow could not open image (%s)", exc)
        return raw_bytes, mime

    w, h = work.size
    if w > max_dim or h > max_dim:
        scale = min(max_dim / w, max_dim / h)
        work = work.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

    def _save_jpeg(im: Any, q: int) -> bytes:
        buf = io.BytesIO()
        (im.convert("RGB") if im.mode != "RGB" else im).save(buf, format="JPEG", quality=q, optimize=True)
        return buf.getvalue()

    if mime in ("image/png", "image/gif", "image/webp") or work.mode in ("RGBA", "LA", "PA"):
        buf = io.BytesIO()
        (work if work.mode in ("RGB", "RGBA", "L", "LA", "P") else work.convert("RGBA")).save(buf, format="PNG", optimize=True)
        if len(buf.getvalue()) <= max_bytes:
            return buf.getvalue(), "image/png"

    cap = min(max_bytes, target_bytes)
    for q in (90, 82, 74, 65, 55):
        if len(jpg := _save_jpeg(work, q)) <= cap:
            return jpg, "image/jpeg"
    cur = work
    for _ in range(5):
        cur = cur.resize((max(1, int(cur.size[0] * 0.75)), max(1, int(cur.size[1] * 0.75))), Image.Resampling.LANCZOS)
        if len(jpg := _save_jpeg(cur, 70)) <= max_bytes:
            return jpg, "image/jpeg"
    return _save_jpeg(cur, 55), "image/jpeg"


async def _prepare_image_block_async(block: dict[str, Any]) -> dict[str, Any]:
    src = block.get("source") if block.get("type") == "image" else None
    if not isinstance(src, dict) or src.get("type") != "base64" or not isinstance(src.get("data"), str) or not src["data"]:
        return block
    try:
        raw = base64.b64decode(src["data"])
    except Exception:
        return block
    declared = str(src.get("media_type") or "image/jpeg")
    out_bytes, final_mime = await asyncio.to_thread(prepare_anthropic_image_sync, raw, declared)
    if out_bytes is raw and final_mime == declared:
        return block
    return {**block, "source": {"type": "base64", "media_type": final_mime, "data": base64.b64encode(out_bytes).decode("ascii")}}


async def _prepare_messages_images_async(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        blocks: list[dict[str, Any]] = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "image":
                blocks.append(await _prepare_image_block_async(blk))
            elif blk.get("type") == "tool_result" and isinstance(blk.get("content"), list):
                inner = [await _prepare_image_block_async(s) if isinstance(s, dict) and s.get("type") == "image" else s for s in blk["content"]]
                blocks.append({**blk, "content": inner})
            else:
                blocks.append(blk)
        out.append({**msg, "content": blocks})
    return out


# ── Tool & message conversion + 3-tier prompt caching ────────────────────────


def _sanitize_tool_id(tool_id: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(tool_id or "")) or "tool_0"


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def _strip_nullable(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip_nullable(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strip_nullable(v) for k, v in node.items() if k != "nullable"}
    if isinstance(out.get("anyOf"), list):
        non_null = [b for b in out["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) < len(out["anyOf"]):
            out = {**{k: v for k, v in out.items() if k != "anyOf"}, **non_null[0]}
    return out


def convert_tools_to_anthropic(tools: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result, seen = [], set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        schema = _strip_nullable(copy.deepcopy(fn.get("parameters") or fn.get("input_schema") or {}))
        schema = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        for banned in ("oneOf", "allOf", "anyOf"):
            schema.pop(banned, None)
        schema.setdefault("type", "object")
        if schema.get("type") == "object" and not isinstance(schema.get("properties"), dict):
            schema["properties"] = {}
        entry: dict[str, Any] = {"name": name, "description": str(fn.get("description") or ""), "input_schema": schema}
        if isinstance(tool.get("cache_control"), dict):
            entry["cache_control"] = dict(tool["cache_control"])
        result.append(entry)
    return result


def _image_block_from_url(url: str) -> dict[str, Any]:
    raw = str(url or "").strip()
    if raw.startswith("data:"):
        hdr, _, data = raw.partition(",")
        mime = hdr[5:].split(";", 1)[0].strip().lower()
        mime = "image/jpeg" if mime == "image/jpg" or not mime.startswith("image/") else mime
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": raw}}


def _convert_part(part: Any) -> Optional[dict[str, Any]]:
    if part is None:
        return None
    if isinstance(part, str):
        return {"type": "text", "text": part} if part.strip() else None
    if not isinstance(part, dict):
        return {"type": "text", "text": str(part)}
    ptype = part.get("type")
    if ptype in ("text", "input_text"):
        txt = str(part.get("text") or "")
        return {"type": "text", "text": txt} if txt.strip() else None
    if ptype in ("image_url", "input_image"):
        val = part.get("image_url")
        url = val.get("url", "") if isinstance(val, dict) else str(val or "")
        return _image_block_from_url(url) if url else None
    if ptype == "image" and isinstance(part.get("source"), dict):
        return {"type": "image", "source": copy.deepcopy(part["source"])}
    if ptype == "thinking":
        blk: dict[str, Any] = {"type": "thinking", "thinking": str(part.get("thinking") or "")}
        if part.get("signature"):
            blk["signature"] = str(part["signature"])
        return blk
    if ptype == "redacted_thinking" and part.get("data"):
        return {"type": "redacted_thinking", "data": str(part["data"])}
    if ptype == "tool_use":
        return {"type": "tool_use", "id": _sanitize_tool_id(part.get("id")), "name": str(part.get("name") or ""), "input": _parse_tool_args(part.get("input"))}
    return None


def _convert_assistant(msg: dict[str, Any], *, compat: Optional[str]) -> dict[str, Any]:
    if isinstance(ordered := msg.get("anthropic_content_blocks"), list) and ordered:
        if blocks := [b for b in (_convert_part(x) for x in ordered) if b is not None]:
            return {"role": "assistant", "content": blocks}
    blocks: list[dict[str, Any]] = [
        b for item in (msg.get("reasoning_details") or [])
        if isinstance(item, dict) and item.get("type") in _THINKING_TYPES and (b := _convert_part(item)) is not None
    ]
    if not blocks and isinstance(msg.get("reasoning_content"), str) and compat in ("kimi", "deepseek"):
        blocks.append({"type": "thinking", "thinking": msg["reasoning_content"]})
    c = msg.get("content")
    if isinstance(c, list):
        blocks.extend(b for p in c if (b := _convert_part(p)) is not None)
    elif isinstance(c, str) and c.strip():
        blocks.append({"type": "text", "text": c})
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            blocks.append({"type": "tool_use", "id": _sanitize_tool_id(tc.get("id")), "name": str(fn.get("name") or ""), "input": _parse_tool_args(fn.get("arguments"))})
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": _EMPTY_PLACEHOLDER}]}


def _convert_tool_result(msg: dict[str, Any]) -> dict[str, Any]:
    raw = msg.get("content")
    if isinstance(raw, list):
        res: Any = [b for p in raw if (b := _convert_part(p)) and b.get("type") in ("text", "image")] or "(no output)"
    elif isinstance(raw, str):
        res = raw if raw.strip() else "(no output)"
    else:
        res = json.dumps(raw, ensure_ascii=False) if raw is not None else "(no output)"
    return {"type": "tool_result", "tool_use_id": _sanitize_tool_id(msg.get("tool_call_id")), "content": res}


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    txt = str(content or "")
    return [{"type": "text", "text": txt if txt.strip() else _EMPTY_PLACEHOLDER}]


def convert_messages_to_anthropic(
    messages: list[dict[str, Any]], *, base_url: str = "", model: str = "",
) -> tuple[Optional[list[dict[str, Any]]], list[dict[str, Any]]]:
    compat = _compat_kind(base_url, model)
    sys_blocks: list[dict[str, Any]] = []
    conv: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        if role in ("system", "developer"):
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                sys_blocks.append({"type": "text", "text": c})
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and p.get("type") == "text" and str(p.get("text") or "").strip():
                        blk = {"type": "text", "text": str(p["text"])}
                        if isinstance(p.get("cache_control"), dict):
                            blk["cache_control"] = dict(p["cache_control"])
                        sys_blocks.append(blk)
        elif role == "assistant":
            conv.append(_convert_assistant(msg, compat=compat))
        elif role == "tool":
            tr = _convert_tool_result(msg)
            if conv and conv[-1]["role"] == "user" and conv[-1]["content"] and conv[-1]["content"][0].get("type") == "tool_result":
                conv[-1]["content"].append(tr)
            else:
                conv.append({"role": "user", "content": [tr]})
        else:
            raw = msg.get("content", "")
            parts = [b for p in raw if (b := _convert_part(p)) is not None] if isinstance(raw, list) else ([{"type": "text", "text": str(raw)}] if str(raw or "").strip() else [])
            conv.append({"role": "user", "content": parts or [{"type": "text", "text": "(empty message)"}]})

    # Prune orphaned tool_use / tool_result pairs before merging.
    for i, m in enumerate(conv):
        if m["role"] == "assistant" and (use_ids := {b["id"] for b in m["content"] if b.get("type") == "tool_use"}):
            next_ids = {b["tool_use_id"] for b in conv[i + 1]["content"] if b.get("type") == "tool_result"} if (i + 1 < len(conv) and conv[i + 1]["role"] == "user") else set()
            if orphaned := (use_ids - next_ids):
                m["content"] = [b for b in m["content"] if not (b.get("type") == "tool_use" and b.get("id") in orphaned)] or [{"type": "text", "text": "(tool call removed)"}]
    surviving = {b["id"] for m in conv if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"}
    for m in conv:
        if m["role"] == "user":
            m["content"] = [b for b in m["content"] if not (b.get("type") == "tool_result" and b.get("tool_use_id") not in surviving)] or [{"type": "text", "text": "(tool result removed)"}]

    # Strict alternating user/assistant merge + leading user guarantee.
    merged: list[dict[str, Any]] = []
    for m in conv:
        if merged and merged[-1]["role"] == m["role"]:
            rhs = [b for b in _as_blocks(m["content"]) if not (m["role"] == "assistant" and b.get("type") in _THINKING_TYPES)]
            merged[-1]["content"] = _as_blocks(merged[-1]["content"]) + rhs
        else:
            merged.append({"role": m["role"], "content": _as_blocks(m["content"])})
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": _EMPTY_PLACEHOLDER}]})

    # Enforce endpoint thinking-signature policy.
    for m in merged:
        if m["role"] != "assistant":
            continue
        cleaned: list[dict[str, Any]] = []
        for b in m["content"]:
            btype = b.get("type")
            if btype not in _THINKING_TYPES:
                cleaned.append(b)
                continue
            b.pop("cache_control", None)
            if compat == "kimi":
                cleaned.append(b)
            elif compat == "deepseek":
                if not (b.get("signature") or b.get("data")):
                    cleaned.append(b)
            elif (btype == "redacted_thinking" and b.get("data")) or (btype == "thinking" and b.get("signature")):
                cleaned.append(b)
            elif btype == "thinking" and b.get("thinking"):
                cleaned.append({"type": "text", "text": b["thinking"]})
        m["content"] = cleaned or [{"type": "text", "text": _EMPTY_PLACEHOLDER}]
    return (sys_blocks or None), merged


def apply_three_tier_cache_control(
    *, system_blocks: Optional[list[dict[str, Any]]], tools: Optional[list[dict[str, Any]]], messages: list[dict[str, Any]],
) -> None:
    """Apply 3-tier ``cache_control: {"type": "ephemeral"}`` (system, tools, last 2 turns; <=4 total)."""
    marker, used = {"type": "ephemeral"}, 0
    if system_blocks:
        system_blocks[-1]["cache_control"] = dict(marker)
        used += 1
    if tools and used < 4:
        tools[-1]["cache_control"] = dict(marker)
        used += 1
    rem = min(2, 4 - used)
    if rem <= 0 or not messages or (not tools and len(messages) <= 1):
        return
    marked = 0
    for msg in reversed(messages):
        if marked >= rem:
            break
        for blk in reversed(msg.get("content") if isinstance(msg.get("content"), list) else []):
            if isinstance(blk, dict) and blk.get("type") in _CACHEABLE_TYPES:
                blk["cache_control"] = dict(marker)
                marked += 1
                break


def _thinking_fields(model: str, reasoning_config: Optional[dict[str, Any]], max_tokens: int) -> tuple[dict[str, Any], Optional[float], int]:
    if not isinstance(reasoning_config, dict):
        return {}, None, max_tokens
    eff = str(reasoning_config.get("effort") or "medium").strip().lower()
    if reasoning_config.get("enabled") is False or eff == "none":
        return ({"thinking": {"type": "disabled"}}, None, max_tokens) if (_is_claude(model) and _supports_adaptive(model)) else ({}, None, max_tokens)
    if _supports_adaptive(model):
        aeff = "max" if eff == "ultra" else (eff if eff in ("low", "medium", "high", "xhigh", "max") else "medium")
        if aeff == "xhigh" and any(t in model.lower() for t in _NO_XHIGH_CLAUDE):
            aeff = "max"
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": aeff}}, None, max_tokens
    budget = _THINKING_BUDGETS.get(eff, 8000)
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}, 1.0, max(max_tokens, budget + 4096)


class AnthropicMessagesTransport:
    """Async streaming transport for Anthropic ``/v1/messages`` and compatible endpoints."""

    api_mode: str = "anthropic_messages"

    def __init__(
        self, *, base_url: str = "https://api.anthropic.com", api_key: str = "", provider: str = "anthropic",
        default_headers: Optional[dict[str, str]] = None, client: Optional[httpx.AsyncClient] = None,
        enable_prompt_caching: bool = True,
    ) -> None:
        self.base_url = str(base_url or "https://api.anthropic.com").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.provider = str(provider or "anthropic").strip()
        self.default_headers = dict(default_headers or {})
        self._client = client
        self.enable_prompt_caching = enable_prompt_caching

    def build_headers(self, extra_headers: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "anthropic-version": ANTHROPIC_API_VERSION, **self.default_headers}
        is_oauth = self.api_key.startswith("sk-ant-oat")
        if is_oauth or _is_minimax_url(self.base_url):
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            if is_oauth:
                headers.setdefault("anthropic-beta", "interleaved-thinking-2025-05-14,claude-code-20250219,oauth-2025-04-20")
        elif self.api_key:
            headers["x-api-key"] = self.api_key
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def build_request_payload(
        self, *, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
        reasoning_config: Optional[dict[str, Any]] = None, extra_body: Optional[dict[str, Any]] = None,
        enable_prompt_caching: Optional[bool] = None,
    ) -> dict[str, Any]:
        eff_max, eff_temp, cleaned_extra = prenormalize_provider_params(
            extra_body=extra_body, max_tokens=max_tokens, temperature=temperature
        )
        wire_model = model[10:] if model.lower().startswith("anthropic/") else model
        sys_blocks, ant_msgs = convert_messages_to_anthropic(messages, base_url=self.base_url, model=wire_model)
        ant_msgs = await _prepare_messages_images_async(ant_msgs)
        ant_tools = convert_tools_to_anthropic(tools)
        if self.enable_prompt_caching if enable_prompt_caching is None else enable_prompt_caching:
            apply_three_tier_cache_control(system_blocks=sys_blocks, tools=ant_tools, messages=ant_msgs)
        tf, forced_temp, resolved_max = _thinking_fields(wire_model, reasoning_config, eff_max if (eff_max and eff_max > 0) else 8192)

        payload: dict[str, Any] = {"model": wire_model, "messages": ant_msgs, "max_tokens": resolved_max, "stream": True}
        if sys_blocks:
            payload["system"] = sys_blocks
        if ant_tools:
            payload["tools"] = ant_tools
        if tf:
            payload.update(tf)
        if forced_temp is not None:
            payload["temperature"] = forced_temp
        elif eff_temp is not None and not _forbids_sampling(wire_model):
            if not (isinstance(tf.get("thinking"), dict) and tf["thinking"].get("type") == "enabled"):
                payload["temperature"] = eff_temp
        for k, v in cleaned_extra.items():
            payload.setdefault(k, v)
        return payload

    async def stream(
        self, *, model: str, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None, temperature: Optional[float] = None,
        reasoning_config: Optional[dict[str, Any]] = None, extra_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None, timeout: Optional[float] = None,
        retry_policy: Optional[RetryPolicy] = None, **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        payload = await self.build_request_payload(
            model=model, messages=messages, tools=tools, max_tokens=max_tokens, temperature=temperature,
            reasoning_config=reasoning_config, extra_body=extra_body, enable_prompt_caching=kwargs.get("enable_prompt_caching"),
        )
        url = normalize_anthropic_messages_url(self.base_url)
        headers = self.build_headers(extra_headers)
        req_timeout = httpx.Timeout(float(timeout) if timeout and timeout > 0 else 180.0, connect=15.0)

        def _attempt(_attempt_num: int) -> AsyncIterator[StreamEvent]:
            return post_and_stream(
                client=self._client, url=url, headers=headers, timeout=req_timeout,
                provider=self.provider, model=model, json_body=payload,
                on_json=self._events_from_json, on_sse=self._parse_sse,
            )

        async for event in stream_with_retry(_attempt, policy=retry_policy, provider=self.provider, model=model):
            yield event

    async def _events_from_json(self, data: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        for idx, blk in enumerate(data.get("content") or []):
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text" and blk.get("text"):
                yield TextDelta(text=str(blk["text"]))
            elif btype == "thinking":
                text, sig = str(blk.get("thinking") or ""), blk.get("signature")
                if text:
                    yield ReasoningDelta(text=text, signature=sig)
                yield ThinkingBlockComplete(block_type="thinking", thinking=text, signature=sig)
            elif btype == "redacted_thinking":
                yield ThinkingBlockComplete(block_type="redacted_thinking", data=str(blk.get("data") or ""))
            elif btype == "tool_use":
                yield ToolCallComplete(id=str(blk.get("id") or f"tool_{idx}"), name=str(blk.get("name") or ""), arguments=json.dumps(blk.get("input") or {}, ensure_ascii=False), index=idx)
        u = data.get("usage") or {}
        usage = UsageInfo(
            input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0), cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )
        yield usage
        yield StreamDone(finish_reason=_STOP_REASON_MAP.get(str(data.get("stop_reason") or ""), "stop"), usage=usage)

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[StreamEvent]:
        blocks: dict[int, dict[str, Any]] = {}
        usage, finish_reason, done = UsageInfo(), "stop", False
        async for ev_name, data in iter_sse_json(resp):
            etype = str(data.get("type") or ev_name or "")
            if etype == "message_start":
                u = (data.get("message") or {}).get("usage") or {}
                usage.input_tokens = int(u.get("input_tokens") or 0)
                usage.cache_read_tokens = int(u.get("cache_read_input_tokens") or 0)
                usage.cache_write_tokens = int(u.get("cache_creation_input_tokens") or 0)
            elif etype == "content_block_start":
                idx, blk = int(data.get("index", len(blocks))), data.get("content_block") or {}
                btype = str(blk.get("type") or "")
                slot: dict[str, Any] = {"type": btype, "id": str(blk.get("id") or ""), "name": str(blk.get("name") or ""), "thinking": [], "signature": [], "data": str(blk.get("data") or ""), "input_json": []}
                if btype == "text" and blk.get("text"):
                    yield TextDelta(text=str(blk["text"]))
                elif btype == "thinking" and blk.get("thinking"):
                    slot["thinking"].append(str(blk["thinking"]))
                    yield ReasoningDelta(text=str(blk["thinking"]))
                elif btype == "tool_use":
                    yield ToolCallDelta(index=idx, id=slot["id"], name=slot["name"], arguments_delta="")
                blocks[idx] = slot
            elif etype == "content_block_delta":
                idx, delta = int(data.get("index", 0)), data.get("delta") or {}
                dtype, slot = str(delta.get("type") or ""), blocks.get(idx)
                if dtype == "text_delta" and (txt := str(delta.get("text") or "")):
                    yield TextDelta(text=txt)
                elif dtype == "thinking_delta" and (txt := str(delta.get("thinking") or "")):
                    if slot is not None:
                        slot["thinking"].append(txt)
                    yield ReasoningDelta(text=txt)
                elif dtype == "signature_delta" and (sig := str(delta.get("signature") or "")) and slot is not None:
                    slot["signature"].append(sig)
                elif dtype == "input_json_delta" and (part := str(delta.get("partial_json") or "")) and slot is not None:
                    slot["input_json"].append(part)
                    yield ToolCallDelta(index=idx, id=slot["id"], name=slot["name"], arguments_delta=part)
            elif etype == "content_block_stop" and (slot := blocks.pop(int(data.get("index", 0)), None)) is not None:
                btype = slot["type"]
                if btype == "thinking":
                    yield ThinkingBlockComplete(block_type="thinking", thinking="".join(slot["thinking"]), signature="".join(slot["signature"]) or None)
                elif btype == "redacted_thinking":
                    yield ThinkingBlockComplete(block_type="redacted_thinking", data=slot["data"])
                elif btype == "tool_use":
                    yield ToolCallComplete(id=slot["id"] or f"tool_{data.get('index', 0)}", name=slot["name"], arguments="".join(slot["input_json"]).strip() or "{}", index=int(data.get("index", 0)))
            elif etype == "message_delta":
                if sr := (data.get("delta") or {}).get("stop_reason"):
                    finish_reason = _STOP_REASON_MAP.get(str(sr), str(sr))
                if (out_t := (data.get("usage") or {}).get("output_tokens")) is not None:
                    usage.output_tokens = int(out_t)
            elif etype == "message_stop":
                done = True
                yield usage
                yield StreamDone(finish_reason=finish_reason, usage=usage)
            elif etype == "error":
                err = data.get("error") or {}
                et = str(err.get("type") or "")
                st = 429 if "rate_limit" in et else (529 if "overloaded" in et else 500)
                raise classify_http_error(st, json.dumps({"error": err}), provider=self.provider, model=str(data.get("model") or ""))
        if not done:
            yield usage
            yield StreamDone(finish_reason=finish_reason, usage=usage)
