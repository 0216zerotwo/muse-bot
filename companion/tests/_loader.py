"""Loads the companion's modules for a test, cut off from real user data.

Each call gets fresh copies of the modules in a package of its own whose ``paths`` module points at
a new temporary folder, and ``MUSE_HOME`` defaults to an isolated temporary directory so nothing a
test writes reaches ``muse-home`` or ``hermes-home``. Runs 100% standalone without any ``hermes/``
source tree.
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
from pathlib import Path
from typing import Any

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


def _setup_test_paths() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    os.environ.setdefault("MUSE_HOME", str(temp_dir("companion-muse-home-")))


_setup_test_paths()


def ensure_hermes_stubs() -> None:
    """Compatibility helper retained as a standalone path-setup no-op (zero Hermes dependencies)."""
    _setup_test_paths()


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
    assert spec is not None and spec.loader is not None
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    return store.ChatStore(folder / "chats")
