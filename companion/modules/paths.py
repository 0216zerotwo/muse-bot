"""Where the companion keeps its own data: ``<MUSE_HOME>/companion``."""

from __future__ import annotations

from pathlib import Path


def get_webui_dir() -> Path:
    """Same name as nanobot's helper so ported modules keep their code unchanged."""
    from muse.paths import get_muse_home

    root = get_muse_home() / "companion"
    root.mkdir(parents=True, exist_ok=True)
    return root
