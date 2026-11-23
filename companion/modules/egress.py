"""After reading private data, sending something to a website waits for the user (nanoMuse's
taint and egress check, simplified).

A Hermes session (and its companion chat, plus any helper or browser desk it delegates work to)
becomes *tainted* when it reads the user's private data through a tool: mail, calendar, contacts,
the password vault. (Its own memory and past chats are left out: they are read on nearly every
turn, and would make every form a question.) For the next hour, a tool call that would carry data
out to a website is held as an approval card (action ``send_private``), naming the site:

- typing into a page or filling a form (the text goes to the page's site);
- a URL with a query string (``?q=…`` can carry anything), opened or fetched;
- a browser script that types, submits or makes its own requests.

Only looking (opening a plain URL, reading a page, clicking a link) goes on as before. A standing
yes on the card covers that site (``send_private:<host>``) for 24 hours.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

TAINT_S = 3600

_lock = threading.Lock()
_tainted: dict[str, float] = {}  # Hermes session id -> when it last read private data
_tainted_chats: dict[str, float] = {}  # companion chat id -> when it (or a delegated helper) read private data

# Reading the user's own data (supports both ``mcp__<server>__<tool>`` and ``mcp_<server>_<tool>``).
_PRIVATE_READS = re.compile(
    r"^(?:mcp__?.+?__?(?:list|get|search|read|fetch|query)_(?:[a-z0-9]+_)*(?:emails?|messages?|threads?|mail|inbox|events?|calendars?|contacts?)(?:_.*)?$"
    r"|mcp__?(?:email|gmail|imap|mail|agentmail|superhuman[-_]mail|hostinger[-_]mail|calendar|calendly|icloud[-_]calendar|google[-_]workspace|microsoft[-_]365|contacts)__?.+"
    r"|browser_vault)$",
    re.IGNORECASE,
)
_TYPING = re.compile(r"^(?:mcp__?.+?__?)?browser_(?:type|fill_form|select_option)$", re.IGNORECASE)
_NAVIGATION = re.compile(r"^(?:(?:mcp__?.+?__?)?browser_navigate|web_extract|web_fetch|web_crawl)$", re.IGNORECASE)
# A browser_exec script that puts data into a page or sends its own requests (its helpers:
# fill_input, raw CDP input, goto_url/new_tab with a query string, or page JS that posts).
_SCRIPT_SENDS = re.compile(
    r"fill_input\s*\(|Input\.(?:insertText|dispatchKeyEvent)|(?:goto_url|new_tab)\s*\(\s*[rf]?['\"][^'\"]*\?"
    r"|fetch\s*\(|XMLHttpRequest|sendBeacon|\.submit\s*\(|\.value\s*=|requests\.(?:get|post)",
)


def _prune_locked(now: float) -> None:
    for store in (_tainted, _tainted_chats):
        for key, at in list(store.items()):
            if now - at >= TAINT_S:
                del store[key]


def note(session_id: Optional[str], tool_name: str, *, chat_id: Optional[str] = None) -> None:
    """After a tool ran: remember that this session (and chat) has read private data."""
    if not _PRIVATE_READS.match(tool_name or ""):
        return
    now = time.time()
    with _lock:
        _prune_locked(now)
        if session_id:
            _tainted[str(session_id)] = now
        if chat_id:
            _tainted_chats[str(chat_id)] = now


def propagate_chat(source_chat: Optional[str], target_chat: Optional[str]) -> None:
    """Carry taint across ``helper_ask`` and ``browser_request`` handoffs in both directions."""
    if not source_chat or not target_chat or source_chat == target_chat:
        return
    now = time.time()
    with _lock:
        _prune_locked(now)
        at = _tainted_chats.get(str(source_chat))
        if at is not None and now - at < TAINT_S:
            _tainted_chats[str(target_chat)] = max(_tainted_chats.get(str(target_chat), 0.0), at)


def tainted(session_id: Optional[str] = None, *, chat_id: Optional[str] = None) -> bool:
    now = time.time()
    with _lock:
        if session_id:
            at = _tainted.get(str(session_id))
            if at is not None and now - at < TAINT_S:
                return True
        if chat_id:
            at = _tainted_chats.get(str(chat_id))
            if at is not None and now - at < TAINT_S:
                return True
    return False


def chat_tainted(chat_id: Optional[str]) -> bool:
    return tainted(chat_id=chat_id)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _urls(args: dict[str, Any]) -> list[str]:
    raw = args.get("urls", args.get("url"))
    items = raw if isinstance(raw, list) else [raw]
    return [u.strip() for u in items if isinstance(u, str) and u.strip()]


def _page_host() -> str:
    from . import computer

    page = computer.status().get("page") or {}
    return _host(page.get("url") or "")


def check(
    session_id: Optional[str],
    tool_name: str,
    args: Any,
    *,
    chat_id: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """The site a call would send data to, when this session/chat is tainted: ``{"host", "what"}``."""
    if not tainted(session_id, chat_id=chat_id):
        return None
    name = tool_name or ""
    args = args if isinstance(args, dict) else {}
    if _NAVIGATION.match(name):
        carrying = [u for u in _urls(args) if urlsplit(u).query]
        if carrying:
            return {"host": _host(carrying[0]) or "未知网站", "what": carrying[0][:300]}
        return None
    if _TYPING.match(name):
        text = args.get("text") or args.get("values") or args.get("fields") or ""
        return {"host": _page_host() or "未知网站", "what": str(text)[:300]}
    code = str(args.get("code") or "")
    if name == "browser_exec" and _SCRIPT_SENDS.search(code):
        # A URL with a query string in the script names its own site; otherwise it is the page's.
        carrying = re.search(r"https?://[^\s'\"]+\?[^\s'\"]*", code)
        host = _host(carrying.group(0)) if carrying else _page_host()
        return {"host": host or "未知网站", "what": code[:300]}
    return None
