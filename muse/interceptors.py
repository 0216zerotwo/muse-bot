"""5-stage async ``ToolInterceptor`` pipeline, dual-track approvals, and 4-layer privacy hardening."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from companion.modules.secrets_guard import (
    MESSAGE as SECRETS_BLOCK_MESSAGE,
    SELF_CHANGE_MESSAGE,
    check as check_tool_secrets,
    is_secret_path,
)

from .config_store import read_env_values
from .context import (
    TurnExecutionContext,
    bind_turn_context,
    get_turn_context,
    reset_turn_context,
    turn_context_scope,
)
from .paths import get_muse_home

logger = logging.getLogger(__name__)
turn_context = turn_context_scope


def _comp_mod(override: Any, name: str) -> Any:
    return override if override is not None else importlib.import_module(f"companion.modules.{name}")


def resolve_turn_context(explicit: Any = None) -> TurnExecutionContext:
    if explicit is not None:
        return explicit
    return get_turn_context() or TurnExecutionContext(chat_id="")


# ── Layer 1 & 2: LogPrivacyFilter + HTTP client URL query secret scrubber ─────

_ALREADY_REDACTED = re.compile(r"^<\d+ 字>$|^\[REDACTED\]$")
_URL_QUERY_SECRET = re.compile(
    r"([?&](?:api[-_]?key|key|token|access[-_]?token|refresh[-_]?token|id[-_]?token"
    r"|secret|client[-_]?secret|password|passwd|pwd|auth|bearer|sig|signature)=)([^&#\s'\"<>]+)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_NAMES = (
    "msg|message|text|content|prompt|user_message|command|cmd|arguments|tool_args"
    "|payload|input|output|reply_to_text|secret|password|api_key|token"
)
_INLINE_PAYLOAD_KV = re.compile(
    rf"\b({_SENSITIVE_FIELD_NAMES})\s*=\s*('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")",
    re.IGNORECASE,
)
_INLINE_COLON_LINE = re.compile(
    r"(?im)(.*?\b(?:prompt|user_message|command|cmd)\s*:\s*)(?!<\d+ 字>|\[REDACTED\])([^\r\n]+?)(?=\s+-\s+[A-Za-z_]+\s*:|$)"
)
_FMT_SPEC = re.compile(
    r"%(?:\(([^)]+)\))?[#0\- +]?(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?([diouxXeEfFgGcrsab%])"
)
_SENSITIVE_PREFIX_BEFORE_SPEC = re.compile(
    rf"\b(?:{_SENSITIVE_FIELD_NAMES})\s*(?:=|:)\s*['\"]?$",
    re.IGNORECASE,
)
_SENSITIVE_DICT_KEYS = frozenset(_SENSITIVE_FIELD_NAMES.split("|"))
_STRING_CONVS = frozenset({"s", "r", "a"})
_LEGACY_LOG_RULES: dict[str, tuple[tuple[str, tuple[int, ...]], ...]] = {
    "gateway.run": (("inbound message:", (3, 5)),),
    "agent.tool_executor": (("Tool %s returned error", (2,)),),
    "tools.terminal_tool": (("Execution error, retrying", (3,)), ("Execution failed after", (1,))),
}
_TARGET_LOGGERS = (
    "muse", "companion", "uvicorn.error", "httpx", "httpcore",
    "gateway.run", "agent.tool_executor", "tools.terminal_tool",
)


def _matches_target_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _TARGET_LOGGERS)


def _size_tag(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if not text else (text if _ALREADY_REDACTED.match(text) else f"<{len(text)} 字>")


def scrub_url_secrets(text: str) -> str:
    """Replace sensitive query string parameters (`?api_key=...`, `&token=...`) with `[REDACTED]`."""
    if not text or ("?" not in text and "&" not in text):
        return text
    return _URL_QUERY_SECRET.sub(r"\1[REDACTED]", text)


def _redact_inline_text(text: str) -> str:
    out = scrub_url_secrets(text)
    if "=" in out:
        def _repl_kv(match: re.Match[str]) -> str:
            key, quoted = match.group(1), match.group(2)
            inner = quoted[1:-1]
            if not inner or _ALREADY_REDACTED.match(inner):
                return match.group(0)
            return f"{key}={quoted[0]}<{len(inner)} 字>{quoted[0]}"

        out = _INLINE_PAYLOAD_KV.sub(_repl_kv, out)
    if ":" in out:
        def _repl_colon(match: re.Match[str]) -> str:
            head, tail = match.group(1), match.group(2).strip()
            if not tail or _ALREADY_REDACTED.match(tail):
                return match.group(0)
            return f"{head}<{len(tail)} 字>"

        out = _INLINE_COLON_LINE.sub(_repl_colon, out)
    return out


def _scan_format_specs(msg: str) -> tuple[dict[int, str], set[int], dict[str, set[str]]]:
    pos_convs: dict[int, str] = {}
    sensitive_pos: set[int] = set()
    named_convs: dict[str, set[str]] = {}
    idx, prev_end = 0, 0
    for match in _FMT_SPEC.finditer(msg):
        key_name, conv = match.group(1), match.group(2)
        if conv == "%":
            prev_end = match.end()
            continue
        if key_name is not None:
            named_convs.setdefault(key_name, set()).add(conv)
        else:
            pos_convs[idx] = conv
            if conv in _STRING_CONVS and _SENSITIVE_PREFIX_BEFORE_SPEC.search(msg[prev_end : match.start()]):
                sensitive_pos.add(idx)
            idx += 1
        prev_end = match.end()
    return pos_convs, sensitive_pos, named_convs


class LogPrivacyFilter(logging.Filter):
    """Redacts user text/commands/tool payloads to `<N 字>` and scrubs URL query secrets safely."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        if not _matches_target_logger(name):
            return True
        if isinstance(record.msg, str) and record.args:
            msg = record.msg
            pos_convs, sensitive_pos, named_convs = _scan_format_specs(msg)
            if isinstance(record.args, tuple):
                args = list(record.args)
                for start, pos_list in _LEGACY_LOG_RULES.get(name, ()):
                    if msg.startswith(start):
                        for pos in pos_list:
                            if pos < len(args) and pos_convs.get(pos, "s") in _STRING_CONVS:
                                args[pos] = _size_tag(args[pos])
                for pos in sensitive_pos:
                    if pos < len(args):
                        args[pos] = _size_tag(args[pos])
                record.args = tuple(args)
            elif isinstance(record.args, dict):
                if named_convs:
                    new_map = dict(record.args)
                    for k, v in record.args.items():
                        convs = named_convs.get(k, set())
                        if k.lower() in _SENSITIVE_DICT_KEYS and convs and convs <= _STRING_CONVS:
                            new_map[k] = _size_tag(v)
                    record.args = new_map
                elif 0 in sensitive_pos:
                    record.args = (_size_tag(record.args),)
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = _redact_inline_text(rendered)
        record.args = ()
        return True


def _ensure_filter(target: Any, flt: LogPrivacyFilter) -> None:
    if not any(isinstance(f, LogPrivacyFilter) for f in getattr(target, "filters", ())):
        target.addFilter(flt)


def install_privacy_hardening() -> LogPrivacyFilter:
    """Attach `LogPrivacyFilter` to `muse.*`, `companion.*`, `uvicorn.error`, and lock `httpx`/`httpcore`."""
    flt = LogPrivacyFilter()
    for http_logger in ("httpx", "httpcore"):
        lg = logging.getLogger(http_logger)
        lg.setLevel(logging.WARNING)
        _ensure_filter(lg, flt)
    for base in ("muse", "companion", "uvicorn.error", "gateway.run", "agent.tool_executor", "tools.terminal_tool"):
        _ensure_filter(logging.getLogger(base), flt)
    for existing_name, existing_obj in list(logging.root.manager.loggerDict.items()):
        if isinstance(existing_obj, logging.Logger) and existing_name.startswith(("muse.", "companion.", "httpx.", "httpcore.")):
            _ensure_filter(existing_obj, flt)
    for handler in logging.root.handlers:
        _ensure_filter(handler, flt)
    return flt


# ── Layer 3: Subprocess Env Sanitizer + ToolResultBounder ─────────────────────

_STRIP_SUBPROCESS_ENV_EXACT = frozenset({
    "COMPANION_AUTH_SECRET", "MUSE_AUTH_SECRET", "MUSE_MASTER_KEY", "HERMES_MASTER_KEY",
    "MUSE_VAULT_KEY", "HERMES_VAULT_KEY", "VAPID_PRIVATE_KEY",
})
_STRIP_SUBPROCESS_ENV_PREFIXES = (
    "COMPANION_MCP_", "COMPANION_PUSH_", "COMPANION_HOOK_", "COMPANION_PAIR_",
    "COMPANION_SECRET_", "MUSE_INTERNAL_",
)
_SECRET_KEY_NAME = re.compile(r"(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|AUTH_SECRET)$", re.IGNORECASE)
_TOKEN_PATTERNS = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xai-[A-Za-z0-9_-]{20,})\b"
)
_VAULT_PASSWORD_JSON = re.compile(r'("(?:password|totp_secret|passcode)"\s*:\s*)"(?:\\.|[^"])+"', re.IGNORECASE)


def sanitize_subprocess_env(
    env: Optional[Mapping[str, str]] = None, *, extra_strip_keys: Iterable[str] = ()
) -> dict[str, str]:
    """Return a clean subprocess environment stripped of server-proprietary credentials."""
    source = os.environ if env is None else env
    extra = {k.upper() for k in extra_strip_keys}
    return {
        k: str(v)
        for k, v in source.items()
        if k.upper() not in _STRIP_SUBPROCESS_ENV_EXACT
        and k.upper() not in extra
        and not k.upper().startswith(_STRIP_SUBPROCESS_ENV_PREFIXES)
    }


class ToolResultBounder:
    """Redacts known secrets, OAuth tokens, and `browser_vault` passwords to `[REDACTED]` and bounds size."""

    def __init__(self, *, max_chars: int = 100_000, home: Optional[Path] = None) -> None:
        self.max_chars = max(512, int(max_chars))
        self._explicit_home = home
        self._lock = threading.RLock()
        self._registered: set[str] = set()

    def register_secret(self, secret: Optional[str]) -> None:
        if isinstance(secret, str) and len(secret.strip()) >= 6:
            with self._lock:
                self._registered.add(secret.strip())

    def _collect_known_secrets(self) -> list[str]:
        with self._lock:
            secrets_set = set(self._registered)
        for env_key, env_val in os.environ.items():
            if (env_key.upper() in _STRIP_SUBPROCESS_ENV_EXACT or _SECRET_KEY_NAME.search(env_key)) and len(env_val.strip()) >= 8:
                secrets_set.add(env_val.strip())
        home = self._explicit_home or get_muse_home()
        with contextlib.suppress(Exception):
            for k, v in read_env_values(home=home).items():
                if _SECRET_KEY_NAME.search(k) and len(v.strip()) >= 8:
                    secrets_set.add(v.strip())
        auth_file = home / "auth.json"
        if auth_file.is_file():
            with contextlib.suppress(OSError, ValueError):
                self._walk_auth_tokens(json.loads(auth_file.read_text(encoding="utf-8")), secrets_set)
        return sorted(secrets_set, key=len, reverse=True)

    @classmethod
    def _walk_auth_tokens(cls, node: Any, sink: set[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in ("access_token", "refresh_token", "id_token", "api_key", "secret") and len(v.strip()) >= 12:
                    sink.add(v.strip())
                else:
                    cls._walk_auth_tokens(v, sink)
        elif isinstance(node, list):
            for item in node:
                cls._walk_auth_tokens(item, sink)

    def bound(self, raw: Any, *, tool_name: str = "") -> str:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        if not text:
            return ""
        text = _TOKEN_PATTERNS.sub("[REDACTED]", scrub_url_secrets(text))
        if tool_name == "browser_vault" or "password" in text.lower() or "totp_secret" in text.lower():
            text = _VAULT_PASSWORD_JSON.sub(r'\1"[REDACTED]"', text)
        for secret in self._collect_known_secrets():
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        if len(text) > self.max_chars:
            keep_head = int(self.max_chars * 0.75)
            keep_tail = self.max_chars - keep_head
            text = f"{text[:keep_head]}\n… [已省略 {len(text) - self.max_chars} 字符] …\n{text[-keep_tail:]}"
        return text


# ── Risky Command Classifier (for Track 2 `hold_exec` async suspension) ───────

_RISKY_CMD_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+-(?:[A-Za-z]*[rf][A-Za-z]*\s+)+", re.I), "删除目录或批量删除文件"),
    (re.compile(r"\b(?:Remove-Item|ri|del|erase|rmdir|rd)\b[^\r\n;|]*-(?:Recurse|Force)\b|\b(?:del|rmdir)\s+/[sq]", re.I), "删除目录或批量删除文件"),
    (re.compile(r"\b(?:mkfs(?:\.\w+)?|fdisk|parted|Format-Volume|Clear-Disk)\b|\bdd\s+if=", re.I), "磁盘格式化或底层写入"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[A-Za-z]*f|push\s+(?:--force|-f)\b)", re.I), "不可逆 Git 覆盖或清理"),
    (re.compile(r"\b(?:curl|wget|Invoke-WebRequest|iwr)\b[^\r\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b|\biex\s*\(", re.I), "下载并直接执行远程脚本"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|Stop-Computer|Restart-Computer)\b", re.I), "关机或重启系统"),
    (re.compile(r"(?:^|[;&|]\s*)sudo\s+", re.I), "管理员提权命令"),
    (re.compile(r"\bchmod\s+(?:-R\s+)?777\b", re.I), "放宽文件全局写权限"),
)


def classify_risky_command(command: str) -> Optional[str]:
    text = (command or "").strip()
    return next((reason for pattern, reason in _RISKY_CMD_RULES if pattern.search(text)), None) if text else None


# ── Pipeline Primitives & 5 Interceptor Stages ────────────────────────────────

@dataclass
class ToolInvocation:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    ctx: Any
    skip_policy_gates: bool = False
    status: str = "ok"
    error_message: Optional[str] = None
    blocked_message: Optional[str] = None

    @property
    def chat_id(self) -> str:
        return str(getattr(self.ctx, "chat_id", "") or "")

    @property
    def facing_chat_id(self) -> str:
        return str(getattr(self.ctx, "facing_chat_id", "") or self.chat_id)

    @property
    def session_id(self) -> str:
        return str(getattr(self.ctx, "session_id", "") or "")

    @property
    def actor_kind(self) -> str:
        return str(getattr(self.ctx, "actor_kind", "companion") or "companion")

    @property
    def helper_id(self) -> Optional[str]:
        val = getattr(self.ctx, "helper_id", None)
        return str(val) if val else None


NextInterceptor = Callable[[ToolInvocation], Awaitable[str]]


@runtime_checkable
class ToolInterceptor(Protocol):
    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str: ...


def _preview_result(value: Optional[str], limit: int = 2000) -> Optional[str]:
    return None if value is None else (value if len(value) <= limit else f"{value[:limit]}…")


class ActivityEmitter:
    """Stage 1: Emits `tool_hint` (`start`/`end`) to `facing_chat_id` with zero SQLite queries."""

    def __init__(self, emit_fn: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None) -> None:
        self._emit_fn = emit_fn

    def bind(self, emit_fn: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._emit_fn = emit_fn

    async def _send(self, chat_id: str, event: dict[str, Any]) -> None:
        if chat_id and self._emit_fn is not None:
            with contextlib.suppress(Exception):
                await self._emit_fn(chat_id, event)

    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str:
        target_chat = call.facing_chat_id
        cid = call.call_id or f"call_{uuid.uuid4().hex[:8]}"
        args_payload = dict(call.arguments) if isinstance(call.arguments, dict) else {}
        base = {"version": 1, "call_id": cid, "name": call.tool_name, "arguments": args_payload, "files": [], "embeds": []}
        await self._send(target_chat, {**base, "phase": "start", "result": None, "error": None})
        try:
            result = await call_next(call)
        except Exception as exc:
            call.status = "error"
            call.error_message = f"{type(exc).__name__}: {exc}"
            await self._send(target_chat, {**base, "phase": "end", "result": None, "error": _preview_result(call.error_message)})
            raise
        shown = call.blocked_message if call.status == "blocked" else result
        err = _preview_result(call.error_message) if call.status == "error" else None
        await self._send(target_chat, {**base, "phase": "end", "result": _preview_result(shown), "error": err})
        return result


class SecretsGuardInterceptor:
    """Stage 2: Blocks sensitive paths/commands across file, shell, and browser calls."""

    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str:
        reason = check_tool_secrets(call.tool_name, call.arguments)
        if reason is not None:
            call.status = "blocked"
            call.blocked_message = reason
            return reason
        return await call_next(call)


class BrowserOwnershipInterceptor:
    """Stage 3: Enforces single-owner browser access, yields during user takeover, and guards teardown."""

    HELPER_BLOCK_MESSAGE = "帮手不能直接用浏览器。用 browser_request 把要在网页上做的事交给主助理，它做完会把结果告诉你。"
    USER_HOLD_MESSAGE = "用户正在亲自操作浏览器，等用户说好了再继续。现在不要操作浏览器，可以先做别的或者等一等。"

    def __init__(
        self, *, computer_mod: Any = None, is_helper_chat_fn: Optional[Callable[[str], bool]] = None,
        has_pending_browser_fn: Optional[Callable[[Optional[str]], bool]] = None,
        hold_wait_timeout_s: float = 15.0, poll_interval_s: float = 0.05,
    ) -> None:
        self._computer_mod = computer_mod
        self._is_helper_chat_fn = is_helper_chat_fn
        self._hold_wait_timeout_s = max(0.0, float(hold_wait_timeout_s))
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        if has_pending_browser_fn is not None:
            self.bind_pending_browser_check(has_pending_browser_fn)

    @property
    def computer(self) -> Any:
        self._computer_mod = _comp_mod(self._computer_mod, "computer")
        return self._computer_mod

    def bind_pending_browser_check(self, check_fn: Callable[[Optional[str]], bool]) -> None:
        with contextlib.suppress(Exception):
            self.computer.set_pending_browser_check(check_fn)

    def _is_helper(self, call: ToolInvocation) -> bool:
        if call.actor_kind == "desk":
            return False
        if call.actor_kind == "helper" or call.helper_id:
            return True
        if self._is_helper_chat_fn is not None and call.chat_id:
            return bool(self._is_helper_chat_fn(call.chat_id))
        if call.chat_id:
            with contextlib.suppress(Exception):
                return bool(_comp_mod(None, "helpers").helper_for_chat(call.chat_id))
        return False

    async def _wait_for_hold_release(self, comp: Any, cancel_scope: Any) -> bool:
        if not comp.holding():
            return True
        if self._hold_wait_timeout_s <= 0.0:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._hold_wait_timeout_s
        while comp.holding():
            if cancel_scope is not None and bool(getattr(cancel_scope, "cancelled", False)):
                raise asyncio.CancelledError
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self._poll_interval_s, remaining))
        return True

    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str:
        comp = self.computer
        if not comp.is_browser_tool(call.tool_name):
            return await call_next(call)
        if not call.skip_policy_gates and self._is_helper(call):
            call.status = "blocked"
            call.blocked_message = self.HELPER_BLOCK_MESSAGE
            return self.HELPER_BLOCK_MESSAGE
        if not call.skip_policy_gates and not await self._wait_for_hold_release(comp, getattr(call.ctx, "cancel_scope", None)):
            call.status = "blocked"
            call.blocked_message = self.USER_HOLD_MESSAGE
            return self.USER_HOLD_MESSAGE
        await asyncio.to_thread(comp.ensure_browser)
        result = await call_next(call)
        if call.status == "ok":
            comp.note_action(call.tool_name, call.arguments, call.chat_id or None)
        return result

    async def close_after_task(self, chat_id: str) -> None:
        if chat_id:
            await asyncio.to_thread(self.computer.close_after_task, chat_id)


class NativeApprovalCoordinator:
    """Manages Track 1 (`defer` replay) and Track 2 (`hold_exec` async Future suspension)."""

    def __init__(
        self, approvals: Any = None, *, adapter: Any = None,
        exec_runner: Optional[Callable[[ToolInvocation], Awaitable[str]]] = None,
        emit_activity_fn: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
        default_exec_timeout_s: float = 300.0,
    ) -> None:
        self.adapter = adapter
        self.approvals = approvals if approvals is not None else getattr(adapter, "approvals", None)
        self._exec_runner = exec_runner
        self._emit_activity_fn = emit_activity_fn if emit_activity_fn is not None else getattr(adapter, "emit_activity", None)
        self.default_exec_timeout_s = max(0.05, float(default_exec_timeout_s))
        self._held_futures: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._session_grants: set[tuple[str, str]] = set()
        if self.approvals is not None and callable(getattr(self.approvals, "bind_native_coordinator", None)):
            self.approvals.bind_native_coordinator(self)

    def bind_runner(self, runner: Callable[[ToolInvocation], Awaitable[str]]) -> None:
        self._exec_runner = runner

    def has_session_grant(self, session_key: str, grant_key: str) -> bool:
        return bool(session_key and grant_key and (session_key, grant_key) in self._session_grants)

    def add_session_grant(self, session_key: str, grant_key: str) -> None:
        if session_key and grant_key:
            self._session_grants.add((session_key, grant_key))

    def has_held_exec(self, approval_id: str) -> bool:
        fut = self._held_futures.get(approval_id)
        return fut is not None and not fut.done()

    def resolve_held_exec(self, approval_id: str, *, approve: bool, scope: str = "once") -> bool:
        fut = self._held_futures.pop(approval_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result((bool(approve), scope or "once"))
        return True

    async def hold_and_await_exec(
        self, call: ToolInvocation, *, command: str, reason: str, facing_chat: str, timeout_s: Optional[float] = None
    ) -> tuple[bool, str, Any]:
        from companion.approvals import DEFERRED_PREFIX, FAILED

        wait_s = self.default_exec_timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        sess_scope = call.session_id or call.chat_id or facing_chat
        record = self.approvals.store.create(
            session_key=f"websocket:{facing_chat}",
            call_id=call.call_id or f"exec_{uuid.uuid4().hex[:8]}",
            tool_name=call.tool_name,
            action="run_command",
            arguments={"command": command, "reason": reason},
            extra={
                "native_exec": True,
                "native_session": sess_scope,
                "choices": ["once", "session", "deny"],
                "expires_at_ms": int(time.time() * 1000) + int(max(1.0, wait_s) * 1000),
            },
        )
        with contextlib.suppress(Exception):
            self.approvals._notify(record)
        await self.approvals._emit_ws_async(record)
        if self._emit_activity_fn is not None:
            with contextlib.suppress(Exception):
                await self._emit_activity_fn(facing_chat, {
                    "version": 1, "phase": "end", "call_id": record.call_id, "name": call.tool_name,
                    "arguments": {"command": command}, "result": f"{DEFERRED_PREFIX} {record.id}",
                    "error": None, "files": [], "embeds": [],
                })
        fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._held_futures[record.id] = fut
        try:
            approved, scope = await asyncio.wait_for(fut, timeout=wait_s)
        except asyncio.TimeoutError:
            self._held_futures.pop(record.id, None)
            expired = self.approvals.store.claim_pending(record.id, FAILED)
            if expired is not None:
                expired.result = "等待确认超时，这一步已经跳过了"
                self.approvals.store.save(expired)
                await self.approvals._emit_ws_async(expired)
            return False, "等待用户确认超时，这条命令没有执行。", record
        except asyncio.CancelledError:
            self._held_futures.pop(record.id, None)
            cancelled = self.approvals.store.claim_pending(record.id, FAILED)
            if cancelled is not None:
                cancelled.result = "已取消，这一步没有执行"
                self.approvals.store.save(cancelled)
                with contextlib.suppress(Exception):
                    asyncio.get_running_loop().create_task(self.approvals._emit_ws_async(cancelled))
            raise
        except BaseException:
            self._held_futures.pop(record.id, None)
            raise
        if approved and scope in ("24h", "session", "always"):
            self.add_session_grant(sess_scope, f"run_command:{reason}")
        if not approved:
            return False, "用户在确认卡片上拒绝了运行这条命令，这一步没有执行。", record
        return True, "", record

    async def execute_approved(self, record: Any, *, skip_policy_gates: bool = True) -> str:
        if self._exec_runner is None:
            raise RuntimeError("NativeApprovalCoordinator runner is not bound")
        chat_id = str(getattr(record, "session_key", "") or "").split(":", 1)[-1]
        ctx = TurnExecutionContext(
            chat_id=chat_id, facing_chat_id=chat_id, session_id=f"approved:{record.id}",
            turn_id=f"apv-turn-{record.id}", actor_kind="companion",
        )
        call = ToolInvocation(
            call_id=str(getattr(record, "call_id", "") or f"call_{uuid.uuid4().hex[:8]}"),
            tool_name=str(record.tool_name),
            arguments=dict(record.arguments) if isinstance(record.arguments, dict) else {},
            ctx=ctx, skip_policy_gates=skip_policy_gates,
        )
        with turn_context(ctx):
            return await self._exec_runner(call)


_HANDED_APPROVAL_SUFFIX = "（确认卡片已放到用户正在看的聊天里，用户同意后主助理会在那边接着做。先把做到哪一步写出来。）"
_NO_CHAT_APPROVAL_MSG = "这个操作需要用户确认，但现在没法让用户确认，所以没有执行。"


class TaintAndPermissionsInterceptor:
    """Stage 4: Egress 3600s taint check + permissions check + Track 1 (`defer`) & Track 2 (`hold_exec`)."""

    def __init__(
        self, coordinator: Optional[NativeApprovalCoordinator] = None, *, egress_mod: Any = None,
        permissions_mod: Any = None, computer_mod: Any = None,
        home_chat_fn: Optional[Callable[[], Optional[str]]] = None, exec_timeout_s: Optional[float] = None,
    ) -> None:
        self.coordinator = coordinator
        self._egress_mod = egress_mod
        self._permissions_mod = permissions_mod
        self._computer_mod = computer_mod
        self._home_chat_fn = home_chat_fn
        self._exec_timeout_s = exec_timeout_s

    def _resolve_home_chat(self) -> Optional[str]:
        if self._home_chat_fn is not None:
            return self._home_chat_fn()
        with contextlib.suppress(Exception):
            cid = _comp_mod(None, "companion_state").read_companion_state().get("chat_id")
            return str(cid) if cid else None
        return None

    async def _forward_and_note_taint(self, eg: Any, call: ToolInvocation, call_next: NextInterceptor) -> str:
        result = await call_next(call)
        if call.status == "ok":
            eg.note(call.session_id or None, call.tool_name, chat_id=call.chat_id or None)
        return result

    @staticmethod
    def _block(call: ToolInvocation, message: str) -> str:
        call.status = "blocked"
        call.blocked_message = message
        return message

    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str:
        eg = _comp_mod(self._egress_mod, "egress")
        if call.skip_policy_gates:
            return await self._forward_and_note_taint(eg, call, call_next)

        from companion.approvals import approval_action

        perms = _comp_mod(self._permissions_mod, "permissions")
        comp = _comp_mod(self._computer_mod, "computer")
        is_browser = comp.is_browser_tool(call.tool_name)
        base_action = approval_action(call.tool_name, call.arguments)
        egress_target = eg.check(call.session_id or None, call.tool_name, call.arguments, chat_id=call.chat_id or None)
        host = (egress_target or {}).get("host")
        base_kind = perms.kind_of(call.tool_name, call.arguments, base_action, is_browser)
        base_level = perms.level_of(base_kind) if base_kind else perms.ALLOW
        if base_kind and base_level == perms.DENY:
            return self._block(call, perms.denied_message(base_kind))
        egress_level = perms.level_of("send_private") if egress_target else perms.ALLOW
        if egress_level == perms.DENY:
            return self._block(call, perms.denied_message("send_private"))

        if base_kind == "run_command" and isinstance(call.arguments, dict):
            cmd = str(call.arguments.get("command") or call.arguments.get("code") or "")
            risk_reason = classify_risky_command(cmd)
            if risk_reason is not None:
                coord = self.coordinator
                if coord is None:
                    return self._block(call, _NO_CHAT_APPROVAL_MSG)
                sess_scope = call.session_id or call.chat_id or call.facing_chat_id
                if not (coord.approvals.granted("run_command", {"reason": risk_reason}) or coord.has_session_grant(sess_scope, f"run_command:{risk_reason}")):
                    facing = call.facing_chat_id or self._resolve_home_chat() or ""
                    if not facing:
                        return self._block(call, _NO_CHAT_APPROVAL_MSG)
                    ok, block_msg, _ = await coord.hold_and_await_exec(
                        call, command=cmd, reason=risk_reason, facing_chat=facing, timeout_s=self._exec_timeout_s
                    )
                    if not ok:
                        return self._block(call, block_msg)

        if base_action == "goal_plan":
            action, kind = "goal_plan", base_kind
        else:
            base_needs_ask = bool(base_kind and base_level == perms.ASK and (base_action or base_kind != "run_command"))
            egress_needs_ask = bool(egress_target and egress_level == perms.ASK)
            if not base_needs_ask and not egress_needs_ask:
                return await self._forward_and_note_taint(eg, call, call_next)
            action = (base_action or base_kind) if base_needs_ask else "send_private"
            kind = base_kind if base_needs_ask else "send_private"

        if not action:
            return await self._forward_and_note_taint(eg, call, call_next)
        if self.coordinator is not None:
            apv = self.coordinator.approvals
            base_covered = (not base_action and action == "send_private") or apv.granted(action, call.arguments, host)
            egress_covered = (not egress_target or egress_level == perms.ALLOW or apv.granted("send_private", call.arguments, host))
            if base_covered and egress_covered and action != "goal_plan":
                return await self._forward_and_note_taint(eg, call, call_next)

        session_chat = call.chat_id or self._resolve_home_chat()
        facing_chat = call.facing_chat_id or session_chat
        if not facing_chat or self.coordinator is None:
            return self._block(call, _NO_CHAT_APPROVAL_MSG)
        message = self.coordinator.approvals.defer(
            facing_chat, call.call_id, call.tool_name, call.arguments, action, egress_target, kind=kind
        )
        if session_chat and facing_chat != session_chat:
            message += _HANDED_APPROVAL_SUFFIX
        return self._block(call, message)


_MEDIA_GEN_TOOL = re.compile(r"(image|video|audio|music|speech|tts)", re.IGNORECASE)


class PostToolArtifactInterceptor:
    """Stage 5: Archives generated images/videos/audio to `creations` and records `media-usage.jsonl`."""

    def __init__(self, *, creations_mod: Any = None, models_mod: Any = None) -> None:
        self._creations_mod = creations_mod
        self._models_mod = models_mod

    async def _archive_if_generated(self, tool_name: str, args: dict[str, Any], result: str) -> None:
        low = (tool_name or "").lower()
        if not result or not _MEDIA_GEN_TOOL.search(low) or low.startswith("browser") or "browser_" in low or "vision" in low:
            return
        cr = _comp_mod(self._creations_mod, "creations")
        raw_prompt = args.get("prompt") if isinstance(args, dict) else None
        prompt = raw_prompt.strip() if isinstance(raw_prompt, str) and raw_prompt.strip() else None
        with contextlib.suppress(Exception):
            await cr.collect_generated(result, title=cr.safe_title(prompt, 30) if prompt else "", prompt=prompt)

    def _record_media_usage(self, tool_name: str, args: dict[str, Any], result: str) -> None:
        if tool_name not in ("image_generate", "video_generate") or not result:
            return
        with contextlib.suppress(Exception):
            parsed = json.loads(result)
            if not isinstance(parsed, dict) or not parsed.get("success"):
                return
            models = _comp_mod(self._models_mod, "models")
            if tool_name == "image_generate":
                models.record_media("image")
            else:
                dur = args.get("duration") if isinstance(args.get("duration"), (int, float)) else 6
                models.record_media("video", seconds=float(dur), resolution=str(args.get("resolution") or "720p"))

    async def intercept(self, call: ToolInvocation, call_next: NextInterceptor) -> str:
        result = await call_next(call)
        if call.status == "ok":
            await self._archive_if_generated(call.tool_name, call.arguments, result)
            await asyncio.to_thread(self._record_media_usage, call.tool_name, call.arguments, result)
        return result


ToolExecutorFn = Callable[[str, dict[str, Any], ToolInvocation], Any]


class ToolInterceptorPipeline:
    """Executes tool calls through the 5-stage security & activity middleware chain."""

    def __init__(
        self, executor: ToolExecutorFn, *, interceptors: Optional[list[ToolInterceptor]] = None,
        bounder: Optional[ToolResultBounder] = None, coordinator: Optional[NativeApprovalCoordinator] = None,
    ) -> None:
        self._executor = executor
        self.bounder = bounder or ToolResultBounder()
        emit_fn = coordinator._emit_activity_fn if coordinator is not None else None
        pending_fn = getattr(coordinator.approvals, "has_pending_browser", None) if (coordinator and coordinator.approvals) else None
        self.interceptors: list[ToolInterceptor] = list(interceptors) if interceptors is not None else [
            ActivityEmitter(emit_fn),
            SecretsGuardInterceptor(),
            BrowserOwnershipInterceptor(has_pending_browser_fn=pending_fn),
            TaintAndPermissionsInterceptor(coordinator),
            PostToolArtifactInterceptor(),
        ]
        if coordinator is not None:
            coordinator.bind_runner(self.execute_invocation)

    async def _invoke_leaf(self, call: ToolInvocation) -> str:
        raw = self._executor(call.tool_name, call.arguments, call)
        if asyncio.iscoroutine(raw) or isinstance(raw, Awaitable):
            raw = await raw
        return self.bounder.bound(raw, tool_name=call.tool_name)

    async def execute_invocation(self, call: ToolInvocation) -> str:
        chain: NextInterceptor = self._invoke_leaf
        for stage in reversed(self.interceptors):
            prev = chain
            chain = lambda inv, st=stage, nxt=prev: st.intercept(inv, nxt)  # noqa: E731
        return await chain(call)

    async def execute(
        self, tool_name: str, arguments: Optional[dict[str, Any]] = None, *,
        call_id: str = "", ctx: Any = None, skip_policy_gates: bool = False,
    ) -> str:
        inv = ToolInvocation(
            call_id=call_id or f"call_{uuid.uuid4().hex[:8]}",
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            ctx=resolve_turn_context(ctx),
            skip_policy_gates=skip_policy_gates,
        )
        return await self.execute_invocation(inv)
