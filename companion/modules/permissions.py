"""权限: what the companion (and its helpers) may do on their own, set by the user per kind of action.

Each kind has a level — 禁止 (``deny``), 询问我 (``ask``) or 允许 (``allow``) — kept in
``<companion dir>/permissions.json``. Before a tool runs (``activity.on_pre_tool_call``), ``kind_of``
names the kind of the call; ``deny`` blocks it with a message saying so, ``ask`` holds it as an
approval card (unless a standing yes from 不用再问的事 covers it), ``allow`` lets it run.

Some kinds don't offer every level: paying can't be 允许, and a command is only ever held when
Hermes judges it risky.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Optional

from .paths import get_webui_dir

DENY, ASK, ALLOW = "deny", "ask", "allow"
LEVELS = (DENY, ASK, ALLOW)

# key, label, what it covers, default, the levels offered
KINDS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("browse", "打开和浏览网页", "在浏览器里打开网页、看内容、点普通的链接和按钮", ALLOW, LEVELS),
    ("browser_submit", "在网页上提交", "提交表单、确认、注册、发布、预订这类会生效的操作", ASK, LEVELS),
    ("payment", "付款和下单", "在网页上付款、下单、转账", ASK, (DENY, ASK)),
    ("browser_upload", "上传文件到网页", "把电脑上的文件传到网站", ASK, LEVELS),
    ("send_private", "把私人信息发到网站", "读过你的邮件或日程之后，把内容填到网页上", ASK, LEVELS),
    ("send_message", "发送邮件和消息", "以你的名义发送、转发邮件和消息", ASK, LEVELS),
    ("delete", "删除", "删除邮件、日程等内容", ASK, LEVELS),
    ("connector_read", "读取连接的服务", "从已连接的服务里查询、读取内容", ALLOW, LEVELS),
    ("connector_write", "在连接的服务里修改", "通过已连接的服务新建、修改内容（比如加日程、建任务）", ALLOW, LEVELS),
    ("run_command", "运行命令和代码", "在电脑上运行命令或代码；有风险的命令总会先问你", ASK, (DENY, ASK)),
    ("schedule", "设定时任务和自动触发", "新建以后自动执行的任务", ALLOW, LEVELS),
)
_DEFAULTS = {key: default for key, _, _, default, _ in KINDS}
_OFFERED = {key: offered for key, _, _, _, offered in KINDS}
_LABELS = {key: label for key, label, _, _, _ in KINDS}
_LOCK = threading.Lock()

_PAY = re.compile(r"付款|支付|购买|下单|订单|结算|转账|买单|pay|purchase|buy|checkout|check\s*out|place\s+order", re.I)
_STRING = re.compile(r"""(["'])((?:(?!\1).){1,200})\1""")
# A page's address names where a script goes, not what it clicks (…/shop/buy-mac/… is no purchase).
_ADDRESS = re.compile(r"^\s*(?:[a-z][\w+.-]*://|//|/|www\.)", re.IGNORECASE)
_READ_ONLY = re.compile(r"(?:^|__|_)(list|get|search|read|query|find|check|fetch|view|describe|count|lookup)(_|$)")
_COMMAND_TOOLS = {"terminal", "execute_code", "exec", "exec_session", "process", "process_manage", "run_cli_app"}
_SCHEDULE_TOOLS = {"cronjob", "cronjob_manage", "trigger_add", "trigger_remove"}
# The approval actions (``approvals.approval_action``) and the kind each belongs to.
_ACTION_KIND = {
    "send_email": "send_message", "forward_email": "send_message", "delete_emails": "delete",
    "email_command": "delete", "delete_event": "delete", "browser_submit": "browser_submit",
    "browser_upload": "browser_upload", "send_private": "send_private",
}


def _path():
    return get_webui_dir() / "permissions.json"


def levels() -> dict[str, str]:
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    return {key: saved[key] if saved.get(key) in _OFFERED[key] else default for key, default in _DEFAULTS.items()}


def set_level(kind: str, level: str) -> dict[str, Any]:
    if kind not in _DEFAULTS:
        raise ValueError("没有这种权限")
    if level not in _OFFERED[kind]:
        raise ValueError("这种权限不能这样设")
    with _LOCK:
        current = levels()
        current[kind] = level
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return payload()


def payload() -> dict[str, Any]:
    current = levels()
    return {"kinds": [{"key": key, "label": label, "detail": detail, "level": current[key], "levels": list(offered)}
                      for key, label, detail, _, offered in KINDS]}


def label(kind: str) -> str:
    return _LABELS.get(kind, kind)


def kind_of(tool_name: str, args: Any, action: Optional[str], is_browser: bool) -> Optional[str]:
    """The kind of a tool call (None: not one the user sets, e.g. reading its own notes)."""
    name = tool_name or ""
    if action == "browser_submit":
        args = args if isinstance(args, dict) else {}
        # What it clicks: the element's name, or (Hermes's own browser tool) the strings in its
        # script that name it (not the code itself: "display" has "pay" in it).
        named = [str(args.get("element") or "")] + [
            m[1] for m in _STRING.findall(str(args.get("code") or "")) if not _ADDRESS.match(m[1])]
        return "payment" if any(_PAY.search(text) for text in named) else "browser_submit"
    if action in _ACTION_KIND:
        return _ACTION_KIND[action]
    if is_browser:
        return "browse"
    if name in _COMMAND_TOOLS:
        return "run_command"
    if name == "cronjob_manage" and isinstance(args, dict) and str(args.get("action") or "").strip().lower() == "list":
        return None
    if name in _SCHEDULE_TOOLS:
        return "schedule"
    if name.startswith(("mcp__", "mcp_")):
        return "connector_read" if _READ_ONLY.search(name) else "connector_write"
    return None


def level_of(kind: Optional[str]) -> str:
    return levels().get(kind or "", ALLOW)


def denied_message(kind: str) -> str:
    return (f"用户在「设置 → 权限」里把「{label(kind)}」设成了禁止，这一步不能做。"
            "告诉用户需要做这一步，以及怎么在权限里打开，或者请用户自己来做。")
