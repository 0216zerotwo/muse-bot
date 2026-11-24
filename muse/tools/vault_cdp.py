"""Encrypted `VaultStore` (`v1.x` Fernet compatible) and native CDP WebSocket `browser_vault`."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import struct
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import websockets
from cryptography.fernet import Fernet, InvalidToken

from muse.paths import get_muse_home
from muse.tools.terminal import register_redacted_secret

TOOLSET = "browser"
VAULT_VERSION = 1
VAULT_KINDS = ("login", "payment", "address")
LOGIN_IDENTIFIER_TYPES = ("email", "phone", "username")
KEY_FILENAME = "vault.key"
DATA_FILENAME = "vault.json.enc"
LOCK_FILENAME = ".vault.lock"
DEFAULT_CDP_URL = "http://127.0.0.1:9333"

_SCRUB_LOCK = threading.RLock()
_SCRUB_SECRETS: set[str] = set()


class VaultError(Exception):
    """User-safe vault validation, encryption, or lookup failure."""


def register_vault_secret(secret: Optional[str]) -> None:
    """Register a sensitive secret so output scrubbers replace any accidental echo."""
    if isinstance(secret, str):
        val = secret.strip()
        if len(val) >= 4:
            with _SCRUB_LOCK:
                _SCRUB_SECRETS.add(val)
            register_redacted_secret(val)


def scrub_secret_from_text(text: str, secret: Optional[dict[str, Any]] = None) -> str:
    """Replace registered vault secrets (and optional `secret` dict values) with `[REDACTED]`."""
    if not text:
        return ""
    with _SCRUB_LOCK:
        items = set(_SCRUB_SECRETS)
    if isinstance(secret, dict):
        for val in secret.values():
            if isinstance(val, str) and len(val.strip()) >= 3:
                items.add(val.strip())
    out = text
    for sec in sorted(items, key=len, reverse=True):
        if sec in out:
            out = out.replace(sec, "[REDACTED]")
    return out


def normalize_origin(raw: str) -> str:
    """Canonicalize a URL or origin to `scheme://hostname[:non_default_port]`."""
    val = (raw or "").strip()
    if not val or any(ch.isspace() for ch in val):
        raise VaultError("Origin URL cannot be empty or contain whitespace.")
    if "://" not in val:
        val = "https://" + val
    parsed = urllib.parse.urlsplit(val)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise VaultError(f"Unsupported origin scheme '{scheme}' (must be http or https).")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or any(ch.isspace() for ch in host):
        raise VaultError(f"Invalid origin URL '{raw}'.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise VaultError(f"Invalid port in origin '{raw}'.") from exc
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _safe_origin_or_empty(raw: str) -> str:
    try:
        return normalize_origin(raw)
    except VaultError:
        return ""


_OTP_ALGOS = {"SHA1": "sha1", "SHA256": "sha256", "SHA512": "sha512"}


def normalize_otp_secret(raw: str) -> str:
    """Validate and normalize a Base32 TOTP secret or `otpauth://totp/...` URI."""
    text = (raw or "").strip()
    if not text:
        return ""
    digits, period, algo = 6, 30, "SHA1"
    if text.lower().startswith("otpauth://"):
        parsed = urllib.parse.urlsplit(text)
        if parsed.netloc.lower() != "totp":
            raise VaultError("Only otpauth://totp URIs are supported.")
        qs = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
        text = qs.get("secret", "")
        try:
            digits = int(qs.get("digits", digits))
            period = int(qs.get("period", period))
        except ValueError as exc:
            raise VaultError("otpauth:// digits and period must be integers.") from exc
        algo = qs.get("algorithm", algo).upper().replace("-", "")
        if digits not in (6, 7, 8) or period <= 0 or algo not in _OTP_ALGOS:
            raise VaultError("Unsupported otpauth:// parameters.")
    cleaned = "".join(ch for ch in text.upper() if not ch.isspace() and ch != "-").rstrip("=")
    if not cleaned:
        raise VaultError("Invalid Base32 TOTP secret.")
    pad_len = (-len(cleaned)) % 8
    try:
        decoded = base64.b32decode(cleaned + ("=" * pad_len), casefold=True)
    except Exception as exc:
        raise VaultError("Invalid Base32 TOTP secret.") from exc
    if not decoded:
        raise VaultError("Decoded TOTP secret is empty.")
    if (digits, period, algo) == (6, 30, "SHA1"):
        return cleaned
    return f"{cleaned}|{digits}|{period}|{algo}"


def totp_now(
    secret: str,
    *,
    digits: int = 6,
    period: int = 30,
    at: Optional[float] = None,
    now: Optional[float] = None,
) -> str:
    """Generate RFC 6238 TOTP code string (100% signature-compatible with `v1.x` `totp_now`)."""
    raw = (secret or "").strip()
    algo = "sha1"
    if "|" in raw:
        seed_part, d_s, p_s, a_s = raw.split("|", 3)
        norm = normalize_otp_secret(seed_part)
        digits = int(d_s)
        period = int(p_s)
        algo = _OTP_ALGOS.get(a_s.upper(), "sha1")
    else:
        norm = normalize_otp_secret(raw)
        if "|" in norm:
            return totp_now(norm, at=(at if at is not None else now))
    if not norm:
        raise VaultError("OTP secret cannot be empty.")
    pad_len = (-len(norm)) % 8
    key = base64.b32decode(norm + ("=" * pad_len), casefold=True)
    ref_ts = at if at is not None else (now if now is not None else time.time())
    counter = int(ref_ts // period)
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo)).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


@dataclass(slots=True)
class VaultItemMeta:
    """Non-secret metadata safe to display in UI and `browser_vault(action='list')`."""

    id: str
    kind: str
    label: str
    origin: Optional[str]
    created_at: str
    identifier: Optional[str] = None
    identifier_type: Optional[str] = None
    has_otp: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "origin": self.origin,
            "created_at": self.created_at,
        }
        if self.identifier is not None:
            out["identifier"] = self.identifier
        if self.identifier_type is not None:
            out["identifier_type"] = self.identifier_type
        if self.kind == "login":
            out["has_otp"] = bool(self.has_otp)
        return out


def _chmod_0600(path: Path) -> None:
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _atomic_write_secret_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
    fd, tmp_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        _chmod_0600(tmp_path)
        os.replace(tmp_path, path)
        _chmod_0600(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


@contextmanager
def _vault_file_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(lock_path.parent, 0o700)
    with open(lock_path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            with contextlib.suppress(OSError):
                fh.seek(0)
                fh.write(b" ")
                fh.flush()
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + max(0.05, float(timeout_seconds))
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VaultError("Timed out acquiring vault file lock.")
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            deadline = time.monotonic() + max(0.05, float(timeout_seconds))
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VaultError("Timed out acquiring vault file lock.")
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class VaultStore:
    """Encrypted credential vault 100% binary & JSON compatible with `v1.x` `vault_store.py`."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        *,
        vault_dir: Optional[Path] = None,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        chosen = base_dir if base_dir is not None else vault_dir
        self._explicit_dir = Path(chosen) if chosen is not None else None
        self.lock_timeout_seconds = max(0.05, float(lock_timeout_seconds))
        self._thread_lock = threading.RLock()

    @property
    def dir(self) -> Path:
        return self._explicit_dir if self._explicit_dir is not None else (get_muse_home() / "vault")

    @property
    def key_path(self) -> Path:
        return self.dir / KEY_FILENAME

    @property
    def data_path(self) -> Path:
        return self.dir / DATA_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK_FILENAME

    def _get_or_create_fernet(self, *, create: bool = True) -> Optional[Fernet]:
        kpath = self.key_path
        if kpath.is_file():
            raw_key = kpath.read_bytes().strip()
            try:
                return Fernet(raw_key)
            except Exception as exc:
                raise VaultError("Corrupted vault.key file; unable to initialize Fernet.") from exc
        if not create:
            return None
        new_key = Fernet.generate_key()
        _atomic_write_secret_file(kpath, new_key + b"\n")
        return Fernet(new_key)

    def _read_unlocked(self) -> dict[str, Any]:
        dpath = self.data_path
        if not dpath.is_file():
            return {"version": VAULT_VERSION, "items": []}
        ciphertext = dpath.read_bytes().strip()
        if not ciphertext:
            return {"version": VAULT_VERSION, "items": []}
        fernet = self._get_or_create_fernet(create=False)
        if fernet is None:
            raise VaultError("vault.json.enc exists without vault.key; cannot decrypt vault.")
        try:
            plaintext = fernet.decrypt(ciphertext)
            data = json.loads(plaintext.decode("utf-8"))
        except InvalidToken as exc:
            raise VaultError("Vault decryption failed (key mismatch or corrupted ciphertext).") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError("Decrypted vault payload is not valid JSON.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise VaultError("Invalid vault JSON structure.")
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        fernet = self._get_or_create_fernet(create=True)
        assert fernet is not None
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        token = fernet.encrypt(raw)
        _atomic_write_secret_file(self.data_path, token)

    @staticmethod
    def _row_to_meta(row: dict[str, Any]) -> VaultItemMeta:
        sec = row.get("secret") if isinstance(row.get("secret"), dict) else {}
        return VaultItemMeta(
            id=str(row.get("id") or ""),
            kind=str(row.get("kind") or "login"),
            label=str(row.get("label") or ""),
            origin=row.get("origin"),
            created_at=str(row.get("created_at") or ""),
            identifier=row.get("identifier"),
            identifier_type=row.get("identifier_type"),
            has_otp=bool(sec.get("otp_secret")),
        )

    def add_item(
        self,
        kind: str,
        label: str,
        secret: dict[str, Any],
        origin: Optional[str] = None,
    ) -> VaultItemMeta:
        """Add a `'login'`, `'payment'`, or `'address'` item (`v1.x` `VaultStore.add_item` parity)."""
        if kind not in VAULT_KINDS:
            raise VaultError(f"Unknown vault kind {kind!r} (expected one of {VAULT_KINDS}).")
        clean_label = (label or "").strip()
        if not clean_label:
            raise VaultError("Label is required.")
        sec_copy = dict(secret or {})
        norm_origin: Optional[str] = None
        id_type: Optional[str] = None
        id_val: Optional[str] = None

        if kind == "login":
            if not origin:
                raise VaultError("Origin is required for login items.")
            norm_origin = normalize_origin(origin)
            raw_id_type = sec_copy.pop("identifier_type", None)
            if raw_id_type not in LOGIN_IDENTIFIER_TYPES:
                raise VaultError(f"identifier_type must be one of {LOGIN_IDENTIFIER_TYPES}.")
            id_val = str(sec_copy.pop("identifier", "") or "").strip()
            pw = str(sec_copy.get("password") or "")
            if not id_val or not pw:
                raise VaultError("Login items require identifier and password.")
            id_type = str(raw_id_type)
            norm_otp = normalize_otp_secret(str(sec_copy.get("otp_secret") or ""))
            stored_secret: dict[str, Any] = {"password": pw}
            if norm_otp:
                stored_secret["otp_secret"] = norm_otp
            register_vault_secret(pw)
        else:
            if origin:
                norm_origin = normalize_origin(origin)
            stored_secret = {str(k): str(v) for k, v in sec_copy.items() if str(v or "").strip()}
            if not stored_secret:
                raise VaultError(f"{kind} items require non-empty secret fields.")

        record: dict[str, Any] = {
            "id": f"vault_{secrets.token_hex(6)}",
            "kind": kind,
            "label": clean_label,
            "origin": norm_origin,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "identifier_type": id_type,
            "identifier": id_val,
            "secret": stored_secret,
        }
        with self._thread_lock, _vault_file_lock(
            self.lock_path, timeout_seconds=self.lock_timeout_seconds
        ):
            data = self._read_unlocked()
            data["items"].append(record)
            self._write_unlocked(data)
        return self._row_to_meta(record)

    def add_login(
        self,
        origin: str,
        password: str,
        *,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        username: Optional[str] = None,
        otp_secret: Optional[str] = None,
        label: Optional[str] = None,
    ) -> VaultItemMeta:
        norm_origin = normalize_origin(origin)
        if not password:
            raise VaultError("Password cannot be empty.")

        provided = [
            ("email", (email or "").strip()),
            ("phone", (phone or "").strip()),
            ("username", (username or "").strip()),
        ]
        non_empty = [(k, v) for k, v in provided if v]
        if len(non_empty) != 1:
            raise VaultError("Provide exactly one login identifier (email, phone, or username).")
        id_type, id_val = non_empty[0]
        host = urllib.parse.urlsplit(norm_origin).hostname or norm_origin
        resolved_label = (label or "").strip() or f"{host} ({id_val})"
        sec: dict[str, Any] = {
            "identifier_type": id_type,
            "identifier": id_val,
            "password": password,
        }
        if otp_secret and otp_secret.strip():
            sec["otp_secret"] = otp_secret
        return self.add_item("login", resolved_label, sec, origin=norm_origin)

    def list_items(
        self,
        *,
        kind: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> list[VaultItemMeta]:
        norm_filter = normalize_origin(origin) if origin else None
        if not self.data_path.is_file():
            return []
        with self._thread_lock, _vault_file_lock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            data = self._read_unlocked()
        out: list[VaultItemMeta] = []
        for row in data.get("items", []):
            if not isinstance(row, dict):
                continue
            if kind and row.get("kind") != kind:
                continue
            if norm_filter and row.get("origin") != norm_filter:
                continue
            out.append(self._row_to_meta(row))
        return out

    def has_items(self) -> bool:
        try:
            return bool(self.list_items())
        except Exception:
            return False

    def get_meta(self, item_id: str) -> Optional[VaultItemMeta]:
        target = (item_id or "").strip()
        if not target or not self.data_path.is_file():
            return None
        with self._thread_lock, _vault_file_lock(
            self.lock_path, timeout_seconds=self.lock_timeout_seconds
        ):
            data = self._read_unlocked()
        for row in data.get("items", []):
            if isinstance(row, dict) and row.get("id") == target:
                return self._row_to_meta(row)
        return None

    def get_secret(self, item_id: str) -> tuple[VaultItemMeta, dict[str, Any]]:
        target = (item_id or "").strip()
        if not target:
            raise VaultError("Vault item id is required.")
        with self._thread_lock, _vault_file_lock(
            self.lock_path, timeout_seconds=self.lock_timeout_seconds
        ):
            data = self._read_unlocked()
        for row in data.get("items", []):
            if isinstance(row, dict) and row.get("id") == target:
                sec = dict(row.get("secret") or {})
                return self._row_to_meta(row), sec
        raise VaultError(f"Vault item '{target}' not found.")

    def resolve_secret(self, item_id: str) -> dict[str, Any]:
        _, sec = self.get_secret(item_id)
        return sec

    def remove_item(self, item_id: str) -> bool:
        target = (item_id or "").strip()
        if not target or not self.data_path.is_file():
            return False
        with self._thread_lock, _vault_file_lock(
            self.lock_path, timeout_seconds=self.lock_timeout_seconds
        ):
            data = self._read_unlocked()
            before = len(data["items"])
            data["items"] = [
                r for r in data["items"] if not (isinstance(r, dict) and r.get("id") == target)
            ]
            if len(data["items"]) == before:
                return False
            self._write_unlocked(data)
            return True

    remove = remove_item


def get_vault_store(vault_dir: Optional[Path] = None) -> VaultStore:
    return VaultStore(vault_dir=vault_dir)


# ── Native Direct CDP WebSocket Autofill (`browser_vault`) ────────────────────

_JS_FILL_TEMPLATE = """(() => {
  const cfg = __PAYLOAD__;
  if (window.location.origin !== cfg.expectedOrigin) {
    return {
      ok: false,
      error_type: "origin_mismatch",
      actual_origin: String(window.location.origin || "")
    };
  }
  const isVisible = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const setNativeValue = (el, val) => {
    el.focus();
    const proto = Object.getPrototypeOf(el);
    const desc = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
    if (desc && desc.set) {
      desc.set.call(el, val);
    } else {
      el.value = val;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const inputs = Array.from(document.querySelectorAll("input")).filter(isVisible);
  const filledKinds = [];

  if (cfg.mode === "enter_code") {
    const otpRe = /otp|totp|2fa|mfa|code|token|verify|pin|验证码|动态码/i;
    const otpInput = inputs.find((el) => {
      const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
      const hint = [el.name, el.id, el.placeholder, el.getAttribute("aria-label")].join(" ");
      return ac === "one-time-code" || otpRe.test(hint);
    }) || inputs.find((el) => ["text", "tel", "number", ""].includes((el.type || "").toLowerCase()));
    if (!otpInput) {
      return { ok: false, error_type: "field_not_found", error: "No visible OTP/verification input found." };
    }
    setNativeValue(otpInput, cfg.otpCode);
    filledKinds.push("otp");
    return { ok: true, filled_fields: filledKinds.length, filled_kinds: filledKinds };
  }

  const pwInput = inputs.find((el) => (el.type || "").toLowerCase() === "password");
  const idRe = /user|email|login|account|phone|mobile|tel|id|账号|邮箱|手机|用户名/i;
  const idInput = inputs.find((el) => {
    const t = (el.type || "text").toLowerCase();
    if (!["text", "email", "tel", ""].includes(t)) return false;
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    const hint = [el.name, el.id, el.placeholder, el.getAttribute("aria-label")].join(" ");
    return ac.includes("username") || ac.includes("email") || idRe.test(hint);
  }) || inputs.find((el) => ["text", "email", "tel"].includes((el.type || "text").toLowerCase()));

  if (cfg.identifier && idInput) {
    setNativeValue(idInput, cfg.identifier);
    filledKinds.push("identifier");
  }
  if (cfg.password && pwInput) {
    setNativeValue(pwInput, cfg.password);
    filledKinds.push("password");
  }
  if (filledKinds.length === 0) {
    return {
      ok: false,
      error_type: "field_not_found",
      error: "No visible login/password fields found on page."
    };
  }
  if (cfg.submit) {
    const form = (pwInput && pwInput.form) || (idInput && idInput.form);
    const btn = (form || document).querySelector(
      "button[type='submit'], input[type='submit']"
    );
    if (btn && isVisible(btn)) {
      btn.click();
    } else if (form && typeof form.requestSubmit === "function") {
      form.requestSubmit();
    }
  }
  return { ok: true, filled_fields: filledKinds.length, filled_kinds: filledKinds };
})()"""


def _is_user_holding_browser(computer_mod: Any = None) -> bool:
    mod = computer_mod
    if mod is None:
        with contextlib.suppress(ImportError):
            from companion.modules import computer as mod
    if mod is not None and callable(getattr(mod, "holding", None)):
        with contextlib.suppress(Exception):
            return bool(mod.holding())
    return False


async def _select_cdp_target(
    cdp_url: str,
    *,
    expected_origin: str,
    target_id: Optional[str] = None,
) -> dict[str, Any]:
    base = cdp_url.rstrip("/")
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        resp = await client.get(f"{base}/json/list")
        resp.raise_for_status()
        targets = resp.json()

    pages = [
        t
        for t in (targets if isinstance(targets, list) else [])
        if isinstance(t, dict)
        and t.get("type") == "page"
        and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        raise VaultError("No active browser page target exposed on CDP /json/list.")
    if target_id:
        for page in pages:
            if str(page.get("id")) == target_id:
                return page
        raise VaultError(f"CDP target '{target_id}' not found.")

    # Prefer non-blank active page; if multiple tabs exist and one matches expected_origin, pick it.
    for page in pages:
        if _safe_origin_or_empty(str(page.get("url") or "")) == expected_origin:
            return page
    non_blank = [p for p in pages if not str(p.get("url") or "").startswith("about:blank")]
    return non_blank[0] if non_blank else pages[0]


async def _cdp_runtime_eval(
    ws: Any,
    msg_id: int,
    expression: str,
    *,
    timeout: float = 15.0,
) -> Any:
    import asyncio

    req = {
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    }
    await ws.send(json.dumps(req))
    deadline = time.monotonic() + max(0.05, float(timeout))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for CDP Runtime.evaluate response")
        raw_frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
        frame = json.loads(raw_frame)
        if frame.get("id") != msg_id:
            continue
        if "error" in frame:
            err_msg = (frame["error"] or {}).get("message") or "CDP Runtime.evaluate error"
            raise VaultError(str(err_msg))
        res_block = frame.get("result") or {}
        if res_block.get("exceptionDetails"):
            raise VaultError("Browser page threw exception during Runtime.evaluate")
        inner = res_block.get("result") or {}
        return inner.get("value")


async def browser_vault_handler(
    args: dict[str, Any],
    *,
    store: Optional[VaultStore] = None,
    cdp_url: Optional[str] = None,
    cdp_eval_timeout: float = 15.0,
    computer_mod: Any = None,
    ctx: Any = None,
    **_: Any,
) -> str:
    """List vault credentials or autofill login/TOTP directly via CDP WebSocket."""
    action = str(args.get("action") or "list").strip().lower()
    vstore = store or get_vault_store()

    if action == "list":
        origin_filter = str(args["origin"]).strip() if args.get("origin") else None
        try:
            items = [m.to_dict() for m in vstore.list_items(origin=origin_filter)]
            return json.dumps(
                {"success": True, "count": len(items), "items": items},
                ensure_ascii=False,
            )
        except VaultError as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    if action not in ("fill", "enter_code"):
        return json.dumps(
            {
                "success": False,
                "error": f"Unknown browser_vault action '{action}'. Use 'list', 'fill', or 'enter_code'.",
            },
            ensure_ascii=False,
        )

    if _is_user_holding_browser(computer_mod):
        return json.dumps(
            {
                "success": False,
                "error_type": "user_holding_browser",
                "error": "用户正在亲自操作浏览器，等用户操作完再自动填表。",
            },
            ensure_ascii=False,
        )

    item_id = str(args.get("id") or args.get("item_id") or "").strip()
    origin_hint = str(args.get("origin") or "").strip()
    try:
        if not item_id and origin_hint:
            matches = vstore.list_items(kind="login", origin=origin_hint)
            if len(matches) == 1:
                item_id = matches[0].id
            elif len(matches) > 1:
                return json.dumps(
                    {
                        "success": False,
                        "error": "Multiple credentials match this origin; specify 'id' from action='list'.",
                        "candidates": [m.to_dict() for m in matches],
                    },
                    ensure_ascii=False,
                )
        if not item_id:
            return json.dumps(
                {"success": False, "error": "Parameter 'id' (or unambiguous 'origin') is required."},
                ensure_ascii=False,
            )
        meta, secret = vstore.get_secret(item_id)
    except VaultError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    if meta.kind != "login" or not meta.origin:
        return json.dumps(
            {"success": False, "error": f"Vault item '{item_id}' is not a website login."},
            ensure_ascii=False,
        )

    otp_code: Optional[str] = None
    if action == "enter_code":
        raw_otp = str(secret.get("otp_secret") or "").strip()
        if not raw_otp:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "no_otp_configured",
                    "error": f"Vault item '{item_id}' has no 2FA/TOTP secret configured.",
                },
                ensure_ascii=False,
            )
        otp_code = totp_now(raw_otp)

    resolved_cdp = (cdp_url or os.environ.get("MUSE_CDP_URL") or DEFAULT_CDP_URL).strip()
    target_id = str(args["target_id"]).strip() if args.get("target_id") else None
    submit = bool(args.get("submit", False))

    try:
        target = await _select_cdp_target(
            resolved_cdp,
            expected_origin=meta.origin,
            target_id=target_id,
        )
        ws_url = str(target["webSocketDebuggerUrl"])

        async with websockets.connect(
            ws_url,
            open_timeout=5.0,
            close_timeout=2.0,
            max_size=4 * 1024 * 1024,
        ) as ws:
            # 1) Pre-execution live DOM origin verification BEFORE touching secrets.
            href_before = str(
                await _cdp_runtime_eval(
                    ws, 1, "String(window.location.href || '')", timeout=cdp_eval_timeout
                )
                or ""
            )
            actual_before = _safe_origin_or_empty(href_before)
            if actual_before != meta.origin:
                return json.dumps(
                    {
                        "success": False,
                        "error_type": "origin_mismatch",
                        "expected_origin": meta.origin,
                        "actual_origin": actual_before or href_before,
                        "error": (
                            f"Refused to autofill credential for {meta.origin}: "
                            f"active browser page is at {actual_before or href_before!r}."
                        ),
                    },
                    ensure_ascii=False,
                )

            # 2) Register secrets on output scrubbers BEFORE sending over local CDP socket.
            pw_val = str(secret.get("password") or "")
            if pw_val:
                register_vault_secret(pw_val)
            if otp_code:
                register_vault_secret(otp_code)
            bounder = getattr(ctx, "bounder", None)
            if bounder is not None and callable(getattr(bounder, "register_secret", None)):
                if pw_val:
                    bounder.register_secret(pw_val)
                if otp_code:
                    bounder.register_secret(otp_code)

            # 3) Atomic in-page JS fill with synchronous second origin guard.
            js_cfg = {
                "mode": action,
                "expectedOrigin": meta.origin,
                "identifier": meta.identifier or "",
                "password": pw_val if action == "fill" else "",
                "otpCode": otp_code or "",
                "submit": submit,
            }
            expr = _JS_FILL_TEMPLATE.replace("__PAYLOAD__", json.dumps(js_cfg, ensure_ascii=False))
            eval_res = await _cdp_runtime_eval(ws, 2, expr, timeout=cdp_eval_timeout)
            if not isinstance(eval_res, dict) or not eval_res.get("ok"):
                err_Info = eval_res if isinstance(eval_res, dict) else {}
                return json.dumps(
                    {
                        "success": False,
                        "error_type": str(err_Info.get("error_type") or "fill_failed"),
                        "expected_origin": meta.origin,
                        "actual_origin": err_Info.get("actual_origin", actual_before),
                        "error": scrub_secret_from_text(
                            str(err_Info.get("error") or "Autofill rejected by browser page.")
                        ),
                    },
                    ensure_ascii=False,
                )

            # 4) Post-execution origin check when submit=False (detect unexpected mid-fill redirect).
            href_after = str(
                await _cdp_runtime_eval(
                    ws, 3, "String(window.location.href || '')", timeout=cdp_eval_timeout
                )
                or ""
            )
            actual_after = _safe_origin_or_empty(href_after)
            if not submit and actual_after and actual_after != meta.origin:
                return json.dumps(
                    {
                        "success": False,
                        "error_type": "origin_changed_after_fill",
                        "expected_origin": meta.origin,
                        "actual_origin": actual_after,
                        "error": f"Page origin unexpectedly changed to {actual_after} during autofill.",
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    "action": action,
                    "id": meta.id,
                    "origin": meta.origin,
                    "filled_fields": int(eval_res.get("filled_fields") or 0),
                    "filled_kinds": list(eval_res.get("filled_kinds") or []),
                },
                ensure_ascii=False,
            )
    except Exception as exc:
        safe_err = scrub_secret_from_text(f"{type(exc).__name__}: {exc}")
        return json.dumps(
            {"success": False, "error_type": "cdp_error", "error": safe_err},
            ensure_ascii=False,
        )


BROWSER_VAULT_SCHEMA: dict[str, Any] = {
    "name": "browser_vault",
    "description": (
        "List saved encrypted website credentials (`action='list'`) or autofill login/password "
        "(`action='fill'`) and 2FA TOTP codes (`action='enter_code'`) directly into the matching "
        "origin page over CDP without exposing plaintext secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "fill", "enter_code"],
                "description": "Vault operation to execute.",
            },
            "id": {
                "type": "string",
                "description": "Vault credential id (e.g. 'vault_a1b2c3d4e5f6') from action='list'.",
            },
            "origin": {
                "type": "string",
                "description": "Optional website origin filter or unambiguous login origin.",
            },
            "submit": {
                "type": "boolean",
                "description": "Click the login submit button after filling (default False).",
                "default": False,
            },
        },
        "required": ["action"],
    },
}


def register_vault_tools(registry: Any) -> None:
    registry.register(
        "browser_vault",
        toolset=TOOLSET,
        schema=BROWSER_VAULT_SCHEMA,
        handler=browser_vault_handler,
        is_async=True,
    )


__all__ = [
    "BROWSER_VAULT_SCHEMA",
    "DEFAULT_CDP_URL",
    "TOOLSET",
    "VaultError",
    "VaultItemMeta",
    "VaultStore",
    "browser_vault_handler",
    "get_vault_store",
    "normalize_origin",
    "normalize_otp_secret",
    "register_vault_secret",
    "register_vault_tools",
    "scrub_secret_from_text",
    "totp_now",
]
