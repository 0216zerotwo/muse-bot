"""Per-session active context window utilization preview (`GET /api/sessions/{key}/context`)."""

from __future__ import annotations

import contextlib
from typing import Any, Optional

from muse.context import (
    TIER1_TOKEN_THRESHOLD,
    TIER2_WINDOW_RATIO,
    ContextCompactor,
    PromptAssembler,
    estimate_tokens,
)
from muse.turn_actor import db_rows_to_llm_messages

from . import helpers, models

DEFAULT_CONTEXT_WINDOW = 128_000


def _resolve_model_and_window(
    adapter: Any,
    chat_id: str,
    meta: dict[str, Any],
) -> tuple[str, int, Optional[str], Optional[str]]:
    model_preset = str(meta.get("model_preset") or "").strip() or None
    reasoning_effort = str(meta.get("reasoning_effort") or "").strip() or None
    model_name = ""
    context_window = DEFAULT_CONTEXT_WINDOW
    window_resolved = False

    if model_preset or reasoning_effort:
        with contextlib.suppress(Exception):
            ov = models.resolve_session_override(meta)
            if isinstance(ov, dict):
                if ov.get("model"):
                    model_name = str(ov["model"])
                if isinstance(ov.get("context_length"), int) and ov["context_length"] > 0:
                    context_window = int(ov["context_length"])
                    window_resolved = True
                if reasoning_effort is None and ov.get("reasoning_effort"):
                    reasoning_effort = str(ov["reasoning_effort"])

    if not model_name and meta.get("helper"):
        with contextlib.suppress(Exception):
            hrow = helpers.helper_for_chat(chat_id)
            h_preset = str((hrow or {}).get("model") or "").strip()
            if h_preset:
                p = (models.read_models().get("presets") or {}).get(h_preset) or {}
                if p.get("model"):
                    model_name = str(p["model"])
                if not window_resolved and isinstance(p.get("context_window_tokens"), int) and p["context_window_tokens"] > 0:
                    context_window = int(p["context_window_tokens"])
                    window_resolved = True

    if not model_name or not window_resolved:
        with contextlib.suppress(Exception):
            mdata = models.read_models()
            active_name = models.chat_preset(mdata)
            p = (mdata.get("presets") or {}).get(active_name or "") or {}
            if not model_name and p.get("model"):
                model_name = str(p["model"])
            if not window_resolved and isinstance(p.get("context_window_tokens"), int) and p["context_window_tokens"] > 0:
                context_window = int(p["context_window_tokens"])
                window_resolved = True

    if not model_name:
        with contextlib.suppress(Exception):
            from muse.config_store import load_config

            cfg_model = load_config().get("model")
            if isinstance(cfg_model, dict):
                model_name = str(cfg_model.get("default") or cfg_model.get("model") or "")
            elif isinstance(cfg_model, str):
                model_name = cfg_model.strip()

    engine = getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)
    actors = getattr(engine, "_actors", None) if engine is not None else None
    actor = actors.get(chat_id) if isinstance(actors, dict) else None
    if actor is not None:
        if not model_name and getattr(actor, "model_name", ""):
            model_name = str(actor.model_name)
        actor_win = getattr(actor, "context_window", None)
        if not window_resolved and isinstance(actor_win, int) and actor_win > 0:
            context_window = actor_win

    return model_name, context_window, model_preset, reasoning_effort


def _fallback_store_tokens(adapter: Any, chat_id: str) -> int:
    msgs: list[dict[str, Any]] = []
    for ev in adapter.store.events(chat_id):
        kind = ev.get("event")
        if kind not in ("user_message", "message"):
            continue
        if ev.get("kind") == "tool_hint" or (ev.get("source") or {}).get("kind") == "notice":
            continue
        text = str(ev.get("text") or "").strip()
        if not text:
            continue
        msgs.append({"role": "user" if kind == "user_message" else "assistant", "content": text})
    return estimate_tokens(msgs) if msgs else 0


@contextlib.contextmanager
def _borrow_existing_db(adapter: Any):
    engine = getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)
    if engine is not None:
        existing_db = getattr(engine, "_db", None)
        if existing_db is not None and not getattr(existing_db, "_closed", False):
            yield existing_db
            return
    adapter_db = getattr(adapter, "db", None)
    if adapter_db is not None and not getattr(adapter_db, "_closed", False):
        yield adapter_db
        return
    from muse.paths import get_muse_db_path
    from muse.runtime import peek_shared_db, use_shared_db

    if peek_shared_db() is not None or get_muse_db_path().is_file():
        with use_shared_db() as shared_db:
            yield shared_db
            return
    yield None


def get_session_context(adapter: Any, chat_id: str) -> dict[str, Any]:
    """Compute active context token usage, thresholds, and effective model config for ``chat_id``."""
    meta = adapter.store.meta(chat_id)
    model_name, context_window, model_preset, reasoning_effort = _resolve_model_and_window(adapter, chat_id, meta)
    engine = getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)

    estimated_tokens = 0
    has_summary_checkpoint = False
    found_db_session = False

    with _borrow_existing_db(adapter) as db:
        if db is not None and callable(getattr(db, "get_active_session", None)):
            source = "cron" if str(chat_id).startswith("cron:") else "companion"
            sess = db.get_active_session(chat_id, source=source)
            if isinstance(sess, dict) and sess.get("id"):
                found_db_session = True
                sid = str(sess["id"])
                checkpoint = str(sess.get("summary_checkpoint") or "").strip()
                has_summary_checkpoint = bool(checkpoint)
                if not model_name and sess.get("model"):
                    model_name = str(sess["model"])
                assembler = getattr(engine, "_assembler", None) or PromptAssembler()
                has_frozen_soul = isinstance((sess.get("metadata") or {}).get("soul_snapshot"), str)
                # Avoid mutating sessions.metadata_json on a read-only GET when soul_snapshot is absent.
                sys_prompt = assembler.assemble(
                    session_id=sid if has_frozen_soul else "",
                    chat_id=chat_id,
                    db=db,
                    summary_checkpoint=checkpoint or None,
                )
                rows = db.get_messages(sid, active_only=True)
                llm_msgs = db_rows_to_llm_messages(rows)
                pruned = ContextCompactor().tier1_prune(llm_msgs, system_prompt=sys_prompt).messages
                estimated_tokens = estimate_tokens(pruned, sys_prompt)

    if not found_db_session:
        estimated_tokens = _fallback_store_tokens(adapter, chat_id)

    tier1_threshold = TIER1_TOKEN_THRESHOLD
    tier2_threshold = int(context_window * TIER2_WINDOW_RATIO)
    usage_ratio = round(estimated_tokens / context_window, 4) if context_window > 0 else 0.0

    return {
        "chat_id": chat_id,
        "estimated_tokens": estimated_tokens,
        "context_window": context_window,
        "usage_ratio": usage_ratio,
        "tier1_threshold": tier1_threshold,
        "tier2_threshold": tier2_threshold,
        "has_summary_checkpoint": has_summary_checkpoint,
        "model": model_name,
        "model_preset": model_preset,
        "reasoning_effort": reasoning_effort,
    }


__all__ = ["DEFAULT_CONTEXT_WINDOW", "get_session_context"]
