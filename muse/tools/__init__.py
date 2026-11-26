"""Muse built-in tools package (`ToolRegistry` and all core toolset registrations)."""

from __future__ import annotations

from muse.tools.clarify_tool import register_clarify_tools
from muse.tools.files import register_file_tools
from muse.tools.knowledge import register_knowledge_tools
from muse.tools.memory_store import (
    ENTRY_DELIMITER,
    MemoryStore,
    get_memory_dir,
    load_on_disk_store,
    register_memory_tools,
)
from muse.tools.registry import (
    CORE_TOOLSETS,
    MAX_DIRECT_TOOLS,
    TOOL_SEARCH_SCHEMA,
    ToolEntry,
    ToolRegistry,
)
from muse.tools.session_search import register_session_search_tools
from muse.tools.terminal import cleanup_all_processes, register_terminal_tools, sanitize_subprocess_env
from muse.tools.tool_search import register_tool_search
from muse.tools.vault_cdp import (
    VaultError,
    VaultItemMeta,
    VaultStore,
    get_vault_store,
    normalize_origin,
    normalize_otp_secret,
    register_vault_tools,
    scrub_secret_from_text,
    totp_now,
)
from muse.tools.web import is_safe_public_url, register_web_tools


def register_skill_tools(registry: ToolRegistry) -> None:
    from muse.skills.tools import register_skill_tools as _reg_skills

    _reg_skills(registry)


def register_media_tools(registry: ToolRegistry) -> None:
    from muse.media import register_media_tools as _reg_media

    _reg_media(registry)


def register_cron_tools(registry: ToolRegistry) -> None:
    from muse.cron import register_cron_tools as _reg_cron

    _reg_cron(registry)


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register all native built-in toolsets onto `registry`."""
    register_terminal_tools(registry)
    register_file_tools(registry)
    register_web_tools(registry)
    register_memory_tools(registry)
    register_knowledge_tools(registry)
    register_skill_tools(registry)
    register_vault_tools(registry)
    register_session_search_tools(registry)
    register_clarify_tools(registry)
    register_media_tools(registry)
    register_cron_tools(registry)
    register_tool_search(registry)
    return registry


def create_default_tool_registry(*, max_direct_tools: int = MAX_DIRECT_TOOLS) -> ToolRegistry:
    """Create a `ToolRegistry` pre-populated with all Muse built-in tools."""
    reg = ToolRegistry(max_direct_tools=max_direct_tools)
    return register_builtin_tools(reg)


__all__ = [
    "CORE_TOOLSETS",
    "ENTRY_DELIMITER",
    "MAX_DIRECT_TOOLS",
    "MemoryStore",
    "TOOL_SEARCH_SCHEMA",
    "ToolEntry",
    "ToolRegistry",
    "VaultError",
    "VaultItemMeta",
    "VaultStore",
    "cleanup_all_processes",
    "create_default_tool_registry",
    "get_memory_dir",
    "get_vault_store",
    "is_safe_public_url",
    "load_on_disk_store",
    "normalize_origin",
    "normalize_otp_secret",
    "register_builtin_tools",
    "register_clarify_tools",
    "register_cron_tools",
    "register_file_tools",
    "register_knowledge_tools",
    "register_media_tools",
    "register_memory_tools",
    "register_session_search_tools",
    "register_skill_tools",
    "register_terminal_tools",
    "register_tool_search",
    "register_vault_tools",
    "register_web_tools",
    "sanitize_subprocess_env",
    "scrub_secret_from_text",
    "totp_now",
]
