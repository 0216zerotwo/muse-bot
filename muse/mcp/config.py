"""MCP server configuration loading, ${VAR} expansion, and Windows-safe stdio helpers."""

from __future__ import annotations

import os
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from muse.config_store import read_env_values, read_raw_config

DEFAULT_PLAYWRIGHT_MCP_PKG = "@playwright/mcp@0.0.83"
DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9333"

_VAR_REF_RE = re.compile(r"\$\{([^}]+)\}")
_SAFE_SERVER_RE = re.compile(r"[^A-Za-z0-9_]")
_SECRET_QUERY_RE = re.compile(r"(key|token|secret|password)=[^&\s]+", re.IGNORECASE)


def safe_server_name(name: str) -> str:
    """Normalize an MCP server name into a safe identifier segment."""
    return _SAFE_SERVER_RE.sub("_", str(name or "").strip())


def mcp_tool_name(server: str, tool_name: str) -> str:
    """Build the canonical ``mcp__<safe_server>__<tool_name>`` name."""
    return f"mcp__{safe_server_name(server)}__{tool_name}"


def mcp_toolset_name(server: str) -> str:
    """Build the canonical ``mcp-<name>`` toolset name."""
    return f"mcp-{server}"


def _extract_var_name(raw_ref: str) -> Optional[str]:
    cleaned = raw_ref.strip()
    if cleaned.startswith("env:"):
        cleaned = cleaned[4:].strip()
    elif ":" in cleaned and re.match(r"^[a-z][a-z0-9_-]*:", cleaned):
        return None
    return cleaned or None


def _lookup_env_var(name: str, dotenv_vals: dict[str, str]) -> Optional[str]:
    if name in dotenv_vals and dotenv_vals[name] != "":
        return dotenv_vals[name]
    val = os.environ.get(name)
    if val is not None:
        return val
    return dotenv_vals.get(name)


def expand_string_vars(text: str, dotenv_vals: dict[str, str]) -> str:
    """Expand ``${VAR}`` and ``${env:VAR}`` references in a string."""
    if not text or "${" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        var = _extract_var_name(match.group(1))
        if not var:
            return match.group(0)
        val = _lookup_env_var(var, dotenv_vals)
        return val if val is not None else match.group(0)

    return _VAR_REF_RE.sub(_replace, text)


def expand_url_vars(url: str, dotenv_vals: dict[str, str]) -> str:
    """Expand ``${VAR}`` in URL path and query params (handling raw and %-encoded refs)."""
    if not url:
        return url
    parsed = urllib.parse.urlsplit(url)
    scheme = expand_string_vars(parsed.scheme, dotenv_vals)
    netloc = expand_string_vars(parsed.netloc, dotenv_vals)
    path = expand_string_vars(parsed.path, dotenv_vals)
    fragment = expand_string_vars(parsed.fragment, dotenv_vals)
    if not parsed.query:
        return urllib.parse.urlunsplit((scheme, netloc, path, "", fragment))
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    expanded_pairs = [
        (expand_string_vars(k, dotenv_vals), expand_string_vars(v, dotenv_vals))
        for k, v in pairs
    ]
    query = urllib.parse.urlencode(expanded_pairs)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def expand_server_config(
    cfg: dict[str, Any],
    dotenv_vals: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Return a deep copy of an MCP server config with ``${VAR}`` expanded everywhere."""
    env_map = dotenv_vals if dotenv_vals is not None else read_env_values()
    out: dict[str, Any] = dict(cfg)

    if isinstance(cfg.get("command"), str):
        out["command"] = expand_string_vars(cfg["command"], env_map)
    if isinstance(cfg.get("cwd"), str):
        out["cwd"] = expand_string_vars(cfg["cwd"], env_map)
    if isinstance(cfg.get("args"), list):
        out["args"] = [
            expand_string_vars(str(arg), env_map) for arg in cfg["args"]
        ]
    if isinstance(cfg.get("env"), dict):
        out["env"] = {
            str(k): expand_string_vars(str(v), env_map)
            for k, v in cfg["env"].items()
            if v is not None
        }
    if isinstance(cfg.get("headers"), dict):
        out["headers"] = {
            str(k): expand_string_vars(str(v), env_map)
            for k, v in cfg["headers"].items()
            if v is not None
        }
    if isinstance(cfg.get("url"), str):
        out["url"] = expand_url_vars(cfg["url"], env_map)
    return out


def is_docker_environment() -> bool:
    """Detect whether the current process is running inside a Docker container."""
    flag = os.environ.get("MUSE_IN_DOCKER", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return Path("/.dockerenv").is_file()


def build_browser_preset(
    *,
    in_docker: Optional[bool] = None,
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
) -> dict[str, Any]:
    """Build the default ``mcp_servers.browser`` preset for local desktop or Docker."""
    docker = is_docker_environment() if in_docker is None else bool(in_docker)
    endpoint = str(cdp_endpoint or DEFAULT_CDP_ENDPOINT).strip()
    if docker:
        return {
            "command": "playwright-mcp",
            "args": ["--no-webmcp", "--cdp-endpoint", endpoint],
            "connect_timeout": 60,
        }
    return {
        "command": "npx",
        "args": ["-y", DEFAULT_PLAYWRIGHT_MCP_PKG, "--no-webmcp", "--cdp-endpoint", endpoint],
        "connect_timeout": 90,
    }


def load_mcp_servers(home: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load and expand all ``mcp_servers`` entries from ``config.yaml`` and ``.env``."""
    raw = read_raw_config(home)
    servers = raw.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    dotenv_vals = read_env_values(home)
    result: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if isinstance(name, str) and isinstance(cfg, dict):
            result[name] = expand_server_config(cfg, dotenv_vals)
    return result


def build_stdio_env(server_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Merge process env with server env and enforce UTF-8 stdio encoding on Windows."""
    merged = dict(os.environ)
    if isinstance(server_env, dict):
        for k, v in server_env.items():
            if v is not None:
                merged[str(k)] = str(v)
    merged["PYTHONUTF8"] = "1"
    merged["PYTHONIOENCODING"] = "utf-8"
    return merged


def resolve_stdio_command(command: str, merged_env: Optional[dict[str, str]] = None) -> str:
    """Resolve ``command`` via ``shutil.which`` (critical for ``npx.cmd`` on Win32)."""
    cmd = str(command or "").strip()
    if not cmd:
        return cmd
    path_env = merged_env.get("PATH") if isinstance(merged_env, dict) else None
    resolved = shutil.which(cmd, path=path_env)
    return resolved or cmd


def detect_transport(cfg: dict[str, Any]) -> str:
    """Normalize configured MCP transport to ``stdio``, ``sse``, or ``streamable_http``."""
    raw = str(cfg.get("transport") or cfg.get("type") or "").strip().lower()
    if raw in ("stdio",):
        return "stdio"
    if raw in ("sse",):
        return "sse"
    if raw in ("streamable_http", "streamablehttp", "streamable-http", "http"):
        return "streamable_http"
    if cfg.get("url"):
        return "streamable_http"
    return "stdio"


def filter_tool_names(tool_names: list[str], cfg: dict[str, Any]) -> list[str]:
    """Apply optional ``tools.include`` / ``tools.exclude`` rules from server config."""
    tools_cfg = cfg.get("tools")
    if not isinstance(tools_cfg, dict):
        return list(tool_names)
    include = tools_cfg.get("include")
    exclude = tools_cfg.get("exclude")
    out = list(tool_names)
    if isinstance(include, list) and include and "*" not in include:
        allowed = {str(x) for x in include}
        out = [name for name in out if name in allowed]
    if isinstance(exclude, list) and exclude:
        blocked = {str(x) for x in exclude}
        out = [name for name in out if name not in blocked]
    return out


def redact_error(message: str, limit: int = 300) -> str:
    """Mask query-string credentials in MCP connection errors."""
    return _SECRET_QUERY_RE.sub(r"\1=***", str(message or ""))[:limit]


__all__ = [
    "DEFAULT_CDP_ENDPOINT",
    "DEFAULT_PLAYWRIGHT_MCP_PKG",
    "build_browser_preset",
    "build_stdio_env",
    "detect_transport",
    "expand_server_config",
    "expand_string_vars",
    "expand_url_vars",
    "filter_tool_names",
    "is_docker_environment",
    "load_mcp_servers",
    "mcp_tool_name",
    "mcp_toolset_name",
    "redact_error",
    "resolve_stdio_command",
    "safe_server_name",
]
