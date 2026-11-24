"""Async CronScheduler with self-trigger loopback SSRF exemption and instant cross-thread wake."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from muse.cron import jobs as cron_jobs
from muse.mcp.config import redact_error
from muse.media._common import is_safe_url
from muse.time_utils import now as muse_now

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 4000
MAX_OUTPUT_CHARS = 8000
MAX_MONITOR_BYTES = 262_144  # 256 KiB
MONITOR_TIMEOUT_S = 30.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_SNAPSHOT_FILENAME = "monitor_last_output.txt"
_SELF_TRIGGER_PREFIX = "/api/webui/companion/triggers/"
_SELF_TRIGGER_SUFFIX = "/state"

JobExecutor = Callable[[dict[str, Any], str], Awaitable[str]]
DeliveryHandler = Callable[[dict[str, Any], str], Awaitable[None]]


@dataclass
class MonitorOutcome:
    ok: bool
    changed: bool = False
    first_run: bool = False
    context_block: Optional[str] = None
    error: Optional[str] = None


def hash_monitor_output(output: str) -> str:
    """SHA-256 hash of exact UTF-8 output bytes (no normalization)."""
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def build_monitor_diff(old: str, new: str) -> str:
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return diff


def _default_self_base_url() -> str:
    try:
        from companion.modules import triggers

        base = getattr(triggers, "_base_url", "")
        if isinstance(base, str) and base.strip():
            return base.strip().rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:8766"


def _effective_port(parsed: Any) -> int:
    if parsed.port is not None:
        return int(parsed.port)
    return 443 if (parsed.scheme or "").lower() == "https" else 80


def is_exempt_self_trigger_url(url: str, self_base_url: Optional[str] = None) -> bool:
    """True ONLY when ``url`` targets this process's own trigger state endpoint."""
    try:
        target = urlsplit(str(url or "").strip())
        base = urlsplit((self_base_url or _default_self_base_url()).strip())
    except ValueError:
        return False
    if target.scheme.lower() not in ("http", "https"):
        return False
    if target.username or target.password:
        return False
    if target.scheme.lower() != base.scheme.lower():
        return False
    if (target.hostname or "").lower() != (base.hostname or "").lower():
        return False
    if _effective_port(target) != _effective_port(base):
        return False
    path = target.path or ""
    if ".." in path or not path.startswith(_SELF_TRIGGER_PREFIX) or not path.endswith(_SELF_TRIGGER_SUFFIX):
        return False
    trigger_id = path[len(_SELF_TRIGGER_PREFIX) : -len(_SELF_TRIGGER_SUFFIX)]
    return bool(trigger_id) and "/" not in trigger_id


async def fetch_monitor_url(
    url: str,
    *,
    self_base_url: Optional[str] = None,
    timeout: float = MONITOR_TIMEOUT_S,
) -> tuple[bool, str]:
    """Fetch ``monitor_url``: exempt own trigger state route (``trust_env=False``), SSRF-check all else."""
    raw_url = str(url or "").strip()
    if not raw_url.lower().startswith(("http://", "https://")):
        return False, f"monitor_url must be http(s): {raw_url!r}"

    if is_exempt_self_trigger_url(raw_url, self_base_url=self_base_url):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                headers={"User-Agent": "muse-cron-monitor"},
            ) as client:
                async with client.stream("GET", raw_url) as resp:
                    if resp.status_code in _REDIRECT_CODES:
                        return False, "monitor_url self-trigger endpoint attempted redirect"
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(65_536):
                        remaining = MAX_MONITOR_BYTES - total
                        if remaining <= 0:
                            break
                        chunks.append(chunk[:remaining])
                        total += len(chunks[-1])
            return True, b"".join(chunks).decode("utf-8", errors="replace")
        except Exception as exc:
            err = redact_error(str(exc) or type(exc).__name__)
            return False, f"monitor_url self-trigger fetch failed: {err}"

    current = raw_url
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "muse-cron-monitor"},
        ) as client:
            for _ in range(5):
                safe = await asyncio.to_thread(is_safe_url, current)
                if not safe:
                    host_hint = urlsplit(current).hostname or current[:60]
                    return False, f"monitor_url blocked by SSRF policy: {host_hint}"
                async with client.stream("GET", current) as resp:
                    if resp.status_code in _REDIRECT_CODES and resp.headers.get("location"):
                        current = urljoin(current, resp.headers["location"])
                        continue
                    resp.raise_for_status()
                    chunks = []
                    total = 0
                    async for chunk in resp.aiter_bytes(65_536):
                        remaining = MAX_MONITOR_BYTES - total
                        if remaining <= 0:
                            break
                        chunks.append(chunk[:remaining])
                        total += len(chunks[-1])
                    return True, b"".join(chunks).decode("utf-8", errors="replace")
        return False, "monitor_url exceeded redirect limit"
    except Exception as exc:
        return False, f"monitor_url fetch failed: {redact_error(str(exc) or type(exc).__name__)}"


def _snapshot_path(job_id: str):
    return cron_jobs.get_cron_output_dir() / job_id / _SNAPSHOT_FILENAME


async def check_monitor(
    job: dict[str, Any],
    *,
    self_base_url: Optional[str] = None,
) -> MonitorOutcome:
    """Evaluate a job's ``monitor_url`` and detect SHA-256 state changes."""
    job_id = str(job.get("id") or "")
    monitor_url = str(job.get("monitor_url") or "").strip()
    if not monitor_url:
        return MonitorOutcome(ok=False, error="monitor job has no monitor_url")

    ok, output = await fetch_monitor_url(monitor_url, self_base_url=self_base_url)
    if not ok:
        return MonitorOutcome(ok=False, error=output)

    new_hash = hash_monitor_output(output)
    raw_state = job.get("monitor_state")
    last_hash = raw_state.get("last_output_hash") if isinstance(raw_state, dict) else None
    if last_hash is not None and new_hash == last_hash:
        return MonitorOutcome(ok=True, changed=False)

    first_run = last_hash is None
    snap = _snapshot_path(job_id)
    old_output = ""
    if not first_run and snap.is_file():
        with contextlib.suppress(OSError):
            old_output = snap.read_text(encoding="utf-8-sig")

    shown = (
        output
        if len(output) <= MAX_OUTPUT_CHARS
        else output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
    )
    current_section = f"### Current output\n\n```\n{shown}\n```"
    if first_run:
        block = (
            "## Monitor Baseline (first run)\n\n"
            "This is the first observation of the monitored source — there is "
            "no previous output to diff against.\n\n" + current_section
        )
    else:
        diff = build_monitor_diff(old_output, output)
        block = (
            "## MONITOR CHANGE DETECTED\n\n"
            "The monitored source's output changed since the last run.\n\n"
            f"### Diff (previous -> current)\n\n```diff\n{diff}\n```\n\n" + current_section
        )

    # Persist new hash + snapshot BEFORE agent runs so failures do not re-alert forever.
    with contextlib.suppress(OSError):
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(output, encoding="utf-8")
    cron_jobs.update_job(
        job_id,
        {
            "monitor_state": {
                "last_output_hash": new_hash,
                "last_changed_at": muse_now().isoformat(),
            }
        },
    )
    return MonitorOutcome(ok=True, changed=True, first_run=first_run, context_block=block)


def resolve_delivery_chat_id(job: dict[str, Any]) -> Optional[str]:
    """Extract target companion ``chat_id`` from ``deliver`` / ``origin``."""
    deliver = str(job.get("deliver") or "").strip()
    if deliver.startswith("companion:"):
        return deliver.split(":", 1)[1].strip() or None
    origin = job.get("origin")
    if deliver == "origin" and isinstance(origin, dict) and origin.get("platform") == "companion":
        return str(origin.get("chat_id") or "").strip() or None
    return None


class CronScheduler:
    """Single-event-loop cron scheduler with millisecond cross-thread wakeup."""

    def __init__(
        self,
        *,
        run_job_fn: Optional[JobExecutor] = None,
        deliver_fn: Optional[DeliveryHandler] = None,
        poll_interval_s: float = 30.0,
        self_base_url: Optional[str] = None,
    ) -> None:
        self._run_job_fn = run_job_fn
        self._deliver_fn = deliver_fn
        self._poll_interval_s = max(0.05, float(poll_interval_s))
        self._self_base_url = self_base_url
        self.wake_event: asyncio.Event = asyncio.Event()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running_jobs: set[str] = set()
        self._wake_cb: Optional[Callable[[], None]] = None

    def is_job_running(self, job_id: str) -> bool:
        return job_id in self._running_jobs

    def wake(self) -> None:
        """Thread-safe instant wakeup via ``loop.call_soon_threadsafe``."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.wake_event.set)
        else:
            self.wake_event.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self._wake_cb = self.wake
        cron_jobs.register_wake_callback(self._wake_cb)
        self._task = asyncio.create_task(self._run_loop(), name="muse-cron-scheduler")

    async def stop(self) -> None:
        if self._wake_cb is not None:
            cron_jobs.unregister_wake_callback(self._wake_cb)
            self._wake_cb = None
        self._stop_event.set()
        self.wake_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _compute_sleep_seconds(self) -> float:
        now_dt = muse_now()
        soonest = self._poll_interval_s
        for job in cron_jobs.list_jobs(include_disabled=False):
            raw = job.get("next_run_at")
            if not raw:
                continue
            try:
                due_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if due_dt.tzinfo is None and now_dt.tzinfo is not None:
                    due_dt = due_dt.replace(tzinfo=now_dt.tzinfo)
                delta = (due_dt - now_dt).total_seconds()
                if delta <= 0:
                    return 0.0
                soonest = min(soonest, delta)
            except ValueError:
                continue
        return max(0.01, soonest)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("muse.cron: scheduler tick error")
            if self._stop_event.is_set():
                break
            delay = self._compute_sleep_seconds()
            self.wake_event.clear()
            if delay > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake_event.wait(), timeout=delay)

    async def tick_once(self) -> list[str]:
        """Run all currently due jobs once; returns list of job IDs processed."""
        due = cron_jobs.get_due_jobs()
        processed: list[str] = []
        for job in due:
            jid = str(job.get("id") or "")
            if not jid or jid in self._running_jobs:
                continue
            processed.append(jid)
            await self.run_job(job)
        return processed

    async def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Execute one job (including optional monitor_url check, [SILENT] suppression, report)."""
        job_id = str(job.get("id") or "")
        self._running_jobs.add(job_id)
        try:
            prompt = str(job.get("prompt") or "")
            extra_prompt = str(
                job.get("manual_run_prompt") or job.get("pending_extra_prompt") or ""
            ).strip()
            if extra_prompt:
                prompt = f"{prompt}\n\n{extra_prompt}".strip()

            if str(job.get("monitor_url") or "").strip():
                outcome = await check_monitor(job, self_base_url=self._self_base_url)
                if not outcome.ok:
                    err_clean = redact_error(outcome.error or "Monitor failed")
                    cron_jobs.mark_job_run(job_id, success=False, error=err_clean)
                    return {"status": "error", "error": err_clean}
                if not outcome.changed:
                    cron_jobs.mark_job_run(job_id, success=True, no_change=True)
                    return {"status": "no_change"}
                if outcome.context_block:
                    prompt = f"{outcome.context_block}\n\n{prompt}".strip()

            if self._run_job_fn is None:
                cron_jobs.mark_job_run(job_id, success=False, error="No cron job executor registered")
                return {"status": "error", "error": "No cron job executor registered"}

            try:
                response = str(await self._run_job_fn(job, prompt) or "").strip()
            except Exception as exc:
                err_msg = redact_error(f"{type(exc).__name__}: {exc}")
                cron_jobs.mark_job_run(job_id, success=False, error=err_msg)
                return {"status": "error", "error": err_msg}

            silent = response == "[SILENT]"
            cron_jobs.mark_job_run(job_id, success=True, output=response, silent=silent)

            chat_id = resolve_delivery_chat_id(job)
            if not silent and response and chat_id and self._deliver_fn is not None:
                await self._deliver_fn(job, response)

            return {"status": "ok", "silent": silent, "output": response}
        finally:
            self._running_jobs.discard(job_id)


__all__ = [
    "CronScheduler",
    "DeliveryHandler",
    "JobExecutor",
    "MAX_DIFF_CHARS",
    "MAX_MONITOR_BYTES",
    "MAX_OUTPUT_CHARS",
    "MonitorOutcome",
    "build_monitor_diff",
    "check_monitor",
    "fetch_monitor_url",
    "hash_monitor_output",
    "is_exempt_self_trigger_url",
    "resolve_delivery_chat_id",
]
