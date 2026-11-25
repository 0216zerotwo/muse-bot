"""Single-process ASGI application mounting `companion/server.py` + native `NativeAgentEngine`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config_store import _ENV_VAR_NAME_DENYLIST, _ENV_VAR_NAME_DENY_PREFIXES, _env_var_policy_name, get_env_value, read_env_values
from .context import _kill_process_tree
from .paths import bind_muse_home, get_muse_home, get_repo_root, reset_muse_home
from .runtime import NativeAgentEngine, install_runtime_privacy
from .streaming import ChatSink, new_history_id

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
STOP_COMMAND = "/stop"
GOAL_CONTROLS = frozenset({"/goal stop", "/goal clear", "/goal pause", "/goal resume"})


def load_home_dotenv(home: Optional[Path | str] = None) -> dict[str, str]:
    """Load `<MUSE_HOME>/.env` into `os.environ` (`override=False`, honoring security denylist)."""
    loaded: dict[str, str] = {}
    with contextlib.suppress(Exception):
        for key, val in read_env_values(home=home).items():
            if not key or val is None or key in os.environ:
                continue
            pol = _env_var_policy_name(key)
            if pol in _ENV_VAR_NAME_DENYLIST or pol.startswith(_ENV_VAR_NAME_DENY_PREFIXES):
                continue
            os.environ[key] = str(val)
            loaded[key] = str(val)
    return loaded


def _read_setting(name: str, default: str = "") -> str:
    val = get_env_value(name) or os.environ.get(name, "")
    return str(val).strip() if val else default


class NativeCompanionAdapter(ChatSink):
    """Hermes-free native host fulfilling `companion/server.py`, tools, and `ChatSink` contracts."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        home: Optional[Path | str] = None,
        web_dist: Optional[Path | str] = None,
        engine: Optional[NativeAgentEngine] = None,
        start_background_services: bool = True,
    ) -> None:
        from companion.approvals import Approvals
        from companion.auth import TokenIssuer
        from companion.devices import Devices
        from companion.media import MediaStore
        from companion.store import ChatStore

        self.host = host
        self.port = int(port)
        self.home = Path(home).expanduser() if home is not None else get_muse_home()
        load_home_dotenv(self.home)
        self.webui_dir = self.home / "companion"
        self.webui_dir.mkdir(parents=True, exist_ok=True)
        default_dist = get_repo_root() / "web" / "dist"
        self.web_dist = (
            Path(web_dist).expanduser()
            if web_dist is not None
            else Path(_read_setting("COMPANION_WEB_DIST", str(default_dist)))
        )

        self.tokens = TokenIssuer(_read_setting("COMPANION_AUTH_SECRET") or None)
        self.devices = Devices(self.webui_dir)
        self.media = MediaStore(self.webui_dir)
        self.store = ChatStore(self.webui_dir)
        self.approvals = Approvals(self, self.webui_dir)
        self.socket_devices: dict[Any, Optional[str]] = {}
        self._subs: dict[str, set[Any]] = {}
        self._turns: dict[str, str] = {}
        self._last_reply: dict[str, str] = {}
        self._attachments: dict[str, tuple[Path, str, str, float]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._engine = engine or NativeAgentEngine(
            self,
            home=self.home,
            start_background_services=start_background_services,
        )

    @property
    def engine(self) -> NativeAgentEngine:
        return self._engine

    # ── WebSocket subscriber management & broadcast ──────────────────────────

    def attach(self, chat_id: str, socket: Any) -> None:
        self._subs.setdefault(chat_id, set()).add(socket)

    def detach_all(self, socket: Any) -> None:
        for sockets in self._subs.values():
            sockets.discard(socket)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Broadcast an event to every connected WebSocket regardless of active chat."""
        sockets = set(self.socket_devices.keys())
        if self._subs:
            sockets |= set().union(*self._subs.values())
        for sock in list(sockets):
            try:
                await sock.send_json(event)
            except Exception:
                self.detach_all(sock)

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

    # ── ChatSink + event fan-out ─────────────────────────────────────────────

    def set_active_turn(self, chat_id: str, turn_id: Optional[str]) -> None:
        if turn_id:
            self._turns[chat_id] = turn_id
        else:
            self._turns.pop(chat_id, None)

    def get_active_turn(self, chat_id: str) -> Optional[str]:
        return self._turns.get(chat_id)

    def clear_active_turn(self, chat_id: str, turn_id: Optional[str] = None) -> None:
        if turn_id is None or self._turns.get(chat_id) == turn_id:
            self._turns.pop(chat_id, None)

    def append_store(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.store.append(chat_id, {**event, "chat_id": chat_id})

    store_append = append_store

    def remove_store_events(self, chat_id: str, predicate: Any) -> int:
        return int(self.store.remove_events(chat_id, predicate))

    def store_events(self, chat_id: str) -> list[dict[str, Any]]:
        return list(self.store.events(chat_id))

    def ensure_chat(self, chat_id: str) -> None:
        if hasattr(self.store, "ensure"):
            self.store.ensure(chat_id)

    def note_turn_output(self, chat_id: str, turn_id: Optional[str]) -> None:
        del chat_id, turn_id

    def is_main_chat(self, chat_id: str) -> bool:
        from companion.modules import companion_state

        return bool(chat_id and companion_state.read_companion_state().get("chat_id") == chat_id)

    def source_for_job(self, job_id: str) -> Optional[dict[str, Any]]:
        from companion.modules import routines

        return routines.source_for_job(job_id)

    def schedule_push(self, title: str, preview: str, *, tag: str = "") -> None:
        with contextlib.suppress(Exception):
            from companion.modules import push

            push.schedule_push(title, preview, tag=tag, loop=self.loop)

    def companion_name(self) -> str:
        from companion.modules import companion_state

        return str(companion_state.read_companion_state().get("name") or "伙伴")

    def _cards(self, text: str) -> str:
        """Sign widget and picture card links so mobile/web apps can open them."""
        from companion.modules import avatar_make, widgets
        from companion.server import AVATARS_PREFIX

        text = widgets.rewrite_cards(text, lambda name: self.media.sign(widgets.PREFIX, name))
        return (
            avatar_make.sign_picture_cards(text, lambda rel: self.media.sign(AVATARS_PREFIX, rel))
            if "pictures" in text
            else text
        )

    def transform_cards(self, chat_id: str, text: str, *, include_pick_card: bool = False) -> str:
        if include_pick_card and self.is_main_chat(chat_id):
            with contextlib.suppress(Exception):
                from companion.modules import avatar_make

                text = avatar_make.with_pick_card(text)
        return self._cards(text)

    async def send_media_file(
        self,
        chat_id: str,
        path: str,
        *,
        caption: str = "",
        turn_id: Optional[str] = None,
        reply_context: str = "",
    ) -> bool:
        import mimetypes

        from companion.modules import creations, widgets

        if reply_context:
            self._last_reply[chat_id] = reply_context
        is_remote = path.startswith(("http://", "https://"))
        item = await self.media.stage_url(path) if is_remote else await asyncio.to_thread(self.media.stage_file, path)
        if item is None:
            mime = mimetypes.guess_type(path)[0] or ""
            fallback = (
                "图片没能发出来"
                if mime.startswith("image/")
                else "视频没能发出来"
                if mime.startswith("video/")
                else "文件没能发出来"
            )
            text = f"{caption}\n\n{fallback}" if caption else fallback
            body: dict[str, Any] = {"event": "message", "text": text, "projection_id": new_history_id()}
            active_tid = turn_id or self._turns.get(chat_id)
            if active_tid:
                body.update(turn_id=active_tid, turn_phase="answer")
            await self._emit(chat_id, body, persist=True)
            return False

        source = self.media.path_of(item) if is_remote else path
        if source:
            with contextlib.suppress(Exception):
                title = creations.reply_title(
                    widgets.strip_cards(caption or reply_context or self._last_reply.get(chat_id, ""))
                )
                await asyncio.to_thread(creations.collect_sent_file, Path(source), title=title)

        body = {"event": "message", "text": caption or "", "media_urls": [item], "projection_id": new_history_id()}
        active_tid = turn_id or self._turns.get(chat_id)
        if active_tid:
            body.update(turn_id=active_tid, turn_phase="answer")
        await self._emit(chat_id, body, persist=True)
        return True

    async def emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
        return await self._emit(chat_id, event, persist=persist)

    async def _emit(self, chat_id: str, event: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
        body = {**event, "chat_id": chat_id}
        if persist:
            body = self.store.append(chat_id, body)
        for sock in list(self._subs.get(chat_id, ())):
            try:
                await sock.send_json(body)
            except Exception:
                self._subs.get(chat_id, set()).discard(sock)
        return body

    async def post_notice(self, chat_id: str, text: str, *, persist: bool = True) -> None:
        await self._emit(
            chat_id,
            {"event": "message", "text": text, "source": {"kind": "notice", "label": ""}},
            persist=persist,
        )

    async def emit_activity(self, chat_id: str, tool_event: dict[str, Any]) -> None:
        """Emit & persist canonical Web/iOS `tool_hint` activity events while `turn_id` is active."""
        turn_id = self._turns.get(chat_id)
        if turn_id is None:
            return
        await self._emit(
            chat_id,
            {
                "event": "message",
                "text": "",
                "kind": "tool_hint",
                "turn_phase": "activity",
                "turn_id": turn_id,
                "tool_events": [dict(tool_event)],
            },
            persist=True,
        )

    async def push_goal_state(self, chat_id: str, socket: Any = None) -> None:
        """Push the goal banner state to one socket (on `attach`) or everyone in `chat_id`."""
        from companion.modules import goals

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
        await self._emit(chat_id, {"event": "goal_state", "goal_state": state}, persist=False)

    # ── Attachments ──────────────────────────────────────────────────────────

    def _prune_uploads(self, max_age_s: float = 3600.0) -> None:
        now = time.time()
        tracked: set[Path] = set()
        for ref, (old, _, _, at) in list(self._attachments.items()):
            if now - at > max_age_s:
                self._attachments.pop(ref, None)
                with contextlib.suppress(OSError):
                    old.unlink(missing_ok=True)
            else:
                tracked.add(old)
        folder = self.webui_dir / "uploads"
        if not folder.is_dir():
            return
        with contextlib.suppress(OSError):
            for entry in folder.iterdir():
                if entry in tracked or not entry.is_file():
                    continue
                with contextlib.suppress(OSError):
                    if now - entry.stat().st_mtime > max_age_s:
                        entry.unlink(missing_ok=True)

    def keep_attachment(
        self, data: bytes | bytearray | memoryview | Path | str, content_type: str, name: str
    ) -> str:
        """Persist an uploaded attachment (raw HTTP bytes or Path) until the WS message consumes it."""
        import mimetypes

        display = name or ("图片" + (mimetypes.guess_extension(content_type) or ".jpg"))
        ref = uuid.uuid4().hex
        if isinstance(data, (bytes, bytearray, memoryview)):
            folder = self.webui_dir / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            suffix = Path(display).suffix or mimetypes.guess_extension(content_type) or ""
            path = folder / f"{ref}{suffix}"
            path.write_bytes(bytes(data))
        else:
            path = Path(data)
        self._attachments[ref] = (path, content_type, display, time.time())
        self._prune_uploads()
        return ref

    def take_attachment(self, ref: str) -> Optional[tuple[Path, str, str]]:
        item = self._attachments.pop(ref, None)
        return (item[0], item[1], item[2]) if item is not None else None

    # ── Helper / browser-desk delegation (companion/modules/helpers.py) ──────

    async def ask_helper(self, origin: Optional[str], helper_chat: str, task: str, timeout: float) -> str:
        return await self._engine.helper_orchestrator.ask_helper(origin, helper_chat, task, timeout=timeout)

    def hand_off(
        self, origin: str, helper: dict[str, Any], task: str, timeout: float, goal_id: str = ""
    ) -> asyncio.Task[None]:
        return self._engine.helper_orchestrator.hand_off(origin, helper, task, timeout=timeout, goal_id=goal_id)

    async def browser_for(self, helper: dict[str, Any], task: str, timeout: float) -> str:
        return await self._engine.helper_orchestrator.browser_for(helper, task, timeout=timeout)

    def helper_origin(self, helper_chat: Optional[str]) -> Optional[str]:
        return self._engine.helper_orchestrator.helper_origin(helper_chat)

    def facing_chat(self, chat_id: str) -> str:
        return self._engine.helper_orchestrator.facing_chat(chat_id) or chat_id

    async def post_as_helper(self, chat_id: str, helper: dict[str, Any], text: str) -> None:
        """Post a helper's own message using the canonical `source: {kind, id, label}` wire format."""
        await self._emit(
            chat_id,
            {
                "event": "message",
                "text": self._cards(text),
                "source": {
                    "kind": "helper",
                    "id": helper["id"],
                    "label": f"{helper.get('emoji') or '🤖'} {helper['name']}",
                },
            },
            persist=True,
        )

    # ── Inbound turns & mutations ────────────────────────────────────────────

    def _recent(self, chat_id: str, count: int = 4) -> list[dict[str, str]]:
        said: list[dict[str, str]] = []
        for event in self.store.events(chat_id)[-60:]:
            if event.get("event") == "user_message" and event.get("text"):
                said.append({"role": "user", "content": str(event["text"])})
            elif event.get("event") == "message" and event.get("turn_phase") == "answer" and event.get("text"):
                said.append({"role": "assistant", "content": str(event["text"])})
        return said[-count:]

    async def submit(
        self,
        chat_id: str,
        text: str,
        turn_id: Optional[str] = None,
        attachments: Optional[list[Any]] = None,
    ) -> None:
        from companion.modules import router

        stripped = (text or "").strip()
        if stripped == STOP_COMMAND:
            await self._engine.stop_turn(chat_id)
            return
        if stripped.lower() in GOAL_CONTROLS:
            await self._engine.handle_goal_control(chat_id, stripped)
            return

        self.store.ensure(chat_id)
        resolved_turn_id = turn_id or uuid.uuid4().hex
        with contextlib.suppress(Exception):
            router.prefetch(text, self._recent(chat_id))

        shown: list[dict[str, Any]] = []
        files: list[tuple[str, str]] = []
        for att in attachments or ():
            if isinstance(att, dict):
                ref = str(att.get("ref") or "")
                taken = self.take_attachment(ref) if ref else None
                if taken is None:
                    continue
                path, mime, fname = taken
            else:
                path, mime, fname = att
            upload_path = Path(path)
            item = await asyncio.to_thread(self.media.stage_file, upload_path, fname)
            with contextlib.suppress(OSError):
                upload_path.unlink(missing_ok=True)
            if item:
                kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "file"
                shown.append({**item, "kind": kind})
                staged_path = self.media.path_of(item)
                if staged_path:
                    files.append((staged_path, mime))

        user_ev: dict[str, Any] = {
            "event": "user_message",
            "text": text,
            "turn_id": resolved_turn_id,
            "starts_turn": True,
            "turn_phase": "user",
        }
        if shown:
            user_ev["media_urls"] = shown
        await self._emit(chat_id, user_ev, persist=True)
        await self._engine.submit_turn(chat_id, text, resolved_turn_id, files=files or None)

    async def submit_internal(self, chat_id: str, text: str) -> str:
        return await self._engine.submit_internal(chat_id, text)

    async def regenerate(self, chat_id: str, turn_id: str) -> None:
        await self._engine.regenerate(chat_id, turn_id)

    async def delete_turn(self, chat_id: str, turn_id: str, role: Optional[str] = None) -> int:
        fn = getattr(self._engine, "delete_turn", None)
        return int(await fn(chat_id, turn_id, role=role)) if callable(fn) else 0

    async def delete_chat(self, chat_id: str) -> None:
        fn = getattr(self._engine, "delete_chat", None)
        if callable(fn):
            await fn(chat_id)

    def _name_chat(self, chat_id: str) -> None:
        """Generate a 2–6 character Chinese topic title for an untitled side chat (`旁聊`)."""
        from companion.modules import models
        from .llm.auxiliary import call_llm

        said = self._recent(chat_id, 2)
        if not said or self.store.title(chat_id):
            return
        first = said[0]["content"][:300]
        title = ""
        try:
            data = models.read_models()
            presets = data.get("presets") or {}
            name = models.quick_model(data, list(presets)) if presets else None
            target = (
                {"provider": models.runtime_provider(name, presets[name]), "model": presets[name]["model"]}
                if name and name in presets
                else {}
            )
            resp = call_llm(
                messages=[
                    {"role": "system", "content": "给这段对话起一个 2~6 个字的中文标题（像「饮食记录」「股票专项」），只输出标题。"},
                    {"role": "user", "content": "\n".join(f"{m['role']}：{m['content'][:300]}" for m in said)},
                ],
                max_tokens=20, temperature=0, timeout=10, reasoning_config={"enabled": False}, **target,
            )
            if resp.choices:
                title = (resp.choices[0].message.content or "").strip().strip("「」\"'《》 ")
        except Exception as exc:
            logger.info("Companion: could not name a side chat (%s)", type(exc).__name__)
        if not self.store.title(chat_id):
            self.store.set_title(chat_id, title[:12] or first[:12])

    async def on_turn_complete(self, chat_id: str, *, succeeded: bool = True) -> None:
        """Post-turn lifecycle hooks: goal banner sync, memory tidy, side-chat auto-title, browser close."""
        if not chat_id or chat_id.startswith("cron:"):
            return
        await self.push_goal_state(chat_id)
        meta = self.store.meta(chat_id)
        is_hidden = bool(meta.get("desk") or meta.get("cron"))
        if not is_hidden:
            with contextlib.suppress(Exception):
                from companion.modules import memory_tidy
                memory_tidy.maybe_tidy_soon()
            if (
                succeeded
                and not self.is_main_chat(chat_id)
                and not meta.get("helper")
                and not meta.get("group")
                and not self.store.title(chat_id)
            ):
                self._spawn_bg(asyncio.to_thread(self._name_chat, chat_id))
        with contextlib.suppress(Exception):
            from companion.modules import computer
            await asyncio.to_thread(computer.close_after_task, chat_id)

    async def browser_done(self, answered: Optional[dict[str, Any]]) -> None:
        if answered and answered.get("chat_id"):
            reason = str(answered.get("reason") or "")
            await self.submit_internal(
                str(answered["chat_id"]),
                f"【系统通知】用户已经在浏览器里完成了：{reason}。接着刚才的事继续。",
            )

    def sync_identity(self) -> None:
        from companion.modules import companion_state, feed, models, plans, triggers
        from companion.modules.persona import write_soul

        changed = write_soul()
        state = companion_state.read_companion_state()
        chat_id = state.get("chat_id")
        if changed and chat_id and self.loop is not None and self.loop.is_running():
            self.loop.call_later(3.0, lambda: asyncio.ensure_future(self._engine.renew_session(chat_id)))
        with contextlib.suppress(Exception):
            models.apply_to_hermes(state.get("timezone"))
        if not chat_id:
            return
        with contextlib.suppress(Exception):
            triggers.ensure_jobs()
        with contextlib.suppress(Exception):
            plans.ensure_jobs()
        with contextlib.suppress(Exception):
            feed.ensure_job()

    # ── Lifespan startup / shutdown ──────────────────────────────────────────

    async def startup(self) -> None:
        from companion import activity
        from companion.modules import avatar_motion, feed, helpers, interrupted, triggers

        started_ms = int(time.time() * 1000)
        self.loop = asyncio.get_running_loop()
        install_runtime_privacy()
        activity.bind(self, self.loop)
        helpers.bind(self, self.loop)
        avatar_motion.bind(self, self.loop)
        feed.bind(self, self.loop)
        triggers.set_base_url(self.host, self.port, _read_setting("COMPANION_PUBLIC_URL"))
        await self._engine.start()
        self.sync_identity()

        if self._engine._start_bg:
            with contextlib.suppress(Exception):
                helpers.ensure_defaults(self.store)
            self._spawn_bg(asyncio.to_thread(self._prune_uploads))
            self._spawn_bg(interrupted.announce(self, started_ms))
            self._spawn_bg(self._keep_signed_in_loop())
            self._spawn_bg(self._time_models_loop())
            self._spawn_bg(self._learn_memory_loop())
            with contextlib.suppress(Exception):
                if not self.devices.any_paired():
                    code = self.devices.new_code()["code"]
                    logger.warning(
                        "Companion: no phone is connected yet. In the app, enter this server's address "
                        "and the pairing code %s (valid 10 minutes; `musebot pair` makes a new one).",
                        code,
                    )

    def _spawn_bg(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _keep_signed_in_loop(self) -> None:
        from companion.modules import oauth_login

        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(oauth_login.keep_signed_in)
            await asyncio.sleep(300)

    async def _time_models_loop(self) -> None:
        from companion.modules import models

        await asyncio.sleep(20)
        health = models.read_health()
        presets = (models.read_models() or {}).get("presets") or {}
        stale = [
            name for name in presets
            if time.time() * 1000 - (health.get(name) or {}).get("checked_at", 0) > 86_400_000
            or not (health.get(name) or {}).get("latency_ms")
        ]
        if not stale:
            return
        with contextlib.suppress(Exception):
            await models.check_models(stale)
            models.apply_to_hermes()

    async def _learn_memory_loop(self) -> None:
        from companion.modules import helper_learn, job_snapshots, memory_fade, memory_learn

        await asyncio.sleep(120)
        while True:
            for fn, args in (
                (job_snapshots.snapshot, ()),
                (memory_learn.learn, (self.store,)),
                (memory_fade.fade, ()),
                (helper_learn.learn, (self.store,)),
            ):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(fn, *args)
            await asyncio.sleep(3600)

    async def shutdown(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()
        await self._engine.stop()
        with contextlib.suppress(Exception):
            from companion.modules import computer

            def _shutdown_browser() -> None:
                computer.release()
                computer.close_browser()
                proc = getattr(computer, "_browser_proc", None)
                if proc is not None and proc.poll() is None and getattr(proc, "pid", 0):
                    _kill_process_tree(int(proc.pid))

            await asyncio.to_thread(_shutdown_browser)


class _LifespanWrapper:
    """Wrap `companion.server.build_app(...)` with native ASGI lifespan management."""

    def __init__(self, inner: Any, adapter: NativeCompanionAdapter, home: Optional[Path]) -> None:
        self.app = inner
        self.adapter = adapter
        self.runtime = adapter.engine
        self._home = home

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            token = bind_muse_home(self._home) if self._home is not None else None
            try:
                while True:
                    msg = await receive()
                    mtype = msg.get("type")
                    if mtype == "lifespan.startup":
                        try:
                            await self.adapter.startup()
                        except Exception as exc:
                            await send({"type": "lifespan.startup.failed", "message": type(exc).__name__})
                            return
                        await send({"type": "lifespan.startup.complete"})
                    elif mtype == "lifespan.shutdown":
                        try:
                            await self.adapter.shutdown()
                        finally:
                            await send({"type": "lifespan.shutdown.complete"})
                        return
            finally:
                if token is not None:
                    reset_muse_home(token)
            return

        token = bind_muse_home(self._home) if self._home is not None else None
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                reset_muse_home(token)


def create_app(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    home: Optional[Path | str] = None,
    web_dist: Optional[Path | str] = None,
    engine: Optional[NativeAgentEngine] = None,
    start_background_services: bool = True,
) -> _LifespanWrapper:
    """Create the single-process ASGI app mounting all Companion routes + `MuseRuntime`."""
    from companion.server import build_app

    resolved_home = Path(home).expanduser() if home is not None else None
    token = bind_muse_home(resolved_home) if resolved_home is not None else None
    try:
        adapter = NativeCompanionAdapter(
            host=host,
            port=port,
            home=resolved_home,
            web_dist=web_dist,
            engine=engine,
            start_background_services=start_background_services,
        )
        inner = build_app(adapter)
    finally:
        if token is not None:
            reset_muse_home(token)
    return _LifespanWrapper(inner, adapter, resolved_home)


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "NativeCompanionAdapter", "create_app", "load_home_dotenv"]
