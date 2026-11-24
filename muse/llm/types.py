"""Typed stream events, normalized responses, error classification, SSE/HTTP helpers, and retry logic."""

from __future__ import annotations

import asyncio
import email.utils
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional, Union
from urllib.parse import urlparse

import httpx

FailureErrorKind = Literal["billing", "rate_limit", "timeout", "connection", "server"]
VALID_FAILURE_ERROR_KINDS: frozenset[str] = frozenset({"billing", "rate_limit", "timeout", "connection", "server"})


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ReasoningDelta:
    text: str
    signature: Optional[str] = None


@dataclass(slots=True)
class ToolCallDelta:
    index: int
    id: Optional[str] = None
    name: Optional[str] = None
    arguments_delta: str = ""
    extra_content: Optional[dict[str, Any]] = None


@dataclass(slots=True)
class ToolCallComplete:
    id: str
    name: str
    arguments: str
    index: int = 0
    extra_content: Optional[dict[str, Any]] = None
    thought_signature: Optional[str] = None


@dataclass(slots=True)
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def completion_tokens(self) -> int:
        return self.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass(slots=True)
class RetryStatusEvent:
    state: Literal["retrying", "recovered"]
    attempt: int
    max_attempts: int
    delay_seconds: float = 0.0
    error_kind: Optional[FailureErrorKind] = None
    message: str = ""

    def to_wire_event(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event": "retry_status", "state": self.state,
            "attempt": self.attempt, "max_attempts": self.max_attempts,
        }
        if self.state == "retrying":
            out["delay_seconds"] = round(self.delay_seconds, 2)
        if self.error_kind:
            out["error_kind"] = self.error_kind
        if self.message:
            out["message"] = self.message
        return out


@dataclass(slots=True)
class EncryptedReasoningItem:
    item_type: str
    encrypted_content: str
    issuer: str
    model: str
    item_id: Optional[str] = None
    summary: Optional[list[dict[str, Any]]] = None

    def to_storage_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": self.item_type, "encrypted_content": self.encrypted_content,
            "_issuer_kind": self.issuer, "_issuer_model": self.model,
        }
        if self.item_id:
            item["id"] = self.item_id
        if self.summary is not None:
            item["summary"] = list(self.summary)
        return item


@dataclass(slots=True)
class ThinkingBlockComplete:
    block_type: Literal["thinking", "redacted_thinking"]
    thinking: str = ""
    signature: Optional[str] = None
    data: Optional[str] = None

    def to_block_dict(self) -> dict[str, Any]:
        if self.block_type == "redacted_thinking":
            return {"type": "redacted_thinking", "data": self.data or ""}
        blk: dict[str, Any] = {"type": "thinking", "thinking": self.thinking}
        if self.signature:
            blk["signature"] = self.signature
        return blk


@dataclass(slots=True)
class MessageCarrierEvent:
    extra_content: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None


@dataclass(slots=True)
class StreamDone:
    finish_reason: str = "stop"
    usage: Optional[UsageInfo] = None


StreamEvent = Union[
    TextDelta, ReasoningDelta, ToolCallDelta, ToolCallComplete, UsageInfo,
    RetryStatusEvent, EncryptedReasoningItem, ThinkingBlockComplete, MessageCarrierEvent, StreamDone,
]


@dataclass
class LLMFunctionCall:
    name: str
    arguments: str
    thought_signature: Optional[str] = None


@dataclass
class LLMToolCall:
    id: str
    function: LLMFunctionCall
    type: str = "function"
    extra_content: Optional[dict[str, Any]] = None

    def to_openai_dict(self) -> dict[str, Any]:
        fn: dict[str, Any] = {"name": self.function.name, "arguments": self.function.arguments}
        if self.function.thought_signature:
            fn["thought_signature"] = self.function.thought_signature
        out: dict[str, Any] = {"id": self.id, "type": self.type, "function": fn}
        if self.extra_content:
            out["extra_content"] = self.extra_content
        return out


@dataclass
class LLMMessage:
    role: str = "assistant"
    content: Optional[str] = ""
    tool_calls: Optional[list[LLMToolCall]] = None
    reasoning_content: Optional[str] = None
    extra_content: Optional[dict[str, Any]] = None
    reasoning_details: Optional[list[dict[str, Any]]] = None
    codex_reasoning_items: Optional[list[dict[str, Any]]] = None
    anthropic_content_blocks: Optional[list[dict[str, Any]]] = None


@dataclass
class LLMChoice:
    index: int = 0
    message: LLMMessage = field(default_factory=LLMMessage)
    finish_reason: str = "stop"


@dataclass
class LLMResponse:
    choices: list[LLMChoice]
    model: str = ""
    usage: Optional[UsageInfo] = None


# ── Shared URL / SSE / HTTP helpers ──────────────────────────────────────────


def host_of(url: Any) -> str:
    try:
        return (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def host_matches(url: Any, domain: str) -> bool:
    host = host_of(url)
    target = domain.lower().rstrip(".")
    return bool(host) and (host == target or host.endswith("." + target))


async def iter_sse_json(resp: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, json_dict)`` pairs from an SSE ``httpx.Response``."""
    event_name = ""
    async for raw_line in resp.aiter_lines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict):
            yield event_name, data


async def post_and_stream(
    *,
    client: Optional[httpx.AsyncClient],
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    provider: str,
    model: str,
    on_json: Callable[[dict[str, Any]], AsyncIterator[StreamEvent]],
    on_sse: Callable[[httpx.Response], AsyncIterator[StreamEvent]],
    json_body: Optional[dict[str, Any]] = None,
    raw_content: Optional[bytes] = None,
) -> AsyncIterator[StreamEvent]:
    """Execute a streaming POST, raise typed ``LLMRequestError`` on >=400, or dispatch JSON/SSE."""
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    req_kw: dict[str, Any] = {"headers": headers, "timeout": timeout}
    if raw_content is not None:
        req_kw["content"] = raw_content
    else:
        req_kw["json"] = json_body
    try:
        async with http.stream("POST", url, **req_kw) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise classify_http_error(resp.status_code, body, resp.headers, provider=provider, model=model)
            ctype = (resp.headers.get("content-type") or "").lower()
            if "application/json" in ctype and "text/event-stream" not in ctype:
                raw_json = json.loads((await resp.aread()).decode("utf-8", errors="replace"))
                async for ev in on_json(raw_json):
                    yield ev
                return
            async for ev in on_sse(resp):
                yield ev
    finally:
        if owned:
            await http.aclose()


# ── Error classification & retry ─────────────────────────────────────────────

_BILLING_PATTERNS = (
    "insufficient credits", "insufficient_quota", "insufficient balance", "credit balance",
    "credits exhausted", "requires available credits", "account balance is too low", "no usable credits",
    "top up your credits", "payment required", "billing hard limit", "exceeded your current quota",
    "account is deactivated", "plan does not include", "out of extra usage", "out of funds",
    "balance_depleted", "budget limit exceeded", "hard billing limit", "credit_balance_exhausted",
    "organization_spend_limit_exceeded", "personal-team-blocked:spending-limit",
    "model_not_supported_on_free_tier", "not available on the free tier",
)
_RATE_LIMIT_PATTERNS = (
    "rate limit", "rate_limit", "too many requests", "throttled", "requests per minute",
    "tokens per minute", "requests per day", "resource exhausted", "resource_exhausted",
    "resource-exhausted", "throttlingexception", "too many concurrent requests",
    "servicequotaexceededexception", "usage limit",
)
_CONTEXT_OVERFLOW_PATTERNS = (
    "context length", "context size", "maximum context", "token limit", "too many tokens",
    "reduce the length", "context window", "prompt is too long", "prompt exceeds max length",
    "maximum number of tokens", "exceeds the max_model_len", "input is too long",
    "maximum model length", "request_too_large", "payload too large", "request entity too large",
    "超过最大长度", "上下文长度",
)
_TIMEOUT_PATTERNS = ("timed out", "timeout", "deadline exceeded", "gateway timeout")
_CONNECTION_PATTERNS = (
    "connection refused", "connection reset", "connection closed", "connection error",
    "name or service not known", "nodename nor servname", "getaddrinfo failed",
    "network is unreachable", "broken pipe", "eof occurred", "ssl:", "remoteprotocolerror",
)


class LLMRequestError(RuntimeError):
    """Structured error from an LLM transport carrying ``failure_error_kind``."""

    def __init__(
        self, message: str, *, error_kind: FailureErrorKind = "server", status_code: Optional[int] = None,
        attempts: int = 1, retryable: bool = False, retry_after: Optional[float] = None,
        is_context_overflow: bool = False, is_auth_error: bool = False, provider: str = "", model: str = "",
    ) -> None:
        super().__init__(message)
        self.error_kind: FailureErrorKind = error_kind
        self.failure_error_kind: FailureErrorKind = error_kind
        self.status_code, self.attempts, self.retryable = status_code, attempts, retryable
        self.retry_after = retry_after
        self.is_context_overflow, self.is_auth_error = is_context_overflow, is_auth_error
        self.provider, self.model = provider, model


def parse_retry_after_seconds(headers: Any) -> Optional[float]:
    raw = headers.get("retry-after") if headers and hasattr(headers, "get") else None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        val = float(text)
        return max(0.0, val) if val >= 0 else None
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(text)
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def extract_error_message(body_text: str, status_code: Optional[int] = None) -> str:
    raw = (body_text or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("detail") or err.get("code") or err.get("type")
                    if msg:
                        return str(msg)
                elif isinstance(err, str) and err.strip():
                    return err.strip()
                for key in ("message", "detail", "error_description"):
                    if parsed.get(key):
                        return str(parsed[key])
        except ValueError:
            pass
        return raw[:400]
    return f"HTTP {status_code}" if status_code is not None else "Unknown model request error"


def classify_failure_error_kind(
    *, status_code: Optional[int] = None, message: str = "", exc: Optional[BaseException] = None,
) -> FailureErrorKind:
    """Map an error to one of the 5 frontend ``failure_error_kind`` values."""
    if isinstance(exc, LLMRequestError):
        return exc.error_kind
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError, ConnectionError)):
        return "connection"
    text = f"{message or ''} {exc or ''}".lower()
    if status_code == 402 or any(p in text for p in _BILLING_PATTERNS):
        return "billing"
    if status_code == 429 or any(p in text for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if status_code in (408, 504) or any(p in text for p in _TIMEOUT_PATTERNS):
        return "timeout"
    if any(p in text for p in _CONNECTION_PATTERNS):
        return "connection"
    return "server"


def is_context_overflow_error(status_code: Optional[int], message: str) -> bool:
    return status_code == 413 or any(p in (message or "").lower() for p in _CONTEXT_OVERFLOW_PATTERNS)


def classify_http_error(
    status_code: int, body_text: str, headers: Any = None, *, provider: str = "", model: str = "",
) -> LLMRequestError:
    msg = extract_error_message(body_text, status_code)
    probe = f"{msg} {body_text[:500]}"
    kind = classify_failure_error_kind(status_code=status_code, message=probe)
    overflow = is_context_overflow_error(status_code, probe)
    is_auth = status_code in (401, 403) and kind != "billing"
    retryable = (
        kind != "billing" and not overflow and not is_auth
        and (status_code in (408, 429, 500, 502, 503, 504, 520, 522, 524, 529) or status_code >= 500)
    )
    return LLMRequestError(
        f"HTTP {status_code}: {msg}", error_kind=kind, status_code=status_code, retryable=retryable,
        retry_after=parse_retry_after_seconds(headers), is_context_overflow=overflow,
        is_auth_error=is_auth, provider=provider, model=model,
    )


def classify_exception(exc: BaseException, *, provider: str = "", model: str = "") -> LLMRequestError:
    if isinstance(exc, LLMRequestError):
        return exc
    kind = classify_failure_error_kind(exc=exc, message=str(exc))
    retryable = kind in ("timeout", "connection", "rate_limit", "server") and not isinstance(exc, (ValueError, TypeError, KeyError))
    return LLMRequestError(str(exc) or type(exc).__name__, error_kind=kind, retryable=retryable, provider=provider, model=model)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25

    def compute_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_delay)
        if self.base_delay <= 0:
            return 0.0
        exp = min(self.base_delay * (2 ** max(0, attempt - 1)), self.max_delay)
        return exp if self.jitter <= 0 else max(0.0, exp * (1.0 + random.uniform(-self.jitter, self.jitter)))


async def stream_with_retry(
    stream_factory: Callable[[int], AsyncIterator[StreamEvent]], *, policy: Optional[RetryPolicy] = None,
    provider: str = "", model: str = "", on_auth_refresh: Optional[Callable[[], Awaitable[bool]]] = None,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> AsyncIterator[StreamEvent]:
    """Retry transient failures before first payload event, emitting ``RetryStatusEvent``s."""
    cfg = policy or RetryPolicy()
    max_attempts = max(1, cfg.max_retries + 1)
    attempt, did_retry, last_kind, refreshed_auth = 1, False, None, False
    while attempt <= max_attempts:
        yielded_payload = False
        try:
            async for event in stream_factory(attempt):
                if did_retry and not yielded_payload:
                    yield RetryStatusEvent(state="recovered", attempt=attempt, max_attempts=max_attempts, error_kind=last_kind)
                    did_retry = False
                if isinstance(event, (TextDelta, ReasoningDelta, ToolCallDelta, ToolCallComplete, ThinkingBlockComplete, StreamDone)):
                    yielded_payload = True
                yield event
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = classify_exception(exc, provider=provider, model=model)
            err.attempts = attempt
            if err.is_auth_error and not yielded_payload and not refreshed_auth and on_auth_refresh is not None:
                refreshed_auth = True
                try:
                    if await on_auth_refresh():
                        continue  # retry same attempt index with refreshed token
                except Exception:
                    pass
            if yielded_payload or not err.retryable or attempt >= max_attempts:
                raise err from exc
            delay = cfg.compute_delay(attempt, err.retry_after)
            did_retry, last_kind = True, err.error_kind
            yield RetryStatusEvent(
                state="retrying", attempt=attempt, max_attempts=max_attempts,
                delay_seconds=delay, error_kind=err.error_kind, message=str(err)[:200],
            )
            if delay > 0:
                await sleep_fn(delay)
            attempt += 1


async def collect_stream(events: AsyncIterator[StreamEvent], *, model: str = "") -> LLMResponse:
    """Assemble typed ``StreamEvent``s into an OpenAI-compatible ``LLMResponse``."""
    text_parts, reasoning_parts, tool_calls, thinking_blocks, ordered_blocks, encrypted = [], [], [], [], [], []
    msg_extra, usage, finish_reason = None, None, "stop"
    async for ev in events:
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            reasoning_parts.append(ev.text)
        elif isinstance(ev, ToolCallComplete):
            tool_calls.append(LLMToolCall(
                id=ev.id, function=LLMFunctionCall(ev.name, ev.arguments, ev.thought_signature), extra_content=ev.extra_content,
            ))
            try:
                parsed = json.loads(ev.arguments) if ev.arguments else {}
            except ValueError:
                parsed = {}
            ordered_blocks.append({"type": "tool_use", "id": ev.id, "name": ev.name, "input": parsed})
        elif isinstance(ev, ThinkingBlockComplete):
            blk = ev.to_block_dict()
            thinking_blocks.append(blk)
            ordered_blocks.append(blk)
        elif isinstance(ev, EncryptedReasoningItem):
            encrypted.append(ev.to_storage_dict())
        elif isinstance(ev, MessageCarrierEvent):
            msg_extra = ev.extra_content or msg_extra
            finish_reason = ev.finish_reason or finish_reason
        elif isinstance(ev, UsageInfo):
            usage = ev
        elif isinstance(ev, StreamDone):
            finish_reason = ev.finish_reason or finish_reason
            usage = ev.usage if ev.usage is not None else usage
    content = "".join(text_parts)
    if content and ordered_blocks:
        ordered_blocks.append({"type": "text", "text": content})
    has_signed = any(b.get("type") in ("thinking", "redacted_thinking") and (b.get("signature") or b.get("data")) for b in thinking_blocks)
    msg = LLMMessage(
        role="assistant", content=content, tool_calls=tool_calls or None,
        reasoning_content="".join(reasoning_parts) or None, extra_content=msg_extra,
        reasoning_details=thinking_blocks or None, codex_reasoning_items=encrypted or None,
        anthropic_content_blocks=ordered_blocks if has_signed and tool_calls else None,
    )
    if tool_calls and finish_reason == "stop":
        finish_reason = "tool_calls"
    return LLMResponse(choices=[LLMChoice(index=0, message=msg, finish_reason=finish_reason)], model=model, usage=usage)
