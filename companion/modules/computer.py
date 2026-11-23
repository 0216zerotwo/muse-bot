"""The companion's browser, live in the app: watch it work, or take over (Muse's "computer").

The companion browses with Playwright (the ``browser`` MCP server). Instead of letting
Playwright start its own Edge, the plugin starts one with a local-only debugging port and
points Playwright at it (``--cdp-endpoint``), so the agent and the app share one browser:

- the app watches it through a screencast over ``/ws/computer`` (frames as they change);
- the user can take control (接管控制): taps, typing, keys, scrolling and the address bar become
  real input events. While the user has it (a *hold*), the agent's browser actions wait — the
  ``pre_tool_call`` hook blocks them with a message saying why. 交还 gives control back;
- it closes when the task that used it is over, after 10 idle minutes, or on 关闭浏览器, and
  starts again when the agent (or the user) next needs it; sign-ins stay in the profile;
- the agent can hand the page over itself (``browser_hand_over``: a login, a captcha, the last
  step of a payment). That shows in 等你确认; 完成 gives it back and tells the agent to go on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .paths import get_webui_dir

logger = logging.getLogger(__name__)

CDP_PORT = 9333
BROWSER_SERVER = "browser"
_BROWSER_TOOL_PREFIX = f"mcp__{BROWSER_SERVER}__"
_BROWSER_TOOL_PREFIXES = (_BROWSER_TOOL_PREFIX, f"mcp_{BROWSER_SERVER}_")
_LOCK = threading.Lock()
_FRAME_QUALITY = 60
_FRAME_MAX = 1280
_POLL_S = 1.5

# The hold: who has the browser right now ("user" after 接手, "handoff" while the agent waits on
# the user), and the agent's open request, if any.
_hold: Optional[str] = None
_handoff: Optional[dict[str, Any]] = None
_last_action: Optional[dict[str, Any]] = None
_started_at: Optional[float] = None
# Last time anyone used the browser (the agent or the user), and whether the agent did since it
# last closed: the browser closes when the task that used it is over (see ``close_after_task``).
_last_used: float = 0.0
_used_by: set[Optional[str]] = set()  # chats whose turn used it; None for scheduled jobs
_IDLE_CLOSE_S = 600
# The pages open when it last closed: a browser started again soon after opens them, so 继续 and a
# 接手 asked for after the turn ended find the page where it was (not a blank start page).
_closed_pages: list[str] = []
_closed_at: float = 0.0
_REOPEN_S = 30 * 60


def cdp_url() -> str:
    return f"http://127.0.0.1:{CDP_PORT}"


def profile_dir() -> Path:
    # The same profile Playwright used before (cwd of the managed server + "browser-profile"),
    # so the sites the user already signed into stay signed in.
    return get_webui_dir() / "mcp" / BROWSER_SERVER / "browser-profile"


def _executable() -> Optional[str]:
    if os.environ.get("COMPANION_BROWSER"):  # a server can name its own Chrome/Chromium
        return os.environ["COMPANION_BROWSER"]
    if sys.platform == "win32":
        candidates = [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                      r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                      r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                      r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]
        return next((p for p in map(os.path.expandvars, candidates) if os.path.exists(p)), None)
    if sys.platform == "darwin":
        candidates = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        return next((p for p in candidates if os.path.exists(p)), None)
    return next((shutil.which(n) for n in ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium",
                                           "chromium-browser") if shutil.which(n)), None)


def _user_agent(exe: str) -> Optional[str]:
    """A normal desktop browser's user agent: headless announces itself as "HeadlessChrome", which
    some sites turn away. The version is the installed one (Edge/Chrome keep it as a folder name)."""
    import re

    versions = sorted((d.name for d in Path(exe).parent.iterdir() if re.fullmatch(r"\d+(\.\d+){3}", d.name)),
                      key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True) if Path(exe).parent.is_dir() else []
    if not versions:  # Linux packages: ask the binary ("Chromium 141.0.7390.54")
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        versions = re.findall(r"\d+\.\d+\.\d+\.\d+", out)[:1]
    if not versions:
        return None
    major = versions[0].split(".")[0]
    platform = {"win32": "Windows NT 10.0; Win64; x64", "darwin": "Macintosh; Intel Mac OS X 10_15_7"}.get(sys.platform, "X11; Linux x86_64")
    agent = f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    return agent + (f" Edg/{versions[0]}" if "edge" in exe.lower() else "")


def _needs_sandbox_bypass() -> bool:
    """Root, Docker, or Ubuntu's AppArmor rule: Chromium only starts with --no-sandbox there.
    Hermes decides this for its own browser; the same answer holds for this one."""
    try:
        from tools.browser_tool_session import _needs_chromium_sandbox_bypass

        return _needs_chromium_sandbox_bypass()
    except Exception:
        return hasattr(os, "geteuid") and os.geteuid() == 0


def _get_json(path: str, timeout: float = 1.5) -> Any:
    import httpx

    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(cdp_url() + path)
        response.raise_for_status()
        return response.json()


def running() -> bool:
    try:
        _get_json("/json/version", timeout=1)
        return True
    except Exception:
        return False


# The page a newly started browser opens on: a search page to start from, as a browser's home.
START_PAGE = "https://www.google.com/"


def _reopen() -> list[str]:
    """What a browser starting now opens: the pages of one closed in the last half hour (once),
    else the start page."""
    global _closed_pages
    pages = _closed_pages if time.time() - _closed_at < _REOPEN_S else []
    _closed_pages = []
    return pages or [START_PAGE]


def ensure_browser() -> bool:
    """Start the shared browser when it is not running. False when no Edge/Chrome is installed."""
    global _started_at
    with _LOCK:
        if running():
            _started_at = _started_at or time.time()
            _start_idle_watch()
            return True
        exe = _executable()
        if exe is None:
            logger.warning("companion: no Edge or Chrome to start the browser with")
            return False
        profile = profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        # Headless, like Muse's computer in the cloud: it is seen and used through the app. A window
        # on the desktop would count as hidden whenever it is minimized or covered, and a hidden page
        # stops painting, which freezes the live view.
        args = [exe, "--headless=new", f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                "--window-size=1280,860", "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                # A server's disk is small: the pages' cache stays under 100 MB.
                "--disk-cache-size=104857600"]
        agent = _user_agent(exe)
        if agent:
            args.append(f"--user-agent={agent}")
        args += _reopen()
        if sys.platform != "win32" and _needs_sandbox_bypass():
            args[1:1] = ["--no-sandbox", "--disable-dev-shm-usage"]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        for _ in range(40):
            if running():
                _started_at = time.time()
                _touch()
                _start_idle_watch()
                logger.info("companion: browser started on %s", cdp_url())
                return True
            time.sleep(0.25)
        logger.warning("companion: the browser did not come up on %s", cdp_url())
        return False


def _touch() -> None:
    global _last_used
    _last_used = time.time()


def close_browser() -> bool:
    """Close the shared browser (``Browser.close``: the profile, and the sign-ins in it, stay on
    disk). Not while someone has it: the user, or the agent waiting on the user."""
    global _started_at
    from websockets.sync.client import connect

    with _LOCK:
        if _hold is not None or not running():
            return False
        pages = [str(p.get("url")) for p in _pages() if str(p.get("url") or "").startswith(("http://", "https://"))]
        try:
            endpoint = _get_json("/json/version")["webSocketDebuggerUrl"]
            with connect(endpoint, proxy=None, open_timeout=3) as ws:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                with contextlib.suppress(Exception):
                    ws.recv(timeout=3)
        except Exception as exc:
            logger.warning("companion: could not close the browser: %s", exc)
            return False
        _started_at = None
        _used_by.clear()
        _remember_closed(pages)
        logger.info("companion: browser closed")
        return True


def _remember_closed(pages: list[str]) -> None:
    global _closed_pages, _closed_at
    _closed_pages, _closed_at = [p for p in pages if p != START_PAGE][:8], time.time()


def close_after_task(chat_id: str) -> None:
    """A chat's turn ended: if it used the browser, that task is over and the browser closes —
    unless something else (another chat, a scheduled job) is still using it."""
    if chat_id not in _used_by:
        return
    _used_by.discard(chat_id)
    if not _used_by:
        close_browser()


_watching = False


def _start_idle_watch() -> None:
    """Close the browser after ``_IDLE_CLOSE_S`` with no use: covers scheduled tasks and the user's
    own browsing, which have no turn end to close it."""
    global _watching
    if _watching:
        return
    _watching = True

    def watch() -> None:
        global _watching
        while running():
            time.sleep(30)
            if _hold is None and time.time() - _last_used > _IDLE_CLOSE_S:
                close_browser()
        _watching = False

    threading.Thread(target=watch, name="companion-browser-idle", daemon=True).start()


def ensure_cdp_config() -> bool:
    """Point the managed ``browser`` server at the shared browser. True when the config changed."""
    from . import connections

    servers = connections._servers()
    cfg = servers.get(BROWSER_SERVER)
    if not isinstance(cfg, dict):
        return False
    args = [str(a) for a in cfg.get("args") or []]
    out: list[str] = []
    skip = 0
    for i, arg in enumerate(args):
        if skip:
            skip -= 1
            continue
        if arg in ("--browser", "--user-data-dir", "--cdp-endpoint"):
            skip = 1
            continue
        if arg in ("--headless", "--isolated"):
            continue
        out.append(arg)
    out += ["--cdp-endpoint", cdp_url()]
    if out == args:
        return False
    servers[BROWSER_SERVER] = {**cfg, "args": out}
    connections._save_servers(servers)
    logger.info("companion: the browser server now uses the shared browser")
    return True


def share_with_builtin_tools() -> None:
    """Point Hermes's own browser tools (``browser_exec``, ``browser_navigate``…) at the shared browser
    too, as a ``/browser connect`` would: in memory for this process, nothing written to config."""
    from tools.browser_tool_cdp import set_live_cdp_override

    set_live_cdp_override(cdp_url())


# ── the hold ──────────────────────────────────────────────────────────────────────────────────────

def is_browser_tool(tool_name: str) -> bool:
    """Playwright's tools (``mcp__browser__*`` / ``mcp_browser_*``) and Hermes's own (``browser_*``),
    not the hand-over or a helper's request for the companion to use it (``browser_request``)."""
    name = tool_name or ""
    return name.startswith((*_BROWSER_TOOL_PREFIXES, "browser_")) and name not in ("browser_hand_over", "browser_request")


def held() -> Optional[str]:
    return _hold


def holding() -> bool:
    """Someone has the browser, so the agent's browser actions wait. A hold on a browser that is no
    longer running (it crashed, or never came back) holds nothing: it is let go, or the agent would
    wait for the user on a page the user cannot see."""
    global _hold, _handoff
    if _hold is None:
        return False
    if running():
        return True
    logger.info("companion: the browser is gone; its hold (%s) is let go", _hold)
    _hold, _handoff = None, None
    return False


def take() -> None:
    global _hold
    _hold = "user"
    _touch()


def let_go() -> None:
    """The viewer that took the browser went away without 完成: the agent may go on, unless it is
    itself still waiting on the user (then the request stays open)."""
    global _hold
    if _hold == "user":
        _hold = "handoff" if _handoff else None


def release() -> Optional[dict[str, Any]]:
    """Give the browser back; the agent's open request, if this answers one."""
    global _hold, _handoff
    answered, _handoff, _hold = _handoff, None, None
    _touch()
    return answered


def hand_over(reason: str, chat_id: Optional[str]) -> dict[str, Any]:
    global _hold, _handoff
    _handoff = {"id": uuid.uuid4().hex[:10], "reason": reason[:120], "at": int(time.time() * 1000), "chat_id": chat_id}
    _hold = "handoff"
    return _handoff


def note_action(tool_name: str, args: Any, chat_id: Optional[str] = None) -> None:
    """What the agent just did in the browser (for the status line), and for which chat."""
    global _last_action
    _used_by.add(chat_id)
    _touch()
    verb = tool_name or ""
    for prefix in (*_BROWSER_TOOL_PREFIXES, "browser_"):
        if verb.startswith(prefix):
            verb = verb[len(prefix):]
    detail = ""
    if isinstance(args, dict):
        detail = str(args.get("element") or args.get("url") or args.get("text") or args.get("key") or "")[:60]
    _last_action = {"verb": verb, "detail": detail, "at": int(time.time() * 1000)}


def _pages() -> list[dict[str, Any]]:
    try:
        targets = _get_json("/json/list")
    except Exception:
        return []
    return [t for t in targets if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")]


def status() -> dict[str, Any]:
    up = running()
    page = (_pages() or [None])[0] if up else None
    return {
        "running": up,
        "since": int(_started_at * 1000) if up and _started_at else None,
        "page": {"title": page.get("title") or "", "url": page.get("url") or ""} if page else None,
        "hold": _hold,
        "handoff": {k: v for k, v in _handoff.items() if k != "chat_id"} if _handoff else None,
        "last_action": _last_action,
    }


# ── live view ─────────────────────────────────────────────────────────────────────────────────────

_KEYS = {
    "Enter": (13, "\r"), "Backspace": (8, None), "Tab": (9, None), "Escape": (27, None),
    "ArrowUp": (38, None), "ArrowDown": (40, None), "ArrowLeft": (37, None), "ArrowRight": (39, None),
}


class _Cdp:
    """A minimal CDP connection to one page: send commands, read events, await a reply when needed."""

    def __init__(self, ws: Any):
        self.ws = ws
        self._id = 0
        self._waiting: dict[int, asyncio.Future] = {}

    async def send(self, method: str, params: Optional[dict[str, Any]] = None) -> int:
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    async def call(self, method: str, params: Optional[dict[str, Any]] = None, timeout: float = 3) -> dict[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._id += 1
        self._waiting[self._id] = future
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._waiting.pop(self._id, None)

    def resolve(self, msg: dict[str, Any]) -> bool:
        """A reply to ``call``: hand it over. False for events."""
        future = self._waiting.get(msg.get("id")) if "id" in msg else None
        if future is not None and not future.done():
            future.set_result(msg.get("result") or {})
            return True
        return False


# What the page has focused after a tap: the app opens the keyboard only for a field.
# 复制: what is selected on the page, or in the focused text field — never a password field's.
_SELECTION_PROBE = """(() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (e && (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA')) {
    if ((e.type || '').toLowerCase() === 'password') return '';
    if (typeof e.selectionStart === 'number' && e.selectionEnd > e.selectionStart) return e.value.slice(e.selectionStart, e.selectionEnd);
  }
  return String(window.getSelection() || '');
})()"""

_FOCUS_PROBE = """(() => {
  let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return false;
  if (e.tagName === 'IFRAME') return true;
  if (e.isContentEditable) return true;
  if (e.tagName === 'TEXTAREA') return !e.readOnly && !e.disabled;
  if (e.tagName !== 'INPUT') return false;
  const t = (e.type || 'text').toLowerCase();
  return !e.readOnly && !e.disabled && !['button','checkbox','radio','submit','reset','file','image','range','color','hidden'].includes(t);
})()"""


# 教它做: while the user teaches a task, what they do is written down as steps (what they clicked,
# by its text; what they typed, never a password; where they went), for a skill draft.
_teach: Optional[list[dict[str, Any]]] = None
_MAX_TEACH_STEPS = 150

_DESCRIBE = """(() => {
  const e = document.elementFromPoint(%f, %f);
  if (!e) return null;
  const t = e.closest('button,a,[role=button],[role=link],[role=tab],[role=menuitem],input,select,textarea,label,summary') || e;
  const text = (t.innerText || t.value || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
  const name = t.getAttribute('aria-label') || t.getAttribute('placeholder') || t.getAttribute('title') || t.getAttribute('name') || '';
  const kind = t.tagName === 'A' ? '链接' : ['INPUT','TEXTAREA'].includes(t.tagName) ? '输入框' : t.tagName === 'SELECT' ? '下拉框' : '按钮';
  return {text, name, kind, url: location.href};
})()"""

_ACTIVE = """(() => { const e = document.activeElement; if (!e) return null;
  return {password: (e.type || '').toLowerCase() === 'password',
          name: e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || '', url: location.href}; })()"""


async def _evaluate(cdp: _Cdp, expression: str) -> Optional[dict[str, Any]]:
    try:
        result = await cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    except (asyncio.TimeoutError, Exception):
        return None
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, dict) else None


def _note_step(step: dict[str, Any]) -> None:
    if _teach is None or len(_teach) >= _MAX_TEACH_STEPS:
        return
    last = _teach[-1] if _teach else None
    if step["do"] == "type" and last and last["do"] == "type" and last.get("target") == step.get("target"):
        # Typing arrives a key or a word at a time: one step per field.
        if last["text"] != "（密码，不记录）":
            last["text"] = (last["text"] + step["text"])[:200]
        return
    _teach.append(step)


async def _record(cdp: _Cdp, msg: dict[str, Any], x: float = 0, y: float = 0) -> None:
    kind = msg.get("type")
    if kind == "click":
        found = await _evaluate(cdp, _DESCRIBE % (x, y)) or {}
        label = found.get("text") or found.get("name") or ""
        target = f"{found.get('kind', '按钮')}「{label}」" if label else "页面上某处"
        _note_step({"do": "click", "target": target, "url": found.get("url", "")})
    elif kind == "text":
        found = await _evaluate(cdp, _ACTIVE) or {}
        text = "（密码，不记录）" if found.get("password") else str(msg.get("text") or "")[:200]
        target = f"输入框「{found['name']}」" if found.get("name") else "输入框"
        _note_step({"do": "type", "target": target, "text": text, "url": found.get("url", "")})
    elif kind == "key" and msg.get("key") in ("Enter", "Tab", "Escape"):
        _note_step({"do": "key", "key": msg["key"]})
    elif kind == "navigate":
        _note_step({"do": "open", "url": str(msg.get("url") or "")[:300]})
    elif kind == "back":
        _note_step({"do": "back"})


def teach_start(url: str = "") -> None:
    global _teach
    _teach = [{"do": "open", "url": url}] if url.startswith(("http://", "https://")) else []


def teach_stop() -> list[dict[str, Any]]:
    global _teach
    steps, _teach = _teach or [], None
    return steps


async def _input(cdp: _Cdp, size: dict[str, float], msg: dict[str, Any], socket: Any) -> None:
    kind = msg.get("type")
    if _teach is not None and kind != "click":
        await _record(cdp, msg)
        await socket.send_json({"type": "teach", "on": True, "count": len(_teach or [])})
    if kind == "click":
        x = float(msg.get("x", 0)) * size["w"]
        y = float(msg.get("y", 0)) * size["h"]
        if _teach is not None:
            # Read what is under the finger before the click changes the page.
            await _record(cdp, msg, x, y)
            await socket.send_json({"type": "teach", "on": True, "count": len(_teach or [])})
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for event in ("mousePressed", "mouseReleased"):
            await cdp.send("Input.dispatchMouseEvent", {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1})
        await asyncio.sleep(0.12)
        try:
            result = await cdp.call("Runtime.evaluate", {"expression": _FOCUS_PROBE, "returnByValue": True})
            editable = bool((result.get("result") or {}).get("value"))
        except (asyncio.TimeoutError, Exception):
            editable = False
        await socket.send_json({"type": "focus", "editable": editable})
    elif kind == "scroll":
        await cdp.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": size["w"] / 2, "y": size["h"] / 2,
                                                    "deltaX": 0, "deltaY": float(msg.get("dy", 0)) * size["h"]})
    elif kind == "text" and isinstance(msg.get("text"), str):
        await cdp.send("Input.insertText", {"text": msg["text"][:2000]})
    elif kind == "key" and msg.get("key") in _KEYS:
        code, text = _KEYS[msg["key"]]
        down = {"type": "keyDown" if text else "rawKeyDown", "windowsVirtualKeyCode": code, "key": msg["key"]}
        if text:
            down["text"] = text
        await cdp.send("Input.dispatchKeyEvent", down)
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": code, "key": msg["key"]})
    elif kind == "navigate" and isinstance(msg.get("url"), str):
        url = msg["url"].strip()
        if url and "://" not in url:
            url = "https://" + url
        if url.startswith(("http://", "https://")):
            await cdp.send("Page.navigate", {"url": url})
    elif kind == "back":
        await cdp.send("Runtime.evaluate", {"expression": "history.back()"})
    elif kind == "forward":
        await cdp.send("Runtime.evaluate", {"expression": "history.forward()"})
    elif kind == "reload":
        await cdp.send("Page.reload")
    elif kind == "copy":
        # The selection, sent to the phone for its clipboard ("" when nothing is selected).
        try:
            result = await cdp.call("Runtime.evaluate", {"expression": _SELECTION_PROBE, "returnByValue": True})
            text = str((result.get("result") or {}).get("value") or "")
        except (asyncio.TimeoutError, Exception):
            text = ""
        await socket.send_json({"type": "copied", "text": text[:20000]})


async def stream(socket: Any, on_done: Any) -> None:
    """Serve one app viewer: frames out, input in. Follows the agent to whichever tab it uses."""
    taken = {"by_me": False}
    try:
        await _stream(socket, on_done, taken)
    finally:
        if taken["by_me"]:
            let_go()


async def _stream(socket: Any, on_done: Any, taken: dict[str, bool]) -> None:
    import websockets

    idle_sent = False
    while True:
        if not await asyncio.to_thread(running):
            # Closed: watching does not start it; opening it full screen (take) does.
            if not idle_sent:
                await socket.send_json({"type": "idle"})
                idle_sent = True
            try:
                msg = await asyncio.wait_for(socket.receive_json(), _POLL_S)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, dict) and msg.get("type") in ("start", "take"):
                if not await asyncio.to_thread(ensure_browser):
                    await socket.send_json({"type": "error", "message": "这台电脑上没有可用的 Edge 或 Chrome"})
                    return
                if msg["type"] == "take":
                    take()
                    taken["by_me"] = True
                    await socket.send_json({"type": "hold", "hold": _hold})
            continue
        idle_sent = False
        pages = await asyncio.to_thread(_pages)
        if not pages:
            await socket.send_json({"type": "page", "title": "", "url": ""})
            await asyncio.sleep(_POLL_S)
            continue
        page = pages[0]
        await socket.send_json({"type": "page", "title": page.get("title") or "", "url": page.get("url") or "",
                                "hold": _hold, "handoff": status()["handoff"]})
        size = {"w": 1280.0, "h": 800.0}
        try:
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None, proxy=None) as ws:
                cdp = _Cdp(ws)
                await cdp.send("Page.enable")
                await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": _FRAME_QUALITY,
                                                        "maxWidth": _FRAME_MAX, "maxHeight": _FRAME_MAX})
                switched = await _pump(socket, ws, cdp, size, page["id"], on_done, taken)
                if not switched:
                    return
        except (OSError, websockets.WebSocketException) as exc:
            logger.info("companion: browser view reconnecting (%s)", type(exc).__name__)
            await asyncio.sleep(_POLL_S)


async def _pump(socket: Any, ws: Any, cdp: _Cdp, size: dict[str, float], target_id: str, on_done: Any,
                taken: dict[str, bool]) -> bool:
    """Run until the viewer leaves (False) or the agent moves to another tab (True)."""
    from starlette.websockets import WebSocketDisconnect

    async def frames() -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if cdp.resolve(msg):
                continue
            if msg.get("method") == "Page.screencastFrame":
                params = msg["params"]
                meta = params.get("metadata") or {}
                size["w"] = float(meta.get("deviceWidth") or size["w"])
                size["h"] = float(meta.get("deviceHeight") or size["h"])
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                await socket.send_json({"type": "frame", "data": params["data"], "w": size["w"], "h": size["h"]})

    async def viewer() -> None:
        while True:
            msg = await socket.receive_json()
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "take":
                take()
                taken["by_me"] = True
                await socket.send_json({"type": "hold", "hold": _hold})
            elif msg.get("type") == "teach_start":
                pages = await asyncio.to_thread(_pages)
                teach_start(pages[0].get("url", "") if pages else "")
                await socket.send_json({"type": "teach", "on": True, "count": len(_teach or [])})
            elif msg.get("type") == "teach_stop":
                await socket.send_json({"type": "teach_steps", "steps": teach_stop()})
                await socket.send_json({"type": "teach", "on": False, "count": 0})
            elif msg.get("type") in ("done", "close"):
                # Hand control back to the agent; "close" also shuts the browser (the frames end,
                # and the stream goes idle until it starts again).
                taken["by_me"] = False
                answered = release()
                await socket.send_json({"type": "hold", "hold": None})
                if msg["type"] == "close":
                    await asyncio.to_thread(close_browser)
                await on_done(answered)
            elif _hold is not None:  # input only while the user has the page
                # Off the receive loop: a tap waits for the page's focus, frames must keep flowing.
                asyncio.ensure_future(_input(cdp, size, msg, socket))

    async def follow() -> None:
        last = None
        while True:
            await asyncio.sleep(_POLL_S)
            pages = await asyncio.to_thread(_pages)
            if pages and pages[0]["id"] != target_id:
                return
            if pages:
                info = (pages[0].get("title") or "", pages[0].get("url") or "")
                if info != last:
                    last = info
                    await socket.send_json({"type": "page", "title": info[0], "url": info[1], "hold": _hold,
                                            "handoff": status()["handoff"]})

    tasks = [asyncio.ensure_future(t()) for t in (frames, viewer, follow)]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    # Several can end at once (the viewer leaving closes the frames too): read every one's error,
    # or asyncio logs the unread ones as never retrieved.
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()
    finished = tasks[1] if tasks[1] in done else next(iter(done))
    if finished is not tasks[1]:
        # The tab closed (or the browser did), or the agent moved to another tab: follow it. The
        # frames' error is that closing; reading it keeps asyncio from logging it as unhandled.
        with contextlib.suppress(Exception):
            finished.exception()
        return True
    error = finished.exception()
    if error is not None and not isinstance(error, (WebSocketDisconnect, RuntimeError)):
        raise error
    return False  # the viewer left


# ── the agent's request to the user ───────────────────────────────────────────────────────────────

TOOLSET = "companion"
HAND_OVER_SCHEMA = {
    "name": "browser_hand_over",
    "description": (
        "在浏览器里遇到必须由用户本人完成的步骤时（登录、输入验证码或密码、扫码、付款前最后确认），"
        "把浏览器交给用户：用户会在 App 里看到实时画面并亲自操作，完成后会通知你。"
        "调用后先停止操作浏览器，简短告诉用户需要他做什么。"
    ),
    "parameters": {"type": "object", "properties": {
        "reason": {"type": "string", "description": "需要用户做什么，一句话，比如「登录 Gmail」"}},
        "required": ["reason"]},
}


def register_tools(ctx: Any, chat_for_session: Any, notify: Any) -> None:
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        reason = " ".join(str(args.get("reason") or "").split()) or "在浏览器里完成一步操作"
        # It may have closed when the last turn ended (the user said 好 to a question): open it again,
        # on the pages it had, before the user is sent to it.
        if not ensure_browser():
            return json.dumps({"success": False, "error": "浏览器没能打开，没法交给用户。告诉用户这一步暂时做不了。"}, ensure_ascii=False)
        request = hand_over(reason, chat_for_session(kwargs.get("session_id")))
        notify(request)
        return json.dumps({"success": True, "message": (
            f"已请用户接手浏览器：{reason}。用户在 App 里完成后会通知你；在那之前不要操作浏览器。")}, ensure_ascii=False)

    ctx.register_tool(name="browser_hand_over", toolset=TOOLSET, schema=HAND_OVER_SCHEMA, handler=handle,
                      description=HAND_OVER_SCHEMA["description"], emoji="🖐")
