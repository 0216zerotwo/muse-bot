"""Subscription OAuth token store and flows for ChatGPT Plus/Pro (openai-codex) and SuperGrok (xai-oauth)."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from muse.llm.types import parse_retry_after_seconds

logger = logging.getLogger(__name__)
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_FORM_JSON_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_USER_AGENT = "muse-bot/2.0"
CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
CODEX_RATE_LIMITED_CODE = "codex_rate_limited"

DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600

DEFAULT_CODEX_MODELS: list[str] = [
    "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4-mini", "gpt-5.4", "gpt-5.3-codex-spark",
]


class AuthError(RuntimeError):
    def __init__(
        self, message: str, *, provider: str = "", code: Optional[str] = None,
        relogin_required: bool = False, retry_after: Optional[float] = None, retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.provider, self.code, self.relogin_required = provider, code, relogin_required
        self.retry_after, self.retryable = retry_after, retryable


def _err(provider: str, msg: str, code: Optional[str] = None, *, relogin: bool = False) -> AuthError:
    return AuthError(msg, provider=provider, code=code, relogin_required=relogin)


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(val: Any) -> str:
    return str(val or "").strip()


def _nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    seg = token.split(".")[1]
    seg += "=" * ((4 - len(seg) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _pkce_code_verifier(length: int = 64) -> str:
    return base64.urlsafe_b64encode(os.urandom(length)).decode("ascii").rstrip("=")[:128]


def _pkce_code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("ascii").rstrip("=")


# ── Auth store (0600 + auth.lock) ────────────────────────────────────────────

_lock_holders: dict[str, threading.local] = {}
_lock_guard = threading.Lock()
_CONTENTION_ERRNOS = frozenset(c for c in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", None)) if c is not None)


def _get_home_dir() -> Path:
    try:
        from muse.paths import get_muse_home
        return Path(get_muse_home())
    except ImportError:
        pass
    for key in ("MUSE_HOME", "HERMES_HOME"):
        val = os.environ.get(key, "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home() / ".muse"


def _auth_file_path(home: Optional[Path] = None) -> Path:
    return (Path(home) if home is not None else _get_home_dir()) / "auth.json"


@contextmanager
def _auth_store_lock(timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS, *, target_path: Optional[Path] = None):
    auth_file = target_path if target_path is not None else _auth_file_path()
    lock_path = auth_file.with_suffix(".lock")
    key = str(auth_file.resolve(strict=False))
    with _lock_guard:
        holder = _lock_holders.setdefault(key, threading.local())
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        fh = None
        if fcntl is not None or msvcrt is not None:
            if msvcrt is not None and (not lock_path.exists() or lock_path.stat().st_size == 0):
                try:
                    lock_path.write_text(" ", encoding="utf-8")
                except (OSError, PermissionError):
                    pass
            fh = stack.enter_context(lock_path.open("r+" if msvcrt is not None else "a+", encoding="utf-8"))
            deadline = time.monotonic() + max(0.05, float(timeout_seconds))
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError, PermissionError) as exc:
                    if not (isinstance(exc, BlockingIOError) or exc.errno in _CONTENTION_ERRNOS):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for auth store lock ({lock_path})") from exc
                    time.sleep(0.05)
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fh is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    else:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _load_auth_store(auth_file: Optional[Path] = None) -> dict[str, Any]:
    target = auth_file if auth_file is not None else _auth_file_path()
    if not target.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except OSError:
        raise
    except Exception:
        try:
            shutil.copy2(target, target.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return {"version": AUTH_STORE_VERSION, "providers": {}}
    if isinstance(raw, dict) and (isinstance(raw.get("providers"), dict) or isinstance(raw.get("credential_pool"), dict)):
        raw.setdefault("providers", {})
        return raw
    return {"version": AUTH_STORE_VERSION, "providers": {}}


def _save_auth_store(auth_store: dict[str, Any], target_path: Optional[Path] = None) -> Path:
    target = target_path if target_path is not None else _auth_file_path()
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(auth_store, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        for i in range(5):
            try:
                os.replace(tmp_path, target)
                break
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.02 * (i + 1))
        if os.name == "posix":
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _provider_state(store: dict[str, Any], provider_id: str) -> Optional[dict[str, Any]]:
    providers = store.get("providers")
    st = providers.get(provider_id) if isinstance(providers, dict) else None
    return dict(st) if isinstance(st, dict) else None


def _store_provider_state(store: dict[str, Any], provider_id: str, state: dict[str, Any], *, set_active: bool = True) -> None:
    providers = store.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = store["providers"] = {}
    providers[provider_id] = state
    if set_active:
        store["active_provider"] = provider_id


# ── Codex headers & Python 3.14 zstd ─────────────────────────────────────────


def is_official_codex_base_url(base_url: str) -> bool:
    try:
        p = urlparse(str(base_url or "").strip())
        path = p.path.rstrip("/")
        return p.scheme == "https" and (p.hostname or "").lower() == "chatgpt.com" and p.port in (None, 443) and (
            path == "/backend-api/codex" or path.startswith("/backend-api/codex/")
        )
    except Exception:
        return False


def codex_account_headers(access_token: str) -> dict[str, str]:
    auth = _decode_jwt_claims(access_token).get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return {}
    out: dict[str, str] = {}
    if isinstance(auth.get("chatgpt_account_id"), str) and auth["chatgpt_account_id"].strip():
        out["ChatGPT-Account-ID"] = auth["chatgpt_account_id"].strip()
    res = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    if isinstance(res, str) and res.strip():
        out["x-openai-internal-codex-residency"] = res.strip()
    return out


def codex_cloudflare_headers(access_token: str, *, base_url: str = DEFAULT_CODEX_BASE_URL) -> dict[str, str]:
    base = (
        {"User-Agent": CODEX_OAUTH_USER_AGENT, "originator": "muse-bot"}
        if is_official_codex_base_url(base_url)
        else {"User-Agent": "codex_cli_rs/0.0.0 (Muse Bot)", "originator": "codex_cli_rs"}
    )
    return {**base, **codex_account_headers(access_token)}


def maybe_zstd_compress(body: bytes, *, base_url: str, path: str = "/responses") -> tuple[bytes, Optional[str]]:
    if not is_official_codex_base_url(base_url) or not path.rstrip("/").endswith("/responses"):
        return body, None
    try:
        from compression import zstd
        return zstd.compress(body), "zstd"
    except Exception:
        return body, None


# ── ChatGPT Plus/Pro (openai-codex) ──────────────────────────────────────────


def _codex_base_url() -> str:
    return (os.getenv("MUSE_CODEX_BASE_URL") or os.getenv("HERMES_CODEX_BASE_URL") or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")


def _codex_access_token_is_expiring(access_token: Any, skew_seconds: int = 0) -> bool:
    exp = _decode_jwt_claims(access_token).get("exp")
    return isinstance(exp, (int, float)) and float(exp) <= time.time() + max(0, int(skew_seconds))


def _read_codex_tokens(*, _lock: bool = True) -> dict[str, Any]:
    if _lock:
        with _auth_store_lock():
            store = _load_auth_store()
    else:
        store = _load_auth_store()
    state = _provider_state(store, "openai-codex")
    tokens = state.get("tokens") if isinstance(state, dict) else None
    if not isinstance(tokens, dict):
        raise _err("openai-codex", "No Codex credentials stored.", "codex_auth_missing", relogin=True)
    if not _nonempty_str(tokens.get("access_token")) or not _nonempty_str(tokens.get("refresh_token")):
        raise _err("openai-codex", "Codex auth is missing tokens.", "codex_auth_missing_tokens", relogin=True)
    return {"tokens": dict(tokens), "last_refresh": state.get("last_refresh")}


def _save_codex_tokens(
    tokens: dict[str, str], last_refresh: Optional[str] = None, label: Optional[str] = None,
    *, set_active: bool = True, write_through: bool = False,
) -> None:
    del write_through
    last_refresh = last_refresh or _utc_now_z()
    with _auth_store_lock():
        store = _load_auth_store()
        state = _provider_state(store, "openai-codex") or {}
        prev_at = _clean((state.get("tokens") or {}).get("access_token")) if isinstance(state.get("tokens"), dict) else ""
        state.update(tokens=dict(tokens), last_refresh=last_refresh, auth_mode="chatgpt")
        if label and str(label).strip():
            state["label"] = str(label).strip()
        _store_provider_state(store, "openai-codex", state, set_active=set_active)
        pool = (store.get("credential_pool") or {}).get("openai-codex") if isinstance(store.get("credential_pool"), dict) else None
        for entry in pool if isinstance(pool, list) else ():
            if isinstance(entry, dict) and (entry.get("source") == "device_code" or (entry.get("source") == "manual:device_code" and prev_at and entry.get("access_token") == prev_at)):
                entry["access_token"] = tokens.get("access_token", "")
                if tokens.get("refresh_token"):
                    entry["refresh_token"] = tokens["refresh_token"]
                entry["last_refresh"] = last_refresh
        _save_auth_store(store)


def _import_codex_cli_tokens() -> Optional[dict[str, str]]:
    """Read ``~/.codex/auth.json`` if valid and unexpired; never writes."""
    codex_home = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    auth_path = Path(codex_home).expanduser() / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        tokens = json.loads(auth_path.read_text(encoding="utf-8-sig")).get("tokens")
        if not (isinstance(tokens, dict) and _nonempty_str(tokens.get("access_token")) and _nonempty_str(tokens.get("refresh_token"))):
            return None
        if _codex_access_token_is_expiring(tokens["access_token"], 0):
            return None
        return {**tokens, "access_token": str(tokens["access_token"]).strip(), "refresh_token": str(tokens["refresh_token"]).strip()}
    except Exception:
        return None


def refresh_codex_oauth_pure(access_token: str, refresh_token: str, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    del access_token
    if not _nonempty_str(refresh_token):
        raise _err("openai-codex", "Codex auth is missing refresh_token.", "codex_auth_missing_refresh_token", relogin=True)
    with httpx.Client(timeout=httpx.Timeout(max(5.0, float(timeout_seconds))), headers={"Accept": "application/json", "User-Agent": CODEX_OAUTH_USER_AGENT}) as client:
        resp = client.post(
            CODEX_OAUTH_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token.strip(), "client_id": CODEX_OAUTH_CLIENT_ID},
        )
    if resp.status_code == 429:
        raise AuthError("Codex provider quota exhausted (429).", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE, retry_after=parse_retry_after_seconds(resp.headers), retryable=True)
    if resp.status_code != 200:
        code, msg = "codex_refresh_failed", f"Codex token refresh failed with status {resp.status_code}."
        try:
            err = resp.json().get("error")
            if isinstance(err, dict):
                code = _clean(err.get("code") or err.get("type")) or code
                msg = f"Codex token refresh failed: {err['message']}" if err.get("message") else msg
            elif _nonempty_str(err):
                code = err.strip()
        except Exception:
            pass
        raise _err("openai-codex", msg, code, relogin=(code in {"invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused"} or resp.status_code in {401, 403}))
    payload = resp.json()
    if not isinstance(payload, dict) or not _nonempty_str(payload.get("access_token")):
        raise _err("openai-codex", "Codex refresh response missing access_token.", "codex_refresh_missing_access_token", relogin=True)
    return {
        "access_token": str(payload["access_token"]).strip(),
        "refresh_token": _clean(payload.get("refresh_token")) or refresh_token.strip(),
        "last_refresh": _utc_now_z(),
    }


def resolve_codex_runtime_credentials(
    *, force_refresh: bool = False, refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS, read_only: bool = False,
) -> dict[str, Any]:
    data: Optional[dict[str, Any]] = None
    read_err: Optional[AuthError] = None
    try:
        data = _read_codex_tokens(_lock=not read_only)
    except AuthError as exc:
        read_err = exc
    if data is None:
        pool = (_load_auth_store().get("credential_pool") or {}).get("openai-codex")
        for entry in pool if isinstance(pool, list) else ():
            if isinstance(entry, dict) and _nonempty_str(entry.get("access_token")):
                return {"provider": "openai-codex", "base_url": _clean(entry.get("base_url")) or _codex_base_url(), "api_key": str(entry["access_token"]).strip(), "source": "credential_pool", "last_refresh": None, "auth_mode": "chatgpt"}
        raise read_err or _err("openai-codex", "No Codex credentials stored.", "codex_auth_missing", relogin=True)

    tokens, last_refresh = dict(data["tokens"]), data.get("last_refresh")
    at = _clean(tokens.get("access_token"))
    needs = lambda tok: (not read_only) and (bool(force_refresh) or (refresh_if_expiring and _codex_access_token_is_expiring(tok, refresh_skew_seconds)))
    if needs(at):
        timeout_s = float(os.getenv("MUSE_CODEX_REFRESH_TIMEOUT_SECONDS", "20") or 20)
        with _auth_store_lock(timeout_seconds=max(AUTH_LOCK_TIMEOUT_SECONDS, timeout_s + 5.0)):
            latest = _read_codex_tokens(_lock=False)
            lt = dict(latest["tokens"])
            lat, lrt = _clean(lt.get("access_token")), _clean(lt.get("refresh_token"))
            if lrt != _clean(tokens.get("refresh_token")) and not _codex_access_token_is_expiring(lat, refresh_skew_seconds):
                tokens, last_refresh = lt, latest.get("last_refresh")
            elif needs(lat):
                try:
                    ref = refresh_codex_oauth_pure(lat, lrt, timeout_seconds=timeout_s)
                    tokens = {**lt, "access_token": ref["access_token"], "refresh_token": ref["refresh_token"]}
                    last_refresh = ref["last_refresh"]
                except AuthError as exc:
                    if not exc.relogin_required:
                        raise
                    imported = _import_codex_cli_tokens()
                    if not imported or _clean(imported.get("refresh_token")) == lrt:
                        raise
                    tokens = {**lt, "access_token": imported["access_token"], "refresh_token": imported["refresh_token"]}
                    last_refresh = _utc_now_z()
                _save_codex_tokens(tokens, last_refresh=last_refresh, set_active=False)
            at = _clean(tokens.get("access_token"))
    return {"provider": "openai-codex", "base_url": _codex_base_url(), "api_key": at, "source": "muse-auth-store", "last_refresh": last_refresh, "auth_mode": "chatgpt"}


def get_codex_auth_status() -> dict[str, Any]:
    try:
        creds = resolve_codex_runtime_credentials(read_only=True)
        tok = _clean(creds.get("api_key"))
        return {
            "provider": "openai-codex",
            "logged_in": bool(tok) and not _codex_access_token_is_expiring(tok, 0),
            "base_url": creds.get("base_url") or _codex_base_url(),
            "source": creds.get("source"),
            "last_refresh": creds.get("last_refresh"),
            "auth_mode": "chatgpt",
        }
    except Exception as exc:
        return {"provider": "openai-codex", "logged_in": False, "error": str(exc), "base_url": _codex_base_url(), "auth_mode": "chatgpt"}


def _codex_request_device_code(issuer: str, client_id: str) -> dict[str, Any]:
    resp = None
    for attempt in range(1, 5):
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(f"{issuer.rstrip('/')}/api/accounts/deviceauth/usercode", json={"client_id": client_id}, headers={"Content-Type": "application/json", "Accept": "application/json"})
        if resp.status_code != 429 or attempt == 4:
            break
        ra = parse_retry_after_seconds(resp.headers)
        time.sleep(max(1.0, min(float(ra if ra is not None else 2 ** attempt), 60.0)))
    assert resp is not None
    if resp.status_code != 200:
        raise _err("openai-codex", f"Device code request returned status {resp.status_code}.", CODEX_RATE_LIMITED_CODE if resp.status_code == 429 else "device_code_request_error")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("user_code") or not data.get("device_auth_id"):
        raise _err("openai-codex", "Device code response missing required fields.", "device_code_incomplete")
    data["interval"] = max(3, int(data.get("interval", 5)))
    return data


def _codex_poll_authorization_code(issuer: str, *, device_auth_id: str, user_code: str, poll_interval: int) -> dict[str, Any]:
    start, blips = time.monotonic(), 0
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        while time.monotonic() - start < 15 * 60:
            time.sleep(max(1, int(poll_interval)))
            try:
                resp = client.post(f"{issuer.rstrip('/')}/api/accounts/deviceauth/token", json={"device_auth_id": device_auth_id, "user_code": user_code}, headers={"Content-Type": "application/json", "Accept": "application/json"})
            except (httpx.TransportError, OSError) as exc:
                blips += 1
                if blips >= 6:
                    raise _err("openai-codex", f"Device auth polling failed: {exc}", "device_code_poll_error") from exc
                continue
            blips = 0
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in {403, 404}:
                raise _err("openai-codex", f"Device auth polling returned status {resp.status_code}.", "device_code_poll_error")
    raise _err("openai-codex", "Login timed out after 15 minutes.", "device_code_timeout")


def _codex_exchange_authorization_code(issuer: str, client_id: str, code_resp: dict[str, Any]) -> dict[str, Any]:
    code, verifier = _clean(code_resp.get("authorization_code")), _clean(code_resp.get("code_verifier"))
    if not code or not verifier:
        raise _err("openai-codex", "Device auth response missing authorization_code or code_verifier.", "device_code_incomplete_exchange")
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        resp = client.post(
            CODEX_OAUTH_TOKEN_URL, headers=_FORM_JSON_HEADERS,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": f"{issuer.rstrip('/')}/deviceauth/callback", "client_id": client_id, "code_verifier": verifier},
        )
    if resp.status_code != 200:
        raise _err("openai-codex", f"Token exchange returned status {resp.status_code}.", CODEX_RATE_LIMITED_CODE if resp.status_code == 429 else "token_exchange_error")
    tokens = resp.json()
    if not isinstance(tokens, dict) or not _nonempty_str(tokens.get("access_token")):
        raise _err("openai-codex", "Token exchange did not return an access_token.", "token_exchange_no_access_token")
    return tokens


def _ranked_codex_slugs(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    rows = [
        (int(it["priority"]) if isinstance(it.get("priority"), (int, float)) else 10_000, it["slug"].strip())
        for it in entries
        if isinstance(it, dict) and isinstance(it.get("slug"), str) and it["slug"].strip()
        and str(it.get("visibility") or "").strip().lower() not in {"hide", "hidden"}
    ]
    rows.sort()
    return list(dict.fromkeys(slug for _, slug in rows))


def get_codex_model_ids(access_token: Optional[str] = None, base_url: Optional[str] = None) -> list[str]:
    if access_token:
        base = (base_url or _codex_base_url()).strip().rstrip("/")
        try:
            resp = httpx.get(
                f"{base}/models", params={"client_version": "0.100.0"},
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", **codex_cloudflare_headers(access_token, base_url=base)},
                timeout=10.0,
            )
            if resp.status_code == 200:
                ranked = _ranked_codex_slugs((resp.json() or {}).get("models"))
                if ranked:
                    return ranked
        except Exception:
            pass
    cache = Path(os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")).expanduser() / "models_cache.json"
    if cache.is_file():
        try:
            cached = _ranked_codex_slugs((json.loads(cache.read_text(encoding="utf-8-sig")) or {}).get("models"))
            if cached:
                return list(dict.fromkeys([*cached, *DEFAULT_CODEX_MODELS]))
        except Exception:
            pass
    return list(DEFAULT_CODEX_MODELS)


# ── SuperGrok (xai-oauth) ────────────────────────────────────────────────────


def _token_pair(tokens: Any) -> tuple[str, str]:
    return (_clean(tokens.get("access_token")), _clean(tokens.get("refresh_token"))) if isinstance(tokens, dict) else ("", "")


def _xai_tokens_from_payload(payload: dict[str, Any], access_token: str, fallback_refresh: str) -> dict[str, Any]:
    return {
        "access_token": access_token.strip(), "refresh_token": (_clean(payload.get("refresh_token")) or fallback_refresh).strip(),
        "id_token": _clean(payload.get("id_token")), "expires_in": payload.get("expires_in"),
        "token_type": _clean(payload.get("token_type")) or "Bearer",
    }


def _xai_validate_oauth_endpoint(url: str, *, field: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or not (host == "x.ai" or host.endswith(".x.ai")):
        raise _err("xai-oauth", f"xAI OIDC discovery returned invalid {field}: {url!r}.", "xai_discovery_invalid")
    return url


def _xai_oauth_inference_base_url() -> str:
    cand = (os.getenv("MUSE_XAI_BASE_URL") or os.getenv("HERMES_XAI_BASE_URL") or os.getenv("XAI_BASE_URL") or "").strip().rstrip("/")
    if cand:
        p = urlparse(cand)
        host = (p.hostname or "").lower()
        if p.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai")):
            return cand
    return DEFAULT_XAI_OAUTH_BASE_URL


def _xai_access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    exp = _decode_jwt_claims(access_token).get("exp")
    return isinstance(exp, (int, float)) and float(exp) <= time.time() + max(0, int(skew_seconds))


def _xai_proactive_refresh_skew_seconds(access_token: str) -> int:
    exp = _decode_jwt_claims(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    rem = float(exp) - time.time()
    return min(120, XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) if 0 < rem <= 45 * 60 else XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS


def _xai_oauth_discovery(timeout_seconds: float = 15.0) -> dict[str, str]:
    try:
        resp = httpx.get(XAI_OAUTH_DISCOVERY_URL, headers={"Accept": "application/json"}, timeout=timeout_seconds)
    except Exception as exc:
        raise _err("xai-oauth", f"xAI OIDC discovery failed: {exc}", "xai_discovery_failed") from exc
    if resp.status_code != 200:
        raise _err("xai-oauth", f"xAI OIDC discovery returned status {resp.status_code}.", "xai_discovery_failed")
    payload = resp.json()
    endpoints = {k: _clean(payload.get(k)) for k in ("authorization_endpoint", "token_endpoint")} if isinstance(payload, dict) else {}
    if not endpoints or not all(endpoints.values()):
        raise _err("xai-oauth", "xAI OIDC discovery missing required endpoints.", "xai_discovery_incomplete")
    for k, v in endpoints.items():
        _xai_validate_oauth_endpoint(v, field=k)
    return endpoints


def _xai_oauth_request_device_code(client: httpx.Client, *, scope: str = XAI_OAUTH_SCOPE) -> dict[str, Any]:
    resp = client.post(XAI_OAUTH_DEVICE_CODE_URL, headers=_FORM_JSON_HEADERS, data={"client_id": XAI_OAUTH_CLIENT_ID, "scope": scope})
    if resp.status_code != 200:
        raise _err("xai-oauth", f"xAI device-code request failed (HTTP {resp.status_code}).", "device_code_request_failed")
    payload = resp.json()
    missing = [k for k in ("device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval") if not isinstance(payload, dict) or k not in payload]
    if missing:
        raise _err("xai-oauth", f"xAI device-code response missing fields: {', '.join(missing)}", "device_code_invalid")
    return payload


def _xai_oauth_poll_device_token(
    client: httpx.Client, *, token_endpoint: str, device_code: str, expires_in: int, poll_interval: int,
) -> dict[str, Any]:
    _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    deadline, interval = time.monotonic() + max(1, int(expires_in)), max(1, int(poll_interval))
    while time.monotonic() < deadline:
        resp = client.post(token_endpoint, headers=_FORM_JSON_HEADERS, data={"grant_type": DEVICE_CODE_GRANT_TYPE, "client_id": XAI_OAUTH_CLIENT_ID, "device_code": device_code})
        if resp.status_code == 200:
            payload = resp.json()
            if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
                raise _err("xai-oauth", "xAI device-code token response missing tokens.", "xai_device_token_invalid")
            return payload
        try:
            err = resp.json()
        except Exception:
            if resp.status_code in {408, 429} or resp.status_code >= 500:
                time.sleep(min(max(parse_retry_after_seconds(resp.headers) or interval, 1.0), max(0.0, deadline - time.monotonic())))
                continue
            raise _err("xai-oauth", "xAI device-code polling returned non-JSON error.", "xai_device_token_failed")
        code = str((err or {}).get("error") or "")
        if code == "authorization_pending":
            time.sleep(interval)
        elif code == "slow_down":
            interval = min(interval + 1, 30)
            time.sleep(interval)
        else:
            raise _err("xai-oauth", f"xAI device-code polling failed: {(err or {}).get('error_description') or code}", "xai_device_token_failed")
    raise _err("xai-oauth", "Timed out waiting for xAI device authorization.", "device_code_timeout")


def _read_xai_oauth_tokens(*, _lock: bool = True) -> dict[str, Any]:
    if _lock:
        with _auth_store_lock():
            store = _load_auth_store()
    else:
        store = _load_auth_store()
    state = _provider_state(store, "xai-oauth")
    if not (isinstance(state, dict) and all(_token_pair(state.get("tokens")))):
        pool = (store.get("credential_pool") or {}).get("xai-oauth") if isinstance(store.get("credential_pool"), dict) else None
        for entry in pool if isinstance(pool, list) else ():
            at, rt = _token_pair(entry)
            if at and rt:
                state = {**(state or {}), "tokens": {"access_token": at, "refresh_token": rt, "token_type": _clean(entry.get("token_type")) or "Bearer"}, "last_refresh": entry.get("last_refresh")}
                break
    if not isinstance(state, dict) or not all(_token_pair(state.get("tokens"))):
        raise _err("xai-oauth", "No xAI OAuth credentials stored.", "xai_auth_missing", relogin=True)
    return {"tokens": dict(state["tokens"]), "last_refresh": state.get("last_refresh"), "discovery": dict(state.get("discovery") or {}), "redirect_uri": _clean(state.get("redirect_uri"))}


def _save_xai_oauth_tokens(
    tokens: dict[str, Any], *, discovery: Optional[dict[str, Any]] = None, redirect_uri: str = "",
    last_refresh: Optional[str] = None, auth_mode: str = "oauth_device_code", set_active: bool = True,
) -> None:
    with _auth_store_lock():
        store = _load_auth_store()
        state = _provider_state(store, "xai-oauth") or {}
        state.update(tokens=dict(tokens), last_refresh=last_refresh or _utc_now_z(), auth_mode=auth_mode)
        if discovery:
            prev_disc = state.get("discovery") if isinstance(state.get("discovery"), dict) else {}
            state["discovery"] = {**prev_disc, **dict(discovery)}
        if redirect_uri:
            state["redirect_uri"] = redirect_uri
        _store_provider_state(store, "xai-oauth", state, set_active=set_active)
        _save_auth_store(store)


def refresh_xai_oauth_pure(access_token: str, refresh_token: str, *, token_endpoint: str = "", timeout_seconds: float = 20.0) -> dict[str, Any]:
    del access_token
    if not _nonempty_str(refresh_token):
        raise _err("xai-oauth", "xAI OAuth missing refresh_token.", "xai_auth_missing_refresh_token", relogin=True)
    endpoint = _xai_validate_oauth_endpoint(token_endpoint.strip() or _xai_oauth_discovery(timeout_seconds)["token_endpoint"], field="token_endpoint")
    with httpx.Client(timeout=httpx.Timeout(max(5.0, float(timeout_seconds))), headers={"Accept": "application/json"}) as client:
        resp = client.post(endpoint, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "refresh_token", "client_id": XAI_OAUTH_CLIENT_ID, "refresh_token": refresh_token.strip()})
    if resp.status_code == 403:
        raise _err("xai-oauth", "xAI token refresh failed with HTTP 403 (tier denied).", "xai_oauth_tier_denied", relogin=False)
    if resp.status_code != 200:
        raise _err("xai-oauth", f"xAI token refresh failed (HTTP {resp.status_code}).", "xai_refresh_failed", relogin=resp.status_code in {400, 401})
    payload = resp.json()
    if not isinstance(payload, dict) or not _nonempty_str(payload.get("access_token")):
        raise _err("xai-oauth", "xAI token refresh response missing access_token.", "xai_refresh_missing_access_token", relogin=True)
    return {**_xai_tokens_from_payload(payload, str(payload["access_token"]), refresh_token), "last_refresh": _utc_now_z()}


def resolve_xai_oauth_runtime_credentials(
    *, force_refresh: bool = False, refresh_if_expiring: bool = True, refresh_skew_seconds: Optional[int] = None,
) -> dict[str, Any]:
    data = _read_xai_oauth_tokens()
    tokens, last_refresh = dict(data["tokens"]), data.get("last_refresh")

    def _should(tok_d: dict[str, Any]) -> bool:
        at = _clean(tok_d.get("access_token"))
        skew = int(refresh_skew_seconds) if refresh_skew_seconds is not None else _xai_proactive_refresh_skew_seconds(at)
        return bool(force_refresh) or bool(refresh_if_expiring and _xai_access_token_is_expiring(at, skew))

    if _should(tokens):
        timeout_s = float(os.getenv("MUSE_XAI_REFRESH_TIMEOUT_SECONDS", "20") or 20)
        with _auth_store_lock(timeout_seconds=max(AUTH_LOCK_TIMEOUT_SECONDS, timeout_s + 5.0)):
            latest = _read_xai_oauth_tokens(_lock=False)
            lt = dict(latest["tokens"])
            if _clean(lt.get("refresh_token")) != _clean(tokens.get("refresh_token")) and not _should(lt):
                tokens, last_refresh = lt, latest.get("last_refresh")
            elif _should(lt):
                ep = _clean((latest.get("discovery") or {}).get("token_endpoint")) or _xai_oauth_discovery(timeout_s)["token_endpoint"]
                ref = refresh_xai_oauth_pure(_clean(lt.get("access_token")), _clean(lt.get("refresh_token")), token_endpoint=ep, timeout_seconds=timeout_s)
                tokens = {**lt, **{k: v for k, v in ref.items() if k != "last_refresh"}}
                last_refresh = ref["last_refresh"]
                merged_disc = {**(latest.get("discovery") or {}), "token_endpoint": ep}
                _save_xai_oauth_tokens(tokens, discovery=merged_disc, redirect_uri=latest.get("redirect_uri") or "", last_refresh=last_refresh, set_active=False)
    return {"provider": "xai-oauth", "base_url": _xai_oauth_inference_base_url(), "api_key": _clean(tokens.get("access_token")), "source": "muse-auth-store", "last_refresh": last_refresh, "auth_mode": "oauth_device_code"}


def get_xai_oauth_auth_status() -> dict[str, Any]:
    try:
        creds = resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)
        tok = _clean(creds.get("api_key"))
        return {"provider": "xai-oauth", "logged_in": bool(tok) and not _xai_access_token_is_expiring(tok, 0), "base_url": creds.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL, "last_refresh": creds.get("last_refresh"), "auth_mode": "oauth_device_code"}
    except Exception as exc:
        return {"provider": "xai-oauth", "logged_in": False, "error": str(exc), "auth_mode": "oauth_device_code"}
