"""Copies of the scheduled jobs (``cron/jobs.json``), so a reminder that goes missing can come back.

A recurring reminder the user asked for (喝水, every two hours) once vanished without anything having
removed it on purpose, and nothing showed what it had been. At start-up and every hour, when the
list has changed since the last copy, a copy is kept under ``<companion dir>/job-snapshots`` (the last
30): enough to see what was there and put it back.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .paths import get_webui_dir

logger = logging.getLogger(__name__)

_KEEP = 30


def _jobs_file() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "cron" / "jobs.json"


def snapshot(source: Optional[Path] = None, folder: Optional[Path] = None, keep: int = _KEEP) -> Optional[Path]:
    """Keep a copy of the jobs when they differ from the newest copy; the copy made, if any."""
    source = source or _jobs_file()
    folder = folder or get_webui_dir() / "job-snapshots"
    try:
        data = source.read_bytes()
    except OSError:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    kept = sorted(folder.glob("jobs-*.json"))
    if kept and kept[-1].read_bytes() == data:
        return None
    target = folder / f"jobs-{time.strftime('%Y%m%d-%H%M%S')}.json"
    if target.exists():  # twice in one second: the newer content wins
        target.unlink()
    target.write_bytes(data)
    for old in sorted(folder.glob("jobs-*.json"))[:-keep]:
        old.unlink(missing_ok=True)
    logger.info("companion: kept a copy of the scheduled jobs (%s)", target.name)
    return target
