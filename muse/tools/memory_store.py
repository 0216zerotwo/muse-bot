"""Bounded `MemoryStore` (`USER.md` / `MEMORY.md` with `\\n§\\n` delimiter) and `memory` tool."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from muse.paths import get_muse_home

TOOLSET = "memory"
ENTRY_DELIMITER = "\n§\n"
DEFAULT_USER_LIMIT = 1375
DEFAULT_MEMORY_LIMIT = 2200

_INVISIBLE_CHARS = frozenset({
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
})
_THREAT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", re.I),
        "prompt_injection",
    ),
    (re.compile(r"you\s+are\s+now\s+", re.I), "role_hijack"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.I), "deception_hide"),
    (re.compile(r"system\s+prompt\s+override", re.I), "sys_prompt_override"),
    (
        re.compile(r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", re.I),
        "disregard_rules",
    ),
    (
        re.compile(
            r"act\s+as\s+(?:if|though)\s+you\s+(?:have\s+no|don't\s+have)\s+(?:restrictions|limits)",
            re.I,
        ),
        "bypass_restrictions",
    ),
    (
        re.compile(r"curl\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.I),
        "exfil_curl",
    ),
    (
        re.compile(r"wget\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.I),
        "exfil_wget",
    ),
    (re.compile(r"authorized_keys", re.I), "ssh_backdoor"),
    (re.compile(r"<!--[^>]*-->"), "hidden_html_comment"),
)


def scan_memory_content(text: str) -> Optional[str]:
    """Scan memory text for prompt injection, invisible Unicode, or exfiltration payloads."""
    for ch in text:
        if ch in _INVISIBLE_CHARS:
            return f"Blocked memory content containing invisible Unicode character U+{ord(ch):04X}."
    for pattern, rule_id in _THREAT_RULES:
        if pattern.search(text):
            return f"Blocked unsafe memory content matching security rule '{rule_id}'."
    return None


@contextmanager
def _cross_process_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    raw_fd = os.open(lock_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            with contextlib.suppress(OSError):
                os.fchmod(raw_fd, 0o600)
        fh = os.fdopen(raw_fd, "r+b")
    except Exception:
        os.close(raw_fd)
        raise

    with fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            with contextlib.suppress(OSError):
                fh.seek(0)
                fh.write(b" ")
                fh.flush()
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + max(0.05, float(timeout_seconds))
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring lock {lock_path}")
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            deadline = time.monotonic() + max(0.05, float(timeout_seconds))
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring lock {lock_path}")
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_entries_strict(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return []
    seen: list[str] = []
    for chunk in raw.split(ENTRY_DELIMITER):
        clean = chunk.strip()
        if clean and clean not in seen:
            seen.append(clean)
    return seen


def _write_entries_atomic(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ENTRY_DELIMITER.join(entries)
    fd, tmp_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def get_memory_dir(home: Optional[Path] = None) -> Path:
    """Return `<MUSE_HOME>/memories` directory (creating it if absent)."""
    mem_dir = (Path(home) if home is not None else get_muse_home()) / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(mem_dir, 0o700)
    return mem_dir


class MemoryStore:
    """Bounded persistent store for `USER.md` and `MEMORY.md` fully compatible with `v1.x`."""

    def __init__(
        self,
        *,
        memory_dir: Optional[Path] = None,
        memory_char_limit: int = DEFAULT_MEMORY_LIMIT,
        user_char_limit: int = DEFAULT_USER_LIMIT,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self._explicit_dir = Path(memory_dir) if memory_dir is not None else None
        self.memory_char_limit = max(100, int(memory_char_limit))
        self.user_char_limit = max(100, int(user_char_limit))
        self.lock_timeout_seconds = max(0.05, float(lock_timeout_seconds))
        self._lock = threading.RLock()
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self._disk_baseline: dict[str, list[str]] = {"memory": [], "user": []}
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

    @property
    def mem_dir(self) -> Path:
        if self._explicit_dir is not None:
            self._explicit_dir.mkdir(parents=True, exist_ok=True)
            return self._explicit_dir
        return get_memory_dir()

    def _file_for(self, target: str) -> Path:
        if target not in ("memory", "user"):
            raise ValueError(f"Invalid target '{target}'; must be 'memory' or 'user'")
        return self.mem_dir / ("USER.md" if target == "user" else "MEMORY.md")

    def _lock_file_for(self, target: str) -> Path:
        fpath = self._file_for(target)
        return fpath.with_suffix(fpath.suffix + ".lock")

    @contextmanager
    def _target_lock(self, target: str) -> Iterator[None]:
        with _cross_process_lock(
            self._lock_file_for(target),
            timeout_seconds=self.lock_timeout_seconds,
        ):
            yield

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    @staticmethod
    def _joined_len(entries: list[str]) -> int:
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    @staticmethod
    def _render_block(target: str, entries: list[str]) -> str:
        if not entries:
            return ""
        body = ENTRY_DELIMITER.join(entries)
        if target == "user":
            return (
                "══════════════════════════════════════════════\n"
                "USER PROFILE (who the user is)\n"
                "══════════════════════════════════════════════\n"
                f"{body}"
            )
        return (
            "══════════════════════════════════════════════\n"
            "MEMORY (your personal notes)\n"
            "══════════════════════════════════════════════\n"
            f"{body}"
        )

    def entries_for(self, target: str) -> list[str]:
        """Public accessor returning the live list of entries for `'user'` or `'memory'`."""
        if target == "user":
            return list(self.user_entries)
        if target == "memory":
            return list(self.memory_entries)
        raise ValueError(f"Invalid target '{target}'; must be 'memory' or 'user'")

    # Transitional alias so legacy callers using `_entries_for` keep working cleanly.
    _entries_for = entries_for

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = list(entries)
        else:
            self.memory_entries = list(entries)

    def load_from_disk(self) -> None:
        with self._lock:
            for target in ("memory", "user"):
                with self._target_lock(target):
                    loaded = _read_entries_strict(self._file_for(target))
                self._set_entries(target, loaded)
                self._disk_baseline[target] = list(loaded)
            self._system_prompt_snapshot = {
                "memory": self._render_block("memory", self.memory_entries),
                "user": self._render_block("user", self.user_entries),
            }

    def save_to_disk(self, target: str) -> None:
        with self._lock:
            fpath = self._file_for(target)
            entries = self.entries_for(target)
            with self._target_lock(target):
                if fpath.exists():
                    on_disk = _read_entries_strict(fpath)
                    if on_disk != self._disk_baseline.get(target, []) and on_disk != entries:
                        bak = fpath.with_name(f"{fpath.name}.bak.{int(time.time())}")
                        with contextlib.suppress(OSError):
                            shutil.copy2(fpath, bak)
                _write_entries_atomic(fpath, entries)
                self._disk_baseline[target] = list(entries)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        block = self._system_prompt_snapshot.get(target, "")
        return block or None

    def _state_response(
        self,
        target: str,
        message: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        entries = self.entries_for(target)
        current = self._joined_len(entries)
        limit = self._char_limit(target)
        pct = int((current / limit) * 100) if limit > 0 else 0
        resp: dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% ({current:,}/{limit:,} chars)",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp.update(extra)
        return resp

    @staticmethod
    def _resolve_match(
        entries: list[str],
        old_text: str,
        *,
        matched_entry: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[dict[str, Any]]]:
        if matched_entry is not None:
            exact_hits = [i for i, item in enumerate(entries) if item == matched_entry]
            if not exact_hits:
                return None, {
                    "success": False,
                    "error": f"Exact entry no longer found: '{matched_entry[:80]}'.",
                }
            return exact_hits[0], None

        needle = (old_text or "").strip()
        if not needle:
            return None, {"success": False, "error": "old_text cannot be empty."}
        matches = [(i, item) for i, item in enumerate(entries) if needle in item]
        if not matches:
            return None, {"success": False, "error": f"No entry matched '{needle}'."}
        if len(matches) > 1:
            exact = [(i, item) for i, item in matches if item == needle]
            if len(exact) == 1:
                return exact[0][0], None
            uniq = {item for _, item in matches}
            if len(uniq) > 1:
                previews = [item[:80] for _, item in matches[:5]]
                return None, {
                    "success": False,
                    "error": f"Multiple entries matched '{needle}'. Be more specific.",
                    "matches": previews,
                }
        return matches[0][0], None

    def add(self, target: str, content: str) -> dict[str, Any]:
        clean = (content or "").strip()
        if not clean:
            return {"success": False, "error": "Content cannot be empty."}
        threat = scan_memory_content(clean)
        if threat:
            return {"success": False, "error": threat}

        with self._lock:
            fpath = self._file_for(target)
            limit = self._char_limit(target)
            with self._target_lock(target):
                entries = _read_entries_strict(fpath) if fpath.exists() else list(self.entries_for(target))
                if clean in entries:
                    self._set_entries(target, entries)
                    self._disk_baseline[target] = list(entries)
                    return self._state_response(target, "Entry already exists (no duplicate added).")
                candidate = [*entries, clean]
                new_total = self._joined_len(candidate)
                if new_total > limit:
                    cur = self._joined_len(entries)
                    return {
                        "success": False,
                        "error": (
                            f"Memory at {cur:,}/{limit:,} chars. Adding {len(clean)} chars would "
                            f"exceed the limit ({new_total:,}/{limit:,}). Condense or remove first."
                        ),
                        "current_entries": entries,
                        "usage": f"{cur:,}/{limit:,}",
                    }
                _write_entries_atomic(fpath, candidate)
                self._set_entries(target, candidate)
                self._disk_baseline[target] = list(candidate)
            return self._state_response(target, "Entry added.")

    def patch(
        self,
        target: str,
        old_text: str,
        new_content: str,
        *,
        matched_entry: Optional[str] = None,
    ) -> dict[str, Any]:
        replacement = (new_content or "").strip()
        if not replacement:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete."}
        threat = scan_memory_content(replacement)
        if threat:
            return {"success": False, "error": threat}

        with self._lock:
            fpath = self._file_for(target)
            limit = self._char_limit(target)
            with self._target_lock(target):
                entries = _read_entries_strict(fpath) if fpath.exists() else list(self.entries_for(target))
                idx, err = self._resolve_match(entries, old_text, matched_entry=matched_entry)
                if err is not None or idx is None:
                    return err or {"success": False, "error": "Match failed."}

                original = entries[idx]
                needle = (old_text or "").strip()
                if matched_entry is not None or original == needle or needle not in original:
                    updated_entry = replacement
                else:
                    updated_entry = original.replace(needle, replacement, 1).strip()

                recheck = scan_memory_content(updated_entry)
                if recheck:
                    return {"success": False, "error": recheck}

                candidate = list(entries)
                candidate[idx] = updated_entry
                # Deduplicate if edit produced an identical existing item.
                deduped: list[str] = []
                for item in candidate:
                    if item and item not in deduped:
                        deduped.append(item)
                new_total = self._joined_len(deduped)
                if new_total > limit:
                    return {
                        "success": False,
                        "error": f"Patched memory ({new_total:,} chars) exceeds limit ({limit:,} chars).",
                        "current_entries": entries,
                    }
                _write_entries_atomic(fpath, deduped)
                self._set_entries(target, deduped)
                self._disk_baseline[target] = list(deduped)
            return self._state_response(target, "Entry patched.")

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        *,
        matched_entry: Optional[str] = None,
    ) -> dict[str, Any]:
        """Replace the full matched entry with `new_content` (used by tidy/fade/learn)."""
        replacement = (new_content or "").strip()
        if not replacement:
            return {"success": False, "error": "new_content cannot be empty."}
        threat = scan_memory_content(replacement)
        if threat:
            return {"success": False, "error": threat}

        with self._lock:
            fpath = self._file_for(target)
            limit = self._char_limit(target)
            with self._target_lock(target):
                entries = _read_entries_strict(fpath) if fpath.exists() else list(self.entries_for(target))
                idx, err = self._resolve_match(entries, old_text, matched_entry=matched_entry)
                if err is not None or idx is None:
                    return err or {"success": False, "error": "Match failed."}
                candidate = list(entries)
                candidate[idx] = replacement
                deduped: list[str] = []
                for item in candidate:
                    if item and item not in deduped:
                        deduped.append(item)
                new_total = self._joined_len(deduped)
                if new_total > limit:
                    return {
                        "success": False,
                        "error": f"Replacement would put memory at {new_total:,}/{limit:,} chars.",
                        "current_entries": entries,
                    }
                _write_entries_atomic(fpath, deduped)
                self._set_entries(target, deduped)
                self._disk_baseline[target] = list(deduped)
            return self._state_response(target, "Entry replaced.")

    def remove(
        self,
        target: str,
        old_text: str,
        *,
        matched_entry: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            fpath = self._file_for(target)
            with self._target_lock(target):
                entries = _read_entries_strict(fpath) if fpath.exists() else list(self.entries_for(target))
                idx, err = self._resolve_match(entries, old_text, matched_entry=matched_entry)
                if err is not None or idx is None:
                    return err or {"success": False, "error": "Match failed."}
                candidate = [item for i, item in enumerate(entries) if i != idx]
                _write_entries_atomic(fpath, candidate)
                self._set_entries(target, candidate)
                self._disk_baseline[target] = list(candidate)
            return self._state_response(target, "Entry removed.")

    def apply_batch(self, target: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Atomically apply a batch of `add`/`remove`/`replace`/`patch` operations under one lock."""
        if not ops:
            return self._state_response(target, "No operations to apply.", applied=0)

        with self._lock:
            fpath = self._file_for(target)
            limit = self._char_limit(target)
            with self._target_lock(target):
                working = _read_entries_strict(fpath) if fpath.exists() else list(self.entries_for(target))
                applied = 0
                errors: list[str] = []

                for op in ops:
                    act = str(op.get("action") or "").strip().lower()
                    matched_entry = (
                        str(op["matched_entry"])
                        if op.get("matched_entry") is not None
                        else None
                    )
                    old_txt = str(op.get("old_text") or "")
                    new_txt = str(op.get("content") or op.get("new_content") or "").strip()

                    if act == "add":
                        if not new_txt:
                            continue
                        threat = scan_memory_content(new_txt)
                        if threat:
                            errors.append(threat)
                            continue
                        if new_txt not in working:
                            trial = [*working, new_txt]
                            if self._joined_len(trial) <= limit:
                                working = trial
                                applied += 1
                            else:
                                errors.append("Character limit exceeded on add.")
                    elif act == "remove":
                        idx, err = self._resolve_match(working, old_txt, matched_entry=matched_entry)
                        if idx is not None and err is None:
                            working.pop(idx)
                            applied += 1
                    elif act in ("replace", "patch"):
                        if not new_txt:
                            continue
                        threat = scan_memory_content(new_txt)
                        if threat:
                            errors.append(threat)
                            continue
                        idx, err = self._resolve_match(working, old_txt, matched_entry=matched_entry)
                        if idx is None or err is not None:
                            continue
                        orig = working[idx]
                        is_span_patch = (
                            act == "patch"
                            and matched_entry is None
                            and bool(old_txt)
                            and old_txt in orig
                            and orig != old_txt
                        )
                        if is_span_patch:
                            cand_entry = orig.replace(old_txt, new_txt, 1).strip()
                        else:
                            cand_entry = new_txt
                        if scan_memory_content(cand_entry):
                            continue
                        trial = list(working)
                        trial[idx] = cand_entry
                        deduped: list[str] = []
                        for item in trial:
                            if item and item not in deduped:
                                deduped.append(item)
                        if self._joined_len(deduped) <= limit:
                            working = deduped
                            applied += 1
                        else:
                            errors.append("Character limit exceeded on replace.")

                _write_entries_atomic(fpath, working)
                self._set_entries(target, working)
                self._disk_baseline[target] = list(working)

            extra: dict[str, Any] = {"applied": applied}
            if errors:
                extra["warnings"] = errors
            return self._state_response(target, f"Applied {applied} operation(s).", **extra)


def load_on_disk_store(home: Optional[Path] = None) -> MemoryStore:
    """Instantiate a `MemoryStore` bound to `<home>/memories` and load current files."""
    store = MemoryStore(memory_dir=get_memory_dir(home))
    store.load_from_disk()
    return store


def memory_tool_handler(
    args: dict[str, Any],
    *,
    store: Optional[MemoryStore] = None,
    **_: Any,
) -> str:
    """LLM `memory` tool handler supporting `add`, `patch`, and `remove`."""
    action = str(args.get("action") or "").strip().lower()
    target = str(args.get("target") or "memory").strip().lower()
    if target not in ("memory", "user"):
        return json.dumps(
            {"success": False, "error": f"Invalid target '{target}'. Must be 'memory' or 'user'."},
            ensure_ascii=False,
        )
    if action == "replace":
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Action 'replace' is deprecated for tool calls. "
                    "Use action='patch' with old_text and content."
                ),
            },
            ensure_ascii=False,
        )
    if action not in ("add", "patch", "remove"):
        return json.dumps(
            {"success": False, "error": f"Unknown action '{action}'. Use 'add', 'patch', or 'remove'."},
            ensure_ascii=False,
        )

    active_store = store or load_on_disk_store()
    content = str(args.get("content") or args.get("new_content") or "")
    old_text = str(args.get("old_text") or "")

    if action == "add":
        res = active_store.add(target, content)
    elif action == "patch":
        res = active_store.patch(target, old_text, content)
    else:
        res = active_store.remove(target, old_text)
    return json.dumps(res, ensure_ascii=False)


MEMORY_SCHEMA: dict[str, Any] = {
    "name": "memory",
    "description": (
        "Save, update (`patch`), or delete (`remove`) durable facts in persistent memory. "
        "Use target='user' for user profile/preferences (`USER.md`) and target='memory' "
        "for environment notes, conventions, and lessons (`MEMORY.md`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "patch", "remove"],
                "description": "Operation to perform.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory file to update ('user' -> USER.md, 'memory' -> MEMORY.md).",
            },
            "content": {
                "type": "string",
                "description": "Entry text for 'add' or replacement text for 'patch'.",
            },
            "old_text": {
                "type": "string",
                "description": "Unique substring identifying the existing entry for 'patch' or 'remove'.",
            },
        },
        "required": ["action", "target"],
    },
}


def register_memory_tools(registry: Any) -> None:
    registry.register("memory", toolset=TOOLSET, schema=MEMORY_SCHEMA, handler=memory_tool_handler)


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_USER_LIMIT",
    "ENTRY_DELIMITER",
    "MEMORY_SCHEMA",
    "MemoryStore",
    "TOOLSET",
    "get_memory_dir",
    "load_on_disk_store",
    "memory_tool_handler",
    "register_memory_tools",
    "scan_memory_content",
]
