"""Async `ChatSink` base class, `DraftSegmentStreamer`, and wire/card/retry formatting helpers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Optional

from .notices import render_notice

APPROVE_COMMAND = "/approve"
CANCEL_COMMAND = "/cancel"

_RECOMMENDED_RE = re.compile(r"\s*[（(]\s*(?:recommended|推荐)\s*[)）]\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:card|json|ui)[ \t]*\n([\s\S]*?)\n```")
_OPEN_FENCE_RE = re.compile(r"```(?:card|json|ui)[ \t]*\n")
_MEDIA_PREFIX = "MEDIA:"
_VALID_FAILURE_KINDS = frozenset({"billing", "rate_limit", "timeout", "connection", "server"})
_RETRY_STATE_MAP = {
    "retrying": "waiting", "waiting": "waiting", "recovered": "recovered",
    "cleared": "cleared", "exhausted": "exhausted",
}


def new_history_id() -> str:
    """Return a canonical `history-<20hex>` message id (matches `web/src/lib/api.ts:402`)."""
    return f"history-{uuid.uuid4().hex[:20]}"


def strip_card_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _without_provisional_fence(text: str) -> str:
    trimmed = text.rstrip()
    return trimmed[:-3].rstrip() if trimmed.endswith("```") else text


def _hold_back_media_tag(text: str) -> str:
    lines = text.split("\n")
    kept = [line for line in lines if not line.lstrip().startswith(_MEDIA_PREFIX)]
    if kept and kept[-1].strip() and _MEDIA_PREFIX.startswith(kept[-1].strip()):
        kept.pop()
    return text if kept == lines else "\n".join(kept).rstrip()


def _hold_back_open_card(text: str) -> str:
    try:
        from companion.modules import widgets
        return widgets.hold_back_open_card(text)
    except Exception:
        closed = max((m.end() for m in _FENCE_RE.finditer(text)), default=0)
        opening = _OPEN_FENCE_RE.search(text, closed)
        return text[: opening.end()] if opening else text


def extract_media_lines(text: str) -> tuple[str, list[str]]:
    """Strip standalone `MEDIA:/abs/path` lines at segment close and return `(clean_text, paths)`."""
    if not text or _MEDIA_PREFIX not in text:
        return text, []
    kept, paths = [], []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(_MEDIA_PREFIX):
            if raw_path := stripped[len(_MEDIA_PREFIX) :].strip():
                paths.append(raw_path)
        else:
            kept.append(line)
    return "\n".join(kept).strip(), paths


def format_clarify_card(
    question: str, choices: Optional[Sequence[str]], *, multi_select: bool = False,
) -> str:
    """Format `clarify` prompt into a `choices` card stripping `(Recommended)`/`（推荐）` -> `primary: True`."""
    q = (question or "").strip()
    options = []
    for choice in choices or ():
        if raw := str(choice).strip():
            label = _RECOMMENDED_RE.sub("", raw).strip() or raw
            options.append({"label": label, "send": raw, "primary": label != raw})
    if not options:
        return q
    card = {"type": "choices", "options": options, "multiple": bool(multi_select)}
    return f"{q}\n\n```card\n{json.dumps(card, ensure_ascii=False)}\n```"


def format_slash_confirm_card(command: str) -> Optional[str]:
    try:
        from companion.modules import hermes_words
        return hermes_words.confirm_text(command)
    except Exception:
        prompts = {
            "new": "要开个新对话吗？现在这段聊天会收起来，我记得的关于你的事都还在。",
            "reset": "要开个新对话吗？现在这段聊天会收起来，我记得的关于你的事都还在。",
            "undo": "要撤回上一轮吗？你上一条消息和我的回复都会删掉。",
        }
        ask = prompts.get(command.strip().lstrip("/").lower())
        if not ask:
            return None
        card = {"type": "buttons", "items": [
            {"label": "好", "send": APPROVE_COMMAND, "primary": True},
            {"label": "算了", "send": CANCEL_COMMAND},
        ]}
        return f"{ask}\n\n```card\n{json.dumps(card, ensure_ascii=False)}\n```"


def translate_retry_status(event: Any, *, chat_id: str, turn_id: Optional[str] = None) -> dict[str, Any]:
    """Translate `RetryStatusEvent` into the exact wire schema validated by `notifications.ts`."""
    is_dict = isinstance(event, dict)
    get = event.get if is_dict else (lambda k, d=None: getattr(event, k, d))
    state = _RETRY_STATE_MAP.get(str(get("state", "waiting") or "waiting").strip().lower(), "waiting")
    attempt = max(1, int(get("attempt", 1) or 1))
    max_attempts = max(attempt, int(get("max_attempts", attempt) or attempt))
    kind = str(get("error_kind", None) or "server")
    frame: dict[str, Any] = {
        "event": "retry_status", "chat_id": str(chat_id), "state": state,
        "attempt": attempt, "max_attempts": max_attempts,
        "error_kind": kind if kind in _VALID_FAILURE_KINDS else "server",
    }
    if turn_id:
        frame["turn_id"] = str(turn_id)
    if state == "waiting":
        delay = get("retry_after_s") if (is_dict and "retry_after_s" in event) else get("delay_seconds")
        if delay is not None:
            frame["retry_after_s"] = round(max(0.0, float(delay)), 2)
    return frame


def classify_turn_failure(exc: BaseException) -> tuple[str, int, bool]:
    """Return `(failure_error_kind, attempts, is_context_overflow)` matching `model-request-failure.ts`."""
    attempts, status = max(1, int(getattr(exc, "attempts", 1) or 1)), getattr(exc, "status_code", None)
    overflow = bool(getattr(exc, "is_context_overflow", False) or status == 413)
    direct = getattr(exc, "failure_error_kind", None) or getattr(exc, "error_kind", None)
    if isinstance(direct, str) and direct in _VALID_FAILURE_KINDS:
        return direct, attempts, overflow
    try:
        from muse.llm.types import classify_failure_error_kind, is_context_overflow_error
        kind = str(classify_failure_error_kind(status_code=status, message=str(exc), exc=exc))
        overflow = overflow or is_context_overflow_error(status, str(exc))
        return (kind if kind in _VALID_FAILURE_KINDS else "server"), attempts, overflow
    except ImportError:
        pass
    text = str(exc).lower()
    overflow = overflow or any(
        p in text for p in ("context length", "context window", "too many tokens", "request_too_large")
    )
    if status == 402 or any(p in text for p in ("insufficient_quota", "billing", "credit", "payment required")):
        return "billing", attempts, overflow
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "rate_limit", attempts, overflow
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or status in (408, 504):
        return "timeout", attempts, overflow
    if "timeout" in text or "timed out" in text:
        return "timeout", attempts, overflow
    if isinstance(exc, ConnectionError) or any(p in text for p in ("connection", "network", "dns", "refused")):
        return "connection", attempts, overflow
    return "server", attempts, overflow


def failure_notice_zh(error_kind: str) -> str:
    key_map = {
        "billing": "gateway.errors.hint_quota", "rate_limit": "gateway.errors.rate_limited",
        "timeout": "gateway.errors.connection_interrupted", "connection": "gateway.errors.unreachable",
    }
    return render_notice(key_map.get(error_kind, "gateway.errors.provider_kept_failing")).strip()


class ChatSink:
    """Clean async delivery & transcript persistence base class replacing `BasePlatformAdapter`."""

    async def emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def append_store(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def remove_store_events(self, chat_id: str, predicate: Callable[[dict[str, Any]], bool]) -> int:
        return 0

    def store_events(self, chat_id: str) -> list[dict[str, Any]]:
        return []

    def ensure_chat(self, chat_id: str) -> None:
        return None

    def set_active_turn(self, chat_id: str, turn_id: Optional[str]) -> None:
        return None

    def get_active_turn(self, chat_id: str) -> Optional[str]:
        return None

    def note_turn_output(self, chat_id: str, turn_id: Optional[str]) -> None:
        return None

    async def push_goal_state(self, chat_id: str) -> None:
        return None

    def is_main_chat(self, chat_id: str) -> bool:
        return False

    def source_for_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return None

    def schedule_push(self, title: str, preview: str, *, tag: str = "") -> None:
        return None

    def companion_name(self) -> str:
        return "伙伴"

    async def post_notice(self, chat_id: str, text: str) -> None:
        ev = {"event": "message", "text": text, "source": {"kind": "notice", "label": ""}}
        await self.emit(chat_id, ev, persist=True)

    def transform_cards(self, chat_id: str, text: str, *, include_pick_card: bool = False) -> str:
        if include_pick_card and self.is_main_chat(chat_id):
            with contextlib.suppress(Exception):
                from companion.modules import avatar_make
                return avatar_make.with_pick_card(text)
        return text

    async def send_media_file(
        self, chat_id: str, path: str, *, caption: str = "",
        turn_id: Optional[str] = None, reply_context: str = "",
    ) -> bool:
        with contextlib.suppress(Exception):
            from companion.modules import creations, widgets
            title = creations.reply_title(widgets.strip_cards(caption or reply_context))
            await asyncio.to_thread(creations.collect_sent_file, Path(path), title=title)
        body: dict[str, Any] = {
            "event": "message", "text": caption, "projection_id": new_history_id(),
            "media_urls": [{"url": path, "name": Path(path).name, "kind": "file"}],
        }
        if turn_id:
            body.update(turn_id=turn_id, turn_phase="answer")
        await self.emit(chat_id, body, persist=True)
        return True


class DraftSegmentStreamer:
    """Streams segmented replies with fence/MEDIA holdback and zero-duplicate `.jsonl` persistence."""

    def __init__(self, sink: ChatSink, chat_id: str, turn_id: str, *, start_draft_id: int = 1) -> None:
        self.sink, self.chat_id, self.turn_id, self.draft_id = sink, chat_id, turn_id, start_draft_id
        self.stream_id: Optional[str] = None
        self.reasoning_stream_id: Optional[str] = None
        self.raw_text = ""
        self.emitted_text = ""
        self.emitted_any_text = False
        self.reasoning_open = False
        self.closed_segments: list[str] = []

    def _filter_live(self, raw_content: str) -> str:
        cleaned = _hold_back_media_tag(_without_provisional_fence(raw_content.lstrip()))
        return _hold_back_open_card(self.sink.transform_cards(self.chat_id, cleaned, include_pick_card=False))

    async def _ws_emit(self, event: str, **fields: Any) -> None:
        await self.sink.emit(self.chat_id, {"event": event, "turn_id": self.turn_id, **fields}, persist=False)

    async def push_reasoning(self, chunk: str) -> None:
        if not chunk:
            return
        if not self.reasoning_open:
            self.reasoning_open = True
            self.reasoning_stream_id = f"reason-{self.draft_id}-{uuid.uuid4().hex[:8]}"
        await self._ws_emit(
            "reasoning_delta", text=chunk, stream_id=self.reasoning_stream_id, turn_phase="reasoning",
        )

    async def close_reasoning(self) -> None:
        if not self.reasoning_open:
            return
        self.reasoning_open, sid, self.reasoning_stream_id = False, self.reasoning_stream_id, None
        await self._ws_emit("reasoning_end", stream_id=sid, turn_phase="reasoning")

    async def push_delta(self, chunk: str) -> None:
        if chunk:
            self.raw_text += chunk
            await self.push_full(self.raw_text)

    async def push_full(self, raw_content: str) -> None:
        self.raw_text = raw_content
        visible = self._filter_live(raw_content)
        if self.stream_id is not None and not visible.startswith(self.emitted_text):
            self.emitted_text = visible
            self.emitted_any_text = self.emitted_any_text or bool(visible)
            await self._ws_emit(
                "stream_end", stream_id=self.stream_id, text=visible,
                resuming=True, merge_next=True, turn_phase="answer",
            )
            return
        if self.stream_id is None:
            if not visible:
                return
            await self.close_reasoning()
            self.stream_id = f"draft-{self.draft_id}-{uuid.uuid4().hex[:8]}"
        if delta := visible[len(self.emitted_text) :]:
            self.emitted_text, self.emitted_any_text = visible, True
            self.sink.note_turn_output(self.chat_id, self.turn_id)
            await self._ws_emit("delta", text=delta, stream_id=self.stream_id, turn_phase="answer")

    async def close_segment(
        self, *, final_raw: Optional[str] = None, for_tools: bool = False,
    ) -> tuple[str, list[str]]:
        """Close draft: WS `stream_end` (`persist=False`) + `append_store()` ONLY (no duplicate WS `message`)."""
        await self.close_reasoning()
        clean, media_paths = extract_media_lines(self.raw_text if final_raw is None else final_raw)
        final_text = self.sink.transform_cards(self.chat_id, clean, include_pick_card=not for_tools).strip()
        stream_id, emitted = self.stream_id, self.emitted_text
        self.stream_id, self.raw_text, self.emitted_text = None, "", ""

        if stream_id is not None:
            if final_text:
                if final_text.startswith(emitted) and len(final_text) > len(emitted):
                    await self._ws_emit(
                        "delta", text=final_text[len(emitted) :], stream_id=stream_id, turn_phase="answer",
                    )
                    await self._ws_emit("stream_end", stream_id=stream_id)
                elif final_text != emitted.strip():
                    await self._ws_emit("stream_end", stream_id=stream_id, text=final_text, turn_phase="answer")
                else:
                    await self._ws_emit("stream_end", stream_id=stream_id)
                self.emitted_any_text = True
                self.sink.note_turn_output(self.chat_id, self.turn_id)
                self.sink.append_store(self.chat_id, {
                    "event": "message", "text": final_text, "turn_id": self.turn_id,
                    "turn_phase": "answer", "projection_id": new_history_id(),
                })
            else:
                await self._ws_emit("stream_end", stream_id=stream_id, text="")
            self.draft_id += 1
        elif final_text:
            self.emitted_any_text = True
            self.sink.note_turn_output(self.chat_id, self.turn_id)
            await self.sink.emit(self.chat_id, {
                "event": "message", "text": final_text, "turn_id": self.turn_id,
                "turn_phase": "answer", "projection_id": new_history_id(),
            }, persist=True)
            self.draft_id += 1

        if final_text:
            self.closed_segments.append(final_text)
        return final_text, media_paths

    async def flush_media(self, media_paths: Sequence[str], *, reply_context: str = "") -> None:
        for path in media_paths:
            await self.sink.send_media_file(self.chat_id, path, turn_id=self.turn_id, reply_context=reply_context)


__all__ = [
    "APPROVE_COMMAND", "CANCEL_COMMAND", "ChatSink", "DraftSegmentStreamer", "classify_turn_failure",
    "extract_media_lines", "failure_notice_zh", "format_clarify_card", "format_slash_confirm_card",
    "new_history_id", "strip_card_fences", "translate_retry_status",
]
