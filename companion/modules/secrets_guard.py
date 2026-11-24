"""Keep the model's shell, file, browser, and media paths away from files that hold secrets, and
from changing the companion's own setup (config, service restarts, webhooks, plugins).

Covers both ``muse-home`` and legacy ``hermes-home``, ``muse.db`` and ``state.db``, ``.env*``
(preserving ``.env.example`` / ``.sample`` / ``.template``), ``auth.json``, ``vault/``,
``media.key``, ``devices.json``, ``triggers.json``, ``subscriptions.json``, ``vault-keys.json``,
``approval-grants.json``, ``vapid_private.pem``, ``~/.codex/auth.json``, ``~/.ssh``, ``file://``,
and self-modifying ``musebot`` / ``hermes`` commands.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any, Optional

_LOCAL_TOOLS = re.compile(
    r"^(?:terminal|execute_code|process|process_manage|run_cli_app|exec|exec_session|read_file|write_file|edit_file"
    r"|apply_patch|patch|search_files|list_dir|find_files|grep|rg|send_file|send_document)$|file"
)
_BROWSER_TOOLS = re.compile(r"^(?:browser_|mcp__?.+?__?browser_)", re.IGNORECASE)
_FILE_URL = re.compile(r"(?:^|[\s'\"(=])file://", re.IGNORECASE)
_HTTP_URL = re.compile(r"https?://[^\s'\"`()<>]+", re.IGNORECASE)
_URL_SCHEMES = ("http://", "https://", "mailto:", "data:", "javascript:", "about:", "blob:")
_WRITE_ONLY_TOOLS = frozenset({"write_file", "edit_file"})
_WRITE_PATH_KEYS = frozenset({"path", "file_path", "filepath", "file", "filename", "target_file", "destination", "dest"})
_BROWSER_LOCAL_KEYS = frozenset({"path", "paths", "file", "files", "file_path", "filepath", "code", "script"})

_PROTECTED = re.compile(
    r"(?:^|[\s/\\'\"`=:(,@])\.env(?!(?:\.example|\.sample|\.template)\b)(?:\.[a-z0-9_-]+)?(?=$|[\s'\"`;|&)>,])"
    r"|(?:^|[\s/\\'\"`=:(@])(?:auth\.json|media\.key|vapid_private\.pem|devices\.json|triggers\.json"
    r"|subscriptions\.json|vault-keys\.json|approval-grants\.json"
    r"|(?:muse\.db|state\.db)(?:-(?:wal|shm|journal))?)(?=$|[\s'\"`;|&)>,])"
    r"|browser-profile|(?:muse-home|hermes-home)[/\\]+vault|(?:muse_home|hermes_home)[^\s]*[/\\]+vault"
    r"|[/\\]vault(?:[/\\]|$)|[/\\](?:Login Data|Cookies|Web Data|Local State)\b"
    r"|[/\\]\.codex[/\\]+auth\.json\b"
    r"|(?:^|[\s'\"`=:(])~[/\\]\.ssh(?:[/\\]|$|[\s'\"`;|&)>,])"
    r"|[/\\]\.ssh(?:[/\\]+(?:id_[a-z0-9_]+|config|authorized_keys|known_hosts)\b|[/\\]|$)"
    r"|[/\\]\.aws[/\\]+credentials\b|[/\\]\.gnupg(?:[/\\]|$)|(?:^|[/\\])_?\.netrc\b",
    re.IGNORECASE,
)

_SELF_CHANGE = re.compile(
    r"\bhermes(?:\.exe)?\s+(?:config\s+(?:set|edit|reset|unset)|gateway\s+(?:stop|restart|start|install|uninstall|run)"
    r"|webhook\s+(?:subscribe|unsubscribe|remove|delete)|plugins?\s+(?:install|enable|disable|remove|update)|update\b|setup\b)"
    r"|\bmusebot(?:\.exe)?\s+(?:serve\b|setup\b|pack\b|doctor\s+--clean-legacy|config\s+(?:set|edit|reset|unset)"
    r"|gateway\s+(?:stop|restart|start|install|uninstall|run)|restart\b|stop\b|update\b|install\b|uninstall\b)"
    r"|(?:muse-home|hermes-home)[/\\]+config\.yaml|(?:muse-home|hermes-home)[/\\]+plugins[/\\]|webhook_subscriptions\.json",
    re.IGNORECASE,
)
_READS = frozenset({"read_file", "search_files", "list_dir", "find_files", "grep", "rg"})

SELF_CHANGE_MESSAGE = (
    "这一步会改动你自己运行的程序（配置、服务重启、webhook、插件），不能自己做。"
    "要在事情发生时自动去做，用 trigger_add；真的需要改设置，告诉用户在 App 的设置里怎么改。"
)

MESSAGE = (
    "这一步碰到了存放密钥或登录信息的文件（.env、auth.json、vault、配对凭证、浏览器登录数据或私钥），为了安全不能读取或修改。"
    "需要用到某个服务时，直接调用已经配置好的工具；需要换密钥或配置时，请用户自己在设置里改。"
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _candidate_homes() -> list[Path]:
    homes: list[Path] = []
    with contextlib.suppress(Exception):
        from muse.paths import get_muse_home, get_repo_root

        homes.append(get_muse_home().resolve())
        repo = get_repo_root()
        for folder in ("muse-home", "hermes-home"):
            cand = (repo / folder).resolve()
            if cand not in homes:
                homes.append(cand)
    return homes


def _without_http_urls(text: str) -> str:
    return _HTTP_URL.sub(" ", text) if ("http://" in text.lower() or "https://" in text.lower()) else text


def is_secret_path(path_or_text: str | Path) -> bool:
    """True when ``path_or_text`` names or resolves to a protected secret file or directory."""
    raw = str(path_or_text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith(_URL_SCHEMES):
        return False
    if lower.startswith("file://"):
        return True
    if _PROTECTED.search(_without_http_urls(raw)):
        return True
    if "\n" in raw or "\r" in raw or len(raw) > 1024:
        return False
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = Path(raw).expanduser().resolve()
        if _PROTECTED.search(str(resolved)) or _PROTECTED.search(resolved.as_posix()):
            return True
        for home in _candidate_homes():
            protected_roots = (
                home / ".env",
                home / "muse.db",
                home / "muse.db-wal",
                home / "muse.db-shm",
                home / "state.db",
                home / "state.db-wal",
                home / "state.db-shm",
                home / "auth.json",
                home / "vault",
                home / "companion" / "media.key",
                home / "companion" / "devices.json",
                home / "companion" / "triggers.json",
                home / "companion" / "vault-keys.json",
                home / "companion" / "approval-grants.json",
                home / "companion" / "approvals",
                home / "companion" / "push",
                home / "companion" / "mcp" / "browser" / "browser-profile",
            )
            for target in protected_roots:
                if resolved == target or target in resolved.parents:
                    return True
    return False


def _targets_protected_config(texts: list[str]) -> bool:
    homes = _candidate_homes()
    if not homes:
        return False
    for text in texts:
        raw = text.strip()
        if not raw or "\n" in raw or "\r" in raw or len(raw) > 1024:
            continue
        if "config.yaml" not in raw and "plugins" not in raw:
            continue
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            resolved = Path(raw).expanduser().resolve()
            for home in homes:
                config_file = home / "config.yaml"
                plugins_dir = home / "plugins"
                if resolved == config_file or plugins_dir == resolved or plugins_dir in resolved.parents:
                    return True
    return False


def _browser_local_texts(name: str, args: Any, all_texts: list[str]) -> list[str]:
    low = name.lower()
    if low == "browser_exec" or any(tok in low for tok in ("upload", "drop", "file")):
        candidates = all_texts
    elif isinstance(args, dict):
        candidates = _strings({k: v for k, v in args.items() if str(k).lower() in _BROWSER_LOCAL_KEYS})
    else:
        return []
    return [t for t in candidates if not t.lstrip().lower().startswith(_URL_SCHEMES)]


def check(tool_name: str, args: Any) -> Optional[str]:
    """The block message when a shell, file, or browser call touches a secret file, else ``None``."""
    name = tool_name or ""
    is_local = bool(_LOCAL_TOOLS.search(name))
    is_browser = bool(_BROWSER_TOOLS.search(name))
    if not (is_local or is_browser):
        return None
    texts = _strings(args)
    if is_browser and any(_FILE_URL.search(text) for text in texts):
        return MESSAGE
    if is_browser and not is_local:
        local_texts = _browser_local_texts(name, args, texts)
        if any(_PROTECTED.search(_without_http_urls(text)) or is_secret_path(text) for text in local_texts):
            return MESSAGE
        return None
    target_texts = texts
    if name in _WRITE_ONLY_TOOLS and isinstance(args, dict):
        extracted = _strings({k: v for k, v in args.items() if str(k).lower() in _WRITE_PATH_KEYS})
        if extracted:
            target_texts = extracted
    if any(_PROTECTED.search(_without_http_urls(text)) or is_secret_path(text) for text in target_texts):
        return MESSAGE
    if is_local and name not in _READS and (
        any(_SELF_CHANGE.search(_without_http_urls(text)) for text in target_texts)
        or _targets_protected_config(target_texts)
    ):
        return SELF_CHANGE_MESSAGE
    return None
