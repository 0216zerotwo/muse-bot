"""Loads the companion's modules for a test, cut off from the real data.

Each call gets fresh copies of the modules in a package of its own whose ``paths`` module points at
a new temporary folder, so nothing a test writes reaches ``hermes-home`` (``HERMES_HOME`` alone
does not isolate a script: Hermes resolves its home in more than one way). Anything that would
reach the scheduled jobs, the network or a model is stubbed by the test itself.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import types
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(*names: str) -> tuple[Path, list[Any]]:
    folder = Path(tempfile.mkdtemp(prefix="companion-test-"))
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
