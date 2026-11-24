"""Agent execution engine seam (``AgentEngine``) and transitional ``HermesEngineBridge``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any, Optional, Protocol, runtime_checkable

from .paths import EngineName, active_engine_name


@runtime_checkable
class AgentEngine(Protocol):
    """Unified interface implemented by both ``HermesEngineBridge`` and native ``MuseEngine``."""

    @property
    def name(self) -> EngineName: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit_turn(self, chat_id: str, text: str, turn_id: str, files: Optional[list[tuple[str, str]]] = None) -> None: ...
    async def stop_turn(self, chat_id: str) -> None: ...
    async def handle_goal_control(self, chat_id: str, command: str) -> None: ...
    async def renew_session(self, chat_id: str) -> bool: ...
    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None: ...
    async def reload_mcp(self) -> dict[str, Any]: ...


class HermesEngineBridge(AgentEngine):
    """Transitional ``AgentEngine`` implementation wrapping Hermes ``gateway_runner`` hooks."""

    def __init__(self, adapter: Any = None, *, runner: Any = None) -> None:
        self._adapter = adapter
        self._explicit_runner = runner

    @property
    def name(self) -> EngineName:
        return "hermes"

    @property
    def runner(self) -> Any:
        return self._explicit_runner if self._explicit_runner is not None else getattr(self._adapter, "gateway_runner", None)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def _build_event(self, chat_id: str, text: str, message_id: str, files: Optional[list[tuple[str, str]]] = None) -> Any:
        if self._adapter is not None and hasattr(self._adapter, "_event"):
            event = self._adapter._event(chat_id, text, message_id)
        else:
            src = type("BridgeSource", (), {"platform": "companion", "chat_id": chat_id})()
            event = type("BridgeEvent", (), {"text": text, "message_id": message_id, "source": src,
                                             "media_urls": [], "media_types": [], "media_text_inlined": []})()
        if files:
            event.media_urls = [p for p, _ in files]
            event.media_types = [m for _, m in files]
            event.media_text_inlined = [None if m.startswith(("image/", "audio/", "video/")) else False for _, m in files]
            try:
                from gateway.platforms.event import MessageType

                if all(m.startswith("image/") for _, m in files):
                    event.message_type = MessageType.PHOTO
                elif not any(m.startswith(("image/", "audio/", "video/")) for _, m in files):
                    event.message_type = MessageType.DOCUMENT
            except ImportError:
                pass
        return event

    async def submit_turn(self, chat_id: str, text: str, turn_id: str, files: Optional[list[tuple[str, str]]] = None) -> None:
        if self._adapter is None:
            raise RuntimeError("HermesEngineBridge requires a bound adapter to submit turns")
        await self._adapter.handle_message(self._build_event(chat_id, text, turn_id, files=files))

    async def stop_turn(self, chat_id: str) -> None:
        if self._adapter is not None:
            await self._adapter.handle_message(self._build_event(chat_id, "/stop", uuid.uuid4().hex))

    async def handle_goal_control(self, chat_id: str, command: str) -> None:
        if self._adapter is not None:
            await self._adapter.handle_message(self._build_event(chat_id, command.strip(), uuid.uuid4().hex))

    async def renew_session(self, chat_id: str) -> bool:
        reset = getattr(self.runner, "_handle_reset_command", None)
        if not chat_id or not callable(reset):
            return False
        await reset(self._build_event(chat_id, "/reset", uuid.uuid4().hex))
        return True

    def set_session_model_override(self, chat_id: str, override: Optional[dict[str, Any]]) -> None:
        runner = self.runner
        key_fn = getattr(runner, "_session_key_for_source", None)
        overrides = getattr(runner, "_session_model_overrides", None)
        if runner is None or not chat_id or not callable(key_fn) or not isinstance(overrides, dict):
            return
        key = key_fn(self._build_event(chat_id, "", "").source)
        if override == overrides.get(key):
            return
        if override:
            overrides[key] = override
        else:
            overrides.pop(key, None)
        if callable(evict := getattr(runner, "_evict_cached_agent", None)):
            evict(key)

    async def reload_mcp(self) -> dict[str, Any]:
        def _sync_reload() -> dict[str, Any]:
            from tools import mcp_tool_discovery as discovery
            from tools.mcp_oauth import suppress_interactive_oauth

            with suppress_interactive_oauth():
                result = discovery.reconcile_mcp_servers_with_config()
                failed = discovery.mcp_servers_not_connected()
            if callable(refresh := getattr(self.runner, "_mcp_reload_refresh_cached_agents", None)):
                refresh(False, None)
            return {
                "ok": not failed, "message": "", "failed": list(failed),
                **{k: list(v) for k, v in (result or {}).items() if isinstance(v, (list, tuple, set))},
            }

        return await asyncio.to_thread(_sync_reload)


_NATIVE_ENGINE_FACTORY: Optional[Callable[..., AgentEngine]] = None


def register_native_engine_factory(factory: Optional[Callable[..., AgentEngine]]) -> None:
    global _NATIVE_ENGINE_FACTORY
    _NATIVE_ENGINE_FACTORY = factory


def build_engine(adapter: Any = None, *, runner: Any = None, engine_name: Optional[EngineName] = None) -> AgentEngine:
    selected = engine_name or active_engine_name()
    if selected == "native":
        if _NATIVE_ENGINE_FACTORY is None:
            raise NotImplementedError("MUSE_ENGINE=native requested before native MuseEngine runtime is registered.")
        return _NATIVE_ENGINE_FACTORY(adapter=adapter)
    return HermesEngineBridge(adapter=adapter, runner=runner)


__all__ = ["AgentEngine", "EngineName", "HermesEngineBridge", "build_engine", "register_native_engine_factory"]
