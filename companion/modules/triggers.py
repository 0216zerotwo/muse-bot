"""Things the companion does when something happens, not only at a time (自动触发).

Four kinds, each a Hermes cron job that runs in the companion chat:

- ``webhook``: another service POSTs to ``/hooks/<id>?key=…`` on the companion's server. The
  payload is kept and the job is run at once (``trigger_job``); its monitor source is this
  trigger's state URL, so the run sees what arrived. Nothing polls in between.
- ``page``: a web page is checked every N minutes through the same state URL, which returns the
  page's readable text; Hermes's monitor runs the companion only when that text changed.
- ``email`` / ``calendar``: every N minutes the companion looks through the connected mail or
  calendar itself, for mail that came in since the last look, or an event about to start, and
  stays silent (``[SILENT]``) when there is none. The look-back window is the interval, so nothing
  needs remembering between runs.

The instruction says what to do with it ("重要的才告诉我", "把附件存下来"…).

A trigger set up in a helper's chat belongs to that helper (``owner``), as Grok Bot's email agent
runs whenever new mail arrives: it runs in the helper's chat, in its role, shows on its profile and
goes when it is deleted.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import net_safety
from .companion_state import read_companion_state
from .paths import get_webui_dir

TOOLSET = "companion"
KINDS = ("webhook", "page", "email", "calendar")
JOB_PREFIX = "自动触发："
_LOCK = threading.Lock()
_PAYLOAD_CHARS = 4000
_PAGE_CHARS = 8000
_DEFAULT_EVERY = {"page": 60, "email": 30, "calendar": 15}
_SERVICE = {"email": re.compile(r"mail|gmail|outlook|imap", re.I), "calendar": re.compile(r"calendar|caldav|google-workspace|microsoft-365", re.I)}

# The companion's own server: as Hermes's monitor reaches it on this machine (state URLs), and as
# other services reach it (webhook URLs; ``COMPANION_PUBLIC_URL`` on a server behind HTTPS).
_base_url = "http://127.0.0.1:8766"
_public_url = ""


def set_base_url(host: str, port: int, public: str = "") -> None:
    global _base_url, _public_url
    local = host in ("", "0.0.0.0", "::")
    _base_url = f"http://{'127.0.0.1' if local else host}:{port}"
    _public_url = public.rstrip("/")


def _path() -> Path:
    return get_webui_dir() / "triggers.json"


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def _write(triggers: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(triggers, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _find(triggers: list[dict[str, Any]], trigger_id: Any) -> dict[str, Any]:
    trigger = next((t for t in triggers if t.get("id") == trigger_id), None)
    if trigger is None:
        raise ValueError(f"没有这个自动触发：{trigger_id}")
    return trigger


def _connected(kind: str) -> bool:
    from . import connections

    return any(_SERVICE[kind].search(name) for name in connections._servers())


def _state_url(trigger: dict[str, Any]) -> str:
    return f"{_base_url}/api/webui/companion/triggers/{trigger['id']}/state?key={trigger['key']}"


def hook_url(trigger: dict[str, Any]) -> str:
    return f"{_public_url or _base_url}/hooks/{trigger['id']}?key={trigger['key']}"


def _prompt(trigger: dict[str, Any]) -> str:
    kind, todo, every = trigger["kind"], trigger["instruction"], trigger.get("every")
    head = f"[这条消息不会直接给用户看。这是自动触发「{trigger['name']}」。]\n\n"
    silent = "没有需要告诉用户的，就只回复 [SILENT]，不要说别的。"
    if kind == "webhook":
        return head + ("上面「MONITOR CHANGE DETECTED」里是外部服务刚发来的内容。"
                       f"用户的要求：{todo}\n照做；内容为空或者跟要求无关，{silent}")
    if kind == "page":
        return head + (f"用户让你盯着这个网页：{trigger['url']}。上面「MONITOR CHANGE DETECTED」是它的文字有了变化。"
                       f"用户的要求：{todo}\n如果这是第一次检查，或者变化只是时间、广告这类无关内容，{silent}"
                       "否则用一两句话告诉用户变了什么。")
    if kind == "email":
        return head + (f"用已连接的邮箱工具，只看最近 {every} 分钟内收到的邮件（更早的上次已经看过了）。"
                       f"用户的要求：{todo}\n没有新邮件，或者新邮件都不符合要求，{silent}"
                       "有的话简短告诉用户（谁发的、什么事），需要用户决定的就问一句。不要替用户回复或删除邮件。")
    lead = trigger.get("lead") or 15
    return head + (f"用已连接的日历工具，找开始时间在 {lead} 分钟后到 {lead + every} 分钟后之间的日程"
                   f"（更早的上次已经提醒过）。用户的要求：{todo}\n没有这样的日程，{silent}"
                   "有的话提醒用户：什么事、几点、在哪；路上要花时间的顺便提醒出发。")


def _schedule_for(trigger: dict[str, Any]) -> str:
    if trigger["kind"] == "webhook":
        # A webhook runs when it is called; the weekly tick only keeps the job alive (unchanged → silent).
        return "0 4 * * 1"
    every = int(trigger.get("every") or 60)
    if every < 60:
        return f"*/{every} * * * *" if 60 % every == 0 else f"every {every}m"
    hours = every // 60
    if hours >= 24:
        return "0 0 * * *"
    return f"0 */{hours} * * *" if 24 % hours == 0 else f"every {every}m"


def _sync_job(trigger: dict[str, Any]) -> None:
    from cron.jobs import create_job, remove_job

    if trigger.get("job_id"):
        remove_job(trigger["job_id"])
        trigger["job_id"] = None
    chat_id = read_companion_state().get("chat_id")
    if trigger.get("owner"):
        from .helpers import find

        chat_id = (find(trigger["owner"]) or {}).get("chat_id")
    if not chat_id or trigger.get("paused"):
        return
    options: dict[str, Any] = {}
    if trigger["kind"] in ("webhook", "page"):
        options["monitor_url"] = _state_url(trigger)
    schedule = _schedule_for(trigger)
    job = create_job(prompt=_prompt(trigger), schedule=schedule, name=JOB_PREFIX + trigger["name"],
                     deliver=f"companion:{chat_id}", attach_to_session=True, failure_deliver="local", **options)
    trigger["job_id"] = job.get("id") if isinstance(job, dict) else None


def ensure_jobs() -> None:
    """Create missing cron jobs once ``chat_id`` is populated."""
    from cron.jobs import get_job

    with _LOCK:
        triggers = _read()
        changed = False
        for trigger in triggers:
            if trigger.get("paused"):
                continue
            job_id = trigger.get("job_id")
            if job_id and get_job(job_id) is not None:
                continue
            trigger["job_id"] = None
            _sync_job(trigger)
            if trigger.get("job_id"):
                changed = True
        if changed:
            _write(triggers)


def clear_job_ref(job_id: str) -> None:
    """Clean up trigger metadata when its cron job is deleted from the 任务 page."""
    if not job_id:
        return
    with _LOCK:
        triggers = _read()
        changed = False
        for trigger in triggers:
            if trigger.get("job_id") == job_id:
                trigger["job_id"] = None
                trigger["paused"] = True
                changed = True
        if changed:
            _write(triggers)


# ── changes ───────────────────────────────────────────────────────────────────────────────────────

def add_trigger(kind: str, name: str, instruction: str, url: Optional[str] = None,
                every: Optional[int] = None, owner: Optional[str] = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")
    name = " ".join(str(name or "").split())[:30]
    instruction = " ".join(str(instruction or "").split())[:400]
    if not name or not instruction:
        raise ValueError("要有名字和要做的事（instruction）")
    if kind == "page":
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            raise ValueError("盯网页要给出 http(s) 网址")
        if not net_safety.is_safe_url_target(url):
            raise ValueError("不能盯本机或内网网址")
    if kind in ("email", "calendar") and not _connected(kind):
        raise ValueError(f"还没有连接{'邮箱' if kind == 'email' else '日历'}，请用户先在 设置 → 连接 里连上")
    if kind != "webhook":
        every = max(5, min(int(every or _DEFAULT_EVERY[kind]), 24 * 60))
        if every >= 60:
            every -= every % 60
    trigger = {"id": uuid.uuid4().hex[:10], "kind": kind, "name": name, "instruction": instruction,
               "url": url if kind == "page" else None, "every": every if kind != "webhook" else None,
               "key": secrets.token_urlsafe(18), "created_at": int(time.time() * 1000),
               "payload": None, "fired_at": None, "paused": False, "job_id": None, "owner": owner or None}
    with _LOCK:
        triggers = _read()
        _sync_job(trigger)
        triggers.append(trigger)
        _write(triggers)
    return trigger


def remove_trigger(trigger_id: str) -> None:
    from cron.jobs import remove_job

    with _LOCK:
        triggers = _read()
        trigger = _find(triggers, trigger_id)
        if trigger.get("job_id"):
            remove_job(trigger["job_id"])
        _write([t for t in triggers if t is not trigger])


def remove_owned(owner: str) -> None:
    """A helper's triggers, when it is deleted."""
    for trigger in _read():
        if trigger.get("owner") == owner:
            remove_trigger(trigger["id"])


def set_paused(trigger_id: str, paused: bool) -> dict[str, Any]:
    with _LOCK:
        triggers = _read()
        trigger = _find(triggers, trigger_id)
        trigger["paused"] = bool(paused)
        _sync_job(trigger)
        _write(triggers)
    return trigger


def public(trigger: dict[str, Any]) -> dict[str, Any]:
    """For the app and the companion: no key, except inside a webhook's own URL."""
    data = {k: v for k, v in trigger.items() if k not in ("key", "payload", "job_id")}
    if trigger.get("owner"):
        from .helpers import find

        data["owner_name"] = (find(trigger["owner"]) or {}).get("name") or ""
    if trigger["kind"] == "webhook":
        data["hook_url"] = hook_url(trigger)
    return data


def list_triggers() -> list[dict[str, Any]]:
    return [public(t) for t in _read()]


# ── the server side: a webhook arrives, a monitor asks for the state ─────────────────────────────

def _check_key(trigger: dict[str, Any], key: Optional[str]) -> bool:
    return bool(key) and hmac.compare_digest(str(key), str(trigger.get("key")))


def receive(trigger_id: str, key: Optional[str], body: str) -> bool:
    """A webhook POST: keep the payload and run the job now. False for a wrong id or key."""
    from cron.jobs import trigger_job

    with _LOCK:
        triggers = _read()
        trigger = next((t for t in triggers if t.get("id") == trigger_id and t["kind"] == "webhook"), None)
        if trigger is None or not _check_key(trigger, key) or trigger.get("paused"):
            return False
        trigger["payload"] = body[:_PAYLOAD_CHARS]
        trigger["fired_at"] = int(time.time() * 1000)
        _write(triggers)
    if trigger.get("job_id"):
        trigger_job(trigger["job_id"])
    return True


def _page_text(url: str) -> str:
    import html

    raw, headers = net_safety.fetch_bytes(
        url,
        max_bytes=2 * 1024 * 1024,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (companion page watch)"},
        truncate=True,
    )
    charset = "utf-8"
    ctype = headers.get("content-type", "")
    if "charset=" in ctype.lower():
        charset = ctype.lower().split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", body)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:_PAGE_CHARS]


def state(trigger_id: str, key: Optional[str]) -> Optional[str]:
    """What the monitor compares between runs; ``None`` for a wrong id or key."""
    trigger = next((t for t in _read() if t.get("id") == trigger_id), None)
    if trigger is None or not _check_key(trigger, key):
        return None
    if trigger["kind"] == "webhook":
        return f"收到时间：{trigger.get('fired_at') or '还没有'}\n{trigger.get('payload') or ''}"
    if trigger["kind"] == "page":
        return _page_text(trigger["url"])
    return ""


# ── the companion's tools ─────────────────────────────────────────────────────────────────────────

def _result(fn) -> str:
    try:
        return json.dumps({"success": True, **fn()}, ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def _handle_add(args: dict[str, Any], **kwargs: Any) -> str:
    from .helpers import _current_chat, helper_for_chat

    # Set up in a helper's chat: it is that helper's (it runs there, in its role).
    owner = (helper_for_chat(_current_chat(kwargs)) or {}).get("id")

    def run() -> dict[str, Any]:
        trigger = public(add_trigger(args.get("kind", ""), args.get("name", ""), args.get("instruction", ""),
                                     args.get("url"), args.get("every_minutes"), owner=owner))
        note = ("把 hook_url 给用户，让他填到要发通知的服务里（POST 任意内容）。"
                + ("" if _public_url else "现在这是本机地址，部署到服务器后才能从外网调用。")
                if trigger["kind"] == "webhook" else "")
        return {"trigger": trigger, **({"note": note} if note else {})}
    return _result(run)


def _handle_list(args: dict[str, Any], **_: Any) -> str:
    return _result(lambda: {"triggers": list_triggers()})


def _handle_remove(args: dict[str, Any], **_: Any) -> str:
    return _result(lambda: (remove_trigger(args.get("trigger_id", "")), {})[1])


TOOLS = (
    ("trigger_add",
     "设置自动触发：有事发生时自动去做，而不是到点才做。kind：email（来了新邮件）、calendar（日程快开始）、"
     "page（某个网页内容变了）、webhook（别的服务发来通知，比如 GitHub、快递、智能家居）。"
     "instruction 写清楚触发后要做什么、什么情况才告诉用户。设置前先跟用户确认。",
     {"kind": {"type": "string", "enum": list(KINDS)},
      "name": {"type": "string", "description": "简短的名字，比如「重要邮件提醒」"},
      "instruction": {"type": "string", "description": "触发后做什么，比如「只有老板和学校发来的才告诉我」"},
      "url": {"type": "string", "description": "kind=page 时要盯的网址"},
      "every_minutes": {"type": "integer", "description": "email/calendar/page 多久看一次，默认 30/15/60 分钟"}},
     ["kind", "name", "instruction"], _handle_add),
    ("trigger_list", "列出已经设置的自动触发。", {}, [], _handle_list),
    ("trigger_remove", "删除一个自动触发。", {"trigger_id": {"type": "string"}}, ["trigger_id"], _handle_remove),
)


def register_tools(ctx: Any) -> None:
    for name, description, properties, required, handler in TOOLS:
        schema = {"name": name, "description": description,
                  "parameters": {"type": "object", "properties": properties, "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description,
                          emoji="⚡")
