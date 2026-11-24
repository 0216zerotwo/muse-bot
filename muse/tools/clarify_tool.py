"""`clarify` tool schema, `(Recommended)` option helpers, and suspension callback bridge."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

TOOLSET = "clarify"
MAX_CHOICES = 5
RECOMMENDED_SUFFIX = " (Recommended)"
_RECOMMENDED_SUFFIX_RE = re.compile(r"\s*[（(]\s*(?:recommended|推荐)\s*[)）]\s*$", re.IGNORECASE)

ClarifyCallback = Callable[[str, Optional[list[str]], bool], Any]
_CLARIFY_CB_VAR: ContextVar[Optional[ClarifyCallback]] = ContextVar("_CLARIFY_CB_VAR", default=None)


def strip_recommended(option: str) -> tuple[str, bool]:
    """Strip trailing `(Recommended)` / `（推荐）` and return `(clean_label, is_recommended)`."""
    raw = str(option or "").strip()
    cleaned = _RECOMMENDED_SUFFIX_RE.sub("", raw).strip()
    if cleaned and cleaned != raw:
        return cleaned, True
    return raw, False


def mark_recommended(option: str, recommended: bool = True) -> str:
    """Ensure trailing ` (Recommended)` suffix is present or stripped cleanly."""
    label, _ = strip_recommended(option)
    if not label:
        return ""
    return f"{label}{RECOMMENDED_SUFFIX}" if recommended else label


def normalize_choices(raw_choices: Any) -> Optional[list[str]]:
    """Deduplicate and cap choices at `MAX_CHOICES` while preserving `(Recommended)` markers."""
    if raw_choices is None:
        return None
    if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
        raise ValueError("'choices' must be an array of strings or null.")
    cleaned: list[str] = []
    seen_keys: set[str] = set()
    for item in raw_choices:
        val = str(item or "").strip()
        if not val:
            continue
        base_label, _ = strip_recommended(val)
        dedup_key = base_label.casefold()
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        cleaned.append(val)
        if len(cleaned) >= MAX_CHOICES:
            break
    return cleaned or None


@contextmanager
def clarify_callback_scope(callback: Optional[ClarifyCallback]) -> Iterator[None]:
    token = _CLARIFY_CB_VAR.set(callback)
    try:
        yield
    finally:
        _CLARIFY_CB_VAR.reset(token)


async def clarify_handler(
    args: dict[str, Any],
    *,
    callback: Optional[ClarifyCallback] = None,
    **_: Any,
) -> str:
    """Validate `clarify` arguments and invoke active suspension callback when outside `ChatTurnActor`."""
    question = str(args.get("question") or "").strip()
    if not question:
        return json.dumps(
            {"success": False, "error": "Parameter 'question' must be a non-empty string."},
            ensure_ascii=False,
        )
    try:
        choices = normalize_choices(args.get("choices"))
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    multi_select = bool(args.get("multi_select", False))

    cb = callback or _CLARIFY_CB_VAR.get()
    if cb is not None:
        res = cb(question, choices, multi_select)
        if inspect.isawaitable(res):
            res = await res
        return str(res) if not isinstance(res, dict) else json.dumps(res, ensure_ascii=False)

    # Note: In live Companion chats, `ChatTurnActor._handle_clarify_tool_call` intercepts `clarify`
    # directly at P1 priority (emitting the choices card + turn_end(turn_A) + awaiting turn_B).
    return json.dumps(
        {
            "status": "awaiting_user_response",
            "question": question,
            "choices": choices,
            "multi_select": multi_select,
        },
        ensure_ascii=False,
    )


CLARIFY_SCHEMA: dict[str, Any] = {
    "name": "clarify",
    "description": (
        "Ask the user an interactive multiple-choice or open-ended question when an important "
        "decision or ambiguity needs user input. Append ' (Recommended)' to the suggested choice."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Clear, concise question presented to the user.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Up to 5 concise options (omit or leave empty for open-ended text reply). "
                    "Suffix the best option with ' (Recommended)'."
                ),
            },
            "multi_select": {
                "type": "boolean",
                "description": "Allow selecting multiple options simultaneously (default False).",
                "default": False,
            },
        },
        "required": ["question"],
    },
}


def register_clarify_tools(registry: Any) -> None:
    registry.register(
        "clarify",
        toolset=TOOLSET,
        schema=CLARIFY_SCHEMA,
        handler=clarify_handler,
        is_async=True,
    )


__all__ = [
    "CLARIFY_SCHEMA",
    "MAX_CHOICES",
    "RECOMMENDED_SUFFIX",
    "TOOLSET",
    "ClarifyCallback",
    "clarify_callback_scope",
    "clarify_handler",
    "mark_recommended",
    "normalize_choices",
    "register_clarify_tools",
    "strip_recommended",
]
