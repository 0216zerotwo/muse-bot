"""连接: the services the companion can use (MCP servers), ported from nanobot's preset API.

The catalog (``mcp_presets.json``, unchanged from nanobot) says how each service
is reached and which values the user must enter. Connecting writes a Hermes
``mcp_servers`` entry; what the user typed (keys, passwords, addresses) goes to
Hermes's ``.env`` as ``COMPANION_MCP_<SERVICE>_<FIELD>`` and the config only
holds ``${…}`` references, which Hermes fills in when it starts the server.
Every change is applied live: Hermes reconnects its MCP servers and refreshes
the tool lists of open chats.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import urllib.parse
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import get_webui_dir

BROWSER_PRESET = "browser"
_LOCK = threading.Lock()
_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_last_checks: dict[str, dict[str, Any]] = {}


class ConnectionError_(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def catalog() -> tuple[dict[str, Any], ...]:
    data = json.loads((Path(__file__).with_name("mcp_presets.json")).read_text(encoding="utf-8"))
    return tuple(data)


def _preset(name: str) -> Optional[dict[str, Any]]:
    return next((p for p in catalog() if p["name"] == name), None)


# ---------- Hermes config ----------


def _servers() -> dict[str, Any]:
    from muse.config_store import read_raw_config

    servers = read_raw_config().get("mcp_servers")
    return dict(servers) if isinstance(servers, dict) else {}


def _save_servers(servers: dict[str, Any]) -> None:
    from muse.config_store import read_raw_config, save_config

    config = read_raw_config()
    config["mcp_servers"] = servers
    save_config(config)


def _env_name(service: str, field: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", f"COMPANION_MCP_{service}_{field}".upper())


def _resolve(value: Optional[str]) -> Optional[str]:
    """A config value with a whole ``${VAR}`` reference looked up (None when unset)."""
    if not value:
        return None
    from muse.config_store import get_env_value

    ref = _REF.match(value)
    return get_env_value(ref.group(1)) or None if ref else value


# ---------- fields ----------


def _url_with_param(url: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != key]
    query.append((key, value))
    # Keep ${VAR} readable in the URL so Hermes can fill it in.
    encoded = urllib.parse.urlencode(query, safe="${}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, parsed.fragment))


def _arg_value(args: list[str], flag: str) -> Optional[str]:
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(f"{flag}="):
            return item[len(flag) + 1:]
    return None


def _with_arg_value(args: list[str], flag: str, value: str) -> list[str]:
    out, skip = [], False
    for item in args:
        if skip:
            skip = False
            continue
        if item == flag:
            skip = True
            continue
        if item.startswith(f"{flag}="):
            continue
        out.append(item)
    return out + [flag, value]


def _field_raw(field: dict[str, Any], cfg: Optional[dict[str, Any]]) -> Optional[str]:
    if not cfg:
        return None
    kind, target = field["target"]
    if kind == "env":
        return (cfg.get("env") or {}).get(target)
    if kind == "header":
        value = (cfg.get("headers") or {}).get(target)
        return value[len(field.get("value_prefix", "")):] if value else None
    if kind == "url":
        return cfg.get("url")
    if kind == "arg":
        return _arg_value(list(cfg.get("args") or []), target)
    if kind == "url_param" and cfg.get("url"):
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(cfg["url"]).query).get(target)
        return values[0] if values else None
    return None


def _field_configured(field: dict[str, Any], cfg: Optional[dict[str, Any]]) -> bool:
    if _resolve(_field_raw(field, cfg)):
        return True
    from muse.config_store import get_env_value

    return bool(field.get("env_var") and get_env_value(str(field["env_var"])))


def _field_payload(field: dict[str, Any], cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": field["name"], "label": field["label"], "secret": field.get("secret", True),
        "required": field.get("required", True), "configured": _field_configured(field, cfg),
        "placeholder": field.get("placeholder", ""), "env_var": field.get("env_var"),
    }


# ---------- building a server entry ----------


def _managed_cwd(name: str) -> Path:
    return get_webui_dir() / "mcp" / name


def browser_launch_args() -> list[str]:
    """``--browser`` for the installed Edge/Chrome; ``--headless`` on a server without a screen."""
    args: list[str] = []
    if sys.platform == "win32":
        candidates = (("msedge", (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                  r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")),
                      ("chrome", (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                  r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")))
        found = next((c for c, paths in candidates if any(os.path.exists(os.path.expandvars(p)) for p in paths)), None)
    elif sys.platform == "darwin":
        found = next((c for c, path in (("chrome", "/Applications/Google Chrome.app"),
                                        ("msedge", "/Applications/Microsoft Edge.app")) if os.path.exists(path)), None)
    else:
        found = next((c for c, names in (("chrome", ("google-chrome", "google-chrome-stable")),
                                         ("msedge", ("microsoft-edge",))) if any(shutil.which(n) for n in names)), None)
    if found:
        args += ["--browser", found]
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        args.append("--headless")
    return args


def _materialize(preset: dict[str, Any], values: dict[str, str], existing: Optional[dict[str, Any]],
                 save_secret: Callable[[str, str], None]) -> dict[str, Any]:
    server = preset.get("server")
    if not server or not preset.get("install_supported"):
        raise ConnectionError_(f"{preset['display_name']} 暂时还不能连接", 409)
    if existing:
        cfg = json.loads(json.dumps(existing))
    else:
        cfg = {}
        if server.get("command"):
            cfg["command"] = server["command"]
            cfg["args"] = list(server.get("args") or [])
            if server.get("env"):
                cfg["env"] = dict(server["env"])
        if server.get("url"):
            cfg["url"] = server["url"]
            if server.get("headers"):
                cfg["headers"] = dict(server["headers"])
            if server.get("type") == "sse":
                cfg["transport"] = "sse"
        if server.get("tool_timeout"):
            cfg["timeout"] = server["tool_timeout"]
        enabled_tools = server.get("enabled_tools") or server.get("enabledTools")
        if enabled_tools and enabled_tools != ["*"]:
            cfg["tools"] = {"include": list(enabled_tools)}
    for field in preset.get("fields") or []:
        provided = (values.get(field["name"]) or "").strip()
        ref = None
        if provided:
            var = _env_name(preset["name"], field["name"])
            save_secret(var, provided)
            ref = f"${{{var}}}"
        elif _resolve(_field_raw(field, cfg)):
            continue  # already set
        elif field.get("env_var"):
            from muse.config_store import get_env_value

            if get_env_value(str(field["env_var"])):
                ref = f"${{{field['env_var']}}}"
        if ref is None:
            if field.get("required", True):
                raise ConnectionError_(f"还缺「{field['label']}」")
            continue
        kind, target = field["target"]
        if kind == "env":
            cfg.setdefault("env", {})[target] = ref
        elif kind == "header":
            cfg.setdefault("headers", {})[target] = field.get("value_prefix", "") + ref
        elif kind == "arg":
            cfg["args"] = _with_arg_value(list(cfg.get("args") or []), target, ref)
        elif kind == "url_param":
            cfg["url"] = _url_with_param(cfg.get("url", ""), target, ref)
        elif kind == "url":
            cfg["url"] = ref
    if preset["name"] == BROWSER_PRESET:
        args = list(cfg.get("args") or [])
        launch = browser_launch_args()
        if "--browser" not in args and "--browser" in launch:
            i = launch.index("--browser")
            args += launch[i:i + 2]
        if "--headless" in launch and "--headless" not in args:
            args.append("--headless")
        cfg["args"] = args
    if cfg.get("command"):
        cwd = _managed_cwd(preset["name"])
        cwd.mkdir(parents=True, exist_ok=True)
        cfg["cwd"] = str(cwd)
        # npx/uvx may first fetch or unpack the package: give the first start room.
        cfg.setdefault("connect_timeout", 120)
    cfg.pop("enabled", None)
    return cfg


# ---------- listing ----------


def _command_available(command: str) -> bool:
    return bool(command) and (bool(shutil.which(command)) or Path(command).expanduser().is_file())


def _status(preset: dict[str, Any], cfg: Optional[dict[str, Any]]) -> str:
    if cfg is None:
        return "not_installed" if preset.get("install_supported") else "coming_soon"
    if any(f.get("required", True) and not _field_configured(f, cfg) for f in preset.get("fields") or []):
        return "missing_credentials"
    if cfg.get("command") and not _command_available(cfg["command"]):
        return "missing_dependency"
    return "configured"


def _summary(cfg: Optional[dict[str, Any]]) -> str:
    if not cfg:
        return ""
    if cfg.get("command"):
        return " ".join([cfg["command"], *list(cfg.get("args") or [])[:2]]).strip()
    if cfg.get("url"):
        parsed = urllib.parse.urlsplit(cfg["url"])
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return ""


def _runtime() -> dict[str, dict[str, Any]]:
    try:
        from muse.mcp import get_mcp_manager

        rows = get_mcp_manager().get_status()
        return {row["name"]: row for row in rows if isinstance(row, dict) and row.get("name")}
    except Exception:
        return {}


def _registered_tools(name: str) -> list[str]:
    try:
        from muse.mcp import get_mcp_manager, mcp_toolset_name, safe_server_name

        prefix = f"mcp__{safe_server_name(name)}__"
        names = get_mcp_manager().get_tool_names_for_toolset(mcp_toolset_name(name))
        return sorted(t[len(prefix):] for t in names if t.startswith(prefix))
    except Exception:
        return []


def presets_payload(last_action: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    servers = _servers()
    runtime = _runtime()
    items = []
    for preset in catalog():
        cfg = servers.get(preset["name"])
        cfg = cfg if isinstance(cfg, dict) else None
        status = _status(preset, cfg)
        configured = cfg is not None and status != "missing_credentials"
        enabled = bool(cfg) and cfg.get("enabled", True) is not False
        run = runtime.get(preset["name"], {})
        check = _last_checks.get(preset["name"], {})
        run_status = {"connected": "connected", "connecting": "connecting", "failed": "failed",
                      "lazy": "connected"}.get(str(run.get("status") or ""))
        include = ((cfg or {}).get("tools") or {}).get("include") if cfg else None
        items.append({
            "name": preset["name"], "display_name": preset["display_name"], "category": preset["category"],
            "description": preset["description"], "docs_url": preset.get("docs_url", ""),
            "transport": preset["transport"], "auth": None, "requires": preset.get("requires", ""),
            "note": preset.get("note", ""), "install_supported": preset.get("install_supported", False),
            "installed": cfg is not None, "configured": configured, "enabled": enabled,
            "available": configured and enabled and (not cfg.get("command") or _command_available(cfg["command"])),
            "status": status, "runtime_status": run_status if enabled else None,
            "logo_url": f"https://www.google.com/s2/favicons?domain={preset['brand_domain']}&sz=64" if preset.get("brand_domain") else None,
            "brand_color": preset.get("brand_color"),
            "required_fields": [_field_payload(f, cfg) for f in preset.get("fields") or []],
            "connection_summary": _summary(cfg),
            "tool_names": check.get("tool_names") or _registered_tools(preset["name"]) or None,
            "tool_count": check.get("tool_count"), "checked_at": check.get("checked_at"),
            "error": check.get("error") or (run.get("error") if run_status == "failed" else None),
            "enabled_tools": list(include) if isinstance(include, list) else ["*"],
            "source": "preset",
        })
    known = {p["name"] for p in catalog()}
    for name, cfg in servers.items():
        if name in known or not isinstance(cfg, dict):
            continue
        enabled = cfg.get("enabled", True) is not False
        run = runtime.get(name, {})
        check = _last_checks.get(name, {})
        run_status = {"connected": "connected", "connecting": "connecting", "failed": "failed",
                      "lazy": "connected"}.get(str(run.get("status") or ""))
        include = (cfg.get("tools") or {}).get("include")
        items.append({
            "name": name, "display_name": name, "category": "custom", "description": "你自己添加的服务",
            "docs_url": "", "transport": "streamableHttp" if cfg.get("url") else "stdio", "auth": None,
            "requires": "", "note": "", "install_supported": True, "installed": True, "configured": True,
            "enabled": enabled, "available": enabled, "status": "configured",
            "runtime_status": run_status if enabled else None, "logo_url": None, "brand_color": None,
            "required_fields": [], "connection_summary": _summary(cfg),
            "tool_names": check.get("tool_names") or _registered_tools(name) or None,
            "tool_count": check.get("tool_count"), "checked_at": check.get("checked_at"),
            "error": check.get("error") or (run.get("error") if run_status == "failed" else None),
            "enabled_tools": list(include) if isinstance(include, list) else ["*"], "source": "custom",
        })
    payload: dict[str, Any] = {"presets": items, "installed_count": sum(1 for i in items if i["installed"])}
    if last_action:
        payload["last_action"] = last_action
    return payload


# ---------- actions ----------


def _message(action: str, preset: dict[str, Any], ok: bool = True) -> dict[str, Any]:
    words = {"enable": "已连接", "disable": "已暂停", "remove": "已断开", "tools": "已更新可用功能"}
    return {"ok": ok, "message": f"{preset['display_name']} {words.get(action, '已更新')}"}


def run_action(action: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
    """``enable`` (connect, or resume a paused one), ``disable`` (pause), ``remove``, ``tools``."""
    from muse.config_store import get_env_value, remove_env_value, save_env_value

    preset = _preset(name)
    with _LOCK:
        servers = _servers()
        existing = servers.get(name) if isinstance(servers.get(name), dict) else None
        if preset is None:
            if existing is None:
                raise ConnectionError_("没有这个服务", 404)
            # One the user added themselves: no catalog entry, nothing to fill in again.
            preset = {"name": name, "display_name": name, "fields": [], "server": None}
            if action == "enable":
                servers[name] = {k: v for k, v in existing.items() if k != "enabled"}
                _save_servers(servers)
                return _message(action, preset)
        if action == "enable":
            text_values = {k: str(v) for k, v in values.items() if isinstance(v, (str, int, float)) and k != "name"}
            servers[name] = _materialize(preset, text_values, existing, save_env_value)
        elif action == "disable":
            if existing is None:
                raise ConnectionError_("还没有连接", 404)
            servers[name] = {**existing, "enabled": False}
        elif action == "remove":
            servers.pop(name, None)
            if _preset(name) is None:
                remove_custom_secrets(name)
            for field in preset.get("fields") or []:
                var = _env_name(name, field["name"])
                if get_env_value(var) is not None:
                    remove_env_value(var)
            _last_checks.pop(name, None)
        elif action == "tools":
            if existing is None:
                raise ConnectionError_("还没有连接", 404)
            tools = values.get("enabled_tools")
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools) or not tools:
                raise ConnectionError_("enabled_tools must be a non-empty list")
            updated = {k: v for k, v in existing.items() if k != "tools"}
            if tools != ["*"]:
                updated["tools"] = {"include": tools}
            servers[name] = updated
        else:
            raise ConnectionError_(f"unknown action: {action}", 404)
        _save_servers(servers)
    return _message(action, preset)


_SERVER_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def add_custom(values: dict[str, Any]) -> dict[str, Any]:
    """A service the user adds themselves: a web address (optionally with a key, sent as a
    Bearer token) or a command that starts it (optionally with KEY=VALUE settings)."""
    from muse.config_store import save_env_value

    name = str(values.get("name") or "").strip().lower()
    kind = values.get("kind")
    if not _SERVER_NAME.match(name):
        raise ConnectionError_("名称用小写英文字母、数字和横线，以字母开头")
    if name in {p["name"] for p in catalog()}:
        raise ConnectionError_("这个名字和已有的服务重了，换一个")
    cfg: dict[str, Any]
    if kind == "url":
        url = str(values.get("url") or "").strip()
        if not re.match(r"^https?://\S+$", url):
            raise ConnectionError_("网址要以 http:// 或 https:// 开头")
        cfg = {"url": url}
        token = str(values.get("token") or "").strip()
        if token:
            var = _env_name(name, "token")
            save_env_value(var, token)
            cfg["headers"] = {"Authorization": f"Bearer ${{{var}}}"}
    elif kind == "command":
        import shlex

        try:
            parts = shlex.split(str(values.get("command") or ""), posix=sys.platform != "win32")
        except ValueError:
            raise ConnectionError_("启动命令写得不对") from None
        if not parts:
            raise ConnectionError_("请填写启动命令")
        cfg = {"command": parts[0], "args": parts[1:]}
        env: dict[str, str] = {}
        for line in str(values.get("env") or "").splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ConnectionError_(f"设置要写成 名字=值：{line.strip()[:30]}")
            var = _env_name(name, key)
            save_env_value(var, value.strip())
            env[key] = f"${{{var}}}"
        if env:
            cfg["env"] = env
        cwd = _managed_cwd(name)
        cwd.mkdir(parents=True, exist_ok=True)
        cfg["cwd"] = str(cwd)
        cfg["connect_timeout"] = 120
    else:
        raise ConnectionError_("请选择网址或命令")
    with _LOCK:
        servers = _servers()
        if name in servers:
            raise ConnectionError_("已经有叫这个名字的服务了")
        servers[name] = cfg
        _save_servers(servers)
    return {"ok": True, "message": f"{name} 已添加"}


def remove_custom_secrets(name: str) -> None:
    from muse.config_store import read_env_values, remove_env_value

    prefix = _env_name(name, "")
    seen = {k for k in os.environ if k.startswith(prefix)} | {
        k for k in read_env_values() if k.startswith(prefix)
    }
    for var in sorted(seen):
        remove_env_value(var)


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine from synchronous callers without nesting event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def apply_live_async(engine: Any = None, runner: Any = None) -> dict[str, Any]:
    """Bring running MCP servers in line with config on the active asyncio event loop."""
    del runner
    if engine is not None and callable(getattr(engine, "reload_mcp", None)):
        return await engine.reload_mcp()
    from muse.mcp import get_mcp_manager

    return await get_mcp_manager().reload_mcp()


def apply_live(runner: Any = None, *, engine: Any = None) -> dict[str, Any]:
    """Sync wrapper around :func:`apply_live_async` for legacy callers."""
    return _run_coro_sync(apply_live_async(engine=engine, runner=runner))


async def test_server_async(name: str) -> dict[str, Any]:
    """Connect once on the active loop, list all offered tools, and disconnect cleanly."""
    from muse.mcp import get_mcp_manager, load_mcp_servers, redact_error

    preset = _preset(name) or {"display_name": name}
    cfg = load_mcp_servers().get(name)
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if not cfg:
        raise ConnectionError_("还没有连接", 404)

    error: Optional[str] = None
    try:
        import importlib

        importlib.import_module("mcp.client.stdio")
    except Exception as exc:
        mod = sys.modules.get("mcp")
        where = getattr(mod, "__path__", None) or getattr(mod, "__file__", None)
        site = [p for p in sys.path if "site-packages" in p][:3]
        error = (
            f"MCP support failed to load: {type(exc).__name__}: {exc} "
            f"(mcp={where!r}, python={sys.executable}, path={site})"
        )[:600]
        _last_checks[name] = {
            "tool_names": [],
            "tool_count": 0,
            "checked_at": checked_at,
            "error": error,
        }
        return {
            "ok": False,
            "message": f"{preset['display_name']} 连不上",
            "tool_count": 0,
            "tool_names": [],
            "checked_at": checked_at,
            "error": error,
        }

    tools: list[str] = []
    try:
        tools = await asyncio.wait_for(
            get_mcp_manager().probe_server(name, cfg),
            timeout=float(cfg.get("connect_timeout") or 60) + 15.0,
        )
    except Exception as exc:
        error = redact_error(str(exc) or type(exc).__name__)[:300]

    _last_checks[name] = {
        "tool_names": tools,
        "tool_count": len(tools),
        "checked_at": checked_at,
        "error": error,
    }
    ok = error is None
    return {
        "ok": ok,
        "message": f"{preset['display_name']} {'可以用' if ok else '连不上'}",
        "tool_count": len(tools),
        "tool_names": tools,
        "checked_at": checked_at,
        "error": error,
    }


def test_server(name: str) -> dict[str, Any]:
    """Sync wrapper around :func:`test_server_async`."""
    return _run_coro_sync(test_server_async(name))


# ---------- 按需连接: the companion asks for a service mid-task ----------

def _find_preset(service: str) -> Optional[dict[str, Any]]:
    wanted = " ".join(str(service or "").split()).lower()
    return _preset(wanted) or next(
        (p for p in catalog() if wanted in (str(p.get("display_name") or "").lower(), str(p.get("brand_domain") or "").lower())),
        None)


def _handle_connect_request(args: dict[str, Any], **_: Any) -> str:
    preset = _find_preset(str(args.get("service") or ""))
    if preset is None or preset["name"] == BROWSER_PRESET:
        names = [f"{p['name']}（{p['display_name']}）" for p in catalog() if p.get("install_supported")][:80]
        return json.dumps({"success": False, "error": "没有这个服务", "available": names}, ensure_ascii=False)
    if _status(preset, _servers().get(preset["name"])) == "configured":
        return json.dumps({"success": True, "connected": True,
                           "note": f"{preset['display_name']} 已经连上了，直接用它的工具"}, ensure_ascii=False)
    reason = " ".join(str(args.get("reason") or "").split())[:40]
    card = json.dumps({"type": "connect", "service": preset["name"], **({"reason": reason} if reason else {})},
                      ensure_ascii=False)
    return json.dumps({
        "success": True, "connected": False,
        "note": ("把下面这张卡片原样放进你的回复（一句话说明为什么需要它），然后停下等用户连接；"
                 "用户连好后会告诉你，再接着做。"),
        "card": f"```card\n{card}\n```",
    }, ensure_ascii=False)


_CONNECT_SCHEMA = {
    "name": "connect_request",
    "description": ("要用的服务（GitHub、Notion、邮箱、日历、Google Workspace 等）还没连上时用：在聊天里给用户一张"
                    "「连接」卡片，用户点一下就能连好，连好后接着做。不要让用户自己去设置里找。"),
    "parameters": {"type": "object", "properties": {
        "service": {"type": "string", "description": "服务名，比如 github、notion、email、google-workspace"},
        "reason": {"type": "string", "description": "为什么要连它，十几个字"},
    }, "required": ["service"]},
}


def register_tools(ctx: Any) -> None:
    ctx.register_tool(name="connect_request", toolset="companion", schema=_CONNECT_SCHEMA,
                      handler=_handle_connect_request, description=_CONNECT_SCHEMA["description"], emoji="🔌")
