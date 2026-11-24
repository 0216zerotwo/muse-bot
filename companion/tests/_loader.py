"""Loads the companion's modules for a test, cut off from the real data.

Each call gets fresh copies of the modules in a package of its own whose ``paths`` module points at
a new temporary folder, and ``MUSE_HOME`` defaults to an isolated temporary directory so nothing a
test writes reaches ``muse-home`` or ``hermes-home``. Anything that would reach the scheduled jobs,
the network or a model is stubbed by the test itself.
"""

from __future__ import annotations

import atexit
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
_TEMP_DIRS: list[Path] = []


def _cleanup_temp_dirs() -> None:
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


atexit.register(_cleanup_temp_dirs)


def temp_dir(prefix: str = "companion-test-") -> Path:
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(folder)
    return folder


def find_hermes_dir() -> Optional[Path]:
    """Locate a Hermes source tree in standard checkouts or via ``HERMES_SRC_DIR``."""
    candidates: list[Path] = [REPO_ROOT / "hermes"]
    env_dir = os.environ.get("HERMES_SRC_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def _setup_test_paths() -> None:
    hermes_dir = find_hermes_dir()
    if hermes_dir is not None and str(hermes_dir) not in sys.path:
        sys.path.insert(0, str(hermes_dir))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    os.environ.setdefault("MUSE_HOME", str(temp_dir("companion-muse-home-")))


_setup_test_paths()


def ensure_hermes_stubs() -> None:
    """Ensure ``gateway.*`` is importable even in standalone CI runs where a full Hermes checkout
    is not present alongside ``companion/``."""
    _setup_test_paths()
    try:
        import gateway.config  # noqa: F401
        import gateway.platforms.base  # noqa: F401
        import gateway.platforms.event  # noqa: F401
        return
    except ImportError:
        pass

    class Platform(str, Enum):
        COMPANION = "companion"

        @classmethod
        def _missing_(cls, value: object) -> "Platform":
            obj = str.__new__(cls, str(value))
            obj._name_ = str(value).upper()
            obj._value_ = str(value)
            return obj

    @dataclass
    class HomeChannel:
        platform: Any
        chat_id: str
        name: str = ""

    @dataclass
    class SendResult:
        success: bool = True
        message_id: Optional[str] = None
        error: Optional[str] = None

    @dataclass
    class ExecApprovalPrompt:
        chat_id: str
        session_key: str
        text: str
        actions: list[tuple[str, str, str]]
        command: str
        description: str
        smart_denied: bool = False
        metadata: Optional[dict[str, Any]] = None

        @property
        def choices(self) -> list[str]:
            return [choice for _, choice, _ in self.actions]

    class MessageType(str, Enum):
        TEXT = "text"
        PHOTO = "photo"
        DOCUMENT = "document"

    @dataclass
    class MessageSource:
        platform: str = "companion"
        chat_id: str = ""
        chat_name: str = ""
        chat_type: str = "dm"
        user_id: str = ""
        user_name: str = ""

    @dataclass
    class MessageEvent:
        text: str
        message_type: Any
        source: MessageSource
        user_id: str = ""
        user_name: str = ""
        message_id: str = ""
        timestamp: Any = None
        media_urls: list[str] = field(default_factory=list)
        media_types: list[str] = field(default_factory=list)
        media_text_inlined: list[Optional[bool]] = field(default_factory=list)

    class BasePlatformAdapter:
        def __init__(self, config: Any = None, platform: Any = None, **kwargs: Any) -> None:
            self.config = config
            self.platform = platform
            self.gateway_runner = None

        def build_source(self, **kwargs: Any) -> MessageSource:
            return MessageSource(**kwargs)

        def _mark_connected(self) -> None:
            pass

        def _mark_disconnected(self) -> None:
            pass

        def _set_fatal_error(self, code: str, message: str, *, retryable: bool = True) -> None:
            pass

        async def handle_message(self, event: MessageEvent) -> None:
            pass

    gw = sys.modules.setdefault("gateway", types.ModuleType("gateway"))
    gw_cfg = types.ModuleType("gateway.config")
    gw_cfg.HomeChannel = HomeChannel
    gw_cfg.Platform = Platform
    gw_cfg.persist_home_channel = lambda home: None
    sys.modules["gateway.config"] = gw_cfg
    gw.config = gw_cfg

    gw_plat = sys.modules.setdefault("gateway.platforms", types.ModuleType("gateway.platforms"))
    gw_base = types.ModuleType("gateway.platforms.base")
    gw_base.BasePlatformAdapter = BasePlatformAdapter
    gw_base.ExecApprovalPrompt = ExecApprovalPrompt
    gw_base.SendResult = SendResult
    sys.modules["gateway.platforms.base"] = gw_base
    gw_plat.base = gw_base

    gw_event = types.ModuleType("gateway.platforms.event")
    gw_event.MessageEvent = MessageEvent
    gw_event.MessageType = MessageType
    sys.modules["gateway.platforms.event"] = gw_event
    gw_plat.event = gw_event


def load(*names: str) -> tuple[Path, list[Any]]:
    folder = temp_dir("companion-test-")
    package = f"companion_test_{uuid.uuid4().hex[:8]}"
    module = types.ModuleType(package)
    module.__path__ = [str(ROOT / "modules")]
    sys.modules[package] = module
    paths = types.ModuleType(f"{package}.paths")
    paths.get_webui_dir = lambda: folder
    sys.modules[f"{package}.paths"] = paths
    return folder, [importlib.import_module(f"{package}.{name}") for name in names]


def chat_store(folder: Path) -> Any:
    """The real chat store (companion/store.py), keeping its chats in ``folder``."""
    spec = importlib.util.spec_from_file_location(f"companion_store_{uuid.uuid4().hex[:8]}", ROOT / "store.py")
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    return store.ChatStore(folder / "chats")
