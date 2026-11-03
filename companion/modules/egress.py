"""After reading private data, sending something to a website waits for the user (nanoMuse's
taint and egress check, simplified).

A Hermes session becomes *tainted* when it reads the user's private data through a tool: mail,
calendar, contacts, the password vault. (Its own memory and past chats are left out: they are read
on nearly every turn, and would make every form a question.) For the next hour, a tool call that
would carry data out to a website is held as an approval card (action ``send_private``), naming
the site:

- typing into a page or filling a form (the text goes to the page's site);
- a URL with a query string (``?q=…`` can carry anything), opened or fetched;
- a browser script that types, submits or makes its own requests.

Only looking (opening a plain URL, reading a page, clicking a link) goes on as before. A standing
yes on the card covers that site (``send_private:<host>``) for 24 hours or always.
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

# Reading the user's own data. Mail tools that send or delete are gated on their own already.
_PRIVATE_READS = re.compile(
    r"^(?:mcp_.+_(?:list|get|search|read|fetch|query)_(?:emails?|messages?|mail|inbox|events?|calendars?|contacts?)"
    r"|mcp_email_.+|browser_vault)$"
)
_TYPING = re.compile(r"^(?:mcp_.+_)?browser_(?:type|fill_form|select_option)$")
_NAVIGATION = re.compile(r"^(?:(?:mcp_.+_)?browser_navigate|web_extract|web_fetch|web_crawl)$")
# A browser_exec script that puts data into a page or sends its own requests (its helpers:
# fill_input, raw CDP input, goto_url/new_tab with a query string, or page JS that posts).
_SCRIPT_SENDS = re.compile(
    r"fill_input\s*\(|Input\.(?:insertText|dispatchKeyEvent)|(?:goto_url|new_tab)\s*\(\s*[rf]?['\"][^'\"]*\?"
    r"|fetch\s*\(|XMLHttpRequest|sendBeacon|\.submit\s*\(|\.value\s*=|requests\.(?:get|post)",
)


def note(session_id: Optional[str], tool_name: str) -> None:
    """After a tool ran: remember that this session has read private data."""
    if session_id and _PRIVATE_READS.match(tool_name or ""):
        with _lock:
            _tainted[str(session_id)] = time.time()


def tainted(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    with _lock:
        at = _tainted.get(str(session_id))
        return at is not None and time.time() - at < TAINT_S


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


def check(session_id: Optional[str], tool_name: str, args: Any) -> Optional[dict[str, str]]:
    """The site a call would send data to, when this session is tainted: ``{"host", "what"}``."""
    if not tainted(session_id):
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
