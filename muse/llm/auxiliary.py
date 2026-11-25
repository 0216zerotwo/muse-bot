"""Dual sync/async auxiliary LLM client (``call_llm`` & ``async_call_llm``), shared HTTP pool,
usage logging, and ``describe_images_if_blind`` vision fallback helper."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import os
import threading
from typing import Any, Optional, Union

import httpx

from muse.llm.anthropic_messages import AnthropicMessagesTransport
from muse.llm.chat_completions import OpenAIChatTransport
from muse.llm.providers import _read_config, detect_api_mode, resolve_runtime_provider
from muse.llm.responses import OpenAIResponsesTransport
from muse.llm.types import LLMResponse, RetryPolicy, UsageInfo, collect_stream

logger = logging.getLogger(__name__)
Transport = Union[OpenAIChatTransport, OpenAIResponsesTransport, AnthropicMessagesTransport]

from muse.runtime import get_runtime_loop, register_runtime_loop

_LOOP_LOCK = threading.Lock()
_loop_clients: dict[int, httpx.AsyncClient] = {}


def get_pooled_async_client(loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[httpx.AsyncClient]:
    running = loop or asyncio.get_running_loop()
    if get_runtime_loop() is not running:
        return None
    lid = id(running)
    with _LOOP_LOCK:
        client = _loop_clients.get(lid)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0),
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=20),
            )
            _loop_clients[lid] = client
        return client


async def close_shared_clients() -> None:
    with _LOOP_LOCK:
        clients = list(_loop_clients.values())
        _loop_clients.clear()
    for client in clients:
        if not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass


def create_transport(
    *, api_mode: str, base_url: str, api_key: str, provider: str = "",
    default_headers: Optional[dict[str, str]] = None, client: Optional[httpx.AsyncClient] = None,
) -> Transport:
    mode = detect_api_mode(provider=provider, base_url=base_url, api_mode=api_mode)
    kw = dict(base_url=base_url, api_key=api_key, provider=provider, default_headers=default_headers, client=client)
    if mode == "anthropic_messages":
        return AnthropicMessagesTransport(**{**kw, "provider": provider or "anthropic"})
    if mode == "codex_responses":
        return OpenAIResponsesTransport(**kw)
    return OpenAIChatTransport(**kw)


def _record_usage_best_effort(
    *, task: str, model: str, provider: str, base_url: str, usage: Optional[UsageInfo],
    session_id: str = "", billing_mode: str = "",
) -> None:
    """Log auxiliary usage into ``muse.db``."""
    if usage is None:
        return
    if not (usage.input_tokens or usage.output_tokens or usage.cache_read_tokens or usage.cache_write_tokens):
        return
    try:
        from muse.runtime import use_shared_db
    except ImportError:
        return
    try:
        with use_shared_db() as db:
            db.record_usage(
                session_id or f"system:{task or 'aux'}",
                model,
                provider=provider,
                base_url=base_url,
                billing_mode=billing_mode,
                task=task or "aux",
                source="companion",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            )
    except Exception as exc:
        logger.debug("muse.llm.auxiliary: usage logging skipped (%s)", type(exc).__name__)


def _resolve_aux_target(
    task: Optional[str], *, provider: Optional[str], model: Optional[str], base_url: Optional[str],
    api_key: Optional[str], api_mode: Optional[str], config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else _read_config()
    chosen_p, chosen_m = provider, model
    if task and not chosen_p:
        aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
        slot = aux.get(task) if isinstance(aux.get(task), dict) else {}
        sp, sm = str(slot.get("provider") or "").strip(), str(slot.get("model") or "").strip()
        if sp and sp.lower() != "auto":
            chosen_p = sp
            if sm and not chosen_m:
                chosen_m = sm
    return resolve_runtime_provider(
        requested=chosen_p, target_model=chosen_m, config=cfg, base_url=base_url, api_key=api_key, api_mode=api_mode,
    )


async def async_call_llm(
    task: Optional[str] = None, *, messages: list[dict[str, Any]], provider: Optional[str] = None,
    model: Optional[str] = None, base_url: Optional[str] = None, api_key: Optional[str] = None,
    api_mode: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None,
    tools: Optional[list[dict[str, Any]]] = None, timeout: Optional[float] = None,
    extra_body: Optional[dict[str, Any]] = None, reasoning_config: Optional[dict[str, Any]] = None,
    extra_headers: Optional[dict[str, str]] = None, retry_policy: Optional[RetryPolicy] = None,
    client: Optional[httpx.AsyncClient] = None, session_id: str = "",
    config: Optional[dict[str, Any]] = None, **kwargs: Any,
) -> LLMResponse:
    """Async auxiliary LLM call returning ``LLMResponse`` (``.choices[0].message.content``)."""
    del kwargs
    runtime = await asyncio.to_thread(
        _resolve_aux_target, task, provider=provider, model=model,
        base_url=base_url, api_key=api_key, api_mode=api_mode, config=config,
    )
    resolved_model = str(model or runtime.get("model") or "").strip()
    if not resolved_model:
        raise RuntimeError("No LLM model configured for auxiliary call.")
    resolved_url = str(base_url or runtime.get("base_url") or "").strip()
    if not resolved_url:
        raise RuntimeError(f"No base_url resolved for provider {runtime.get('provider')!r}.")

    merged_extra = {**(runtime.get("extra_body") if isinstance(runtime.get("extra_body"), dict) else {}), **(extra_body or {})}
    merged_headers = {**(runtime.get("extra_headers") if isinstance(runtime.get("extra_headers"), dict) else {}), **(extra_headers or {})}
    transport = create_transport(
        api_mode=str(api_mode or runtime.get("api_mode") or "chat_completions"),
        base_url=resolved_url, api_key=str(api_key or runtime.get("api_key") or ""),
        provider=str(runtime.get("provider") or provider or ""), default_headers=merged_headers,
        client=client if client is not None else get_pooled_async_client(),
    )
    stream = transport.stream(
        model=resolved_model, messages=messages, tools=tools,
        max_tokens=max_tokens if max_tokens is not None else runtime.get("max_tokens"),
        temperature=temperature if temperature is not None else runtime.get("temperature"),
        reasoning_config=reasoning_config, extra_body=merged_extra or None,
        timeout=timeout, retry_policy=retry_policy or RetryPolicy(max_retries=2, base_delay=0.5, max_delay=8.0),
    )
    response = await collect_stream(stream, model=resolved_model)
    if response.usage is not None and (task != "" or session_id):
        await asyncio.to_thread(
            _record_usage_best_effort, task=task or "aux", model=resolved_model,
            provider=str(runtime.get("provider") or ""), base_url=resolved_url,
            usage=response.usage, session_id=session_id, billing_mode=transport.api_mode,
        )
    return response


def call_llm(
    task: Optional[str] = None, *, messages: list[dict[str, Any]], provider: Optional[str] = None,
    model: Optional[str] = None, base_url: Optional[str] = None, api_key: Optional[str] = None,
    api_mode: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None,
    tools: Optional[list[dict[str, Any]]] = None, timeout: Optional[float] = None,
    extra_body: Optional[dict[str, Any]] = None, reasoning_config: Optional[dict[str, Any]] = None,
    extra_headers: Optional[dict[str, str]] = None, retry_policy: Optional[RetryPolicy] = None,
    client: Optional[httpx.AsyncClient] = None, session_id: str = "",
    config: Optional[dict[str, Any]] = None, **kwargs: Any,
) -> LLMResponse:
    """Thread-safe sync ``call_llm`` reusing the registered runtime loop's HTTP pool when available."""
    coro_kw = dict(
        task=task, messages=messages, provider=provider, model=model, base_url=base_url, api_key=api_key,
        api_mode=api_mode, temperature=temperature, max_tokens=max_tokens, tools=tools, timeout=timeout,
        extra_body=extra_body, reasoning_config=reasoning_config, extra_headers=extra_headers,
        retry_policy=retry_policy, client=client, session_id=session_id, config=config, **kwargs,
    )
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    reg_loop = get_runtime_loop()
    wait_s = (float(timeout) + 15.0) if (timeout and timeout > 0) else 150.0

    if reg_loop is not None and current_loop is not reg_loop:
        fut = asyncio.run_coroutine_threadsafe(async_call_llm(**coro_kw), reg_loop)
        try:
            return fut.result(timeout=wait_s)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise TimeoutError(f"call_llm timed out after {wait_s:.1f}s") from exc
    if current_loop is not None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="muse-call-llm") as pool:
            return pool.submit(lambda: asyncio.run(async_call_llm(**coro_kw))).result(timeout=wait_s)
    return asyncio.run(async_call_llm(**coro_kw))


# ── Vision fallback for blind text models ────────────────────────────────────


def _part_is_image(part: Any) -> bool:
    return isinstance(part, dict) and str(part.get("type") or "").strip().lower() in ("image_url", "input_image", "image")


def _resolve_vision_slot(
    *, vision_provider: Optional[str], vision_model: Optional[str], config: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    if vision_provider and vision_model:
        return vision_provider, vision_model
    cfg = config if isinstance(config, dict) else _read_config()
    vslot = (cfg.get("auxiliary") or {}).get("vision") if isinstance(cfg.get("auxiliary"), dict) else {}
    if isinstance(vslot, dict):
        prov, mod = str(vslot.get("provider") or "").strip(), str(vslot.get("model") or "").strip()
        if prov and prov.lower() != "auto" and mod:
            return prov, mod
    return vision_provider, vision_model


def _flatten_if_text_only(msg: dict[str, Any]) -> dict[str, Any]:
    parts = msg.get("content")
    if isinstance(parts, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in parts):
        return {**msg, "content": "\n".join(str(p.get("text") or "") for p in parts if p.get("text"))}
    return msg


async def describe_images_if_blind(
    messages: list[dict[str, Any]], *, chat_vision: bool, vision_provider: Optional[str] = None,
    vision_model: Optional[str] = None, vision_base_url: Optional[str] = None, vision_api_key: Optional[str] = None,
    vision_api_mode: Optional[str] = None, timeout: float = 60.0,
    client: Optional[httpx.AsyncClient] = None, config: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Replace inline images with textual descriptions via the configured ``vision=True`` model when ``chat_vision=False``."""
    if chat_vision:
        return messages
    has_img = any(isinstance(m, dict) and isinstance(m.get("content"), list) and any(_part_is_image(p) for p in m["content"]) for m in messages)
    if not has_img:
        return [_flatten_if_text_only(m) if isinstance(m, dict) else m for m in messages]
    v_prov, v_model = _resolve_vision_slot(vision_provider=vision_provider, vision_model=vision_model, config=config)
    can_desc = bool(v_model and (v_prov or vision_base_url))
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list) or not any(_part_is_image(p) for p in content):
            out.append(_flatten_if_text_only(msg) if isinstance(msg, dict) else msg)
            continue
        imgs = [p for p in content if _part_is_image(p)]
        texts = [p for p in content if not _part_is_image(p)]
        desc = ""
        if can_desc:
            try:
                ctx = " ".join(str(p.get("text") or "") for p in texts if isinstance(p, dict) and p.get("text")).strip()
                prompt = "请客观、准确、简明地描述图片中的所有可见内容（包含文字、数字、界面元素和主体细节），供纯文本对话模型理解。" + (f"\n用户同时问了：{ctx[:300]}" if ctx else "")
                resp = await async_call_llm(
                    task="vision", messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, *imgs]}],
                    provider=v_prov, model=v_model, base_url=vision_base_url, api_key=vision_api_key,
                    api_mode=vision_api_mode, max_tokens=1000, timeout=timeout, client=client, config=config,
                )
                desc = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            except Exception as exc:
                logger.warning("muse.llm.auxiliary: vision fallback failed (%s)", type(exc).__name__)
        new_parts = [copy.deepcopy(p) for p in texts]
        new_parts.append({"type": "text", "text": f"[图片内容描述]\n{desc}" if desc else "[图片：当前对话模型不支持直接看图，且未能生成图片描述]"})
        out.append(_flatten_if_text_only({**msg, "content": new_parts}))
    return out
