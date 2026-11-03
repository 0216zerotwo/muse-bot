"""Keep the model's shell and file tools away from the files that hold secrets, and from changing
the companion's own setup (Hermes's config, gateway restarts, webhooks, plugins).

API keys live in ``<HERMES_HOME>/.env`` (and ``auth.json``), the password vault in ``vault/``, the
media signing key in ``companion/media.key``, and the shared browser's sign-ins (cookies) in its
profile. Hermes already masks secret-looking text in tool output; this is the layer in front of
it: a terminal command, a code run or a file tool that names one of these files is blocked before
it runs, so the key never reaches the model, however it would be printed.

A command can still reach a file without naming it (globs, a script written earlier); the output
masking stays the fallback for those.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Tools that touch the local disk or run code.
_LOCAL_TOOLS = re.compile(
    r"^(?:terminal|execute_code|process_manage|run_cli_app|exec|exec_session|read_file|write_file|edit_file"
    r"|apply_patch|patch|search_files|list_dir|find_files|grep|rg)$|file"
)
# What a call may not name. ``.env.example`` (no secrets) stays readable.
_PROTECTED = re.compile(
    r"(?:^|[\s/\\'\"`=:(])\.env(?!\.example)(?:\.[a-z]+)?(?=$|[\s'\"`;|&)>,])"
    r"|auth\.json|media\.key|browser-profile|hermes-home[/\\]+vault|hermes_home[^\s]*[/\\]+vault"
    r"|[/\\]vault[/\\]|[/\\](?:Login Data|Cookies)\b",
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
    "这一步碰到了存放密钥或登录信息的文件（.env、auth.json、vault、浏览器登录数据），为了安全不能读取或修改。"
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


def check(tool_name: str, args: Any) -> Optional[str]:
    """The block message when a shell or file call names a secret file, else ``None``."""
    if not _LOCAL_TOOLS.search(tool_name or ""):
        return None
    texts = _strings(args)
    if any(_PROTECTED.search(text) for text in texts):
        return MESSAGE
    # Reading its own config is fine; running these commands or writing those files is not.
    if tool_name not in _READS and any(_SELF_CHANGE.search(text) for text in texts):
        return SELF_CHANGE_MESSAGE
    return None
