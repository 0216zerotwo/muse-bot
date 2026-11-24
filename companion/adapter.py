"""The companion app as a Hermes platform.

The phone web app (and the Android shell around it) talks to this adapter over
the same WebSocket protocol it used with nanobot: ``new_chat`` / ``attach`` /
``message`` / ``webui_request`` in, ``ready`` / ``attached`` / ``message_accepted``
/ ``user_message`` / ``delta`` / ``stream_end`` / ``message`` / ``turn_end`` /
``webui_response`` out. Messages go to the agent through ``handle_message``;
the agent's streamed drafts and final sends come back here and are relayed to
every connection attached to the chat, and recorded so the app can replay them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from gateway.config import HomeChannel, Platform, persist_home_channel
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import activity
from .approvals import DEFERRED_PREFIX, Approvals
from .auth import TokenIssuer
from .devices import Devices
from .media import MediaStore
from .modules import log_privacy, stt_sensevoice
from .modules import avatar_make, avatar_motion, companion_state, computer, connections, creations, egress, feed, goals, helper_learn, helpers, hermes_words, interrupted, job_snapshots, memory_fade, memory_learn, memory_tidy, models, oauth_login, plans, push, router, routines, triggers, vault, video_backends, widgets
from .modules.paths import get_webui_dir
from .modules.persona import write_locale_overlay, write_soul
from .store import ChatStore

logger = logging.getLogger(__name__)

PLATFORM = "companion"
OWNER_ID = "owner"
STOP_COMMAND = "/stop"
RETRY_COMMAND = "/retry"
GOAL_CONTROLS = {"/goal stop", "/goal clear", "/goal pause", "/goal resume"}
DEFAULT_PORT = 8766
_DEBUG_FRAMES = bool(os.environ.get("COMPANION_DEBUG_FRAMES"))
DEFAULT_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def _env(name: str, default: str = "") -> str:
    """Plugin settings from the profile's .env (read through Hermes's scoped secrets)."""
    with contextlib.suppress(Exception):
        from gateway.platforms._shared import get_scoped_secret

        value = get_scoped_secret(name, "")
        if value:
            return str(value)
    return os.environ.get(name, default) or default


@dataclass
class _Draft:
    stream_id: str
    text: str
    turn_id: Optional[str]
    # Hermes's id for one segment of a reply (a new one after each tool call).
    draft_id: Optional[int] = None


class CompanionAdapter(BasePlatformAdapter):
    """Serves the companion web app and relays its chats to the agent."""

    # The gateway splits longer replies into "(1/2)" parts (chat apps' size limits) and reads this,
    # not the registry's max_message_length; the default 4096 cut a ```ui page in half. The app has
    # no limit, so a reply always arrives whole.
    MAX_MESSAGE_LENGTH = 1_000_000

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        self.host = _env("COMPANION_HOST", "127.0.0.1")
        self.port = int(_env("COMPANION_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)
        self.web_dist = Path(_env("COMPANION_WEB_DIST", str(DEFAULT_WEB_DIST)))
        self.tokens = TokenIssuer(_env("COMPANION_AUTH_SECRET") or None)
        self.devices = Devices(get_webui_dir())
        # Open sockets and the device each belongs to: removing a device closes them.
        self.socket_devices: dict[Any, Optional[str]] = {}
        self.store = ChatStore(get_webui_dir())
        self.approvals = Approvals(self, get_webui_dir())
        self.media = MediaStore(get_webui_dir())
        # chat_id -> live WebSocket connections attached to it
        self._subs: dict[str, set[Any]] = {}
        # chat_id -> the turn the agent is answering, and any queued follow-up turn ids behind it
        self._turns: dict[str, str] = {}
        self._chain_root: dict[str, str] = {}
        self._inband_active: set[str] = set()
        self._pending_turns: dict[str, list[str]] = {}
        # Helpers working on something another chat handed them: chat -> (turn id, the answer).
        self._helper_waits: dict[str, tuple[str, asyncio.Future]] = {}
        # One task at a time per helper; the rest wait in order.
        self._helper_locks: dict[str, asyncio.Lock] = {}
        # Serialize internal system turns (approval outcomes, browser handoff resumes) per chat.
        self._internal_locks: dict[str, asyncio.Lock] = {}
        self._turn_idle: dict[str, asyncio.Event] = {}
        # Pictures uploaded for a message about to be sent: reference -> (file, type, name, when).
        self._attachments: dict[str, tuple[Path, str, str, float]] = {}
        # One browser: helpers' browser requests take turns.
        self._browser_lock = asyncio.Lock()
        # Which chat a helper's current task came from (a group lets it pass work on to members).
        self._helper_origins: dict[str, str] = {}
        self._drafts: dict[str, _Draft] = {}
        # (chat_id, turn_id) pairs whose turn has shown real output (a reply, a draft, tool activity)
        self._turn_output: set[Any] = set()
        # chat_id -> the companion's latest reply text (titles files it sends right after)
        self._last_reply: dict[str, str] = {}
        self._server_task: Optional[asyncio.Task] = None
        self._bg_tasks: set[asyncio.Task] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Any = None

    @property
    def name(self) -> str:
        return "Companion"

    @property
    def engine(self) -> Any:
        eng = getattr(self, "_engine", None)
        if eng is None:
            from muse.engine import build_engine

            eng = build_engine(self)
            self._engine = eng
        return eng

    # -- lifecycle -------------------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import uvicorn

        from .server import MAX_FRAME_BYTES, build_app

        started_ms = int(time.time() * 1000)
        app = build_app(self)
        helpers.bind(self, asyncio.get_running_loop())
        avatar_motion.bind(self, asyncio.get_running_loop())
        feed.bind(self, asyncio.get_running_loop())
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning", ws="websockets", ws_max_size=MAX_FRAME_BYTES
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # the gateway owns signals
        self._server_task = asyncio.create_task(self._server.serve())
        for _ in range(50):
            if self._server.started:
                break
            if self._server_task.done():
                return self._fail("listen_failed", f"could not listen on {self.host}:{self.port}")
            await asyncio.sleep(0.1)
        self._mark_connected()
        self.loop = asyncio.get_running_loop()
        activity.bind(self, self.loop)
        self.sync_identity()
        triggers.set_base_url(self.host, self.port, _env("COMPANION_PUBLIC_URL"))
        self._spawn_bg(asyncio.to_thread(self._prune_uploads))
        self._spawn_bg(self._time_models())
        if not is_reconnect:
            # A task the last stop cut off (the window closed mid-task): the chat says so, with 接着做.
            self._spawn_bg(interrupted.announce(self, started_ms))
        with contextlib.suppress(Exception):
            feed.ensure_job()
        with contextlib.suppress(Exception):
            helpers.ensure_defaults(self.store)
        self._spawn_bg(self._keep_signed_in())
        self._spawn_bg(self._learn_memory())
        if not self.devices.any_paired():
            code = self.devices.new_code()["code"]
            logger.warning("Companion: no phone is connected yet. In the app, enter this server's address "
                           "and the pairing code %s (valid 10 minutes; tools/companion-pair.py makes a new one).", code)
        logger.info("Companion: serving the app on http://%s:%s", self.host, self.port)
        return True

    async def _time_models(self) -> None:
        """Measure each model's answer time when the last check is over a day old: the small calls
        in front of every reply go to the quickest (see ``models.quick_model``)."""
        await asyncio.sleep(20)  # after start-up, not in its way
        health = models.read_health()
        stale = [name for name in models.read_models()["presets"]
                 if time.time() * 1000 - (health.get(name) or {}).get("checked_at", 0) > 86_400_000
                 or not (health.get(name) or {}).get("latency_ms")]
        if not stale:
            return
        try:
            await models.check_models(stale)
            models.apply_to_hermes()
        except Exception:
            logger.exception("Companion: could not time the models")

    async def browser_done(self, answered: Optional[dict]) -> None:
        """The user handed the browser back (or closed it): when the agent had asked for it, tell it to go on."""
        if answered and answered.get("chat_id"):
            await self.submit_internal(
                answered["chat_id"], f"【系统通知】用户已经在浏览器里完成了：{answered['reason']}。接着刚才的事继续。")

    def sync_identity(self) -> None:
        """Keep Hermes's persona and home channel in step with the companion's own state."""
        persona_changed = write_soul()
        if persona_changed:
            # Hermes freezes the system prompt for a session's lifetime (it keeps the prompt cache);
            # a new session is how the companion picks up its new name, voice or abilities.
            chat = companion_state.read_companion_state().get("chat_id")
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_later(3, lambda: asyncio.ensure_future(self.renew_session(chat)))
        try:
            models.apply_to_hermes(companion_state.read_companion_state().get("timezone"))
        except Exception:
            logger.exception("Companion: could not update the agent's settings")
        with contextlib.suppress(Exception):
            write_locale_overlay()
        chat_id = companion_state.read_companion_state().get("chat_id")
        if not chat_id:
            return
        with contextlib.suppress(Exception):
            triggers.ensure_jobs()
        with contextlib.suppress(Exception):
            plans.ensure_jobs()
        with contextlib.suppress(Exception):
            feed.ensure_job()
        home = HomeChannel(platform=Platform(PLATFORM), chat_id=chat_id, name="伙伴")
        current = getattr(self.config, "home_channel", None)
        if current is not None and getattr(current, "chat_id", None) == chat_id:
            return
        # Scheduled results and proactive messages land in the companion chat, and
        # Hermes never asks the user to /sethome.
        self.config.home_channel = home
        with contextlib.suppress(Exception):
            persist_home_channel(home)

    def _cards(self, text: str) -> str:
        """Small pages in the reply get a signed link the app can open."""
        from .server import AVATARS_PREFIX

        text = widgets.rewrite_cards(text, lambda name: self.media.sign(widgets.PREFIX, name))
        return avatar_make.sign_picture_cards(text, lambda rel: self.media.sign(AVATARS_PREFIX, rel)) if "pictures" in text else text

    def companion_name(self) -> str:
        return companion_state.read_companion_state().get("name") or "伙伴"

    async def renew_session(self, chat_id: Optional[str]) -> None:
        """Start the chat's agent session afresh (Hermes's /reset, without its notice in the chat).
        The app's history stays; the agent keeps its long-term memory but not the old working context."""
        if not chat_id or chat_id in self._turns:
            return  # mid-turn: the next persona change (or restart) will catch up
        try:
            if await self.engine.renew_session(chat_id):
                logger.info("Companion: started a new agent session for the new persona")
        except Exception:
            logger.exception("Companion: could not start a new agent session")

    async def broadcast(self, event: dict[str, Any]) -> None:
        """To every connected app, whichever chat it shows (动态 has new posts)."""
        sockets = set(getattr(self, "socket_devices", {}).keys())
        if getattr(self, "_subs", None):
            sockets |= set().union(*self._subs.values())
        for socket in list(sockets):
            try:
                await socket.send_json(event)
            except Exception:
                self.detach_all(socket)

    async def emit_approval_updated(self, record: Any) -> None:
        """Live WebSocket event whenever an approval card is created or decided."""
        session_key = str(getattr(record, "session_key", "") or "")
        chat_id = session_key.split(":", 1)[-1] if ":" in session_key else session_key
        await self.broadcast({
            "event": "approval_updated",
            "chat_id": chat_id,
            "session_key": session_key,
            "approval_id": record.id,
            "status": record.status,
            "approval": record.public(),
        })

    def _fail(self, code: str, message: str) -> bool:
        self._set_fatal_error(code, message, retryable=True)
        return False

    def _spawn_bg(self, coro: Any) -> asyncio.Task:
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = set()
            self._bg_tasks = tasks
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def _chain_root_map(self) -> dict[str, str]:
        roots = getattr(self, "_chain_root", None)
        if roots is None:
            roots = {}
            self._chain_root = roots
        return roots

    def _inband_set(self) -> set[str]:
        inband = getattr(self, "_inband_active", None)
        if inband is None:
            inband = set()
            self._inband_active = inband
        return inband

    def _pending_map(self) -> dict[str, list[str]]:
        pending = getattr(self, "_pending_turns", None)
        if pending is None:
            pending = {}
            self._pending_turns = pending
        return pending

    def _set_active_turn(self, chat_id: str, turn_id: str, *, as_root: bool = False) -> None:
        self._turns[chat_id] = turn_id
        roots = self._chain_root_map()
        if as_root or chat_id not in roots:
            roots[chat_id] = turn_id
            self._inband_set().discard(chat_id)
        idle = getattr(self, "_turn_idle", None)
        if idle is not None and chat_id in idle:
            idle[chat_id].clear()

    def _clear_active_turn(self, chat_id: str) -> Optional[str]:
        popped = self._turns.pop(chat_id, None)
        self._chain_root_map().pop(chat_id, None)
        self._inband_set().discard(chat_id)
        idle = getattr(self, "_turn_idle", None)
        if idle is not None and chat_id in idle:
            idle[chat_id].set()
        return popped

    def _note_turn_output(self, chat_id: str, turn_id: Optional[str]) -> None:
        if not turn_id:
            return
        if not hasattr(self, "_turn_output"):
            self._turn_output = set()
        self._turn_output.add((chat_id, str(turn_id)))

    def _has_turn_output(self, chat_id: str, turn_id: Optional[str]) -> bool:
        out = getattr(self, "_turn_output", ())
        return bool((turn_id and (chat_id, str(turn_id)) in out) or chat_id in out)

    def _discard_turn_output(self, chat_id: str, turn_id: Optional[str]) -> None:
        out = getattr(self, "_turn_output", None)
        if out is None:
            return
        if turn_id:
            out.discard((chat_id, str(turn_id)))
        out.discard(chat_id)

    async def disconnect(self) -> None:
        self._mark_disconnected()
        bg = list(getattr(self, "_bg_tasks", ()))
        for task in bg:
            task.cancel()
        if bg:
            with contextlib.suppress(Exception):
                await asyncio.gather(*bg, return_exceptions=True)
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server_task, timeout=5)

    # -- connections -----------------------------------------------------------------------------

    def attach(self, chat_id: str, socket: Any) -> None:
        self._subs.setdefault(chat_id, set()).add(socket)

    def detach_all(self, socket: Any) -> None:
        for sockets in self._subs.values():
            sockets.discard(socket)

    async def _emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> None:
        body = {**event, "chat_id": chat_id}
        if event.get("event") in ("delta", "message") and (event.get("source") or {}).get("kind") != "notice":
            self._note_turn_output(chat_id, event.get("turn_id"))
        if persist:
            body = self.store.append(chat_id, body)
        if _DEBUG_FRAMES:
            logger.warning("Companion frame -> %d socket(s): %s", len(self._subs.get(chat_id, ())), str(body)[:160])
        for socket in list(self._subs.get(chat_id, ())):
            try:
                await socket.send_json(body)
            except Exception:
                self.detach_all(socket)

    # -- inbound (phone → agent) -----------------------------------------------------------------

    def _prune_uploads(self, max_age_s: float = 3600.0) -> None:
        now = time.time()
        tracked: set[Path] = set()
        for ref, (old, _, _, at) in list(getattr(self, "_attachments", {}).items()):
            if now - at > max_age_s:
                self._attachments.pop(ref, None)
                with contextlib.suppress(OSError):
                    old.unlink(missing_ok=True)
            else:
                tracked.add(old)
        folder = get_webui_dir() / "uploads"
        if not folder.is_dir():
            return
        with contextlib.suppress(OSError):
            for entry in folder.iterdir():
                if entry in tracked or not entry.is_file():
                    continue
                with contextlib.suppress(OSError):
                    if now - entry.stat().st_mtime > max_age_s:
                        entry.unlink(missing_ok=True)

    def keep_attachment(self, data: bytes, mime: str, name: str) -> str:
        """A picture the app uploaded for its next message; its reference (the message names it)."""
        import mimetypes

        display = name or ("图片" + (mimetypes.guess_extension(mime) or ".jpg"))
        folder = get_webui_dir() / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        reference = uuid.uuid4().hex
        path = folder / f"{reference}{Path(display).suffix or mimetypes.guess_extension(mime) or ''}"
        path.write_bytes(data)
        self._attachments[reference] = (path, mime, display, time.time())
        # Uploaded but never sent (the app gave up, or left over from an earlier run): gone after an hour.
        self._prune_uploads()
        return reference

    def take_attachment(self, reference: str) -> Optional[tuple[Path, str, str]]:
        kept = self._attachments.pop(reference, None)
        return kept[:3] if kept else None

    async def submit(self, chat_id: str, text: str, turn_id: Optional[str],
                     attachments: Optional[list[tuple[Path, str, str]]] = None) -> str:
        """A message typed in the app (with any pictures it sent): record it, then hand it to the
        agent as one turn."""
        if text.strip() == STOP_COMMAND:
            return await self.stop(chat_id)
        if text.strip().lower() in GOAL_CONTROLS:
            # Stopping or pausing a goal is a button in the app, not something the user said.
            await self.handle_message(self._event(chat_id, text.strip(), uuid.uuid4().hex))
            asyncio.get_running_loop().call_later(1.5, lambda: asyncio.ensure_future(self.push_goal_state(chat_id)))
            return ""
        self.store.ensure(chat_id)
        turn_id = turn_id or uuid.uuid4().hex
        # How to present the answer is judged now, alongside the turn's set-up, not after it.
        router.prefetch(text, self._recent(chat_id))
        # The pictures go into the chat's media (shown with the message, now and after a reload);
        # the agent gets the same files.
        shown, files = [], []
        for path, mime, name in attachments or []:
            item = await asyncio.to_thread(self.media.stage_file, path, name)
            path.unlink(missing_ok=True)
            if item:
                kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "file"
                shown.append({**item, "kind": kind})
                files.append((self.media.path_of(item), mime))
        event: dict[str, Any] = {"event": "user_message", "text": text, "starts_turn": True, "turn_id": turn_id,
                                 "turn_phase": "user"}
        if shown:
            event["media_urls"] = shown
        await self._emit(chat_id, event, persist=True)
        return await self._start_turn(chat_id, text, turn_id, [f for f in files if f[0]])

    async def _keep_signed_in(self) -> None:
        """An imported ChatGPT sign-in can be logged out by the Codex CLI refreshing it: look every
        few minutes and take the CLI's current one again (see oauth_login.keep_signed_in)."""
        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(oauth_login.keep_signed_in)
            await asyncio.sleep(300)

    async def _learn_memory(self) -> None:
        """Every hour, what was said since the last pass goes into memory (see memory_learn); once a
        day, what has not come up for long fades (see memory_fade)."""
        await asyncio.sleep(120)  # after start-up's own work
        while True:
            with contextlib.suppress(Exception):
                # A copy of the scheduled jobs when they changed: a reminder that goes missing can come back.
                await asyncio.to_thread(job_snapshots.snapshot)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(memory_learn.learn, self.store)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(memory_fade.fade)
            with contextlib.suppress(Exception):
                # Each helper picks up the user's habits in its line of work (see helper_learn).
                await asyncio.to_thread(helper_learn.learn, self.store)
            await asyncio.sleep(3600)

    def _name_chat(self, chat_id: str) -> None:
        """2–6 words for a side chat's topic (like 饮食记录、股票专项), from its first exchange."""
        said = self._recent(chat_id, 2)
        if not said:
            return
        first = said[0]["content"][:300]
        title = ""
        try:
            from agent.auxiliary_client import call_llm

            data = models.read_models()
            name = models.quick_model(data, list(data["presets"]))
            target = {"provider": models.runtime_provider(name, data["presets"][name]),
                      "model": data["presets"][name]["model"]} if name else {}
            response = call_llm(
                messages=[{"role": "system", "content": "给这段对话起一个 2~6 个字的中文标题（像「饮食记录」「股票专项」），只输出标题。"},
                          {"role": "user", "content": "\n".join(f"{m['role']}：{m['content'][:300]}" for m in said)}],
                max_tokens=20, temperature=0, timeout=10, reasoning_config={"enabled": False}, **target)
            title = (response.choices[0].message.content or "").strip().strip("「」\"'《》 ")
        except Exception as exc:
            logger.info("Companion: could not name a side chat (%s)", type(exc).__name__)
        self.store.set_title(chat_id, title[:12] or first[:12])

    def _recent(self, chat_id: str, count: int = 4) -> list[dict[str, str]]:
        """The last few things said in this chat, as the router reads them."""
        said = []
        for event in self.store.events(chat_id)[-60:]:
            if event.get("event") == "user_message" and event.get("text"):
                said.append({"role": "user", "content": str(event["text"])})
            elif event.get("event") == "message" and event.get("turn_phase") == "answer" and event.get("text"):
                said.append({"role": "assistant", "content": str(event["text"])})
        return said[-count:]

    async def ask_helper(self, origin: Optional[str], helper_chat: str, task: str, timeout: float) -> str:
        """Hand a helper a piece of work and wait for its answer.

        A helper does one thing at a time: work handed to it while it is busy waits its turn (in the
        order it came), within the same ``timeout``. The task shows in the helper's working chat as a
        message from ``origin``, the helper works on it there, and what it answers comes back."""
        if not helpers.helper_for_chat(helper_chat) and not helpers.is_desk(helper_chat):
            raise RuntimeError("这个帮手已经删了")  # deleted while the work waited: don't bring its chat back
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        lock = self._helper_locks.setdefault(helper_chat, asyncio.Lock())
        await asyncio.wait_for(lock.acquire(), timeout)
        try:
            # Still finishing work whose asker stopped waiting: let it end first.
            while helper_chat in self._turns:
                if loop.time() > deadline:
                    raise TimeoutError
                await asyncio.sleep(1)
            return await self._hand_helper(origin, helper_chat, task, max(1.0, deadline - loop.time()))
        finally:
            lock.release()

    def hand_off(self, origin: str, helper: dict[str, Any], task: str, timeout: float, goal_id: str = "") -> None:
        """Work handed to a helper without waiting: the chat goes on, and when the helper is done its
        answer shows in ``origin`` as its own message (and on the phone), and the companion there
        hears about it so it can carry on."""
        async def run() -> None:
            name = helper["name"]
            try:
                answer, ok = await self.ask_helper(origin, helper["chat_id"], task, timeout), True
            except Exception as exc:
                answer, ok = (str(exc) if not isinstance(exc, TimeoutError) else "") or "没做完", False
            if goal_id:
                plans.log_event(goal_id, "helper_done" if ok else "helper",
                                f"「{name}」{'做完了' if ok else '没做成'}：{answer}", helper=name, emoji=helper.get("emoji"))
            await self.post_as_helper(origin, helper, answer if ok else f"这件事没做成：{answer}")
            push.schedule_push(f"{helper.get('emoji') or '🤖'} {name}", push.preview(widgets.strip_cards(answer)), tag=f"helper-{helper['id']}")
            # Let a turn already running there finish before the companion picks this up.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 600
            while origin in self._turns and loop.time() < deadline:
                await asyncio.sleep(2)
            await self.submit_internal(origin, (
                f"【系统通知】你之前在后台交给「{name}」的事{'做完了' if ok else '没做成'}，它的回答已经以它自己的名义显示给用户了，不要复述。"
                f"\n交给它的事：{task[:300]}\n它的回答：{answer[:3000]}\n"
                "如果这是为了接下来的某一步，就接着做；否则只用一句很短的话接上（比如点出最要紧的一点，或问用户下一步要不要做什么），不要重复它的回答。"))

        def done(task_: "asyncio.Task[None]") -> None:
            if not task_.cancelled() and task_.exception():
                logger.error("companion: background work for %s failed", helper.get("name"), exc_info=task_.exception())

        asyncio.get_running_loop().create_task(run()).add_done_callback(done)

    async def browser_for(self, helper: dict[str, Any], task: str, timeout: float) -> str:
        """Browser work a helper asked for, done by the companion in a browser desk of its own — one
        request at a time, as there is one browser."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.wait_for(self._browser_lock.acquire(), timeout)
        try:
            desk = await asyncio.to_thread(helpers.new_desk, self.store)
            # It goes when its turn ends (on_processing_complete), also when the asker stopped waiting.
            return await self.ask_helper(helper["chat_id"], desk, task, max(1.0, deadline - loop.time()))
        finally:
            self._browser_lock.release()

    def facing_chat(self, chat_id: str) -> str:
        """Where the user is waiting on work going on in ``chat_id``: a helper (or the browser desk)
        doing work another chat handed it answers to that chat, so a card asking the user goes there."""
        for _ in range(4):
            origin = self._helper_origins.get(chat_id)
            if chat_id not in self._helper_waits or not origin:
                break
            chat_id = origin
        return chat_id

    def helper_origin(self, helper_chat: Optional[str]) -> Optional[str]:
        return self._helper_origins.get(helper_chat or "")

    async def post_notice(self, chat_id: str, text: str) -> None:
        """A small grey line in a chat, not a message (a helper learned something about the user)."""
        await self._emit(chat_id, {"event": "message", "text": text, "source": {"kind": "notice", "label": ""}},
                         persist=True)

    async def post_as_helper(self, chat_id: str, helper: dict[str, Any], text: str) -> None:
        """In a group chat: a helper's answer, shown as its own message (its emoji and name)."""
        await self._emit(chat_id, {"event": "message", "text": self._cards(text),
                                   "source": {"kind": "helper", "id": helper["id"],
                                              "label": f"{helper.get('emoji') or '🤖'} {helper['name']}"}},
                         persist=True)

    async def _hand_helper(self, origin: Optional[str], helper_chat: str, task: str, timeout: float) -> str:
        if origin:
            self._helper_origins[helper_chat] = origin
            egress.propagate_chat(origin, helper_chat)
        main = companion_state.read_companion_state().get("chat_id")
        label = "主要聊天" if origin and origin == main else (self.store.title(origin) or "旁聊") if origin else "定时任务"
        turn_id = uuid.uuid4().hex
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._helper_waits[helper_chat] = (turn_id, waiter)
        try:
            self.store.ensure(helper_chat)
            await self._emit(helper_chat, {
                "event": "user_message", "text": task, "starts_turn": True, "turn_id": turn_id, "turn_phase": "user",
                "provenance": {"session_message": {"message_id": turn_id, "session": {"id": origin or "", "name": label}}},
            }, persist=True)
            self._use_helper_model(helper_chat)
            await self._start_turn(helper_chat, f"【{label}交给你的事】{task}", turn_id)
            return await asyncio.wait_for(asyncio.shield(waiter), timeout)
        finally:
            if origin:
                egress.propagate_chat(helper_chat, origin)
            if self._helper_waits.get(helper_chat, (None,))[0] == turn_id and waiter.done():
                self._helper_waits.pop(helper_chat, None)

    def _use_helper_model(self, helper_chat: str) -> None:
        """The helper's own model for its turns (Hermes's per-session override), or the main
        assistant's when it has none."""
        helper = helpers.helper_for_chat(helper_chat)
        if helper is None:
            return
        try:
            self.engine.set_session_model_override(helper_chat, helpers.model_override(helper))
        except Exception:
            logger.exception("companion: could not set %s's model", helper.get("name"))

    def _answer_of(self, chat_id: str, turn_id: str) -> str:
        """What a turn answered, as one text."""
        return "\n\n".join(str(e.get("text") or "").strip() for e in self.store.events(chat_id)
                            if e.get("event") == "message" and e.get("turn_phase") == "answer"
                            and e.get("turn_id") == turn_id and str(e.get("text") or "").strip())

    async def _wait_chat_idle(self, chat_id: str, timeout: float = 600.0) -> None:
        if chat_id not in self._turns:
            return
        idle_map = getattr(self, "_turn_idle", None)
        if idle_map is None:
            idle_map = {}
            self._turn_idle = idle_map
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while chat_id in self._turns:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            ev = idle_map.setdefault(chat_id, asyncio.Event())
            ev.clear()
            if chat_id not in self._turns:
                ev.set()
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), min(remaining, 2.0))

    async def submit_internal(self, chat_id: str, text: str, *, timeout: float = 600.0) -> str:
        """A turn the user didn't type (e.g. the outcome of an approval card): serialized behind any
        active turn on ``chat_id`` so it never clobbers a running turn's state."""
        locks = getattr(self, "_internal_locks", None)
        if locks is None:
            locks = {}
            self._internal_locks = locks
        lock = locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._wait_chat_idle(chat_id, timeout=timeout)
            self.store.ensure(chat_id)
            return await self._start_turn(chat_id, text, uuid.uuid4().hex)

    async def stop(self, chat_id: str) -> str:
        """The app's stop button (it sends ``/stop``): not a chat message, so no bubble, and the
        running turn keeps its id — Hermes cancels it and its completion closes the turn."""
        turn_id = self._turns.get(chat_id)
        if turn_id is None:
            # Nothing running here: just let the app leave its "working" state.
            await self._emit(chat_id, {"event": "turn_end", "turn_phase": "complete"})
            return ""
        await self.handle_message(self._event(chat_id, STOP_COMMAND, uuid.uuid4().hex))
        return turn_id

    async def regenerate(self, chat_id: str, turn_id: str) -> None:
        """重新生成: the chat's last answer again. Its old answer (and what it did on the way) leaves
        the chat, and Hermes's /retry takes the agent's own history back to the question and asks it
        again, under the same turn, so the new answer takes the old one's place."""
        if chat_id in self._turns:
            raise ValueError("还在回复，等它说完再重新生成")
        events = self.store.events(chat_id)
        index = next((i for i in range(len(events) - 1, -1, -1)
                      if events[i].get("event") == "user_message" and events[i].get("starts_turn")), None)
        # Only the last question: anything after it from another turn (an approval's outcome, a
        # helper) is in the agent's history too, and /retry would ask that again instead.
        if index is None or events[index].get("turn_id") != turn_id or any(
                e.get("turn_id") not in (None, turn_id) for e in events[index + 1:]):
            raise ValueError("只能重新生成最后一条回复")
        if events[index].get("media_urls"):
            raise ValueError("带图片或文件的消息暂时不能重新生成")  # /retry can't send them again
        await asyncio.to_thread(self.store.remove_events, chat_id,
                                lambda e: e.get("turn_id") == turn_id and e.get("event") != "user_message")
        self._set_active_turn(chat_id, turn_id, as_root=True)  # at work from now: a reload shows it so
        asyncio.ensure_future(self._start_turn(chat_id, RETRY_COMMAND, turn_id))

    async def _start_turn(self, chat_id: str, text: str, turn_id: str,
                          files: Optional[list[tuple[str, str]]] = None) -> str:
        active = self._turns.get(chat_id)
        queued_here = False
        if active is None or active == turn_id:
            self._set_active_turn(chat_id, turn_id, as_root=True)
        else:
            queue = self._pending_map().setdefault(chat_id, [])
            if turn_id not in queue:
                queue.append(turn_id)
                queued_here = True
        event = self._event(chat_id, text, turn_id)
        if files:
            event.media_urls = [path for path, _ in files]
            event.media_types = [mime for _, mime in files]
            # Files other than pictures: the agent is told where each is and reads it itself.
            event.media_text_inlined = [None if mime.startswith(("image/", "audio/", "video/")) else False
                                        for _, mime in files]
            if all(mime.startswith("image/") for _, mime in files):
                event.message_type = MessageType.PHOTO
            elif not any(mime.startswith(("image/", "audio/", "video/")) for _, mime in files):
                event.message_type = MessageType.DOCUMENT
        try:
            await self.handle_message(event)
        except BaseException:
            if queued_here:
                queue = self._pending_map().get(chat_id)
                if queue and turn_id in queue:
                    queue.remove(turn_id)
                    if not queue:
                        self._pending_map().pop(chat_id, None)
            elif self._turns.get(chat_id) == turn_id:
                self._discard_turn_output(chat_id, turn_id)
                self._clear_active_turn(chat_id)
            raise
        return turn_id

    def _event(self, chat_id: str, text: str, message_id: str) -> MessageEvent:
        source = self.build_source(chat_id=chat_id, chat_name="伙伴", chat_type="dm", user_id=OWNER_ID, user_name="我")
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            user_id=OWNER_ID,
            user_name="我",
            message_id=message_id,
            timestamp=datetime.now(),
        )

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Hermes asks before a risky command: show it as an approval card (the agent waits)."""
        if self.approvals.granted("run_command", {"reason": prompt.description}):
            # A standing yes for this kind of command: answer for the user (the request is queued).
            from tools.approval import resolve_gateway_approval

            resolve_gateway_approval(prompt.session_key, "session")
            return SendResult(success=True)
        raw_chat = prompt.chat_id or activity._home_chat() or ""
        facing = self.facing_chat(raw_chat) if raw_chat else raw_chat
        record = self.approvals.hold_exec(
            facing, prompt.command, prompt.description, prompt.session_key, prompt.choices)
        await self.emit_activity(facing, {
            "version": 1, "phase": "end", "call_id": record.call_id, "name": "terminal",
            "arguments": {"command": prompt.command}, "result": f"{DEFERRED_PREFIX} {record.id}",
            "error": None, "files": [], "embeds": [],
        })
        return SendResult(success=True, message_id=record.id)

    async def emit_activity(self, chat_id: str, tool_event: dict[str, Any]) -> None:
        """One tool call starting or finishing (``tool_hint``, as nanobot sent them)."""
        turn_id = self._turns.get(chat_id)
        if turn_id is None:
            # Housekeeping after the reply (Hermes's self-review): not part of the conversation.
            return
        await self._emit(chat_id, {
            "event": "message", "text": "", "kind": "tool_hint", "turn_phase": "activity",
            "turn_id": turn_id, "tool_events": [tool_event],
        }, persist=True)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """A turn the agent started on its own (a goal's next step) or a queued follow-up now draining."""
        chat_id = event.source.chat_id if event.source else None
        if not chat_id:
            return
        msg_id = event.message_id or ""
        pending_map = self._pending_map()
        queue = pending_map.get(chat_id)
        active = self._turns.get(chat_id)
        if active is None:
            if queue and msg_id in queue:
                queue.remove(msg_id)
                if not queue:
                    pending_map.pop(chat_id, None)
            self._set_active_turn(chat_id, msg_id or uuid.uuid4().hex, as_root=True)
            return
        # In-band queued follow-up (Hermes run_turn): close the finished active turn before starting msg_id,
        # while keeping _chain_root set to the outer base.py turn that opened the chain.
        if msg_id and msg_id != active and queue and msg_id in queue and self._has_turn_output(chat_id, active):
            queue.remove(msg_id)
            if not queue:
                pending_map.pop(chat_id, None)
            self._chain_root_map().setdefault(chat_id, active)
            self._inband_set().add(chat_id)
            self._discard_turn_output(chat_id, active)
            await self._close_draft(chat_id)
            await self._emit(chat_id, {"event": "turn_end", "turn_id": active, "turn_phase": "complete"}, persist=True)
            self._set_active_turn(chat_id, msg_id, as_root=False)

    async def on_processing_complete(self, event: MessageEvent, outcome: Any) -> None:
        chat_id = event.source.chat_id if event.source else None
        if not chat_id:
            return
        active = self._turns.get(chat_id)
        msg_id = event.message_id or ""
        if active is None:
            return
        chain_root = self._chain_root_map().get(chat_id, active)
        inband = chat_id in self._inband_set()
        if inband and msg_id == active and msg_id != chain_root:
            # Hermes run_turn.py fires the inner follow-up completion hook BEFORE base.py delivers the
            # terminal final send(); wait for the final send() + outer chain_root completion hook.
            return
        if msg_id and msg_id != active and not (inband and msg_id == chain_root):
            # A mid-turn steered or intake-queued message finished its outer hook while ``active`` is still running.
            return
        succeeded = getattr(outcome, "value", outcome) == "success"
        if succeeded and not self._has_turn_output(chat_id, active):
            # Queued (e.g. while the gateway was still starting) rather than answered: the turn
            # really completes when the queued message is processed.
            return
        self._discard_turn_output(chat_id, active)
        await self._close_draft(chat_id)
        turn_id = self._clear_active_turn(chat_id) or active
        pending_map = self._pending_map()
        if chat_id in pending_map and not pending_map[chat_id]:
            pending_map.pop(chat_id, None)
        await self._emit(chat_id, {"event": "turn_end", "turn_id": turn_id, "turn_phase": "complete"}, persist=True)
        await self.push_goal_state(chat_id)
        # A helper finished work another chat handed it: its answer goes back there.
        wait = self._helper_waits.pop(chat_id, None)
        if wait and not wait[1].done():
            answer = self._answer_of(chat_id, wait[0])
            if succeeded:
                wait[1].set_result(answer or "（它做完了，但没有写结果）")
            else:
                wait[1].set_exception(RuntimeError(answer or "它没做成"))
        if helpers.is_desk(chat_id):
            # A browser desk is done with its one request (its answer was read above): it goes.
            await asyncio.to_thread(self.store.delete, chat_id)
        memory_tidy.maybe_tidy_soon()
        if succeeded and not self.store.title(chat_id) and chat_id != companion_state.read_companion_state().get("chat_id"):
            # A side chat (旁聊) gets a short name from its first exchange.
            asyncio.get_running_loop().run_in_executor(None, self._name_chat, chat_id)
        # The task that used the browser is over: close it (it starts again when needed).
        asyncio.get_running_loop().run_in_executor(None, computer.close_after_task, chat_id)

    async def push_goal_state(self, chat_id: str, socket: Any = None) -> None:
        """The goal banner's state (Hermes ``/goal``), to one socket or everyone in the chat."""
        try:
            state = await asyncio.to_thread(goals.goal_state, chat_id)
        except Exception:
            logger.debug("Companion: could not read the goal state", exc_info=True)
            return
        frame = {"event": "goal_state", "chat_id": chat_id, "goal_state": state}
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.send_json(frame)
            return
        await self._emit(chat_id, {"event": "goal_state", "goal_state": state})

    # -- outbound (agent → phone) ----------------------------------------------------------------

    def format_tool_event(self, event: Any, *, mode: str = "all", preview_max_len: int = 40) -> Optional[str]:
        """No text bubbles for tool progress: the app shows activity from ``tool_hint`` events."""
        return None

    def supports_draft_streaming(self, chat_type=None, metadata=None, chat_id=None) -> bool:
        return True

    async def send_draft(self, chat_id: str, draft_id: int, content: str, metadata=None) -> SendResult:
        # A draft after a tool call can start with the previous segment's trailing blank lines.
        content = widgets.hold_back_open_card(
            self._cards(_hold_back_media_tag(_without_provisional_fence(content.lstrip()))))
        draft = self._drafts.get(chat_id)
        if draft is not None and draft.draft_id != draft_id:
            # A new segment of the reply (after a tool call): its own bubble.
            await self._close_draft(chat_id)
            draft = None
        if draft is not None and not content.startswith(draft.text):
            # The same segment, rewritten rather than extended (an earlier part changed): the
            # bubble takes the new text in place and stays open. Starting another bubble here left
            # the half-written one on screen above it.
            draft.text = content
            await self._emit(chat_id, {
                "event": "stream_end", "stream_id": draft.stream_id, "text": content, "resuming": True,
                "merge_next": True, "turn_id": draft.turn_id, "turn_phase": "answer",
            })
            return SendResult(success=True)
        if draft is None:
            draft = _Draft(stream_id=f"draft-{draft_id}-{uuid.uuid4().hex[:8]}", text="", turn_id=self._turns.get(chat_id),
                           draft_id=draft_id)
            self._drafts[chat_id] = draft
        delta = content[len(draft.text):]
        draft.text = content
        if delta:
            await self._emit(chat_id, {
                "event": "delta", "text": delta, "stream_id": draft.stream_id,
                "turn_id": draft.turn_id, "turn_phase": "answer",
            })
        return SendResult(success=True)

    async def _close_draft(self, chat_id: str, final_text: Optional[str] = None) -> Optional[_Draft]:
        draft = self._drafts.pop(chat_id, None)
        if draft is None:
            return None
        final_text = final_text.strip() if final_text else final_text
        if final_text and final_text.startswith(draft.text) and len(final_text) > len(draft.text):
            await self._emit(chat_id, {
                "event": "delta", "text": final_text[len(draft.text):], "stream_id": draft.stream_id,
                "turn_id": draft.turn_id, "turn_phase": "answer",
            })
            draft.text = final_text
        elif final_text and final_text != draft.text.strip():
            # The final reply differs from what streamed (not just longer): the streamed bubble
            # becomes the final text, instead of staying half-written above a second copy.
            await self._emit(chat_id, {"event": "stream_end", "stream_id": draft.stream_id, "text": final_text,
                                       "turn_id": draft.turn_id, "turn_phase": "answer"})
            draft.text = final_text
            return draft
        await self._emit(chat_id, {"event": "stream_end", "stream_id": draft.stream_id, "turn_id": draft.turn_id})
        return draft

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        content = _goal_notice(content)
        if content is None:
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        # Hermes's one-time English tips ("💡 First-time tip — … /busy queue …") are about its own
        # commands, not this app: never shown.
        content = hermes_words.in_our_words(_without_tips(content))
        if not content.strip():
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        if _is_busy_ack(content):
            # "好，按你刚说的调整。" — the answer to a message sent while it works: a grey line, and it
            # must not close the reply being streamed (it isn't that reply).
            await self.post_notice(chat_id, content.strip())
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        if chat_id == companion_state.read_companion_state().get("chat_id"):
            content = avatar_make.with_pick_card(content)
        content = self._cards(content)
        if chat_id not in self._turns and content.strip() in _idle_lifecycle_notices():
            # "Shutting down / back online" only matter while a task is running; restarting the
            # server while idle shouldn't leave notices in the chat.
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        if (metadata or {}).get("_interim_send"):
            # Hermes's mid-turn status (the "⏳ 正在处理……" heartbeat every few minutes, a stuck
            # warning, a notice): never the turn's answer, so it must not close the reply being
            # streamed or be kept as one. The heartbeat says what the app already shows live (what
            # the companion is doing), and this adapter can't edit it in place, so a long task would
            # pile them up: dropped. Anything else is a grey line.
            if not content.lstrip().startswith("⏳"):
                await self.post_notice(chat_id, content)
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        job = (metadata or {}).get("job_id")
        # A scheduled job's message (早报, 主动关心, a trigger) is its own, even when it lands while
        # the user's turn is running: it is not that turn's answer, and the reply being streamed
        # stays open.
        turn_id = None if job else self._turns.get(chat_id)
        draft = None if job else await self._close_draft(chat_id, content)
        if not job:
            self._last_reply[chat_id] = content
        body: dict[str, Any] = {"event": "message", "text": content}
        if turn_id:
            body.update(turn_id=turn_id, turn_phase="answer")
        source = routines.source_for_job(job) if job else None
        if source:
            # The companion reaching out on its own (早报, 主动关心, a reminder): 动态 lists these,
            # and the phone gets a notification.
            body["source"] = source
            push.schedule_push(self.companion_name(), push.preview(widgets.strip_cards(content)), tag=source["label"])
        if draft is not None and draft.text.strip() == content.strip():
            # Already shown live as a stream: only record it for replay.
            self._note_turn_output(chat_id, turn_id)
            self.store.append(chat_id, {**body, "chat_id": chat_id})
        else:
            await self._emit(chat_id, body, persist=True)
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str, confirm_id: str,
                                 metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Hermes wants a command confirmed (/new, /undo): a sentence and 好 / 算了, whose taps send
        the /approve or /cancel it reads as the answer. One it doesn't know: Hermes's own prompt."""
        text = hermes_words.confirm_text(title)
        if text is None:
            return SendResult(success=False, error="Not supported")
        return await self.send(chat_id, text, metadata=metadata)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Hermes asks the user something: its options become a choices card. Tapping one sends the
        option's exact text, which Hermes takes as the answer; typing something else works too."""
        if not choices:
            return await self.send(chat_id, question, metadata=metadata)
        from tools.clarify_gateway import get_pending, mark_awaiting_text

        multi = bool(getattr(get_pending(clarify_id), "multi_select", False))
        mark_awaiting_text(clarify_id)
        options = []
        for choice in choices:
            text = str(choice).strip()
            label = _RECOMMENDED.sub("", text).strip() or text
            options.append({"label": label, "send": text, "primary": label != text})
        card = {"type": "choices", "options": options, "multiple": multi}
        body = f"{question.strip()}\n\n```card\n{json.dumps(card, ensure_ascii=False)}\n```"
        return await self.send(chat_id, body, metadata=metadata)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """The app shows its own typing state from the turn lifecycle."""

    # -- media: shown inline in the chat (images, videos) or as a file card ----------------------

    async def _send_media(self, chat_id: str, item: Optional[dict[str, str]], caption: Optional[str],
                          fallback: str, source: Optional[str] = None) -> SendResult:
        if source and item is not None:
            # What it sends the user is kept in 创意, titled from the reply that carried it.
            title = creations.reply_title(widgets.strip_cards(caption or self._last_reply.get(chat_id, "")))
            await asyncio.to_thread(creations.collect_sent_file, Path(source), title=title)
        if item is None:
            # Couldn't keep a copy: say so in words rather than drop it silently.
            return await self.send(chat_id, f"{caption}\n\n{fallback}" if caption else fallback)
        turn_id = self._turns.get(chat_id)
        await self._close_draft(chat_id)
        body: dict[str, Any] = {"event": "message", "text": caption or "", "media_urls": [item]}
        if turn_id:
            body.update(turn_id=turn_id, turn_phase="answer")
        await self._emit(chat_id, body, persist=True)
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata=None) -> SendResult:
        if image_url.startswith(("http://", "https://")):
            item = await self.media.stage_url(image_url)
        else:
            item = self.media.stage_file(image_url)
        source = image_url if not image_url.startswith(("http://", "https://")) else self.media.path_of(item)
        return await self._send_media(chat_id, item, caption, image_url, source)

    async def send_animation(self, chat_id: str, animation_url: str, caption: Optional[str] = None,
                             reply_to: Optional[str] = None, metadata=None) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption, reply_to, metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, self.media.stage_file(image_path), caption, "图片没能发出来", image_path)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, self.media.stage_file(video_path), caption, "视频没能发出来", video_path)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, self.media.stage_file(audio_path), caption, "语音没能发出来")

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata=None, **kwargs) -> SendResult:
        item = self.media.stage_file(file_path, name=file_name)
        return await self._send_media(chat_id, item, caption, f"文件 {file_name or Path(file_path).name} 没能发出来",
                                      file_path)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": "伙伴", "type": "dm", "chat_id": chat_id}


# Hermes's goal loop reports progress in fixed English lines (its judge's reasoning included).
# The app shows the goal in its banner; in the chat only the outcome, in the companion's words.
_GOAL_NOTICES = (
    (("✓ Goal achieved", "✓ Goal done"), "✓ 这件事办完了。"),
    (("⏸ Goal paused",), "⏸ 这件事先停下了：做了不少步还没办完，或者遇到了需要你看一下的情况。想继续的话跟我说一声。"),
    (("⏳ Goal parked",), "⏳ 在等一件事完成，好了我接着做。"),
    (("↻ Continuing toward goal", "✗ Quality gate failed"), None),
    # "Still working, N minutes" (the app shows live status itself) and Hermes's notes on the
    # skills it saved for itself — housekeeping, not something to say to the user.
    (("⏳ 正在处理——", "💾 自我改进回顾"), None),
)


def _goal_notice(text: str) -> Optional[str]:
    """The companion's version of a goal-loop notice (None: don't show it); other text unchanged."""
    stripped = text.lstrip()
    for prefixes, replacement in _GOAL_NOTICES:
        if stripped.startswith(prefixes):
            return replacement
    # Setting a goal appends how Hermes's judge works, in English.
    return text.split("\nAfter each turn, a judge model", 1)[0]


# How the model marks the option it suggests: shown as the highlighted choice instead.
_RECOMMENDED = re.compile(r"\s*[（(]\s*(?:recommended|推荐)\s*[)）]\s*$", re.IGNORECASE)

def _without_provisional_fence(text: str) -> str:
    """Hermes closes a code block that is still being written with a ``` at the end of every draft;
    left in, each draft would end differently and never extend the last one (a new bubble per
    draft). A block that really ended comes back in the next draft or the final reply."""
    trimmed = text.rstrip()
    return trimmed[:-3].rstrip() if trimmed.endswith("```") else text


_MEDIA_TAG = "MEDIA:"


def _hold_back_media_tag(text: str) -> str:
    """Drafts stream before Hermes pulls ``MEDIA:/path`` lines out of the reply: keep those lines
    (and a last line that may still become one) off the screen; the file arrives as its own message."""
    lines = text.split("\n")
    kept = [line for line in lines if not line.lstrip().startswith(_MEDIA_TAG)]
    if kept and kept[-1].strip() and _MEDIA_TAG.startswith(kept[-1].strip()):
        kept.pop()
    return text if kept == lines else "\n".join(kept).rstrip()


_TIP = "💡 First-time tip"
_PARAGRAPH = chr(10) * 2  # a blank line between paragraphs


def _without_tips(text: str) -> str:
    """The text without Hermes's first-time tips (paragraphs of their own)."""
    if _TIP not in text:
        return text
    paragraphs = text.split(_PARAGRAPH)
    return _PARAGRAPH.join(part for part in paragraphs if not part.lstrip().startswith(_TIP)).strip()


def _translate_or_overlay(key: str) -> str:
    try:
        from agent.i18n import t

        return t(key).strip()
    except ImportError:
        from .modules import persona

        return str(persona._COMPANION_ZH.get(key, "")).strip()
    except Exception:
        return ""


def _busy_heads() -> tuple[str, ...]:
    """How each answer to a message sent while it works begins (in the overlay's words)."""
    heads = []
    for key in ("steered_head", "steered_subagents_head", "redirected_head", "queued_head", "interrupting_head",
                "subagent_working_head", "compressing_head"):
        head = _translate_or_overlay(f"gateway.busy.{key}")
        if head:
            heads.append(head)
    return tuple(heads)


def _is_busy_ack(text: str) -> bool:
    heads = _busy_heads()
    return bool(heads) and text.lstrip().startswith(heads)


def _idle_lifecycle_notices() -> set[str]:
    keys = ("gateway.shutdown.notice_shutdown", "gateway.shutdown.notice_restart",
            "gateway.startup.restarted", "gateway.startup.online")
    notices = set()
    for key in keys:
        val = _translate_or_overlay(key)
        if val:
            notices.add(val)
    return notices


# ── Plugin registration ──────────────────────────────────────────────────────────────────────────


def check_requirements() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


def _env_enablement() -> dict[str, Any]:
    """Always on: the companion is this bot's own front door."""
    return {"port": _env("COMPANION_PORT", str(DEFAULT_PORT))}


def _notify_hand_over(request: dict) -> None:
    """A notification on the phone: the companion needs the user in the browser."""
    push.schedule_push(companion_state.read_companion_state().get("name") or "伙伴", f"需要你来一下：{request['reason']}",
                       tag="computer", loop=activity._loop)


def register(ctx):
    # What the user writes stays out of the server's log files (only its size is logged).
    log_privacy.install()
    ctx.register_hook("pre_tool_call", activity.on_pre_tool_call)
    ctx.register_hook("post_tool_call", activity.on_post_tool_call)
    # Picks text / card / generated UI for each turn before the companion writes it.
    ctx.register_hook("pre_llm_call", router.on_pre_llm_call)
    # A helper's job and notes on each of its turns (and elsewhere, which helpers there are).
    ctx.register_hook("pre_llm_call", helpers.on_pre_llm_call)
    plans.register_tools(ctx)
    triggers.register_tools(ctx)
    feed.register_tools(ctx)
    helpers.register_tools(ctx)
    connections.register_tools(ctx)
    vault.register_tools(ctx)
    avatar_make.register_tools(ctx)
    avatar_motion.register_tools(ctx)
    # 海螺、Seedance、万相 straight from MiniMax, 火山方舟 and 百炼 (with the key added on the model page).
    video_backends.register(ctx)
    # 语音转文字 on this server (SenseVoice), used once its model is in place.
    stt_sensevoice.register(ctx)
    try:
        stt_sensevoice.enable()
    except Exception:
        logger.exception("companion: could not turn on local speech-to-text")
    # Before Hermes starts its MCP servers: Playwright drives the shared browser (see computer).
    try:
        computer.ensure_cdp_config()
        computer.share_with_builtin_tools()
    except Exception:
        logger.exception("Companion: could not point the browser server at the shared browser")
    computer.register_tools(ctx, lambda session_id: activity._chat_for_session(str(session_id)) if session_id else None,
                            _notify_hand_over)
    ctx.register_platform(
        name=PLATFORM,
        label="伙伴",
        dashboard_description="The personal companion web app and Android shell.",
        docs_url="",
        adapter_factory=CompanionAdapter,
        check_fn=check_requirements,
        validate_config=lambda config: True,
        is_connected=lambda config: True,
        required_env=[],
        install_hint="Needs the web extra (fastapi, uvicorn)",
        env_enablement_fn=_env_enablement,
        allowed_users_env="COMPANION_ALLOWED_USERS",
        # Scheduled tasks can deliver into the companion chat (deliver="companion:<chat_id>").
        cron_deliver_env_var="COMPANION_HOME_CHANNEL",
        allow_all_env="COMPANION_ALLOW_ALL_USERS",
        max_message_length=0,  # no chunking: see CompanionAdapter.MAX_MESSAGE_LENGTH
        emoji="🐾",
        pii_safe=True,
        platform_hint=(
            "You are chatting in the user's personal companion app on their phone. "
            "Reply like a text message: short paragraphs, markdown is rendered. "
            "To send a file, write MEDIA:/absolute/path/to/file on its own line: images, videos and audio "
            "show inline, other files become a file card. Remote images: ![alt](url). Never paste a local "
            "path as plain text expecting the user to open it — they are on their phone."
        ),
    )
