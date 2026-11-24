"""Cron job storage 100% compatible with ``<HOME>/cron/jobs.json``, ``.jobs.lock``, and output reports."""

from __future__ import annotations

import contextlib
import copy
import errno
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from croniter import croniter

from muse.paths import get_muse_home
from muse.time_utils import get_timezone, now as muse_now

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_JOBS_LOCK = threading.RLock()
_THREAD_HELD = threading.local()
_LOCK_CONTENTION_ERRNOS = frozenset(
    c for c in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", None)) if c is not None
)
_WINDOWS_REPLACE_ERRS = frozenset({5, 32, 33})

_CRON_DIR_CTX: ContextVar[Optional[Path]] = ContextVar("_CRON_DIR_CTX", default=None)

_WAKE_LOCK = threading.Lock()
_WAKE_CALLBACKS: list[Callable[[], None]] = []

_EVERY_RE = re.compile(
    r"^(?:every\s+)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.I,
)
_IN_RE = re.compile(
    r"^in\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.I,
)


def register_wake_callback(callback: Callable[[], None]) -> None:
    """Register a cross-thread wake hook invoked immediately by ``trigger_job``."""
    with _WAKE_LOCK:
        if callback not in _WAKE_CALLBACKS:
            _WAKE_CALLBACKS.append(callback)


def unregister_wake_callback(callback: Callable[[], None]) -> None:
    with _WAKE_LOCK:
        if callback in _WAKE_CALLBACKS:
            _WAKE_CALLBACKS.remove(callback)


def notify_scheduler_wake() -> None:
    """Wake all registered schedulers immediately (thread-safe)."""
    with _WAKE_LOCK:
        callbacks = list(_WAKE_CALLBACKS)
    for cb in callbacks:
        try:
            cb()
        except Exception as exc:
            logger.debug("muse.cron: wake callback failed (%s)", type(exc).__name__)


@contextlib.contextmanager
def use_cron_store(home: str | Path):
    """Scope cron storage to ``<home>/cron`` inside the current context."""
    token = _CRON_DIR_CTX.set(Path(home).expanduser().resolve() / "cron")
    try:
        yield
    finally:
        _CRON_DIR_CTX.reset(token)


def get_cron_dir() -> Path:
    override = _CRON_DIR_CTX.get()
    if override is not None:
        return override
    return get_muse_home().resolve() / "cron"


def get_jobs_file() -> Path:
    return get_cron_dir() / "jobs.json"


def get_cron_output_dir() -> Path:
    return get_cron_dir() / "output"


def _kernel_lock(fh: Any, acquire: bool) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _jobs_lock(timeout_s: float = 15.0):
    with _JOBS_LOCK:
        lock_path = get_cron_dir() / ".jobs.lock"
        held: dict[str, int] = getattr(_THREAD_HELD, "depths", None) or {}
        _THREAD_HELD.depths = held
        key = os.path.normcase(str(lock_path))
        if held.get(key, 0) > 0:
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
                if held[key] <= 0:
                    held.pop(key, None)
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
        acquired = False
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\n")
                fh.flush()
            deadline, delay = time.monotonic() + timeout_s, 0.01
            while True:
                try:
                    _kernel_lock(fh, acquire=True)
                    acquired = True
                    break
                except OSError as exc:
                    contended = (
                        isinstance(exc, BlockingIOError)
                        or getattr(exc, "winerror", None) in (33, 36)
                        or exc.errno in _LOCK_CONTENTION_ERRNOS
                    )
                    if not contended:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for cron lock {lock_path}") from exc
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.05)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    _kernel_lock(fh, acquire=False)
            fh.close()


def _atomic_write_json(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".jobs_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_name, target)
                break
            except OSError as exc:
                win_retry = (
                    os.name == "nt"
                    and getattr(exc, "winerror", None) in _WINDOWS_REPLACE_ERRS
                    and attempt < 4
                )
                if win_retry:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(target, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        tz = get_timezone()
        dt = dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()
    return dt


def parse_schedule(schedule: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a schedule string or dict into canonical ``cron`` / ``interval`` / ``once`` dicts."""
    if isinstance(schedule, dict):
        kind = str(schedule.get("kind") or "").strip().lower()
        if kind == "cron":
            expr = str(schedule.get("expr") or "").strip()
            if not expr or not croniter.is_valid(expr):
                raise ValueError(f"Invalid cron expression: {expr!r}")
            return {"kind": "cron", "expr": expr, "display": str(schedule.get("display") or expr)}
        if kind in ("interval", "every"):
            mins = schedule.get("minutes")
            if mins is None and schedule.get("every_ms"):
                mins = max(1, int(round(int(schedule["every_ms"]) / 60_000)))
            minutes = int(mins or 0)
            if minutes <= 0:
                raise ValueError(f"Interval minutes must be > 0 (got {mins!r})")
            return {
                "kind": "interval",
                "minutes": minutes,
                "display": str(schedule.get("display") or f"every {minutes}m"),
            }
        if kind in ("once", "at"):
            run_at = str(schedule.get("run_at") or schedule.get("at") or "").strip()
            dt = _parse_dt(run_at)
            iso = dt.isoformat()
            return {
                "kind": "once",
                "run_at": iso,
                "display": str(schedule.get("display") or f"once at {iso}"),
            }
        raise ValueError(f"Unsupported schedule kind: {kind!r}")

    text = str(schedule or "").strip()
    if not text:
        raise ValueError("Schedule cannot be empty")

    if m := _EVERY_RE.match(text):
        amount, unit = int(m.group(1)), m.group(2).lower()
        if amount <= 0:
            raise ValueError(f"Interval must be positive: {text!r}")
        if unit.startswith("d"):
            minutes = amount * 24 * 60
        elif unit.startswith("h"):
            minutes = amount * 60
        else:
            minutes = amount
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}

    if m := _IN_RE.match(text):
        amount, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("d"):
            delta = timedelta(days=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        elif unit.startswith("m"):
            delta = timedelta(minutes=amount)
        else:
            delta = timedelta(seconds=amount)
        iso = (muse_now() + delta).isoformat(timespec="seconds")
        return {"kind": "once", "run_at": iso, "display": f"once at {iso}"}

    parts = text.split()
    if len(parts) == 5 and croniter.is_valid(text):
        return {"kind": "cron", "expr": text, "display": text}

    try:
        dt = _parse_dt(text)
        iso = dt.isoformat()
        return {"kind": "once", "run_at": iso, "display": f"once at {iso}"}
    except ValueError as exc:
        raise ValueError(f"Could not parse schedule: {text!r}") from exc


def compute_next_run(schedule: dict[str, Any], last_run_at: Optional[str] = None) -> Optional[str]:
    """Compute the next ISO-8601 run timestamp for a parsed schedule dict."""
    if not isinstance(schedule, dict):
        return None
    kind = str(schedule.get("kind") or "").lower()
    base = _parse_dt(last_run_at) if last_run_at else muse_now()
    if kind == "cron":
        expr = str(schedule.get("expr") or "").strip()
        if not expr:
            return None
        it = croniter(expr, base)
        nxt: datetime = it.get_next(datetime)
        if nxt.tzinfo is None and base.tzinfo is not None:
            nxt = nxt.replace(tzinfo=base.tzinfo)
        return nxt.isoformat()
    if kind in ("interval", "every"):
        minutes = int(schedule.get("minutes") or 0)
        if minutes <= 0 and schedule.get("every_ms"):
            minutes = max(1, int(round(int(schedule["every_ms"]) / 60_000)))
        if minutes <= 0:
            return None
        return (base + timedelta(minutes=minutes)).isoformat()
    if kind in ("once", "at"):
        if last_run_at:
            return None
        raw = str(schedule.get("run_at") or schedule.get("at") or "").strip()
        return _parse_dt(raw).isoformat() if raw else None
    return None


def load_jobs() -> list[dict[str, Any]]:
    """Load all jobs from ``<HOME>/cron/jobs.json``."""
    jobs_file = get_jobs_file()
    if not jobs_file.is_file():
        return []
    try:
        raw = json.loads(jobs_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("muse.cron: failed to read jobs.json (%s)", type(exc).__name__)
        return []
    if isinstance(raw, list):
        return [j for j in raw if isinstance(j, dict)]
    if isinstance(raw, dict):
        items = raw.get("jobs")
        if isinstance(items, list):
            return [j for j in items if isinstance(j, dict)]
        if isinstance(items, dict):
            return [{**v, "id": v.get("id") or k} for k, v in items.items() if isinstance(v, dict)]
    return []


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    """Persist ``jobs`` atomically to ``<HOME>/cron/jobs.json`` under ``.jobs.lock``."""
    with _jobs_lock():
        _atomic_write_json(
            get_jobs_file(),
            {"jobs": list(jobs), "updated_at": muse_now().isoformat()},
        )


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    jobs = load_jobs()
    if include_disabled:
        return jobs
    return [j for j in jobs if j.get("enabled", True) is not False]


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    for job in load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def create_job(
    prompt: Optional[str],
    schedule: str | dict[str, Any],
    *,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[dict[str, Any]] = None,
    skill: Optional[str] = None,
    skills: Optional[list[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    script: Optional[str] = None,
    enabled_toolsets: Optional[list[str]] = None,
    attach_to_session: Optional[bool] = None,
    monitor_script: Optional[str] = None,
    monitor_url: Optional[str] = None,
    failure_deliver: Optional[str] = None,
    paused: bool = False,
    paused_reason: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """Create and persist a new cron job record compatible with Hermes ``jobs.json``."""
    parsed = parse_schedule(schedule)
    prompt_text = str(prompt or "").strip()
    if not prompt_text and not script and not skills and not skill:
        raise ValueError("Cron job requires a prompt, script, or skill")
    if deliver is None:
        deliver = "origin" if origin else "local"
    norm_skills = list(skills) if isinstance(skills, list) else ([skill] if skill else [])
    if isinstance(repeat, dict):
        repeat_obj: dict[str, Any] = {
            "times": repeat.get("times"),
            "completed": int(repeat.get("completed") or 0),
        }
    else:
        eff_repeat = 1 if (parsed["kind"] == "once" and repeat is None) else repeat
        repeat_obj = {"times": eff_repeat, "completed": 0}
    now_iso = muse_now().isoformat()
    next_run = None if paused else compute_next_run(parsed)
    sched_display = str(parsed.get("display") or (schedule if isinstance(schedule, str) else ""))
    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "name": str(name or prompt_text[:50] or "定时任务").strip(),
        "prompt": prompt_text,
        "schedule": parsed,
        "schedule_display": sched_display,
        "repeat": repeat_obj,
        "deliver": deliver,
        "origin": copy.deepcopy(origin) if isinstance(origin, dict) else None,
        "skills": norm_skills,
        "skill": norm_skills[0] if norm_skills else None,
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "script": script,
        "enabled_toolsets": list(enabled_toolsets) if enabled_toolsets is not None else None,
        "attach_to_session": bool(attach_to_session) if isinstance(attach_to_session, bool) else False,
        "monitor_script": monitor_script,
        "monitor_url": monitor_url,
        "monitor_state": None,
        "failure_deliver": failure_deliver or "local",
        "enabled": not paused,
        "state": "paused" if paused else "scheduled",
        "paused_at": now_iso if paused else None,
        "paused_reason": paused_reason if paused else None,
        "created_at": now_iso,
        "next_run_at": next_run,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "run_count": 0,
    }
    for k, v in extra.items():
        job.setdefault(k, v)

    with _jobs_lock():
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    notify_scheduler_wake()
    return job


def update_job(job_id: str, changes: dict[str, Any]) -> Optional[dict[str, Any]]:
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            updated = dict(changes)
            if "schedule" in updated and not isinstance(updated["schedule"], dict):
                updated["schedule"] = parse_schedule(updated["schedule"])
                updated.setdefault("next_run_at", compute_next_run(updated["schedule"]))
            job.update(updated)
            save_jobs(jobs)
            return job
    return None


def remove_job(job_id: str) -> bool:
    with _jobs_lock():
        jobs = load_jobs()
        kept = [j for j in jobs if j.get("id") != job_id]
        if len(kept) == len(jobs):
            return False
        save_jobs(kept)
    return True


def pause_job(job_id: str, reason: str = "") -> Optional[dict[str, Any]]:
    return update_job(
        job_id,
        {
            "enabled": False,
            "state": "paused",
            "paused_at": muse_now().isoformat(),
            "paused_reason": reason or None,
        },
    )


def resume_job(job_id: str) -> Optional[dict[str, Any]]:
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            job["enabled"] = True
            job["state"] = "scheduled"
            job["paused_at"] = None
            job["paused_reason"] = None
            if not job.get("next_run_at") and isinstance(job.get("schedule"), dict):
                job["next_run_at"] = compute_next_run(job["schedule"])
            save_jobs(jobs)
            notify_scheduler_wake()
            return job
    return None


def trigger_job(job_id: str, extra_prompt: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Mark ``job_id`` immediately due and wake the running ``CronScheduler`` in milliseconds."""
    with _jobs_lock():
        jobs = load_jobs()
        target: Optional[dict[str, Any]] = None
        for job in jobs:
            if job.get("id") == job_id:
                manual_run_at = muse_now().isoformat()
                job["next_run_at"] = manual_run_at
                job["manual_run_at"] = manual_run_at
                job["manual_run_prompt"] = str(extra_prompt) if extra_prompt else None
                job["enabled"] = True
                job["state"] = "scheduled"
                job["paused_at"] = None
                job["paused_reason"] = None
                if extra_prompt:
                    job["pending_extra_prompt"] = str(extra_prompt)
                else:
                    job.pop("pending_extra_prompt", None)
                target = copy.deepcopy(job)
                save_jobs(jobs)
                break
    if target is not None:
        notify_scheduler_wake()
    return target


def get_due_jobs(now_dt: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Return enabled jobs whose ``next_run_at <= now_dt``."""
    current = now_dt if now_dt is not None else muse_now()
    due: list[dict[str, Any]] = []
    for job in load_jobs():
        if job.get("enabled", True) is False:
            continue
        raw = job.get("next_run_at")
        if not raw:
            continue
        try:
            run_dt = _parse_dt(str(raw))
        except ValueError:
            continue
        if run_dt <= current:
            due.append(job)
    return due


def save_run_output(
    job: dict[str, Any],
    *,
    success: bool,
    output: str = "",
    error: Optional[str] = None,
    silent: bool = False,
    run_at: Optional[datetime] = None,
) -> Path:
    """Write ``cron/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md`` in ``automations.py`` format."""
    job_id = str(job.get("id") or "unknown")
    name = str(job.get("name") or "定时任务")
    prompt = str(job.get("prompt") or "")
    stamp_dt = run_at if run_at is not None else muse_now()
    stamp = stamp_dt.strftime("%Y-%m-%d_%H-%M-%S")
    folder = get_cron_output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}.md"
    suffix = "" if success else " (FAILED)"
    if success:
        body = "[SILENT]" if silent else (output or "")
        tail = f"## Response\n\n{body}\n"
    else:
        tail = f"## Error\n\n{error or 'Unknown error'}\n"
    content = (
        f"# Cron Job: {name}{suffix}\n\n"
        f"**Job ID:** {job_id}\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"{tail}"
    )
    path.write_text(content, encoding="utf-8")
    return path


def mark_job_run(
    job_id: str,
    *,
    success: bool,
    output: str = "",
    error: Optional[str] = None,
    silent: bool = False,
    no_change: bool = False,
    run_at: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Advance ``next_run_at``, record run status, and write report markdown."""
    finished_dt = run_at if run_at is not None else muse_now()
    finished_iso = finished_dt.isoformat()
    with _jobs_lock():
        jobs = load_jobs()
        target: Optional[dict[str, Any]] = None
        for job in jobs:
            if job.get("id") != job_id:
                continue
            job["last_run_at"] = finished_iso
            job["last_status"] = "no_change" if no_change else ("ok" if success else "error")
            job["last_error"] = None if success else str(error or "")[:500]
            job["run_count"] = int(job.get("run_count") or 0) + 1
            job.pop("pending_extra_prompt", None)
            job.pop("manual_run_at", None)
            job.pop("manual_run_prompt", None)

            schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
            repeat = job.get("repeat")
            exhausted = False
            if isinstance(repeat, dict):
                completed = int(repeat.get("completed") or 0) + 1
                repeat["completed"] = completed
                times = repeat.get("times")
                if isinstance(times, int) and completed >= times:
                    exhausted = True
            elif isinstance(repeat, int) and job["run_count"] >= repeat:
                exhausted = True
            if schedule.get("kind") == "once" or exhausted:
                job["enabled"] = False
                job["state"] = "completed"
                job["next_run_at"] = None
            else:
                job["next_run_at"] = compute_next_run(schedule, last_run_at=finished_iso)
            target = copy.deepcopy(job)
            save_jobs(jobs)
            break

    if target is not None and not no_change:
        save_run_output(
            target,
            success=success,
            output=output,
            error=error,
            silent=silent,
            run_at=finished_dt,
        )
    return target


__all__ = [
    "compute_next_run",
    "create_job",
    "get_cron_dir",
    "get_cron_output_dir",
    "get_due_jobs",
    "get_job",
    "get_jobs_file",
    "list_jobs",
    "load_jobs",
    "mark_job_run",
    "notify_scheduler_wake",
    "parse_schedule",
    "pause_job",
    "register_wake_callback",
    "remove_job",
    "resume_job",
    "save_jobs",
    "save_run_output",
    "trigger_job",
    "unregister_wake_callback",
    "update_job",
    "use_cron_store",
]
