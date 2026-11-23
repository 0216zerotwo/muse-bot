"""Keep the model's shell, file, browser, and media paths away from files that hold secrets, and
from changing the companion's own setup (Hermes's config, gateway restarts, webhooks, plugins).

API keys live in ``<HERMES_HOME>/.env`` (and ``auth.json``), the password vault in ``vault/``, the
media signing key in ``companion/media.key``, paired devices/webhooks/push keys in ``companion/``,
and the shared browser's sign-ins (cookies) in its profile. Hermes already masks secret-looking
text in tool output; this layer sits in front of tool execution and file staging (``MEDIA:``,
widgets, creations, avatar references) so protected files cannot be read, uploaded, or sent out.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any, Optional

# Tools that touch the local disk or run code.
_LOCAL_TOOLS = re.compile(
    r"^(?:terminal|execute_code|process|process_manage|run_cli_app|exec|exec_session|read_file|write_file|edit_file"
    r"|apply_patch|patch|search_files|list_dir|find_files|grep|rg|send_file|send_document)$|file"
)
# Browser tools (Playwright MCP ``mcp__browser__*`` / ``mcp_browser_*`` and Hermes ``browser_*``).
_BROWSER_TOOLS = re.compile(r"^(?:browser_|mcp__?.+?__?browser_)", re.IGNORECASE)
_FILE_URL = re.compile(r"(?:^|[\s'\"(=])file://", re.IGNORECASE)

# What a call or staged path may not name. ``.env.example`` / ``.env.sample`` / ``.env.template`` stay readable.
_PROTECTED = re.compile(
    r"(?:^|[\s/\\'\"`=:(])\.env(?!(?:\.example|\.sample|\.template)\b)(?:\.[a-z0-9_-]+)?(?=$|[\s'\"`;|&)>,])"
    r"|(?:^|[\s/\\'\"`=:()])(?:auth\.json|media\.key|vapid_private\.pem|devices\.json|triggers\.json"
    r"|subscriptions\.json|vault-keys\.json|approval-grants\.json)(?=$|[\s/\\'\"`;|&)>,])"
    r"|browser-profile|hermes-home[/\\]+vault|hermes_home[^\s]*[/\\]+vault"
    r"|[/\\]vault(?:[/\\]|$)|[/\\](?:Login Data|Cookies|Web Data|Local State)\b"
    r"|[/\\]\.codex[/\\]+auth\.json\b"
    r"|[/\\]\.ssh[/\\]+(?:id_[a-z0-9_]+|config|authorized_keys|known_hosts)\b"
    r"|[/\\]\.aws[/\\]+credentials\b|[/\\]\.gnupg[/\\]|(?:^|[/\\])_?\.netrc\b",
    re.IGNORECASE,
)

# Changing the companion's own setup: Hermes's config, its gateway (a restart cuts every chat
# off mid-turn), webhooks, plugins. The user changes these in the app's settings.
_SELF_CHANGE = re.compile(
    r"\bhermes(?:\.exe)?\s+(?:config\s+(?:set|edit|reset|unset)|gateway\s+(?:stop|restart|start|install|uninstall|run)"
    r"|webhook\s+(?:subscribe|unsubscribe|remove|delete)|plugins?\s+(?:install|enable|disable|remove|update)|update\b|setup\b)"
    r"|hermes-home[/\\]+config\.yaml|hermes-home[/\\]+plugins[/\\]|webhook_subscriptions\.json",
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


def _hermes_home() -> Optional[Path]:
    with contextlib.suppress(Exception):
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).resolve()
    return None


def is_secret_path(path_or_text: str | Path) -> bool:
    """True when ``path_or_text`` names or resolves to a protected secret file or directory."""
    raw = str(path_or_text or "").strip()
    if not raw:
        return False
    if _PROTECTED.search(raw):
        return True
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = Path(raw).expanduser().resolve()
        if _PROTECTED.search(str(resolved)) or _PROTECTED.search(resolved.as_posix()):
            return True
        home = _hermes_home()
        if home is not None:
            protected_roots = (
                home / ".env",
                home / "auth.json",
                home / "vault",
                home / "companion" / "media.key",
                home / "companion" / "devices.json",
                home / "companion" / "triggers.json",
                home / "companion" / "vault-keys.json",
                home / "companion" / "approval-grants.json",
                home / "companion" / "push",
                home / "companion" / "mcp" / "browser" / "browser-profile",
            )
            for target in protected_roots:
                if resolved == target or target in resolved.parents:
                    return True
    return False


def _targets_hermes_config(texts: list[str]) -> bool:
    home = _hermes_home()
    if home is None:
        return False
    config_file = home / "config.yaml"
    plugins_dir = home / "plugins"
    for text in texts:
        if "config.yaml" not in text and "plugins" not in text:
            continue
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            resolved = Path(text.strip()).expanduser().resolve()
            if resolved == config_file or plugins_dir == resolved or plugins_dir in resolved.parents:
                return True
    return False


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
    if any(_PROTECTED.search(text) or is_secret_path(text) for text in texts):
        return MESSAGE
    # Reading its own config is fine; running these commands or writing those files is not.
    if is_local and name not in _READS and (any(_SELF_CHANGE.search(text) for text in texts) or _targets_hermes_config(texts)):
        return SELF_CHANGE_MESSAGE
    return None
