"""Windows- and POSIX-safe atomic file writes with unique temporary names and lock-retry."""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional


def _replace_with_retry(tmp: Path, target: Path, *, retries: int = 5) -> None:
    for attempt in range(retries):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt + 1 == retries:
                raise
            time.sleep(0.01 * (attempt + 1))


def atomic_write_bytes(path: Path, data: bytes, *, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_bytes(data)
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        _replace_with_retry(tmp, path)
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, *, mode: Optional[int] = None) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), mode=mode)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
    trailing_newline: bool = False,
    mode: Optional[int] = None,
) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, mode=mode)
