"""Cross-platform `terminal` and `process_manage` tools with process-tree & secret isolation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import psutil

from muse.config_store import read_env_values
from muse.paths import get_muse_home

TOOLSET = "terminal"
MAX_OUTPUT_CHARS = 50_000
MAX_RING_CHARS = 200_000

_STRIP_ENV_EXACT = frozenset({
    "COMPANION_AUTH_SECRET",
    "MUSE_AUTH_SECRET",
    "MUSE_MASTER_KEY",
    "HERMES_MASTER_KEY",
    "MUSE_VAULT_KEY",
    "HERMES_VAULT_KEY",
    "VAPID_PRIVATE_KEY",
})
_STRIP_ENV_PREFIXES = (
    "COMPANION_MCP_",
    "COMPANION_PUSH_",
    "COMPANION_HOOK_",
    "COMPANION_PAIR_",
    "COMPANION_SECRET_",
    "MUSE_INTERNAL_",
)
_SECRET_NAME_RE = re.compile(
    r"(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|AUTH_SECRET)$",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xai-[A-Za-z0-9_-]{20,})\b"
)
_PS_UTF8_PRELUDE = (
    "[Console]::InputEncoding = [Console]::OutputEncoding = "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

_DYNAMIC_SECRETS_LOCK = threading.RLock()
_DYNAMIC_SECRETS: set[str] = set()


def register_redacted_secret(secret: Optional[str]) -> None:
    """Register a runtime secret (such as a vault password) for subprocess & pipeline redaction."""
    if isinstance(secret, str):
        val = secret.strip()
        if len(val) >= 6:
            with _DYNAMIC_SECRETS_LOCK:
                _DYNAMIC_SECRETS.add(val)
            with contextlib.suppress(ImportError):
                from muse.interceptors import register_global_secret

                register_global_secret(val)


def sanitize_subprocess_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    extra_strip_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Strip server-internal secrets while delegating to `muse.interceptors` if present."""
    try:
        from muse.interceptors import sanitize_subprocess_env as _interceptor_sanitize

        clean = _interceptor_sanitize(env, extra_strip_keys=extra_strip_keys)
    except ImportError:
        source = os.environ if env is None else env
        extra = {k.upper() for k in extra_strip_keys}
        clean = {}
        for key, val in source.items():
            upper = key.upper()
            if upper in _STRIP_ENV_EXACT or upper in extra:
                continue
            if upper.startswith(_STRIP_ENV_PREFIXES):
                continue
            clean[key] = str(val)
    clean.setdefault("PYTHONIOENCODING", "utf-8")
    clean.setdefault("PYTHONUTF8", "1")
    return clean


def redact_process_output(text: str, *, home: Optional[Path] = None) -> str:
    """Scrub known API keys, server secrets, and registered vault passwords from output."""
    if not text:
        return ""
    out = _KNOWN_TOKEN_RE.sub("[REDACTED]", text)
    with _DYNAMIC_SECRETS_LOCK:
        secrets_set = set(_DYNAMIC_SECRETS)
    for k, v in os.environ.items():
        if (k.upper() in _STRIP_ENV_EXACT or _SECRET_NAME_RE.search(k)) and len(v.strip()) >= 8:
            secrets_set.add(v.strip())
    resolved_home = home or get_muse_home()
    with contextlib.suppress(Exception):
        for k, v in read_env_values(home=resolved_home).items():
            if _SECRET_NAME_RE.search(k) and len(v.strip()) >= 8:
                secrets_set.add(v.strip())
    for sec in sorted(secrets_set, key=len, reverse=True):
        if sec in out:
            out = out.replace(sec, "[REDACTED]")
    return out


def _find_windows_bash() -> Optional[str]:
    candidates: list[Path] = []
    for base_env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(base_env)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    found = shutil.which("bash.exe") or shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return found


def build_shell_argv(command: str, shell: Optional[str] = None) -> list[str]:
    """Build cross-platform shell argv without `shell=True` ambiguity."""
    req = (shell or "auto").strip().lower()
    if os.name == "nt":
        if req == "bash":
            bash_bin = _find_windows_bash()
            if not bash_bin:
                raise FileNotFoundError("bash executable not found on Windows")
            return [bash_bin, "-lc", command]
        if req == "cmd":
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return [comspec, "/d", "/s", "/c", f"chcp 65001 >nul & {command}"]
        ps_bin = (
            shutil.which("pwsh.exe")
            or shutil.which("pwsh")
            or shutil.which("powershell.exe")
            or "powershell.exe"
        )
        return [
            ps_bin,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"{_PS_UTF8_PRELUDE}{command}",
        ]

    if req in ("pwsh", "powershell"):
        ps_bin = shutil.which("pwsh") or shutil.which("powershell")
        if not ps_bin:
            raise FileNotFoundError("pwsh executable not found")
        return [ps_bin, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if req == "sh":
        return [shutil.which("sh") or "/bin/sh", "-c", command]
    bash_bin = shutil.which("bash") or "/bin/bash"
    if Path(bash_bin).exists():
        return [bash_bin, "-lc", command]
    return [shutil.which("sh") or "/bin/sh", "-c", command]


def is_pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe via `psutil` (never uses POSIX signal-0 probing)."""
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def kill_process_tree(pid: int, *, timeout: float = 0.5) -> bool:
    """Recursively terminate and kill a process and all its descendants via `psutil`."""
    if pid <= 0:
        return False
    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False

    targets: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        targets.extend(root.children(recursive=True))
    targets.append(root)

    for target in targets:
        with contextlib.suppress(psutil.Error):
            target.terminate()
    _, alive = psutil.wait_procs(targets, timeout=max(0.05, timeout))
    for target in alive:
        with contextlib.suppress(psutil.Error):
            target.kill()
    if alive:
        psutil.wait_procs(alive, timeout=0.2)
    return True


def _popen_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


class _OutputBuffer:
    def __init__(self, max_chars: int = MAX_RING_CHARS) -> None:
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._chunks: list[str] = []
        self._size = 0

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            if self._size > self.max_chars:
                joined = "".join(self._chunks)[-self.max_chars :]
                self._chunks = [joined]
                self._size = len(joined)

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._chunks)


def _start_pipe_reader(pipe: Any, buf: _OutputBuffer) -> threading.Thread:
    def _pump() -> None:
        try:
            for raw_line in iter(pipe.readline, b""):
                buf.append(raw_line.decode("utf-8", errors="replace"))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                pipe.close()

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    return t


@dataclass
class ManagedProcess:
    proc_id: str
    command: str
    workdir: str
    shell: str
    popen: subprocess.Popen[bytes]
    stdout_buf: _OutputBuffer = field(default_factory=_OutputBuffer)
    stderr_buf: _OutputBuffer = field(default_factory=_OutputBuffer)
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    readers: list[threading.Thread] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return int(self.popen.pid or 0)

    def poll_status(self) -> tuple[str, Optional[int]]:
        rc = self.popen.poll()
        if rc is not None:
            if self.ended_at is None:
                self.ended_at = time.time()
            return "exited", int(rc)
        if not is_pid_alive(self.pid):
            rc2 = self.popen.poll()
            if self.ended_at is None:
                self.ended_at = time.time()
            return "exited", (int(rc2) if rc2 is not None else -1)
        return "running", None

    def close_handles(self) -> None:
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()


_BG_LOCK = threading.RLock()
_BG_PROCS: dict[str, ManagedProcess] = {}


def _bound_text(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return f"{text[:head]}\n... [truncated {omitted} chars] ...\n{text[-tail:]}"


def _resolve_cancel_scope(ctx: Any) -> Any:
    scope = getattr(ctx, "cancel_scope", None)
    if scope is not None:
        return scope
    try:
        from muse.context import get_turn_context

        tctx = get_turn_context()
        return getattr(tctx, "cancel_scope", None)
    except ImportError:
        return None


def _spawn_managed(
    command: str,
    *,
    workdir: Optional[str] = None,
    shell: Optional[str] = None,
    extra_env: Optional[Mapping[str, str]] = None,
) -> ManagedProcess:
    cwd = Path(workdir).expanduser().resolve() if workdir else Path.cwd()
    if not cwd.is_dir():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")
    argv = build_shell_argv(command, shell=shell)
    merged_env = sanitize_subprocess_env()
    if isinstance(extra_env, Mapping):
        clean_extra = sanitize_subprocess_env({str(k): str(v) for k, v in extra_env.items()})
        merged_env.update(clean_extra)

    popen = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=merged_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_popen_group_kwargs(),
    )
    out_buf = _OutputBuffer()
    err_buf = _OutputBuffer()
    readers: list[threading.Thread] = []
    if popen.stdout is not None:
        readers.append(_start_pipe_reader(popen.stdout, out_buf))
    if popen.stderr is not None:
        readers.append(_start_pipe_reader(popen.stderr, err_buf))
    return ManagedProcess(
        proc_id=f"proc_{uuid.uuid4().hex[:10]}",
        command=command,
        workdir=str(cwd),
        shell=shell or ("powershell" if os.name == "nt" else "bash"),
        popen=popen,
        stdout_buf=out_buf,
        stderr_buf=err_buf,
        readers=readers,
    )


async def terminal_handler(args: dict[str, Any], *, ctx: Any = None, **_: Any) -> str:
    """Execute a shell command in foreground or background with full tree & secret hygiene."""
    command = str(args.get("command") or "").strip()
    if not command:
        return json.dumps(
            {"success": False, "error": "Parameter 'command' is required."},
            ensure_ascii=False,
        )
    workdir = str(args["workdir"]).strip() if args.get("workdir") else None
    shell = str(args["shell"]).strip() if args.get("shell") else None
    background = bool(args.get("background", False))
    timeout_s = max(0.2, min(1800.0, float(args.get("timeout") or 120.0)))
    extra_env = args.get("env") if isinstance(args.get("env"), Mapping) else None

    try:
        managed = await asyncio.to_thread(
            _spawn_managed,
            command,
            workdir=workdir,
            shell=shell,
            extra_env=extra_env,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )

    if background:
        with _BG_LOCK:
            _BG_PROCS[managed.proc_id] = managed
        await asyncio.sleep(0.08)
        status, exit_code = managed.poll_status()
        return json.dumps(
            {
                "success": True,
                "background": True,
                "proc_id": managed.proc_id,
                "pid": managed.pid,
                "status": status,
                "exit_code": exit_code,
                "workdir": managed.workdir,
            },
            ensure_ascii=False,
        )

    scope = _resolve_cancel_scope(ctx)
    if scope is not None and callable(getattr(scope, "register_pid", None)):
        scope.register_pid(managed.pid)

    timed_out = False
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            if scope is not None and callable(getattr(scope, "check", None)):
                scope.check()
            if managed.popen.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                await asyncio.to_thread(kill_process_tree, managed.pid)
                break
            await asyncio.sleep(min(0.04, remaining))
        for reader in managed.readers:
            await asyncio.to_thread(reader.join, 0.5)
    except asyncio.CancelledError:
        await asyncio.to_thread(kill_process_tree, managed.pid)
        raise
    finally:
        if scope is not None and callable(getattr(scope, "unregister_pid", None)):
            scope.unregister_pid(managed.pid)
        managed.close_handles()

    _, rc = managed.poll_status()
    stdout_clean = _bound_text(redact_process_output(managed.stdout_buf.snapshot()))
    stderr_clean = _bound_text(redact_process_output(managed.stderr_buf.snapshot()))
    return json.dumps(
        {
            "success": (not timed_out) and (rc == 0),
            "exit_code": rc,
            "timed_out": timed_out,
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "duration_s": round(time.time() - managed.started_at, 3),
        },
        ensure_ascii=False,
    )


def _tail_lines(text: str, count: int) -> str:
    if not text or count <= 0:
        return text
    lines = text.splitlines()
    return "\n".join(lines[-count:]) if len(lines) > count else text


async def process_manage_handler(args: dict[str, Any], **_: Any) -> str:
    """List, poll logs, or kill background processes started via `terminal(background=True)`."""
    action = str(args.get("action") or "list").strip().lower()
    proc_id = str(args.get("proc_id") or args.get("session_id") or "").strip()
    tail_n = max(1, min(2000, int(args.get("tail_lines") or 200)))

    if action == "list":
        with _BG_LOCK:
            items = list(_BG_PROCS.values())
        rows = []
        for item in items:
            st, rc = item.poll_status()
            end_ts = item.ended_at or time.time()
            rows.append({
                "proc_id": item.proc_id,
                "pid": item.pid,
                "status": st,
                "exit_code": rc,
                "workdir": item.workdir,
                "elapsed_s": round(max(0.0, end_ts - item.started_at), 2),
            })
        return json.dumps({"success": True, "processes": rows}, ensure_ascii=False)

    if not proc_id:
        return json.dumps(
            {"success": False, "error": "Parameter 'proc_id' is required for this action."},
            ensure_ascii=False,
        )

    with _BG_LOCK:
        target = _BG_PROCS.get(proc_id)
    if target is None:
        return json.dumps(
            {"success": False, "error": f"Unknown background process: {proc_id}"},
            ensure_ascii=False,
        )

    if action in ("poll", "output", "log", "status"):
        st, rc = target.poll_status()
        out_txt = _bound_text(redact_process_output(_tail_lines(target.stdout_buf.snapshot(), tail_n)))
        err_txt = _bound_text(redact_process_output(_tail_lines(target.stderr_buf.snapshot(), tail_n)))
        return json.dumps(
            {
                "success": True,
                "proc_id": target.proc_id,
                "pid": target.pid,
                "status": st,
                "exit_code": rc,
                "stdout": out_txt,
                "stderr": err_txt,
            },
            ensure_ascii=False,
        )

    if action in ("kill", "terminate", "stop"):
        await asyncio.to_thread(kill_process_tree, target.pid)
        for reader in target.readers:
            await asyncio.to_thread(reader.join, 0.4)
        target.close_handles()
        st, rc = target.poll_status()
        with _BG_LOCK:
            _BG_PROCS.pop(proc_id, None)
        return json.dumps(
            {
                "success": True,
                "proc_id": proc_id,
                "pid": target.pid,
                "status": st,
                "exit_code": rc,
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {"success": False, "error": f"Unsupported process_manage action: {action!r}"},
        ensure_ascii=False,
    )


def cleanup_all_processes() -> int:
    """Kill and remove all tracked background processes (safe synchronous teardown helper)."""
    with _BG_LOCK:
        items = list(_BG_PROCS.values())
        _BG_PROCS.clear()
    for item in items:
        kill_process_tree(item.pid)
        for reader in item.readers:
            reader.join(0.2)
        item.close_handles()
    return len(items)


TERMINAL_SCHEMA: dict[str, Any] = {
    "name": "terminal",
    "description": (
        "Execute a shell command (PowerShell UTF-8 by default on Windows, bash on POSIX; "
        "supports shell='bash' or background=True)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command line to run."},
            "workdir": {"type": "string", "description": "Optional working directory path."},
            "timeout": {
                "type": "number",
                "description": "Foreground timeout in seconds (default 120).",
                "default": 120,
            },
            "background": {
                "type": "boolean",
                "description": "Run asynchronously in background and return proc_id.",
                "default": False,
            },
            "shell": {
                "type": "string",
                "enum": ["auto", "powershell", "pwsh", "bash", "cmd", "sh"],
                "description": "Optional explicit shell interpreter.",
            },
        },
        "required": ["command"],
    },
}

PROCESS_MANAGE_SCHEMA: dict[str, Any] = {
    "name": "process_manage",
    "description": "List, inspect output logs, or terminate background processes.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "output", "kill"],
                "description": "Management action to perform.",
            },
            "proc_id": {
                "type": "string",
                "description": "Target background process id from terminal(background=True).",
            },
            "tail_lines": {
                "type": "integer",
                "description": "Maximum trailing lines of stdout/stderr to return (default 200).",
                "default": 200,
            },
        },
        "required": ["action"],
    },
}


def register_terminal_tools(registry: Any) -> None:
    registry.register(
        "terminal",
        toolset=TOOLSET,
        schema=TERMINAL_SCHEMA,
        handler=terminal_handler,
        is_async=True,
    )
    registry.register(
        "process_manage",
        toolset=TOOLSET,
        schema=PROCESS_MANAGE_SCHEMA,
        handler=process_manage_handler,
        is_async=True,
    )


__all__ = [
    "PROCESS_MANAGE_SCHEMA",
    "TERMINAL_SCHEMA",
    "TOOLSET",
    "build_shell_argv",
    "cleanup_all_processes",
    "is_pid_alive",
    "kill_process_tree",
    "process_manage_handler",
    "redact_process_output",
    "register_redacted_secret",
    "register_terminal_tools",
    "sanitize_subprocess_env",
    "terminal_handler",
]
