"""Turn execution context (ContextVar), system prompt assembly, and 3-tier lossless compaction."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, TypeVar

from .paths import get_muse_home

logger = logging.getLogger(__name__)
T = TypeVar("T")

ActorKind = Literal["companion", "helper", "desk", "cron", "internal"]
CompactionPhase = Literal["started", "succeeded", "failed", "cancelled"]
_VALID_COMPACTION_PHASES = frozenset({"started", "succeeded", "failed", "cancelled"})
_PHASE_ALIASES: dict[str, CompactionPhase] = {
    "start": "started", "started": "started", "end": "succeeded", "done": "succeeded",
    "succeeded": "succeeded", "failed": "failed", "error": "failed",
    "cancelled": "cancelled", "canceled": "cancelled",
}

MEMORY_DELIMITER = "\n§\n"
DEFAULT_USER_MEMORY_LIMIT = 1375
DEFAULT_NOTES_MEMORY_LIMIT = 2200
TIER1_TOKEN_THRESHOLD = 40_000
TIER1_KEEP_RECENT_TURNS = 4
TIER1_TOOL_CHAR_LIMIT = 1_000
TIER1_MAX_INLINE_IMAGES = 4
TIER2_WINDOW_RATIO = 0.75
TIER2_KEEP_RECENT_TURNS = 4

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_SNAPSHOT_TOOLS = frozenset({
    "web_extract", "browser_snapshot", "mcp__browser__browser_snapshot", "mcp__browser__browser_navigate",
})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})


def _kill_process_tree(pid: int) -> None:
    """Recursively terminate and kill a child process tree via psutil (cross-platform)."""
    if pid <= 0:
        return
    try:
        import psutil
        root = psutil.Process(pid)
    except Exception:
        return
    targets = []
    with contextlib.suppress(Exception):
        targets.extend(root.children(recursive=True))
    targets.append(root)
    for proc in targets:
        with contextlib.suppress(Exception):
            proc.terminate()
    for proc in psutil.wait_procs(targets, timeout=0.4)[1]:
        with contextlib.suppress(Exception):
            proc.kill()


class CancelScope:
    """Cooperative cancellation scope linking active asyncio tasks and spawned child PIDs."""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pids: set[int] = set()
        self._callbacks: list[Callable[[], Any]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def register_task(self, task: asyncio.Task[Any]) -> None:
        if self._cancelled:
            if not task.done():
                task.cancel()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def register_pid(self, pid: int) -> None:
        if pid <= 0:
            return
        if self._cancelled:
            _kill_process_tree(pid)
            return
        self._pids.add(int(pid))

    def unregister_pid(self, pid: int) -> None:
        self._pids.discard(int(pid))

    def add_callback(self, callback: Callable[[], Any]) -> None:
        if self._cancelled:
            with contextlib.suppress(Exception):
                callback()
            return
        self._callbacks.append(callback)

    def check(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError(self._reason or "cancelled")

    def cancel(self, reason: str = "user_stop") -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        self._event.set()
        pids, self._pids = list(self._pids), set()
        for pid in pids:
            _kill_process_tree(pid)
        current: Optional[asyncio.Task[Any]] = None
        with contextlib.suppress(RuntimeError):
            current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()
        callbacks, self._callbacks = list(self._callbacks), []
        for cb in callbacks:
            with contextlib.suppress(Exception):
                cb()


@dataclass(slots=True)
class TurnExecutionContext:
    """Per-turn execution metadata propagated via ContextVar across tasks & `asyncio.to_thread`."""

    chat_id: str
    facing_chat_id: str = ""
    session_id: str = ""
    turn_id: Optional[str] = None
    actor_kind: ActorKind = "companion"
    helper_id: Optional[str] = None
    enabled_toolsets: tuple[str, ...] = ()
    cancel_scope: CancelScope = field(default_factory=CancelScope)

    def __post_init__(self) -> None:
        if not self.facing_chat_id:
            self.facing_chat_id = self.chat_id
        if not isinstance(self.enabled_toolsets, tuple):
            self.enabled_toolsets = tuple(self.enabled_toolsets)


_TURN_CTX: ContextVar[Optional[TurnExecutionContext]] = ContextVar("_TURN_CTX", default=None)


def get_turn_context() -> Optional[TurnExecutionContext]:
    return _TURN_CTX.get()


def bind_turn_context(ctx: Optional[TurnExecutionContext]) -> Token[Optional[TurnExecutionContext]]:
    return _TURN_CTX.set(ctx)


def reset_turn_context(token: Token[Optional[TurnExecutionContext]]) -> None:
    _TURN_CTX.reset(token)


@contextmanager
def turn_context_scope(ctx: TurnExecutionContext) -> Iterator[TurnExecutionContext]:
    token = bind_turn_context(ctx)
    try:
        yield ctx
    finally:
        reset_turn_context(token)


async def run_in_turn_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking work in a worker thread while preserving the active `TurnExecutionContext`."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip("\"'")
    return meta


def load_memory_entries(home: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """Read `memories/USER.md` and `memories/MEMORY.md` split by canonical `\\n§\\n`."""
    mem_dir = (Path(home) if home is not None else get_muse_home()) / "memories"

    def _load(filename: str, char_limit: int) -> list[str]:
        raw = _read_text_safe(mem_dir / filename).strip()
        kept, used = [], 0
        for item in (part.strip() for part in raw.split(MEMORY_DELIMITER) if part.strip()):
            cost = len(item) + (len(MEMORY_DELIMITER) if kept else 0)
            if used + cost > char_limit and kept:
                break
            kept.append(item)
            used += cost
        return kept

    return _load("USER.md", DEFAULT_USER_MEMORY_LIMIT), _load("MEMORY.md", DEFAULT_NOTES_MEMORY_LIMIT)


def load_skill_index(home: Optional[Path] = None, *, include_bundled: bool = False) -> list[tuple[str, str]]:
    """Return lightweight `(name, one_line_summary)` pairs from `<HOME>/skills/**/SKILL.md`."""
    root = (Path(home) if home is not None else get_muse_home()) / "skills"
    if not root.is_dir():
        return []
    bundled = {
        line.split(":", 1)[0].strip()
        for line in _read_text_safe(root / ".bundled_manifest").splitlines()
        if line.strip()
    }
    rows: list[tuple[str, str]] = []
    for path in sorted(root.glob("**/SKILL.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        meta = _parse_frontmatter(_read_text_safe(path))
        name = (meta.get("name") or path.parent.name).strip()
        if name and (include_bundled or (name not in bundled and path.parent.name not in bundled)):
            rows.append((name, " ".join((meta.get("description") or "").split())[:200]))
    return rows


class PromptAssembler:
    """Assembles deterministic system prompts with per-session frozen `SOUL.md` snapshots."""

    def __init__(self) -> None:
        self._soul_snapshots: dict[str, str] = {}

    def get_soul_snapshot(self, session_id: str, *, db: Any = None, home: Optional[Path] = None) -> str:
        if session_id and session_id in self._soul_snapshots:
            return self._soul_snapshots[session_id]
        session_meta: dict[str, Any] = {}
        if session_id and db is not None and callable(getattr(db, "get_session", None)):
            if isinstance(row := db.get_session(session_id), dict):
                session_meta = dict(row.get("metadata") or {})
                if isinstance(frozen := session_meta.get("soul_snapshot"), str):
                    self._soul_snapshots[session_id] = frozen
                    return frozen
        root = Path(home) if home is not None else get_muse_home()
        soul = _read_text_safe(root / "SOUL.md").strip()
        if session_id:
            self._soul_snapshots[session_id] = soul
            if db is not None and callable(getattr(db, "update_session", None)):
                session_meta["soul_snapshot"] = soul
                with contextlib.suppress(Exception):
                    db.update_session(session_id, metadata=session_meta)
        return soul

    def evict_session(self, session_id: str) -> None:
        self._soul_snapshots.pop(session_id, None)

    def assemble(
        self, *, session_id: str, chat_id: str = "", db: Any = None, home: Optional[Path] = None,
        goal_state: Optional[dict[str, Any]] = None, plan_context: Optional[str] = None,
        summary_checkpoint: Optional[str] = None, extra_sections: Sequence[str] = (),
    ) -> str:
        sections: list[str] = []
        if soul := self.get_soul_snapshot(session_id, db=db, home=home):
            sections.append(soul)
        user_mem, notes_mem = load_memory_entries(home=home)
        if user_mem or notes_mem:
            blocks = ["## Long-Term Memory"]
            if user_mem:
                blocks.append("### User Profile (USER.md)\n" + MEMORY_DELIMITER.join(user_mem))
            if notes_mem:
                blocks.append("### Notes (MEMORY.md)\n" + MEMORY_DELIMITER.join(notes_mem))
            sections.append("\n\n".join(blocks))
        if skills := load_skill_index(home=home):
            header = "## Available Skills (use `skill_view` for full instructions)"
            sections.append("\n".join([header, *(f"- `{n}`: {d}" if d else f"- `{n}`" for n, d in skills)]))
        resolved_goal = goal_state
        if resolved_goal is None and chat_id and db is not None:
            if callable(getattr(db, "get_goal_banner_state", None)):
                with contextlib.suppress(Exception):
                    resolved_goal = db.get_goal_banner_state(chat_id)
        if isinstance(resolved_goal, dict) and resolved_goal.get("active"):
            g = [
                "## Active Goal",
                f"- Objective: {resolved_goal.get('objective') or ''}",
                f"- Status: {resolved_goal.get('status') or 'active'}",
            ]
            if resolved_goal.get("ui_summary"):
                g.append(f"- Summary: {resolved_goal['ui_summary']}")
            sections.append("\n".join(g))
        if plan_context and plan_context.strip():
            sections.append(f"## Active Plan\n{plan_context.strip()}")
        checkpoint = summary_checkpoint
        if checkpoint is None and session_id and db is not None and callable(getattr(db, "get_session", None)):
            if isinstance(row := db.get_session(session_id), dict):
                checkpoint = row.get("summary_checkpoint")
        if checkpoint and str(checkpoint).strip():
            sections.append(f"## Conversation Summary Checkpoint\n{str(checkpoint).strip()}")
        sections.extend(str(extra).strip() for extra in extra_sections if extra and str(extra).strip())
        return "\n\n".join(sections)


def _is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and str(part.get("type") or "").strip().lower() in _IMAGE_PART_TYPES


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 0x7F)
    return max(1, (len(text) - non_ascii + 3) // 4 + int(non_ascii * 0.75) + (1 if non_ascii else 0))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total, content = 4, message.get("content")
    if isinstance(content, str):
        total += estimate_text_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if _is_image_part(part):
                total += 800
            elif isinstance(part, dict):
                total += estimate_text_tokens(str(part.get("text") or ""))
    if isinstance(tcs := message.get("tool_calls"), list) and tcs:
        total += estimate_text_tokens(json.dumps(tcs, ensure_ascii=False))
    return total


def estimate_tokens(messages: Sequence[dict[str, Any]], system_prompt: str = "") -> int:
    return estimate_text_tokens(system_prompt) + sum(estimate_message_tokens(m) for m in messages)


def split_user_groups(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Partition messages strictly on `role == 'user'` boundaries so tool call pairs never split."""
    groups: list[list[dict[str, Any]]] = []
    for msg in messages:
        if msg.get("role") == "user" or not groups:
            groups.append([msg])
        else:
            groups[-1].append(msg)
    return groups


def build_compaction_event(
    chat_id: str, compaction_id: str, phase: str, *, turn_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build strict wire-validated `context_compaction` event (passes `api.ts` & `notifications.ts`)."""
    cid = str(compaction_id or "").strip()
    if not cid:
        raise ValueError("compaction_id must be a non-empty string")
    canonical = _PHASE_ALIASES.get(str(phase or "").strip().lower())
    if canonical not in _VALID_COMPACTION_PHASES:
        raise ValueError(f"Unsupported compaction phase: {phase!r}")
    ev: dict[str, Any] = {
        "event": "context_compaction", "chat_id": str(chat_id),
        "compaction_id": cid, "phase": canonical, "turn_phase": "activity",
    }
    if turn_id:
        ev["turn_id"] = str(turn_id)
    return ev


@dataclass(slots=True)
class CompactionResult:
    compacted: bool
    tier: int
    messages: list[dict[str, Any]]
    summary_checkpoint: Optional[str] = None
    pruned_tool_outputs: int = 0
    evicted_images: int = 0
    dropped_messages: int = 0
    compaction_id: Optional[str] = None


CompactionEmitter = Callable[[dict[str, Any]], Awaitable[Any]]
SummarizerFn = Callable[[list[dict[str, Any]], str], Awaitable[str]]


class ContextCompactor:
    """3-tier lossless context compactor preserving `tool_call_id` pairs and wire schemas."""

    def __init__(
        self, *, tier1_threshold: int = TIER1_TOKEN_THRESHOLD,
        tier1_keep_recent_turns: int = TIER1_KEEP_RECENT_TURNS,
        tier1_tool_char_limit: int = TIER1_TOOL_CHAR_LIMIT,
        tier1_max_inline_images: int = TIER1_MAX_INLINE_IMAGES,
        tier2_window_ratio: float = TIER2_WINDOW_RATIO,
        tier2_keep_recent_turns: int = TIER2_KEEP_RECENT_TURNS,
        summarizer: Optional[SummarizerFn] = None,
    ) -> None:
        self.tier1_threshold = tier1_threshold
        self.tier1_keep_recent_turns = tier1_keep_recent_turns
        self.tier1_tool_char_limit = tier1_tool_char_limit
        self.tier1_max_inline_images = tier1_max_inline_images
        self.tier2_window_ratio = tier2_window_ratio
        self.tier2_keep_recent_turns = tier2_keep_recent_turns
        self._default_summarizer = summarizer

    @staticmethod
    async def _emit(
        emitter: Optional[CompactionEmitter], chat_id: str, cid: str, phase: str, turn_id: Optional[str],
    ) -> None:
        if emitter is not None:
            await emitter(build_compaction_event(chat_id, cid, phase, turn_id=turn_id))

    def tier1_prune(
        self, messages: Sequence[dict[str, Any]], *, system_prompt: str = "", force: bool = False,
    ) -> CompactionResult:
        """Tier 1: 0ms zero-LLM pruning of old large tool outputs/snapshots & old inline images."""
        cloned = [copy.deepcopy(m) for m in messages]
        image_slots = [
            (mi, pi) for mi, msg in enumerate(cloned) if isinstance(msg.get("content"), list)
            for pi, part in enumerate(msg["content"]) if _is_image_part(part)
        ]
        evicted_images = 0
        if len(image_slots) > self.tier1_max_inline_images:
            for mi, pi in image_slots[: -self.tier1_max_inline_images]:
                cloned[mi]["content"][pi] = {"type": "text", "text": "[Earlier image omitted to save context]"}
                evicted_images += 1
        pruned_tools = 0
        if force or estimate_tokens(cloned, system_prompt) >= self.tier1_threshold:
            groups = split_user_groups(cloned)
            for grp in groups[: max(0, len(groups) - self.tier1_keep_recent_turns)]:
                for msg in grp:
                    if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
                        continue
                    text, tname = msg["content"], str(msg.get("tool_name") or msg.get("name") or "")
                    if len(text) > self.tier1_tool_char_limit or (tname in _SNAPSHOT_TOOLS and len(text) > 300):
                        preview = text[:200].replace("\r", " ").replace("\n", " ").strip()
                        msg["content"] = f"[Pruned {tname or 'tool'} output ({len(text)} chars); preview: {preview}...]"
                        pruned_tools += 1
        return CompactionResult(
            bool(pruned_tools or evicted_images), 1, cloned,
            pruned_tool_outputs=pruned_tools, evicted_images=evicted_images,
        )

    async def tier2_compact(
        self, messages: Sequence[dict[str, Any]], *, db: Any = None, session_id: str = "", chat_id: str = "",
        turn_id: Optional[str] = None, context_window: int = 128_000, system_prompt: str = "",
        existing_checkpoint: str = "", force: bool = False, summarizer: Optional[SummarizerFn] = None,
        emit_event: Optional[CompactionEmitter] = None,
    ) -> CompactionResult:
        """Tier 2: user-boundary structured summary compaction updating the same `session_id` in-place."""
        msg_list = [copy.deepcopy(m) for m in messages]
        threshold = int(max(1, context_window) * self.tier2_window_ratio)
        if not force and estimate_tokens(msg_list, system_prompt) < threshold:
            return CompactionResult(False, 2, msg_list)
        groups = split_user_groups(msg_list)
        keep_n = self.tier2_keep_recent_turns
        if len(groups) <= keep_n:
            if not force or len(groups) <= 1:
                return CompactionResult(False, 2, msg_list)
            keep_n = 1
        older_msgs = [m for grp in groups[:-keep_n] for m in grp]
        recent_msgs = [m for grp in groups[-keep_n:] for m in grp]
        if not older_msgs:
            return CompactionResult(False, 2, msg_list)

        cid, fn = f"cmp-{uuid.uuid4().hex[:12]}", summarizer or self._default_summarizer
        await self._emit(emit_event, chat_id, cid, "started", turn_id)
        try:
            summary = (
                (await fn(older_msgs, existing_checkpoint)).strip()
                if fn is not None
                else await self._default_summarize(older_msgs, existing_checkpoint, session_id=session_id)
            )
            if not summary:
                raise RuntimeError("Empty compaction checkpoint returned")
            if db is not None and session_id and callable(getattr(db, "compact_session_messages", None)):
                ids = [int(m["id"]) for m in older_msgs if isinstance(m.get("id"), int)]
                if ids:
                    db.compact_session_messages(session_id, upto_message_id=max(ids), summary_checkpoint=summary)
                elif callable(getattr(db, "update_session", None)):
                    db.update_session(session_id, summary_checkpoint=summary)
        except asyncio.CancelledError:
            await self._emit(emit_event, chat_id, cid, "cancelled", turn_id)
            raise
        except Exception as exc:
            logger.warning("Tier-2 compaction failed (%s)", type(exc).__name__)
            await self._emit(emit_event, chat_id, cid, "failed", turn_id)
            return CompactionResult(False, 2, msg_list, compaction_id=cid)
        await self._emit(emit_event, chat_id, cid, "succeeded", turn_id)
        return CompactionResult(
            True, 2, recent_msgs, summary_checkpoint=summary,
            dropped_messages=len(older_msgs), compaction_id=cid,
        )

    async def tier3_emergency_drop(
        self, messages: Sequence[dict[str, Any]], *, db: Any = None, session_id: str = "", chat_id: str = "",
        turn_id: Optional[str] = None, emit_event: Optional[CompactionEmitter] = None,
    ) -> CompactionResult:
        """Tier 3: deterministic drop of oldest complete user Q&A group on HTTP 400/413 overflow."""
        del session_id
        msg_list = [copy.deepcopy(m) for m in messages]
        groups = split_user_groups(msg_list)
        if len(groups) <= 1:
            return CompactionResult(False, 3, msg_list)
        cid = f"cmp-{uuid.uuid4().hex[:12]}"
        await self._emit(emit_event, chat_id, cid, "started", turn_id)
        dropped, remaining = groups[0], [m for grp in groups[1:] for m in grp]
        if db is not None and callable(getattr(db, "deactivate_messages", None)):
            if ids := [int(m["id"]) for m in dropped if isinstance(m.get("id"), int)]:
                db.deactivate_messages(ids)
        await self._emit(emit_event, chat_id, cid, "succeeded", turn_id)
        return CompactionResult(True, 3, remaining, dropped_messages=len(dropped), compaction_id=cid)

    @staticmethod
    async def _default_summarize(
        older_msgs: list[dict[str, Any]], existing_checkpoint: str, *, session_id: str = "",
    ) -> str:
        from muse.llm.auxiliary import async_call_llm

        lines = [f"[Previous Checkpoint]\n{existing_checkpoint.strip()}"] if existing_checkpoint.strip() else []
        for msg in older_msgs:
            content = msg.get("content")
            if isinstance(content, list):
                txt = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("text"))
            else:
                txt = str(content or "")
            if txt.strip():
                lines.append(f"{msg.get('role') or 'unknown'}: {txt.strip()[:1200]}")
        prompt = (
            "Summarize the conversation history into a concise structured checkpoint preserving: "
            "1) user goals & preferences, 2) key facts/decisions/file paths, "
            "3) unfinished tasks or blockers. Output concise bullet points only."
        )
        resp = await async_call_llm(
            task="compression",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": "\n".join(lines)[:24_000]}],
            max_tokens=1200, temperature=0.2, reasoning_config={"enabled": False}, session_id=session_id,
        )
        return (resp.choices[0].message.content or "").strip() if resp.choices else ""


__all__ = [
    "ActorKind", "CancelScope", "CompactionPhase", "CompactionResult", "ContextCompactor", "MEMORY_DELIMITER",
    "PromptAssembler", "TurnExecutionContext", "bind_turn_context", "build_compaction_event",
    "estimate_message_tokens", "estimate_text_tokens", "estimate_tokens", "get_turn_context",
    "load_memory_entries", "load_skill_index", "reset_turn_context", "run_in_turn_thread",
    "split_user_groups", "turn_context_scope",
]
