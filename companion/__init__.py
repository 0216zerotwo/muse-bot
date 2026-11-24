"""Companion platform for Muse / Hermes Agent: the phone app's server, bridged to the agent."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent, _HERE.parent.parent / "companion-lib"):
    if (_cand / "muse" / "__init__.py").is_file() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "register":
        from .adapter import register

        return register
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["register"]
