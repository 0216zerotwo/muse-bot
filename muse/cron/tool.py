"""Built-in ``cronjob_manage`` tool for in-chat scheduling and reminders."""

from __future__ import annotations

import json
from typing import Any, Optional

from muse.cron import jobs as cron_jobs


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _resolve_origin(args: dict[str, Any], kwargs: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw_origin = args.get("origin")
    if isinstance(raw_origin, dict) and raw_origin.get("platform"):
        return dict(raw_origin)
    chat_id = str(kwargs.get("chat_id") or args.get("chat_id") or "").strip()
    if not chat_id:
        ctx = kwargs.get("ctx")
        if ctx is None:
            try:
                from muse.context import get_turn_context

                ctx = get_turn_context()
            except Exception:
                ctx = None
        if ctx is not None:
            chat_id = str(
                getattr(ctx, "chat_id", None) or getattr(ctx, "facing_chat_id", None) or ""
            ).strip()
    platform = str(kwargs.get("platform") or args.get("platform") or "companion").strip()
    if chat_id:
        return {"platform": platform, "chat_id": chat_id}
    return None


def _handle_cronjob_manage(args: dict[str, Any], **kwargs: Any) -> str:
    """Unified ``cronjob_manage`` tool handler (create/list/get/pause/resume/remove/run/update)."""
    action = str(args.get("action") or "list").strip().lower()

    if action in ("create", "schedule", "add"):
        prompt = str(args.get("prompt") or "").strip()
        schedule = args.get("schedule")
        if not prompt or not schedule:
            return _json({"success": False, "error": "prompt and schedule are required"})
        origin = _resolve_origin(args, kwargs)
        deliver = args.get("deliver")
        if deliver is None:
            deliver = "origin" if origin else "local"
        try:
            job = cron_jobs.create_job(
                prompt=prompt,
                schedule=schedule,
                name=args.get("name"),
                repeat=args.get("repeat"),
                deliver=str(deliver),
                origin=origin,
                attach_to_session=bool(args.get("attach_to_session", True)),
                enabled_toolsets=args.get("enabled_toolsets"),
                monitor_url=args.get("monitor_url"),
                failure_deliver=str(args.get("failure_deliver") or "local"),
            )
        except ValueError as exc:
            return _json({"success": False, "error": str(exc)})
        return _json({"success": True, "job": job})

    if action == "list":
        include_disabled = bool(args.get("include_disabled", True))
        return _json({"success": True, "jobs": cron_jobs.list_jobs(include_disabled=include_disabled)})

    job_id = str(args.get("job_id") or args.get("id") or "").strip()
    if not job_id:
        return _json({"success": False, "error": "job_id is required"})

    if action == "get":
        job = cron_jobs.get_job(job_id)
        return _json({"success": True, "job": job} if job else {"success": False, "error": "Job not found"})

    if action == "pause":
        job = cron_jobs.pause_job(job_id, reason=str(args.get("reason") or ""))
        return _json({"success": True, "job": job} if job else {"success": False, "error": "Job not found"})

    if action == "resume":
        job = cron_jobs.resume_job(job_id)
        return _json({"success": True, "job": job} if job else {"success": False, "error": "Job not found"})

    if action in ("remove", "delete"):
        ok = cron_jobs.remove_job(job_id)
        return _json({"success": ok, **({} if ok else {"error": "Job not found"})})

    if action in ("run", "trigger"):
        job = cron_jobs.trigger_job(job_id, extra_prompt=args.get("extra_prompt"))
        return _json({"success": True, "job": job} if job else {"success": False, "error": "Job not found"})

    if action == "update":
        updatable = (
            "name", "prompt", "schedule", "deliver",
            "enabled_toolsets", "attach_to_session", "monitor_url",
        )
        changes = {k: args[k] for k in updatable if k in args and args[k] is not None}
        try:
            job = cron_jobs.update_job(job_id, changes)
        except ValueError as exc:
            return _json({"success": False, "error": str(exc)})
        return _json({"success": True, "job": job} if job else {"success": False, "error": "Job not found"})

    return _json({"success": False, "error": f"Unsupported action: {action}"})


CRONJOB_MANAGE_SCHEMA: dict[str, Any] = {
    "name": "cronjob_manage",
    "description": (
        "Create, list, pause, resume, update, trigger, or delete scheduled reminders and recurring jobs. "
        "Supports cron expressions ('0 9 * * *'), intervals ('every 30m'), or one-shot timestamps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "pause", "resume", "update", "remove", "run"],
                "description": "Action to perform on scheduled jobs.",
            },
            "job_id": {
                "type": "string",
                "description": "Target job ID for get/pause/resume/update/remove/run.",
            },
            "name": {"type": "string", "description": "Short human-readable job name."},
            "prompt": {"type": "string", "description": "Prompt to run when the job fires."},
            "schedule": {
                "description": (
                    "Cron string ('0 9 * * *'), interval ('every 30m'), ISO timestamp, or structured dict "
                    "({kind: 'cron'|'interval'|'once', ...})."
                ),
            },
            "deliver": {
                "type": "string",
                "description": "Delivery target ('origin', 'local', or 'companion:<id>').",
            },
            "attach_to_session": {"type": "boolean", "default": True},
            "enabled_toolsets": {"type": "array", "items": {"type": "string"}},
            "monitor_url": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
    },
}


def register_cron_tools(registry: Any) -> None:
    """Register `cronjob_manage` onto `registry`."""
    registry.register(
        name="cronjob_manage",
        toolset="cronjob",
        description=CRONJOB_MANAGE_SCHEMA["description"],
        parameters=CRONJOB_MANAGE_SCHEMA["parameters"],
        handler=_handle_cronjob_manage,
        is_async=False,
        emoji="⏰",
    )


__all__ = ["CRONJOB_MANAGE_SCHEMA", "_handle_cronjob_manage", "register_cron_tools"]
