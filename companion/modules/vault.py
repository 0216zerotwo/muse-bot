"""安全凭证: site logins and API keys the user keeps for the companion, filled in by the user only.

- Site logins go into Hermes's own encrypted vault (``agent.vault_store``): the password is encrypted
  at rest and the model never sees it — it fills a login page through Hermes's ``browser_vault``
  tool, which resolves the password server-side and only ever shows the model an opaque handle.
- API keys go into ``<HERMES_HOME>/.env`` under the variable the user names (as the 模型 page does
  with a provider's key), so a connected service or tool reads it from there; the key itself never
  enters a chat. ``vault-keys.json`` remembers which variables were added here, by name only.

The user adds them in 设置 → 安全凭证, or on a card the companion puts in the chat when it needs one
(``credential_request``); what is typed there goes straight to the server, not into the chat.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

from .paths import get_webui_dir

TOOLSET = "companion"
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_FORBIDDEN_ENV = frozenset({
    "PATH", "PATHEXT", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHONINSPECT",
    "PYTHONWARNINGS", "PYTHONNOUSERSITE", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_OPTIONS",
    "NODE_EXTRA_CA_CERTS", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
    "COMSPEC", "SHELL", "SYSTEMROOT", "WINDIR", "HERMES_HOME", "MUSE_HOME", "MUSE_ENGINE",
    "BROWSER", "GIT_ASKPASS", "SSH_AUTH_SOCK",
})
_FORBIDDEN_ENV_PREFIXES = ("COMPANION_", "HERMES_", "MUSE_", "MUSEBOT_", "PYTHON", "LD_", "DYLD_", "GIT_SSH", "SSH_")
_LOCK = threading.Lock()


def is_allowed_env_name(env: str) -> bool:
    name = (env or "").strip().upper()
    if not _ENV.match(name):
        return False
    if name in _FORBIDDEN_ENV or name.startswith(_FORBIDDEN_ENV_PREFIXES):
        return False
    return True


def _keys_path():
    return get_webui_dir() / "vault-keys.json"


def _read_keys() -> list[dict[str, Any]]:
    try:
        rows = json.loads(_keys_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("env")] if isinstance(rows, list) else []


def _write_keys(rows: list[dict[str, Any]]) -> None:
    path = _keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _store():
    from agent.vault_store import get_vault_store

    return get_vault_store()


def payload() -> dict[str, Any]:
    logins = [m.to_dict() for m in _store().list_items() if m.kind == "login"]
    keys = [{**k, "set": bool(os.environ.get(k["env"]))} for k in _read_keys()]
    return {"logins": [{k: m.get(k) for k in ("id", "label", "origin", "identifier", "created_at")} for m in logins],
            "keys": keys}


def add_login(label: str, origin: str, identifier: str, password: str) -> dict[str, Any]:
    from agent.vault_store import VaultError, normalize_origin

    identifier, password = str(identifier or "").strip(), str(password or "")
    if not identifier or not password:
        raise ValueError("账号和密码都要填")
    url = str(origin or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        norm = normalize_origin(url)
    except Exception:
        raise ValueError("网站地址不对，比如 https://github.com") from None
    kind = "email" if "@" in identifier else "phone" if re.fullmatch(r"\+?\d[\d\s-]{5,}", identifier) else "username"
    try:
        _store().add_item("login", (label or "").strip() or norm.split("://", 1)[-1], {
            "identifier_type": kind, "identifier": identifier, "password": password}, origin=norm)
    except VaultError as exc:
        raise ValueError(str(exc)) from None
    return payload()


def add_key(label: str, env: str, value: str) -> dict[str, Any]:
    from muse.config_store import save_env_value

    env, value = str(env or "").strip().upper(), str(value or "").strip()
    if not _ENV.match(env):
        raise ValueError("变量名用大写字母、数字和下划线，比如 GITHUB_TOKEN")
    if not is_allowed_env_name(env):
        raise ValueError("这个环境变量属于系统或运行配置，不能作为服务密钥保存")
    if not value:
        raise ValueError("密钥是空的")
    save_env_value(env, value)
    os.environ[env] = value  # tools started from now on read it without a restart
    with _LOCK:
        rows = [r for r in _read_keys() if r["env"] != env]
        rows.append({"env": env, "label": (label or "").strip()[:30] or env, "created_at": int(time.time() * 1000)})
        _write_keys(rows)
    return payload()


def remove(kind: str, item: str) -> dict[str, Any]:
    if kind == "login":
        _store().remove_item(item)
    elif kind == "key":
        from muse.config_store import remove_env_value

        with _LOCK:
            rows = _read_keys()
            if any(r["env"] == item for r in rows):
                remove_env_value(item)
                os.environ.pop(item, None)
                _write_keys([r for r in rows if r["env"] != item])
    else:
        raise ValueError("kind 只能是 login 或 key")
    return payload()


# ── the companion's tool: ask the user for one, on a card ────────────────────────────────────────

def _handle_request(args: dict[str, Any], **_: Any) -> str:
    kind = str(args.get("kind") or "")
    service = " ".join(str(args.get("service") or "").split())[:30]
    reason = " ".join(str(args.get("reason") or "").split())[:40]
    if kind not in ("login", "api_key") or not service:
        return json.dumps({"success": False, "error": "kind 是 login 或 api_key，service 要写是哪个服务"}, ensure_ascii=False)
    card: dict[str, Any] = {"type": "credential", "kind": kind, "service": service}
    if kind == "login":
        origin = str(args.get("origin") or "").strip()
        if origin:
            card["origin"] = origin[:200]
        known = [m for m in _store().list_items() if m.kind == "login" and origin and (m.origin or "") in origin]
        if known:
            return json.dumps({"success": True, "exists": True,
                               "note": f"已经存过这个网站的登录（{known[0].identifier}），用 browser_vault 的 fill 填进登录页"},
                              ensure_ascii=False)
    else:
        env = str(args.get("env") or "").strip().upper()
        if not _ENV.match(env):
            return json.dumps({"success": False, "error": "env 要写这个密钥用的环境变量名，比如 GITHUB_TOKEN"}, ensure_ascii=False)
        if not is_allowed_env_name(env):
            return json.dumps({"success": False, "error": "这个环境变量属于系统或运行配置，不能在这里请求"}, ensure_ascii=False)
        if os.environ.get(env):
            return json.dumps({"success": True, "exists": True, "note": f"{env} 已经设好了，直接用"}, ensure_ascii=False)
        card["env"] = env
    if reason:
        card["reason"] = reason
    return json.dumps({
        "success": True,
        "note": ("把下面这张卡片原样放进回复，一句话说明为什么需要它，然后停下等用户填。不要让用户把密码或密钥直接发在聊天里。"
                 "用户填好后会告诉你，再接着做（登录用 browser_vault 的 fill）。"),
        "card": "```card\n" + json.dumps(card, ensure_ascii=False) + "\n```",
    }, ensure_ascii=False)


_SCHEMA = {
    "name": "credential_request",
    "description": ("需要用户的网站登录（账号密码）或某个服务的 API 密钥时用：在聊天里给用户一张安全卡片让用户自己填，"
                    "填的内容直接加密保存，不经过聊天、你也看不到。绝不要让用户把密码、密钥发在聊天里。"),
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["login", "api_key"]},
        "service": {"type": "string", "description": "哪个服务，比如 GitHub、12306"},
        "origin": {"type": "string", "description": "（登录）登录页的网址"},
        "env": {"type": "string", "description": "（API 密钥）要存进的环境变量名，比如 GITHUB_TOKEN"},
        "reason": {"type": "string", "description": "为什么需要，十几个字"},
    }, "required": ["kind", "service"]},
}


def register_tools(ctx: Any) -> None:
    ctx.register_tool(name="credential_request", toolset=TOOLSET, schema=_SCHEMA, handler=_handle_request,
                      description=_SCHEMA["description"], emoji="🔐")
