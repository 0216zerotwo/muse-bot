"""Scheduled tasks for the 任务 page: Hermes cron jobs in the shape the app lists.

The app shows what will run and when, and can pause, resume, delete or run one
now. 主动关心 is managed from 主动找你, so it is marked protected and left out of
the list, as nanobot's system check-in job was.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Optional

from .feed import JOB_NAME as FEED_JOB_NAME
from .routines import CHECKIN_JOB_NAME


def _ms(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError:
        return None


def _schedule(raw: Any) -> dict[str, Any]:
    schedule = raw if isinstance(raw, dict) else {}
    kind = schedule.get("kind")
    if kind == "cron":
        from muse.time_utils import get_timezone_name

        return {"kind": "cron", "expr": schedule.get("expr"), "tz": get_timezone_name() or None}
    if kind in ("interval", "every"):
        minutes = schedule.get("minutes") or 0
        return {"kind": "every", "every_ms": int(minutes) * 60_000 if minutes else schedule.get("every_ms")}
    if kind in ("once", "at"):
        return {"kind": "at", "at_ms": _ms(schedule.get("run_at") or schedule.get("at"))}
    return {"kind": str(kind or "cron"), "expr": schedule.get("display") or schedule.get("expr")}


_UPCOMING_HORIZON_S = 48 * 3600
_UPCOMING_LIMIT = 24


def upcoming(job: dict[str, Any], now: Optional[float] = None) -> list[int]:
    """When it runs next, over the coming two days (ms), by Hermes's own clock rules: the iPhone app
    gets no push, so it sets its own notifications for these times."""
    from muse.cron.jobs import compute_next_run

    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    if not job.get("enabled", True) or not schedule:
        return []
    now = time.time() if now is None else now
    times: list[int] = []
    at = job.get("next_run_at")
    for _ in range(_UPCOMING_LIMIT * 2):
        ms = _ms(at)
        if ms is None or ms / 1000 > now + _UPCOMING_HORIZON_S or len(times) >= _UPCOMING_LIMIT:
            break
        if ms / 1000 > now:
            times.append(ms)
        if schedule.get("kind") == "once":
            break
        at = compute_next_run(schedule, last_run_at=at)
    return times


def notice(job: dict[str, Any]) -> Optional[str]:
    """What the phone says when it runs (a reminder's name, without its "(…)" note), or None for jobs
    that don't speak to the user on time: 动态 posts and 主动关心 (which may well stay silent)."""
    name = str(job.get("name") or "")
    if job_chat(job) is None or name in (FEED_JOB_NAME, CHECKIN_JOB_NAME):
        return None
    short = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", name).strip()
    return short or name or "定时任务"


def job_chat(job: dict[str, Any]) -> Optional[str]:
    """The companion chat a job reports to: named (``companion:<chat>``), or the chat it was set up
    in (``origin``, as the scheduling tool saves a job made in a chat)."""
    deliver = str(job.get("deliver") or "")
    if deliver.startswith("companion:"):
        return deliver.split(":", 1)[1] or None
    origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
    if deliver == "origin" and origin.get("platform") == "companion":
        return str(origin.get("chat_id") or "") or None
    return None


def _owner(job: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The helper a job belongs to: one it set up from its own chat reports back there."""
    chat = job_chat(job)
    if not chat:
        return None
    from .helpers import helper_for_chat

    helper = helper_for_chat(chat)
    return {"id": helper["id"], "name": helper["name"], "emoji": helper.get("emoji") or ""} if helper else None


# Hermes keeps each run's report as cron/output/<job id>/<YYYY-MM-DD_HH-MM-SS>.md.
_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_KEEP_RUNS = 20


def _run_files(job_id: str) -> list[Any]:
    from muse.cron.jobs import get_cron_output_dir

    folder = get_cron_output_dir() / job_id
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.md") if _RUN_ID.match(p.stem)), key=lambda p: p.stem, reverse=True)


def _runs(job_id: str) -> list[dict[str, Any]]:
    """Its last runs, newest first (运行记录): when, and whether it worked."""
    runs = []
    for path in _run_files(job_id)[:_KEEP_RUNS]:
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:200].split("\n", 1)[0]
            at = int(datetime.strptime(path.stem, "%Y-%m-%d_%H-%M-%S").timestamp() * 1000)
        except (OSError, ValueError):
            continue
        runs.append({"id": path.stem, "at_ms": at, "status": "error" if "(FAILED)" in head else "ok"})
    return runs


def run_report(job_id: str, run_id: str) -> dict[str, Any]:
    """What one run answered (its ``## Response``), or why it failed (``## Error``)."""
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ValueError("没有这次运行")
    path = next((p for p in _run_files(str(job_id)) if p.stem == run_id), None)
    if path is None:
        raise ValueError("没有这次运行")
    text = path.read_text(encoding="utf-8", errors="replace")
    for title, status in (("## Response", "ok"), ("## Error", "error")):
        if title in text:
            body = text.split(title, 1)[1].strip()
            silent = status == "ok" and body == "[SILENT]"
            return {"status": status, "text": "" if silent else body, "silent": silent}
    return {"status": "ok", "text": "", "silent": False}


def _job(job: dict[str, Any]) -> dict[str, Any]:
    owner = _owner(job)
    return {
        **({"owner": owner} if owner else {}),
        "id": job["id"],
        "name": job.get("name") or "定时任务",
        "enabled": bool(job.get("enabled", True)),
        "protected": job.get("name") in (CHECKIN_JOB_NAME, FEED_JOB_NAME),
        "kind": "cron",
        "created_at_ms": _ms(job.get("created_at")),
        "schedule": _schedule(job.get("schedule")),
        "payload": {"message": job.get("prompt") or "", "kind": "agent_turn"},
        "state": {
            "next_run_at_ms": _ms(job.get("next_run_at")),
            "last_run_at_ms": _ms(job.get("last_run_at")),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
        },
        "runs": _runs(job["id"]),
        # The phone's own notifications for it (the app has no push): what to say, and when.
        "notice": notice(job),
        "upcoming_ms": upcoming(job) if notice(job) else [],
    }


def list_automations() -> dict[str, Any]:
    from muse.cron.jobs import list_jobs

    jobs = [_job(job) for job in list_jobs(include_disabled=True) if isinstance(job, dict) and job.get("id")]
    jobs.sort(key=lambda j: j["state"]["next_run_at_ms"] or 2**62)
    return {"jobs": jobs}


def run_action(action: str, job_id: str) -> dict[str, Any]:
    """``enable`` (resume), ``disable`` (pause), ``delete`` or ``run`` (now)."""
    import contextlib
    from muse.cron import jobs

    job = jobs.get_job(job_id) if isinstance(job_id, str) else None
    if job is None:
        raise ValueError("这个任务已经不在了")
    if action == "enable":
        jobs.resume_job(job_id)
    elif action == "disable":
        jobs.pause_job(job_id, reason="在 App 里暂停")
    elif action == "delete":
        if job.get("name") in (CHECKIN_JOB_NAME, FEED_JOB_NAME):
            raise ValueError("系统内置任务不能删除，请在对应设置里关闭")
        jobs.remove_job(job_id)
        with contextlib.suppress(Exception):
            from . import plans, triggers

            triggers.clear_job_ref(job_id)
            plans.clear_job_ref(job_id)
    elif action == "run":
        jobs.trigger_job(job_id)
    else:
        raise ValueError(f"不支持的操作：{action}")
    return list_automations()
