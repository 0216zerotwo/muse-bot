"""Companion web/mobile server and product modules for Muse Bot."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent, _HERE.parent.parent / "companion-lib"):
    if (_cand / "muse" / "__init__.py").is_file() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))
        break

__all__: list[str] = []
