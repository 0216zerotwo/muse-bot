"""Sign in with a subscription instead of an API key: ChatGPT (Plus / Pro, through the Codex
route) and xAI (SuperGrok / X Premium+). Models then run on the plan's allowance, not on a key.

Both use the OAuth device flow Hermes itself uses (``hermes auth add openai-codex`` /
``xai-oauth``), driven from the app instead of a terminal: the server asks for a short code, the
app shows it with the sign-in page, the user opens the page on the phone and enters the code, and a
background thread waits for the approval, swaps it for tokens and saves them in Hermes's auth store
(``set_active=False``: which model answers stays the 模型 page's choice). Hermes refreshes the
tokens from then on.

When this machine is already signed in to ChatGPT through the Codex CLI / app
(``~/.codex/auth.json``, as nanobot used it), that sign-in can simply be imported instead. The two
then share one rotating refresh token, so when the Codex CLI refreshes it Hermes's copy stops
working; ``keep_signed_in`` notices that and takes the CLI's new pair again (a sign-in with a code
of its own has no such tie and is left alone).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Login kind -> the Hermes provider its models run on.
PROVIDERS = {"chatgpt": "openai-codex", "grok": "xai-oauth"}
_CODEX_ISSUER = "https://auth.openai.com"
_LOCK = threading.Lock()
_logins: dict[str, dict[str, Any]] = {}  # login id -> its state, while it is in progress (and a bit after)


def _sources_path():
    from .paths import get_webui_dir

    return get_webui_dir() / "logins.json"


def _source(kind: str) -> Optional[str]:
    """How this plan was signed in here: "codex-cli" (imported) or "code" (signed in with a code)."""
    try:
        return json.loads(_sources_path().read_text(encoding="utf-8")).get(kind)
    except (OSError, ValueError, AttributeError):
        return None


def _remember_source(kind: str, source: str) -> None:
    from .fs_atomic import atomic_write_json

    with _LOCK:
        path = _sources_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data[kind] = source
        atomic_write_json(path, data, indent=None, mode=0o600)


def signed_in(kind: str) -> bool:
    try:
        if kind == "chatgpt":
            from hermes_cli.auth import get_codex_auth_status

            return bool(get_codex_auth_status().get("logged_in"))
        if kind == "grok":
            from hermes_cli.auth import _read_xai_oauth_tokens

            _read_xai_oauth_tokens()
            return True
    except Exception:
        return False
    return False


def _set(login_id: str, **changes: Any) -> None:
    with _LOCK:
        if login_id in _logins:
            _logins[login_id].update(changes)


def _codex_start() -> tuple[dict[str, Any], Any]:
    from hermes_cli.auth_codex import (
        _codex_exchange_authorization_code, _codex_poll_authorization_code, _codex_request_device_code,
        _save_codex_tokens)
    from hermes_cli.auth_constants import CODEX_OAUTH_CLIENT_ID

    device = _codex_request_device_code(_CODEX_ISSUER, CODEX_OAUTH_CLIENT_ID)

    def finish() -> None:
        from hermes_cli.auth import _utc_now_z

        code = _codex_poll_authorization_code(
            _CODEX_ISSUER, device_auth_id=device["device_auth_id"], user_code=device["user_code"],
            poll_interval=device["interval"])
        tokens = _codex_exchange_authorization_code(_CODEX_ISSUER, CODEX_OAUTH_CLIENT_ID, code)
        _save_codex_tokens({"access_token": tokens.get("access_token", ""), "refresh_token": tokens.get("refresh_token", "")},
                           _utc_now_z(), set_active=False)
        _remember_source("chatgpt", "code")

    return {"user_code": device["user_code"], "url": f"{_CODEX_ISSUER}/codex/device", "expires_in": 15 * 60}, finish


def _grok_start() -> tuple[dict[str, Any], Any]:
    import httpx
    from hermes_cli.auth import _utc_now_z, _xai_oauth_discovery
    from hermes_cli.auth_xai import (
        _save_xai_oauth_tokens, _token_pair, _xai_oauth_poll_device_token, _xai_oauth_request_device_code,
        _xai_tokens_from_payload)

    discovery = _xai_oauth_discovery(20.0)
    with httpx.Client(timeout=20.0, headers={"Accept": "application/json"}) as client:
        device = _xai_oauth_request_device_code(client)

    def finish() -> None:
        with httpx.Client(timeout=20.0, headers={"Accept": "application/json"}) as client:
            payload = _xai_oauth_poll_device_token(
                client, token_endpoint=discovery["token_endpoint"], device_code=str(device["device_code"]),
                expires_in=int(device["expires_in"]), poll_interval=int(device["interval"]))
        access, refresh = _token_pair(payload)
        if not access or not refresh:
            raise RuntimeError("xAI 没有返回完整的登录凭证")
        _save_xai_oauth_tokens(_xai_tokens_from_payload(payload, access, refresh), discovery=discovery,
                               last_refresh=_utc_now_z(), set_active=False)

    url = str(device.get("verification_uri_complete") or device["verification_uri"])
    return {"user_code": str(device["user_code"]), "url": url, "expires_in": int(device["expires_in"])}, finish


def start(kind: str) -> dict[str, Any]:
    """Ask for a sign-in code; the approval is awaited in the background. Returns the login's id,
    the code to enter and the page to enter it on."""
    if kind not in PROVIDERS:
        raise ValueError("不支持这种登录")
    try:
        shown, finish = _codex_start() if kind == "chatgpt" else _grok_start()
    except Exception as exc:
        logger.warning("companion: %s sign-in could not start: %s", kind, exc)
        raise ValueError(f"现在拿不到登录码：{exc}") from None
    login_id = uuid.uuid4().hex[:12]
    with _LOCK:
        for old in [k for k, v in _logins.items() if time.time() - v["started"] > 3600]:
            _logins.pop(old)
        _logins[login_id] = {"kind": kind, "status": "waiting", "error": None, "started": time.time(), **shown}

    def wait() -> None:
        try:
            finish()
            _set(login_id, status="done")
            logger.info("companion: signed in with %s", kind)
        except Exception as exc:
            logger.info("companion: %s sign-in did not complete: %s", kind, exc)
            _set(login_id, status="failed", error=str(exc)[:200])

    threading.Thread(target=wait, name=f"companion-login-{kind}", daemon=True).start()
    return status(login_id)


def status(login_id: str) -> dict[str, Any]:
    with _LOCK:
        login = _logins.get(login_id)
        if login is None:
            raise ValueError("这次登录已经过期，重新开始吧")
        return {"id": login_id, **{k: v for k, v in login.items() if k != "started"}}


def local_login(kind: str) -> bool:
    """Whether this machine already has the plan signed in elsewhere: the Codex CLI / app keeps a
    ChatGPT sign-in in ``~/.codex/auth.json`` (what nanobot read too). Only valid, unexpired ones."""
    if kind != "chatgpt":
        return False
    try:
        from hermes_cli.auth_codex import _import_codex_cli_tokens

        return _import_codex_cli_tokens() is not None
    except Exception:
        return False


def import_local(kind: str) -> None:
    """Use that sign-in here instead of signing in again (copied into Hermes's auth store, which
    refreshes it from then on)."""
    from hermes_cli.auth import _utc_now_z
    from hermes_cli.auth_codex import _import_codex_cli_tokens, _save_codex_tokens

    tokens = _import_codex_cli_tokens() if kind == "chatgpt" else None
    if not tokens:
        raise ValueError("这台电脑上没有可用的 ChatGPT 登录")
    _save_codex_tokens({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]},
                       _utc_now_z(), set_active=False)
    _remember_source(kind, "codex-cli")
    logger.info("companion: imported the ChatGPT sign-in from the Codex CLI")


def keep_signed_in() -> bool:
    """An imported ChatGPT sign-in that stopped working (the Codex CLI refreshed the shared token):
    take the CLI's current one again. True when it was taken. A sign-in made with a code here is
    its own and never replaced."""
    if _source("chatgpt") == "code" or signed_in("chatgpt") or not local_login("chatgpt"):
        return False
    try:
        import_local("chatgpt")
    except Exception as exc:
        logger.warning("companion: could not take the Codex CLI's ChatGPT sign-in again (%s)", exc)
        return False
    logger.warning("companion: the ChatGPT sign-in had stopped working; took the Codex CLI's current one again")
    return True


def models(kind: str) -> list[str]:
    """Model ids the signed-in plan offers (ChatGPT: asked live; Grok: xAI's catalog)."""
    if kind == "chatgpt":
        from hermes_cli.codex_models import DEFAULT_CODEX_MODELS

        try:
            from hermes_cli.auth import resolve_codex_runtime_credentials
            from hermes_cli.codex_models import get_codex_model_ids

            creds = resolve_codex_runtime_credentials()
            return get_codex_model_ids(creds.get("api_key"), creds.get("base_url")) or list(DEFAULT_CODEX_MODELS)
        except Exception:
            return list(DEFAULT_CODEX_MODELS)
    return []
