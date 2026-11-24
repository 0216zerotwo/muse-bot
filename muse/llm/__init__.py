"""Muse native async LLM transport, OAuth subscription, auxiliary inference, and models.dev package."""

from __future__ import annotations

from muse.llm.anthropic_messages import AnthropicMessagesTransport
from muse.llm.auxiliary import (
    async_call_llm,
    call_llm,
    close_shared_clients,
    create_transport,
    describe_images_if_blind,
    get_runtime_loop,
    register_runtime_loop,
)
from muse.llm.chat_completions import OpenAIChatTransport
from muse.llm.models_dev import fetch_models_dev
from muse.llm.providers import (
    _is_anthropic_endpoint,
    detect_api_mode,
    is_anthropic_endpoint,
    is_openai_reasoning_model,
    prenormalize_provider_params,
    resolve_runtime_provider,
)
from muse.llm.responses import OpenAIResponsesTransport
from muse.llm.types import (
    EncryptedReasoningItem,
    FailureErrorKind,
    LLMChoice,
    LLMFunctionCall,
    LLMMessage,
    LLMRequestError,
    LLMResponse,
    LLMToolCall,
    MessageCarrierEvent,
    ReasoningDelta,
    RetryPolicy,
    RetryStatusEvent,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingBlockComplete,
    ToolCallComplete,
    ToolCallDelta,
    UsageInfo,
    classify_failure_error_kind,
    collect_stream,
)

__all__ = [
    "AnthropicMessagesTransport",
    "EncryptedReasoningItem",
    "FailureErrorKind",
    "LLMChoice",
    "LLMFunctionCall",
    "LLMMessage",
    "LLMRequestError",
    "LLMResponse",
    "LLMToolCall",
    "MessageCarrierEvent",
    "OpenAIChatTransport",
    "OpenAIResponsesTransport",
    "ReasoningDelta",
    "RetryPolicy",
    "RetryStatusEvent",
    "StreamDone",
    "StreamEvent",
    "TextDelta",
    "ThinkingBlockComplete",
    "ToolCallComplete",
    "ToolCallDelta",
    "UsageInfo",
    "_is_anthropic_endpoint",
    "async_call_llm",
    "call_llm",
    "classify_failure_error_kind",
    "close_shared_clients",
    "collect_stream",
    "create_transport",
    "describe_images_if_blind",
    "detect_api_mode",
    "fetch_models_dev",
    "get_runtime_loop",
    "is_anthropic_endpoint",
    "is_openai_reasoning_model",
    "prenormalize_provider_params",
    "register_runtime_loop",
    "resolve_runtime_provider",
]
