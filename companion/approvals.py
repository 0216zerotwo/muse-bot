"""Human approval for tool calls that act in the user's name (ported from nanobot).

Some tools reach outside the agent on the user's behalf: sending or forwarding
email, deleting mail or calendar events, committing something on a web page.
When the model calls one, the ``pre_tool_call`` hook blocks it: the exact
arguments are recorded as a pending approval and the model is told the action
awaits the user. The app renders that record as a card; only an explicit
approval from the signed-in user runs it, with the recorded arguments, so the
model cannot change what was approved. The decision is then reported back into
the chat as a short turn, so the companion can reply.

Hermes's own confirmation prompts (risky shell commands) arrive through the
adapter's ``send_exec_approval`` and are shown with the same cards; those are
answered through Hermes's approval queue instead (the agent is waiting on them).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .adapter import CompanionAdapter

logger = logging.getLogger(__name__)

# MCP tools are ``mcp__<server>__<tool>`` (or legacy ``mcp_<server>_<tool>``); any server exposing these tools is gated.
_NAMED_ACTIONS = {
    "send_email": "发送邮件",
    "forward_email": "转发邮件",
    "delete_emails": "删除邮件",
    "email_command": "执行邮件协议命令",
    "delete_event": "删除日程",
}
_TOOL_ACTION_ALIASES = {
    "send_email": "send_email",
    "send_mail": "send_email",
    "reply_email": "send_email",
    "reply_to_email": "send_email",
    "send_message": "send_email",
    "forward_email": "forward_email",
    "forward_mail": "forward_email",
    "delete_emails": "delete_emails",
    "delete_email": "delete_emails",
    "trash_email": "delete_emails",
    "trash_emails": "delete_emails",
    "email_command": "email_command",
    "delete_event": "delete_event",
    "delete_events": "delete_event",
    "remove_event": "delete_event",
    "cancel_event": "delete_event",
}
APPROVAL_ACTIONS = {
    **_NAMED_ACTIONS,
    "browser_submit": "在网页上点按钮",
    "browser_upload": "上传文件到网页",
    "run_command": "运行命令",
    "goal_plan": "修改目标计划",
    "send_private": "把信息发到网站",
    # Set to 询问我 in 设置 → 权限 (otherwise these run without asking).
    "browse": "打开网页",
    "payment": "付款或下单",
    "connector_read": "读取连接的服务",
    "connector_write": "在连接的服务里修改",
    "schedule": "设定时任务",
}
_NAMED_TOOL = re.compile(
    r"^mcp__?.+?__?(" + "|".join(sorted(_TOOL_ACTION_ALIASES, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)
_MCP_TOOL = re.compile(r"^mcp__?.+?__?(browser_click|browser_file_upload|browser_drop)$", re.IGNORECASE)
_CONSEQUENTIAL_CLICK = re.compile(
    r"提交|确认|确定|付款|支付|购买|下单|结算|预订|预约|订购|发送|发布|注册|删除|转账|捐|签署|订阅|退订"
    r"|\b(submit|confirm|pay|purchase|buy|checkout|check\s*out|place\s+order|order|book|reserve|send|post"
    r"|publish|sign\s*up|register|delete|remove|transfer|donate|subscribe|unsubscribe)\b",
    re.IGNORECASE,
)
# Hermes's own browser tool runs Python against the page (``browser_exec``): what it interacts
# with is only in its code — a click by coordinates names its target in the code that finds it.
_EXEC_INTERACTS = re.compile(
    r"click_at_xy|fill_input|\.click\(|dispatchMouseEvent|dispatchKeyEvent|Input\.insertText|\.submit\(|requestSubmit")
_EXEC_UPLOAD = re.compile(r"setFileInputFiles|set_input_files")
_STRING = re.compile(r"""(["'])((?:(?!\1).){1,200})\1""")
# A page's address (https://…, //…, /shop/…, www.…): where the script goes, not what it clicks. A
# store's addresses are full of such words (apple.com.cn/shop/buy-mac/…), and reading a price
# there once came up as an order to approve.
_ADDRESS = re.compile(r"^\s*(?:[a-z][\w+.-]*://|//|/|www\.)", re.IGNORECASE)


def exec_target(code: str) -> Optional[str]:
    """The consequential thing a ``browser_exec`` script names in a string (``立即付款``), if any."""
    for match in _STRING.finditer(code or ""):
        text = match.group(2)
        if not _ADDRESS.match(text) and _CONSEQUENTIAL_CLICK.search(text):
            return text[:60]
    return None


# Where a browser script goes: goto_url("…"), new_tab("…"), navigate(url="…").
_EXEC_GOES = re.compile(r"""\b(?:goto_url|new_tab|navigate|goto)\(\s*(?:url\s*=\s*)?(["'])(.+?)\1""")
_FILE_NAME = re.compile(r"[^/\\]+\.[A-Za-z0-9]{1,5}$")
# "购买 MacBook Air - Apple (中国大陆)": the page's own name, before the site's.
_TITLE_TAIL = re.compile(r"\s+[-|–—·]\s+")
_BROWSER_ACTIONS = ("browser_submit", "browser_upload", "browse")


def browser_context(arguments: Any, action: str, page: Optional[dict[str, Any]] = None,
                    paying: bool = False) -> Optional[dict[str, Any]]:
    """A held step in the browser, in words for its card: the site, the page, what it presses, and
    what pressing it does. ``page`` is the page open in the browser (``url``, ``title``), for a step
    that doesn't say where it goes."""
    if action not in _BROWSER_ACTIONS:
        return None
    args = arguments if isinstance(arguments, dict) else {}
    code = str(args.get("code") or "")
    gone = [match.group(2) for match in _EXEC_GOES.finditer(code)]
    url, title = (gone[-1] if gone else str(args.get("url") or "")), ""
    if not url and page:
        url = str(page.get("url") or "")
        title = _TITLE_TAIL.split(str(page.get("title") or "").strip())[0][:30]
    host = (urlparse(url).hostname or "") if "://" in url else ""
    host = host[4:] if host.startswith("www.") else host
    place = f"{host}「{title}」页面" if host and title else host
    on = f" {place}上" if title else f" {host} 上" if host else "网页上"
    if action == "browse":
        summary = f"要打开 {host} 的网页。" if host else "要接着在网页上操作。"
        return {"host": host, "page": title, "target": "", "paying": False, "summary": summary}
    if action == "browser_upload":
        paths = args.get("paths") if isinstance(args.get("paths"), list) else [
            text for _, text in _STRING.findall(code) if not _ADDRESS.match(text) and _FILE_NAME.search(text)]
        names = "、".join(f"「{Path(str(p)).name}」" for p in paths[:3]) or "文件"
        summary = f"要把{names}上传到{f' {place}' if place else '网页上'}。传上去以后，这个网站就能看到文件内容。"
        return {"host": host, "page": title, "target": "", "paying": False, "summary": summary}
    target = str(args.get("element") or "") or exec_target(code) or ""
    pressed = f"点「{target}」" if target else "点一个按钮"
    after = "点了可能就下单或付款了，看清楚是不是你要的再确认。" if paying else "点了就会提交出去，可能没法撤回。"
    return {"host": host, "page": title, "target": target, "paying": paying, "summary": f"要在{on}{pressed}。{after}"}


# The block message starts with this; the app recognizes held calls by it.
DEFERRED_PREFIX = "[等待用户确认]"
_RESULT_PREVIEW_CHARS = 600
# Some MCP servers (e.g. the iCloud calendar) report failures as ordinary text.
_FAILURE_TEXT_PREFIXES = ("Error", "Failed")

# How long a yes on a card lasts. "once" covers that call only; the others become standing grants.
SCOPES = ("once", "24h", "always")
_DAY_MS = 24 * 3600 * 1000

PENDING = "pending"
EXECUTED = "executed"
FAILED = "failed"
REJECTED = "rejected"


def approval_action(tool_name: str, arguments: Any = None) -> Optional[str]:
    """The gated action a tool call performs, or ``None`` when it runs without approval."""
    if tool_name == "goal_propose_plan":
        # The companion never rewrites a plan on its own: the user picks 用新计划 or 保持原样.
        return "goal_plan"
    named = _NAMED_TOOL.match(tool_name or "")
    if named:
        return _TOOL_ACTION_ALIASES[named.group(1).lower()]
    if tool_name == "browser_exec":
        code = str((arguments if isinstance(arguments, dict) else {}).get("code") or "")
        if _EXEC_UPLOAD.search(code):
            return "browser_upload"
        return "browser_submit" if _EXEC_INTERACTS.search(code) and exec_target(code) else None
    browser = _MCP_TOOL.match(tool_name or "")
    if not browser:
        return None
    args = arguments if isinstance(arguments, dict) else {}
    if browser.group(1).lower() == "browser_click":
        element = args.get("element")
        if isinstance(element, str) and _CONSEQUENTIAL_CLICK.search(element):
            return "browser_submit"
        return None
    # No paths means closing the file chooser, which sends nothing.
    return "browser_upload" if args.get("paths") else None


def _recipients_list(args: dict[str, Any]) -> list[str]:
    raw = args.get("recipients", args.get("to"))
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        for part in re.split(r"[,;]", item):
            clean = part.strip()
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                out.append(clean)
    return out


def _recipients(args: dict[str, Any]) -> list[str]:
    return sorted({part.lower() for part in _recipients_list(args)})


def _email_ids(args: dict[str, Any]) -> list[str]:
    for field_name in ("email_ids", "email_id", "message_ids", "message_id", "ids"):
        if field_name in args and args[field_name] is not None:
            raw = args[field_name]
            break
    else:
        return []
    if isinstance(raw, (list, tuple)):
        items = raw
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [raw]
    out: list[str] = []
    for item in items:
        if isinstance(item, bool) or item is None:
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def grant_keys(action: str, args: dict[str, Any], host: Optional[str] = None) -> list[str]:
    """What a standing yes for this call binds to: the action plus its target, one key per target
    (an email to several people needs every recipient covered). Empty when only a one-off yes is
    offered: deletions, raw mail commands and web page clicks have no target worth trusting."""
    if action in ("send_email", "forward_email"):
        return [f"{action}:{r}" for r in _recipients(args)]
    if action == "run_command":
        # Hermes asks for a command because it matches a risky pattern; the grant covers that pattern.
        reason = args.get("reason")
        return [f"run_command:{reason}"] if isinstance(reason, str) and reason.strip() else []
    if action == "send_private":
        # After reading private data: a yes covers that one site (see modules/egress).
        return [f"send_private:{host}"] if host and host != "未知网站" else []
    return []


def grant_label(key: str) -> str:
    action, _, target = key.partition(":")
    verb = APPROVAL_ACTIONS.get(action, action)
    return f"{verb}给 {target}" if action in ("send_email", "forward_email") else f"{verb}（{target}）"


@dataclass
class Grant:
    key: str
    scope: str
    created_at_ms: int
    expires_at_ms: Optional[int] = None

    def live(self, now_ms: int) -> bool:
        return self.expires_at_ms is None or self.expires_at_ms > now_ms

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "label": grant_label(self.key)}


class GrantStore:
    """Standing yeses in ``<companion dir>/approval-grants.json`` (from a card: a day at most)."""

    def __init__(self, root: Path):
        self.path = Path(root) / "approval-grants.json"
        self._lock = threading.Lock()

    def _read(self) -> list[Grant]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            grants = [Grant(**row) for row in rows]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("companion: unreadable grants %s: %s", self.path, exc)
            return []
        now = _now_ms()
        return [g for g in grants if g.live(now)]

    def _write(self, grants: list[Grant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_text(json.dumps([asdict(g) for g in grants], ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def list(self) -> list[Grant]:
        with self._lock:
            return sorted(self._read(), key=lambda g: -g.created_at_ms)

    def covers(self, keys: list[str]) -> bool:
        if not keys:
            return False
        with self._lock:
            live = {g.key for g in self._read()}
        return all(key in live for key in keys)

    def add(self, keys: list[str], scope: str) -> None:
        now = _now_ms()
        expires = now + _DAY_MS if scope == "24h" else None
        with self._lock:
            grants = [g for g in self._read() if g.key not in keys]
            grants += [Grant(key=key, scope=scope, created_at_ms=now, expires_at_ms=expires) for key in keys]
            self._write(grants)

    def revoke(self, key: Optional[str]) -> None:
        """Revoke one grant, or every grant when ``key`` is None."""
        with self._lock:
            self._write([g for g in self._read() if key is not None and g.key != key])


@dataclass
class ApprovalRecord:
    id: str
    session_key: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = PENDING
    created_at_ms: int = 0
    decided_at_ms: Optional[int] = None
    result: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    action: str = ""
    scope: str = "once"

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", None) or {}
        args = dict(self.arguments) if isinstance(self.arguments, dict) else {}
        action = self.action or approval_action(self.tool_name, args) or ""
        if action in ("send_email", "forward_email"):
            recipients = _recipients_list(args)
            if recipients:
                args["recipients"] = recipients
        elif action == "delete_emails":
            email_ids = _email_ids(args)
            if email_ids:
                args["email_ids"] = email_ids
        data["arguments"] = args
        data["expires_at_ms"] = extra.get("expires_at_ms")
        context = {key: extra[key] for key in ("goal", "egress", "browser") if extra.get(key)}
        if context:
            data["context"] = context
        # The card offers the standing scopes only when the call has a target to bind them to.
        # A standing yes on a card lasts a day at most; for good, the user sets 允许 in 设置 → 权限.
        data["scopes"] = ["once", "24h"] if grant_keys(action, args, _host(self)) else ["once"]
        return data


def _host(record: "ApprovalRecord") -> Optional[str]:
    return (record.extra.get("egress") or {}).get("host")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _open_page() -> Optional[dict[str, Any]]:
    """The page open in the shared browser (``url``, ``title``), if it can be asked."""
    try:
        from .modules import computer

        return computer.status().get("page")
    except Exception:
        return None


class ApprovalStore:
    """One JSON file per approval under ``<companion dir>/approvals``."""

    def __init__(self, root: Path):
        self.root = Path(root) / "approvals"
        self._lock = threading.Lock()

    def _path(self, approval_id: str) -> Path:
        return self.root / f"{approval_id}.json"

    def _write(self, record: ApprovalRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.id)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def create(self, *, session_key: str, call_id: str, tool_name: str, arguments: dict[str, Any],
               action: str, extra: Optional[dict[str, Any]] = None) -> ApprovalRecord:
        record = ApprovalRecord(
            id=f"apv_{uuid.uuid4().hex[:12]}", session_key=session_key, call_id=call_id,
            tool_name=tool_name, arguments=arguments, created_at_ms=_now_ms(), action=action,
            extra=dict(extra or {}),
        )
        with self._lock:
            self._write(record)
        return record

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        if not approval_id.startswith("apv_") or "/" in approval_id or "\\" in approval_id:
            return None
        path = self._path(approval_id)
        if not path.is_file():
            return None
        try:
            return ApprovalRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("companion: unreadable approval %s: %s", path, exc)
            return None

    def list_all(self) -> list[ApprovalRecord]:
        """Every chat's approvals, newest first (审核记录)."""
        if not self.root.is_dir():
            return []
        records = [r for p in self.root.glob("apv_*.json") if (r := self.get(p.stem))]
        return sorted(records, key=lambda r: -r.created_at_ms)

    def list_for_session(self, session_key: str) -> list[ApprovalRecord]:
        if not self.root.is_dir():
            return []
        records = [r for p in self.root.glob("apv_*.json") if (r := self.get(p.stem)) and r.session_key == session_key]
        return sorted(records, key=lambda r: r.created_at_ms)

    def claim_pending(self, approval_id: str, status: str) -> Optional[ApprovalRecord]:
        """Atomically move a pending approval to ``status``; ``None`` if already decided."""
        with self._lock:
            record = self.get(approval_id)
            if record is None or record.status != PENDING:
                return None
            record.status = status
            record.decided_at_ms = _now_ms()
            self._write(record)
            return record

    def save(self, record: ApprovalRecord) -> None:
        with self._lock:
            self._write(record)


def describe_approval(record: ApprovalRecord) -> str:
    args = record.arguments
    action = record.action or approval_action(record.tool_name, args)
    verb = APPROVAL_ACTIONS.get(action or "", record.tool_name)
    browser = record.extra.get("browser") or {}
    if browser.get("summary"):
        # "要在 apple.com.cn 上点「立即购买」": what it is, without the advice after it.
        return str(browser["summary"]).split("。")[0]
    parts = [verb]
    element = args.get("element")
    if action == "browser_submit" and isinstance(element, str) and element:
        parts.append(f"点击「{element}」")
    recipients = _recipients_list(args)
    if recipients:
        parts.append("收件人 " + "、".join(recipients))
    subject = args.get("subject")
    if isinstance(subject, str) and subject:
        parts.append(f"主题「{subject}」")
    goal = record.extra.get("goal")
    if action == "goal_plan" and isinstance(goal, dict):
        parts.append(f"「{goal.get('title')}」")
    if action == "send_private" and _host(record):
        parts.append(f"发到 {_host(record)}")
    command = args.get("command")
    if action == "run_command" and isinstance(command, str) and command:
        parts.append(f"「{command[:80]}」")
    return "，".join(parts)


def _failed(result: str) -> bool:
    text = result.lstrip()
    if text.startswith(_FAILURE_TEXT_PREFIXES):
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("error")) and set(data) <= {"error", "success", "details"}


class Approvals:
    """The adapter's binding of the store to tool execution and chat delivery."""

    def __init__(self, adapter: "CompanionAdapter", root: Path):
        self.adapter = adapter
        self.store = ApprovalStore(root)
        self.grants = GrantStore(root)
        self._native_coordinator: Any = None
        with contextlib.suppress(Exception):
            from .modules import computer

            computer.set_pending_browser_check(self.has_pending_browser)

    def bind_native_coordinator(self, coordinator: Any) -> None:
        self._native_coordinator = coordinator

    def _resolve_native_coordinator(self) -> Any:
        if self._native_coordinator is not None:
            return self._native_coordinator
        engine = getattr(self.adapter, "_engine", None)
        return getattr(engine, "approval_coordinator", None) or getattr(engine, "_approval_coordinator", None)

    def has_pending_browser(self, chat_id: Optional[str] = None) -> bool:
        """True when ``chat_id`` (or any chat, when ``chat_id`` is None) has a pending browser approval."""
        from .modules import computer

        records = (
            self.store.list_for_session(f"websocket:{chat_id}")
            if chat_id
            else self.store.list_all()
        )
        return any(
            r.status == PENDING and (r.action in _BROWSER_ACTIONS or computer.is_browser_tool(r.tool_name))
            for r in records
        )

    def granted(self, action: str, arguments: Any, host: Optional[str] = None) -> bool:
        """The user already said yes to this action and target for a while."""
        args = arguments if isinstance(arguments, dict) else {}
        return self.grants.covers(grant_keys(action, args, host))

    # -- held tool calls (pre_tool_call, on the agent's thread) --------------------------------------

    def defer(self, chat_id: str, call_id: str, tool_name: str, arguments: Any, action: str,
              egress: Optional[dict[str, str]] = None, kind: Optional[str] = None) -> str:
        args = dict(arguments) if isinstance(arguments, dict) else {"value": arguments}
        extra: dict[str, Any] = {"egress": egress} if egress else {}
        if action in _BROWSER_ACTIONS:
            # The card says where and what in words (a script or an address alone means nothing).
            extra["browser"] = browser_context(args, action, _open_page(), paying=kind == "payment")
        if tool_name == "browser_exec" and not args.get("element"):
            # For older apps' 点击 line (the script itself ignores it).
            args["element"] = exec_target(str(args.get("code") or "")) or "网页上的操作"
        if action == "goal_plan":
            from .modules.plans import goal_for_approval

            goal = goal_for_approval(args.get("goal_id"))
            if goal is None:
                return "没有这个目标，先用 goal_list 看看现有目标的 id。"
            # The card shows the plan as it is now next to the proposal.
            extra["goal"] = {"title": goal["title"], "steps": goal["steps"]}
        record = self.store.create(
            session_key=f"websocket:{chat_id}", call_id=call_id or f"call_{uuid.uuid4().hex[:8]}",
            tool_name=tool_name, action=action, arguments=args, extra=extra,
        )
        logger.info("companion: tool call %s held for approval as %s", tool_name, record.id)
        self._notify(record)
        self._emit_ws_sync(record)
        return (
            f"{DEFERRED_PREFIX} 这个操作没有执行，已作为确认卡片展示给用户（{record.id}）。"
            "用户会在聊天里看到完整内容并决定是否执行；结果会另行通知你。"
            "不要重复调用这个工具，也不要说已经完成。用一句话告诉用户内容已准备好、请他确认。"
        )

    # -- Hermes's own prompts (send_exec_approval) ----------------------------------------------------

    def hold_exec(self, chat_id: str, command: str, description: str, hermes_session: str,
                  choices: list[str], *, expires_in_s: Optional[float] = None) -> ApprovalRecord:
        if expires_in_s is None:
            expires_in_s = 300.0
            with contextlib.suppress(Exception):
                from tools.approval_context import _get_approval_timeout

                expires_in_s = float(_get_approval_timeout())
        now_ms = _now_ms()
        record = self.store.create(
            session_key=f"websocket:{chat_id}", call_id=f"exec_{uuid.uuid4().hex[:8]}",
            tool_name="terminal", action="run_command",
            arguments={"command": command, "reason": description},
            extra={
                "hermes_session": hermes_session,
                "choices": choices,
                "expires_at_ms": now_ms + int(max(1.0, expires_in_s) * 1000),
            },
        )
        self._notify(record)
        self._emit_ws_sync(record)
        return record

    def _emit_ws_sync(self, record: ApprovalRecord) -> None:
        emit = getattr(self.adapter, "emit_approval_updated", None)
        loop = getattr(self.adapter, "loop", None)
        if not callable(emit):
            return
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(emit(record), loop)
                return
        with contextlib.suppress(RuntimeError):
            running = asyncio.get_running_loop()
            running.create_task(emit(record))

    async def _emit_ws_async(self, record: ApprovalRecord) -> None:
        emit = getattr(self.adapter, "emit_approval_updated", None)
        if callable(emit):
            with contextlib.suppress(Exception):
                await emit(record)

    def _notify(self, record: ApprovalRecord) -> None:
        """A notification on the phone: something is waiting for the user's yes."""
        from .modules import push

        push.schedule_push(self.adapter.companion_name(), f"等你确认：{describe_approval(record)}",
                           tag="approval", loop=getattr(self.adapter, "loop", None))

    # -- the user's decision -----------------------------------------------------------------------------

    async def decide(self, approval_id: str, approve: bool, scope: str = "once") -> Optional[ApprovalRecord]:
        record = self.store.claim_pending(approval_id, EXECUTED if approve else REJECTED)
        if record is None:
            return None
        keys = grant_keys(record.action, record.arguments, _host(record))
        if approve and scope in SCOPES and scope != "once" and keys:
            record.scope = scope
            self.grants.add(keys, scope)
        if record.extra.get("native_exec"):
            if approve and scope in (*SCOPES, "session"):
                record.scope = scope
            decided = self._answer_native_exec(record, approve, scope=scope)
            await self._emit_ws_async(decided)
            return decided
        if record.extra.get("hermes_session"):
            decided = self._answer_exec(record, approve)
            await self._emit_ws_async(decided)
            return decided
        if approve:
            result = await self._execute_record(record)
            record.status = FAILED if _failed(result) else EXECUTED
            record.result = result[:_RESULT_PREVIEW_CHARS]
            self.store.save(record)
        await self._emit_ws_async(record)
        await self._report(record)
        return record

    async def _execute_record(self, record: ApprovalRecord) -> str:
        from muse.paths import active_engine_name

        coord = self._resolve_native_coordinator()
        engine = getattr(self.adapter, "_engine", None)
        use_native = (
            active_engine_name() == "native"
            or coord is not None
            or callable(getattr(engine, "execute_approved", None))
        )
        has_native_runner = (
            (coord is not None and getattr(coord, "_exec_runner", None) is not None)
            or callable(getattr(engine, "execute_approved", None))
        )
        if not use_native or not has_native_runner:
            return await asyncio.to_thread(self._run, record)
        try:
            from .modules import computer

            if record.action in _BROWSER_ACTIONS or computer.is_browser_tool(record.tool_name):
                await asyncio.to_thread(computer.ensure_browser)
            if coord is not None and getattr(coord, "_exec_runner", None) is not None:
                raw = await coord.execute_approved(record, skip_policy_gates=True)
            else:
                raw = await engine.execute_approved(record, skip_policy_gates=True)
        except Exception as exc:
            logger.exception("companion: native approved call %s failed", record.id)
            return f"Error: {exc}"
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)

    def _answer_native_exec(self, record: ApprovalRecord, approve: bool, *, scope: str = "once") -> ApprovalRecord:
        coord = self._resolve_native_coordinator()
        effective_scope = scope if (approve and scope in (*SCOPES, "session")) else record.scope
        resolved = (
            coord.resolve_held_exec(record.id, approve=approve, scope=effective_scope)
            if coord is not None
            else False
        )
        if not resolved:
            record.status = FAILED
            record.result = "等待确认超时，这一步已经跳过了"
        self.store.save(record)
        return record

    def _run(self, record: ApprovalRecord) -> str:
        try:
            from .modules import computer

            if record.action in _BROWSER_ACTIONS or computer.is_browser_tool(record.tool_name):
                computer.ensure_browser()
            from model_tools import handle_function_call

            result = handle_function_call(
                record.tool_name, dict(record.arguments), tool_call_id=record.call_id,
                skip_pre_tool_call_hook=True,
            )
        except Exception as exc:  # the card must show the failure, not hang
            logger.exception("companion: approved call %s failed", record.id)
            return f"Error: {exc}"
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    def _answer_exec(self, record: ApprovalRecord, approve: bool) -> ApprovalRecord:
        from tools.approval import resolve_gateway_approval

        # A standing yes also lets the rest of this run go on without asking again.
        choice = ("session" if record.scope != "once" else "once") if approve else "deny"
        resolved = resolve_gateway_approval(record.extra["hermes_session"], choice)
        if not resolved:
            # The agent stopped waiting (timed out or the turn ended): nothing ran.
            record.status = FAILED
            record.result = "等待确认超时，这一步已经跳过了"
        self.store.save(record)
        return record

    async def _report(self, record: ApprovalRecord) -> None:
        """A short turn in the originating chat so the companion tells the user the outcome."""
        chat_id = record.session_key.split(":", 1)[-1]
        what = describe_approval(record)
        if record.status == EXECUTED:
            content = (
                f"【系统通知】用户在确认卡片上批准了：{what}。已执行成功。"
                f"返回结果：{record.result or '（无）'}\n用一两句话自然地告诉用户已经办好，不要再次调用这个工具。"
            )
        elif record.status == FAILED:
            content = f"【系统通知】用户批准了：{what}，但执行失败：{record.result}\n告诉用户失败原因，并建议怎么处理。"
        else:
            content = f"【系统通知】用户在确认卡片上选择不执行：{what}。简短回应一句，并问是否需要修改。"
        try:
            await self.adapter.submit_internal(chat_id, content)
        except Exception:
            logger.exception("companion: could not report approval %s back to the chat", record.id)
