"""Side-effect-free path resolution and ContextVar home isolation for Muse."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Literal

EngineName = Literal["native"]
_UNSET = object()
_MUSE_HOME_CTX: ContextVar[Path | object] = ContextVar("_MUSE_HOME_CTX", default=_UNSET)


def _expand_home_path(raw: Path | str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(raw).strip())))


def get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def has_muse_home_override() -> bool:
    return _MUSE_HOME_CTX.get() is not _UNSET


def bind_muse_home(path: Path | str | None) -> Token:
    if path is None or (isinstance(path, str) and not path.strip()):
        return _MUSE_HOME_CTX.set(_UNSET)
    return _MUSE_HOME_CTX.set(_expand_home_path(path))


def reset_muse_home(token: Token) -> None:
    _MUSE_HOME_CTX.reset(token)


@contextmanager
def muse_home_override(path: Path | str) -> Iterator[Path]:
    token = bind_muse_home(path)
    try:
        yield get_muse_home()
    finally:
        reset_muse_home(token)


def get_muse_home() -> Path:
    """Resolve active Muse home directory with NO filesystem write side effects."""
    ctx_val = _MUSE_HOME_CTX.get()
    if isinstance(ctx_val, Path):
        return ctx_val
    for env_key in ("MUSE_HOME", "HERMES_HOME"):
        if env_val := os.environ.get(env_key, "").strip():
            return _expand_home_path(env_val)
    repo = get_repo_root()
    muse_dir, hermes_dir = repo / "muse-home", repo / "hermes-home"
    return muse_dir if muse_dir.exists() else (hermes_dir if hermes_dir.exists() else muse_dir)


def ensure_muse_home() -> Path:
    home = get_muse_home()
    created = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    if created and os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(home, 0o700)
    return home


def get_muse_db_path(home: Path | None = None) -> Path:
    return (_expand_home_path(home) if home is not None else get_muse_home()) / "muse.db"


def get_legacy_state_db_path(home: Path | None = None) -> Path:
    return (_expand_home_path(home) if home is not None else get_muse_home()) / "state.db"


def get_config_path(home: Path | None = None) -> Path:
    return (_expand_home_path(home) if home is not None else get_muse_home()) / "config.yaml"


def get_env_path(home: Path | None = None) -> Path:
    return (_expand_home_path(home) if home is not None else get_muse_home()) / ".env"


def active_engine_name() -> EngineName:
    return "native"


__all__ = [
    "EngineName", "_MUSE_HOME_CTX", "active_engine_name", "bind_muse_home", "ensure_muse_home",
    "get_config_path", "get_env_path", "get_legacy_state_db_path", "get_muse_db_path",
    "get_muse_home", "get_repo_root", "has_muse_home_override", "muse_home_override", "reset_muse_home",
]
