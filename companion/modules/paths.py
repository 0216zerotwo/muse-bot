"""Where the companion keeps its own data: ``<HERMES_HOME>/companion``."""

from __future__ import annotations

from pathlib import Path


def get_webui_dir() -> Path:
    """Same name as nanobot's helper so ported modules keep their code unchanged."""
    from hermes_constants import get_hermes_home

    root = Path(get_hermes_home()) / "companion"
    root.mkdir(parents=True, exist_ok=True)
    return root
