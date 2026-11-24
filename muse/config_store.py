"""Thread-safe & cross-process-safe ``config.yaml`` and ``.env`` store for Muse."""

from __future__ import annotations

import codecs
import copy
import errno
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .paths import _expand_home_path, get_muse_home, has_muse_home_override

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_IS_WINDOWS = platform.system() == "Windows"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF_RE = re.compile(r"\$\{([^}]+)\}")
_ENV_VAR_NAME_DENY_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG_")
_ENV_VAR_NAME_DENYLIST = frozenset(
    "LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH "
    "DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE "
    "PYTHONEXECUTABLE PYTHONNOUSERSITE PYTHONBREAKPOINT PYTHONCASEOK NODE_OPTIONS NODE_PATH PERL5OPT PERL5LIB PERLLIB "
    "RUBYOPT RUBYLIB CLASSPATH JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS GOFLAGS RUSTFLAGS PATH SHELL BROWSER "
    "EDITOR VISUAL PAGER MANPAGER GIT_SSH_COMMAND GIT_EXEC_PATH GIT_SHELL GIT_SSH GIT_ASKPASS SSH_ASKPASS SUDO_ASKPASS "
    "GIT_EDITOR GIT_SEQUENCE_EDITOR GIT_PAGER GIT_EXTERNAL_DIFF GIT_PROXY_COMMAND GIT_TEMPLATE_DIR GIT_DIR BASH_ENV "
    "ENV ZDOTDIR PROMPT_COMMAND VIMINIT EXINIT MUSE_HOME MUSE_ENGINE MUSEBOT_APP_DIR HERMES_HOME HERMES_PROFILE "
    "HERMES_CONFIG HERMES_ENV HERMES_CONFIG_PATH HERMES_ENV_PATH HERMES_OPTIONAL_MCPS HERMES_COPILOT_ACP_COMMAND "
    "HERMES_COPILOT_ACP_ARGS HERMES_YOLO_MODE HERMES_ACCEPT_HOOKS HERMES_REDACT_SECRETS HERMES_INTERACTIVE "
    "HERMES_EXEC_ASK HERMES_GATEWAY_SESSION HERMES_CRON_SESSION HERMES_SINGLE_QUERY_SESSION HERMES_SESSION_KEY "
    "HERMES_SESSION_PLATFORM".split()
)
_YAML11_AMBIGUOUS_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"})

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "", "providers": {}, "fallback_providers": [], "fallback": {"min_switch_reset_seconds": 0},
    "database": {"journal_mode": "wal"},
    "agent": {"max_turns": None, "api_max_retries": 3, "auto_recovery_cycles": 5, "clarify_timeout": 3600,
              "reasoning_overrides": {}, "disabled_toolsets": []},
    "terminal": {"backend": "local", "cwd": ".", "timeout": 180},
    "web": {"backend": "", "search_backend": "", "extract_backend": "", "extract_char_limit": 15000},
    "compression": {"enabled": True, "proactive_prune_tokens": 40000},
    "display": {"language": "zh", "file_mutation_verifier": False},
    "cron": {"wrap_response": False}, "mcp_servers": {}, "auxiliary": {},
    "security": {"redact_secrets": True}, "timezone": "",
}


class InvalidConfigError(RuntimeError):
    """Raised when refusing to overwrite an unreadable or malformed ``config.yaml``."""


_CONFIG_LOCK = threading.RLock()
_ENV_LOCK = threading.RLock()
_THREAD_HELD_LOCKS = threading.local()
_RAW_CONFIG_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_LOAD_CONFIG_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, Any], dict[str, Optional[str]]]] = {}
_LAST_GOOD_CONFIG_BY_PATH: dict[str, dict[str, Any]] = {}
_ENV_FILE_CACHE: OrderedDict[str, tuple[bytes, dict[str, str]]] = OrderedDict()
_ENV_FILE_CACHE_LOCK = threading.Lock()
_ENV_FILE_CACHE_MAX = 64
_WINDOWS_CONTENDED_REPLACE_ERRORS = frozenset({5, 32, 33})
_CROSS_DEVICE_ERRNOS = (errno.EXDEV, errno.EBUSY)
_LOCK_CONTENTION_ERRNOS = frozenset(c for c in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", None)) if c is not None)


def _resolve_home(home: Path | str | None) -> Path:
    return _expand_home_path(home) if home is not None else get_muse_home()


def _file_signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def _env_var_policy_name(key: str, *, is_windows: Optional[bool] = None) -> str:
    return key.upper() if (_IS_WINDOWS if is_windows is None else is_windows) else key


def validate_env_var_name_for_write(key: str) -> None:
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    p = _env_var_policy_name(key)
    if p in _ENV_VAR_NAME_DENYLIST or p.startswith(_ENV_VAR_NAME_DENY_PREFIXES):
        raise ValueError(f"Environment variable {key!r} is on the writer denylist.")


def _kernel_lock(fh: Any, acquire: bool) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)
    elif msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)


@contextmanager
def _cross_process_lock(lock_path: Path, in_proc_lock: threading.RLock, timeout_s: float = 15.0):
    with in_proc_lock:
        held: dict[str, int] = getattr(_THREAD_HELD_LOCKS, "depths", None) or {}
        _THREAD_HELD_LOCKS.depths = held
        try:
            key = os.path.normcase(str(lock_path.resolve(strict=False)))
        except OSError:
            key = os.path.normcase(str(lock_path))
        if held.get(key, 0) > 0:
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
                if held[key] <= 0:
                    held.pop(key, None)
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
        if os.name == "posix":
            with suppress(OSError):
                os.fchmod(fh.fileno(), 0o600)
        acquired = False
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\n")
                fh.flush()
            deadline, delay = time.monotonic() + timeout_s, 0.01
            while True:
                try:
                    _kernel_lock(fh, acquire=True)
                    acquired = True
                    break
                except OSError as exc:
                    contended = isinstance(exc, BlockingIOError) or getattr(exc, "winerror", None) in (33, 36) or exc.errno in _LOCK_CONTENTION_ERRNOS
                    if not contended:
                        if exc.errno in (getattr(errno, "ENOSYS", -1), getattr(errno, "EOPNOTSUPP", -1), getattr(errno, "ENOLCK", -1)):
                            break
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lockfile: {lock_path}") from exc
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.05)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
        finally:
            if acquired:
                with suppress(OSError):
                    _kernel_lock(fh, acquire=False)
            fh.close()


def _atomic_write_file(target: Path, writer_fn: Any, *, prefix: str, mode: int = 0o600, preserve_existing_mode: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    effective_mode = mode
    if preserve_existing_mode and os.name == "posix":
        with suppress(OSError):
            if target.exists():
                effective_mode = stat.S_IMODE(target.stat().st_mode)
    real_target = os.path.realpath(str(target)) if os.path.islink(str(target)) else str(target)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(real_target) or str(target.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            if os.name == "posix" and hasattr(os, "fchmod"):
                with suppress(OSError):
                    os.fchmod(fh.fileno(), effective_mode)
            writer_fn(fh)
            fh.flush()
            with suppress(OSError):
                os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_path, real_target)
                break
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) in _WINDOWS_CONTENDED_REPLACE_ERRORS and attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                if exc.errno in _CROSS_DEVICE_ERRNOS:
                    shutil.copyfile(tmp_path, real_target)
                    with suppress(OSError), open(real_target, "rb") as rf:
                        os.fsync(rf.fileno())
                    os.unlink(tmp_path)
                    break
                raise
        with suppress(OSError, NotImplementedError):
            os.chmod(real_target, effective_mode)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _to_plain_data(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k) if isinstance(k, str) else k: _to_plain_data(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_to_plain_data(x) for x in v]
    for t in (bool, int, float, str):
        if isinstance(v, t):
            return t(v)
    return v


def _rt_value(v: Any) -> Any:
    if isinstance(v, dict):
        m = CommentedMap()
        for k, val in v.items():
            m[k] = _rt_value(val)
        return m
    if isinstance(v, (list, tuple)):
        return CommentedSeq(_rt_value(x) for x in v)
    return DoubleQuotedScalarString(v) if isinstance(v, str) and v.lower() in _YAML11_AMBIGUOUS_WORDS else v


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = base.copy()
    for k, v in override.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif not (isinstance(out.get(k), dict) and v is None):
            out[k] = copy.deepcopy(v)
    return out


def _canonicalize_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    if "max_turns" in out:
        agent_cfg = dict(out.get("agent") or {})
        if agent_cfg.get("max_turns") is None:
            agent_cfg["max_turns"] = out["max_turns"]
        out["agent"] = agent_cfg
        out.pop("max_turns", None)
    m_in = out.get("model")
    if not any(out.get(k) for k in ("provider", "base_url", "context_length", "api_base")) and not (
        isinstance(m_in, dict) and (
            (m_in.get("provider") is not None and not isinstance(m_in.get("provider"), str))
            or any(m_in.get(k) for k in ("api_base", "model", "name"))
            or any(isinstance(m_in.get(k), dict) for k in ("default", "model", "name"))
        )
    ):
        return out
    model = dict(m_in) if isinstance(m_in, dict) else ({"default": m_in} if m_in else {})
    out["model"] = model
    for k in ("default", "model", "name"):
        if isinstance(val := model.get(k), dict):
            model[k] = str(val.get("model") or val.get("default") or "").strip()
            if (np := str(val.get("provider") or "").strip()) and str(model.get("provider") or "").strip() in ("", "auto"):
                model["provider"] = np
    for k in ("provider", "base_url", "context_length"):
        if out.get(k) and not model.get(k):
            model[k] = out[k]
        out.pop(k, None)
    if model.get("provider") is not None:
        model["provider"] = str(model["provider"]).strip()
    for alias_val in (out.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    out.pop("api_base", None)
    model.pop("api_base", None)
    if not model.get("default") and (alias := model.get("model") or model.get("name")):
        model["default"] = alias
    if model.get("default"):
        model.pop("model", None)
        model.pop("name", None)
    return out


def _env_ref_var_name(ref: str) -> Optional[str]:
    c = ref.strip()
    if c.startswith("env:"):
        return c[4:].strip() or None
    return None if (":" in c and re.match(r"^[a-z][a-z0-9_-]*:", c)) else (c or None)


def _lookup_env(name: str, dotenv_vals: dict[str, str], prefer_dotenv: bool) -> Optional[str]:
    return dotenv_vals[name] if (prefer_dotenv and name in dotenv_vals) else (v if (v := os.environ.get(name)) is not None else dotenv_vals.get(name))


def _expand_env_vars(obj: Any, dotenv_vals: dict[str, str], prefer_dotenv: bool = False) -> Any:
    if isinstance(obj, str):
        return _ENV_REF_RE.sub(lambda m: v if (n := _env_ref_var_name(m.group(1))) and (v := _lookup_env(n, dotenv_vals, prefer_dotenv)) is not None else m.group(0), obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v, dotenv_vals, prefer_dotenv) for k, v in obj.items()}
    return [_expand_env_vars(x, dotenv_vals, prefer_dotenv) for x in obj] if isinstance(obj, list) else obj


def _env_ref_snapshot(obj: Any, dotenv_vals: dict[str, str], prefer_dotenv: bool, out: Optional[dict[str, Optional[str]]] = None) -> dict[str, Optional[str]]:
    out = {} if out is None else out
    if isinstance(obj, str):
        for inner in _ENV_REF_RE.findall(obj):
            if (n := _env_ref_var_name(inner)) is not None:
                out[n] = _lookup_env(n, dotenv_vals, prefer_dotenv)
    elif isinstance(obj, dict):
        for v in obj.values():
            _env_ref_snapshot(v, dotenv_vals, prefer_dotenv, out)
    elif isinstance(obj, list):
        for x in obj:
            _env_ref_snapshot(x, dotenv_vals, prefer_dotenv, out)
    return out


def _preserve_templates(cur: Any, raw: Any, exp: Any, dotenv_vals: dict[str, str], prefer_dotenv: bool) -> Any:
    if isinstance(cur, str) and isinstance(raw, str) and _ENV_REF_RE.search(raw):
        return raw if cur in (raw, exp) or _expand_env_vars(raw, dotenv_vals, prefer_dotenv) == cur else cur
    if isinstance(cur, dict) and isinstance(raw, dict):
        return {k: _preserve_templates(v, raw.get(k), exp.get(k) if isinstance(exp, dict) else None, dotenv_vals, prefer_dotenv) for k, v in cur.items()}
    if isinstance(cur, list) and isinstance(raw, list):
        return [_preserve_templates(x, raw[i] if i < len(raw) else None, exp[i] if isinstance(exp, list) and i < len(exp) else None, dotenv_vals, prefer_dotenv) for i, x in enumerate(cur)]
    return cur


def _parse_yaml_mapping(config_path: Path, *, strict: bool) -> dict[str, Any]:
    try:
        config_path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if strict:
            raise InvalidConfigError(f"Cannot access existing config file {config_path}: {exc}") from exc
        logger.warning("Could not stat config file %s: %s", config_path, exc)
        return {}
    try:
        with open(config_path, encoding="utf-8-sig") as fh:
            loaded = YAML(typ="safe").load(fh)
    except Exception as exc:
        if strict:
            raise InvalidConfigError(f"Refusing to overwrite malformed YAML in {config_path}: {exc}") from exc
        logger.warning("Failed to parse %s: %s", config_path, exc)
        return {}
    if loaded is not None and not isinstance(loaded, dict):
        if strict:
            raise InvalidConfigError(f"Refusing to overwrite {config_path}: top-level YAML must be a mapping")
        logger.warning("Top-level YAML in %s must be a mapping, got %s", config_path, type(loaded).__name__)
        return {}
    return _to_plain_data(loaded) if isinstance(loaded, dict) else {}


def read_raw_config(home: Path | str | None = None) -> dict[str, Any]:
    config_path = _resolve_home(home) / "config.yaml"
    path_key = str(config_path)
    with _CONFIG_LOCK:
        try:
            sig = _file_signature(config_path.stat())
        except FileNotFoundError:
            _RAW_CONFIG_CACHE.pop(path_key, None)
            return {}
        except OSError as exc:
            logger.warning("Could not stat config file %s: %s", config_path, exc)
            return {}
        if (cached := _RAW_CONFIG_CACHE.get(path_key)) is not None and cached[0] == sig:
            return copy.deepcopy(cached[1])
        plain = _parse_yaml_mapping(config_path, strict=False)
        _RAW_CONFIG_CACHE[path_key] = (sig, copy.deepcopy(plain))
        return plain


def load_config(home: Path | str | None = None) -> dict[str, Any]:
    resolved_home = _resolve_home(home)
    config_path, path_key = resolved_home / "config.yaml", str(resolved_home / "config.yaml")
    prefer_dotenv = home is not None or has_muse_home_override()
    dotenv_vals = read_env_values(resolved_home)
    with _CONFIG_LOCK:
        sig: Optional[tuple[int, int, int, int]] = None
        with suppress(OSError):
            sig = _file_signature(config_path.stat())
        if sig is not None and (cached := _LOAD_CONFIG_CACHE.get(path_key)) is not None and cached[0] == sig:
            if all(_lookup_env(k, dotenv_vals, prefer_dotenv) == v for k, v in cached[2].items()):
                return copy.deepcopy(cached[1])
        merged = copy.deepcopy(DEFAULT_CONFIG)
        if sig is not None:
            try:
                merged = _deep_merge(merged, _parse_yaml_mapping(config_path, strict=True))
            except InvalidConfigError as exc:
                logger.warning("Failed to parse %s (%s); serving last-known-good or defaults", config_path, exc)
                if (lkg := _LAST_GOOD_CONFIG_BY_PATH.get(path_key)) is not None:
                    return copy.deepcopy(lkg)
        normalized = _canonicalize_config(merged)
        expanded: dict[str, Any] = _expand_env_vars(normalized, dotenv_vals, prefer_dotenv=prefer_dotenv)
        _LAST_GOOD_CONFIG_BY_PATH[path_key] = copy.deepcopy(expanded)
        if sig is not None:
            _LOAD_CONFIG_CACHE[path_key] = (sig, copy.deepcopy(expanded), _env_ref_snapshot(normalized, dotenv_vals, prefer_dotenv))
        else:
            _LOAD_CONFIG_CACHE.pop(path_key, None)
        return expanded


def _roundtrip_merge_map(dst: CommentedMap, src: dict[str, Any]) -> None:
    def _merge_item(c: Any, k: Any, v: Any) -> None:
        cur = c[k]
        if isinstance(v, dict) and isinstance(cur, CommentedMap):
            _roundtrip_merge_map(cur, v)
        elif isinstance(v, list) and isinstance(cur, CommentedSeq):
            for i, item in enumerate(v):
                _merge_item(cur, i, item) if i < len(cur) else cur.append(_rt_value(item))
            del cur[len(v):]
        elif not (cur == v and isinstance(cur, bool) is isinstance(v, bool)):
            c[k] = _rt_value(v)

    for k, v in src.items():
        _merge_item(dst, k, v) if k in dst else dst.__setitem__(k, _rt_value(v))
    for gone in [k for k in dst if k not in src]:
        del dst[gone]


def save_config(
    config: dict[str, Any], home: Path | str | None = None, *,
    strip_defaults: bool = False, preserve_keys: Optional[set[tuple[str, ...]]] = None, merge_existing: bool = False,
) -> None:
    if not isinstance(config, dict):
        raise TypeError(f"config must be a dict, got {type(config).__name__}")
    resolved_home = _resolve_home(home)
    config_path, path_key = resolved_home / "config.yaml", str(resolved_home / "config.yaml")
    prefer_dotenv = home is not None or has_muse_home_override()
    with _cross_process_lock(resolved_home / ".config.lock", _CONFIG_LOCK):
        existing_raw = _parse_yaml_mapping(config_path, strict=True)
        payload = _deep_merge(existing_raw, _to_plain_data(copy.deepcopy(config))) if (merge_existing and existing_raw) else _to_plain_data(copy.deepcopy(config))
        normalized = _canonicalize_config(payload)
        if existing_raw:
            normalized = _preserve_templates(normalized, _canonicalize_config(existing_raw), _LAST_GOOD_CONFIG_BY_PATH.get(path_key), read_env_values(resolved_home), prefer_dotenv)
        if strip_defaults:
            explicit = set(preserve_keys or ()) | {(str(k),) for k in existing_raw}
            normalized = {k: v for k, v in normalized.items() if (k,) in explicit or k not in DEFAULT_CONFIG or v != DEFAULT_CONFIG[k]}
        yaml_rt = YAML(typ="rt")
        yaml_rt.preserve_quotes, yaml_rt.allow_unicode, yaml_rt.default_flow_style, yaml_rt.width = True, True, False, 4096
        yaml_rt.indent(mapping=2, sequence=4, offset=2)
        raw_text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
        rt_doc = yaml_rt.load(raw_text) if raw_text.strip() else None
        doc = rt_doc if isinstance(rt_doc, CommentedMap) else CommentedMap(rt_doc or {})
        _roundtrip_merge_map(doc, normalized)
        _atomic_write_file(config_path, lambda fh: yaml_rt.dump(doc, fh), prefix=".config_", mode=0o600, preserve_existing_mode=True)
        _RAW_CONFIG_CACHE.pop(path_key, None)
        _LOAD_CONFIG_CACHE.pop(path_key, None)
        if (hc := sys.modules.get("hermes_cli.config")) is not None:
            for attr in ("_RAW_CONFIG_CACHE", "_LOAD_CONFIG_CACHE"):
                if isinstance(cache := getattr(hc, attr, None), dict):
                    cache.pop(path_key, None)


def _strip_inline_comment(v: str) -> str:
    v = v.strip()
    if v and v[0] in ("'", '"'):
        q, i = v[0], 1
        while i < len(v):
            if q == '"' and v[i] == "\\":
                i += 2
                continue
            if v[i] == q:
                return v[: i + 1] if v[i + 1:].lstrip().startswith("#") else v
            i += 1
        return v
    return re.split(r"\s+#", v, maxsplit=1)[0].strip() if v else v


def _parse_env_value(raw_val: str) -> str:
    v = raw_val.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        quoted, parsed, i = v[1:-1], [], 0
        while i < len(quoted):
            esc = quoted[i] == "\\" and quoted[i + 1 : i + 2] in ('"', "\\")
            parsed.append(quoted[i + 1] if esc else quoted[i])
            i += 2 if esc else 1
        return "".join(parsed)
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == "'" else v


def _quote_env_value(v: str) -> str:
    if not v or not ("#" in v or '"' in v or "'" in v or any(c.isspace() for c in v)):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sanitize_env_lines(lines: list[str]) -> list[str]:
    return [((raw if not (s := raw.strip()) or s.startswith("#") else s) + "\n") for line in lines for raw in (line.rstrip("\r\n"),)]


def _env_line_defines_key(line: str, key: str, *, is_windows: Optional[bool] = None) -> bool:
    s = line.strip()
    s = s[7:].lstrip() if s.startswith("export ") else s
    assigned, sep, _ = s.partition("=")
    return bool(sep) and _env_var_policy_name(assigned.strip(), is_windows=is_windows) == _env_var_policy_name(key, is_windows=is_windows)


def _invalidate_env_cache(env_path: Optional[Path] = None) -> None:
    with _ENV_FILE_CACHE_LOCK:
        _ENV_FILE_CACHE.clear() if env_path is None else _ENV_FILE_CACHE.pop(str(env_path), None)
    if (sec := sys.modules.get("agent.secret_scope")) is not None and callable(inv := getattr(sec, "invalidate_env_file_cache", None)):
        with suppress(Exception):
            inv(env_path)


def _publish_env_value(key: str, value: Optional[str]) -> None:
    os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
    if (sec := sys.modules.get("agent.secret_scope")) is not None:
        with suppress(Exception):
            if callable(cur_fn := getattr(sec, "current_secret_scope", None)) and isinstance(scope := cur_fn(), dict):
                scope.pop(key, None) if value is None else scope.__setitem__(key, value)


def read_env_values(home: Path | str | None = None) -> dict[str, str]:
    env_path = _resolve_home(home) / ".env"
    cache_key = str(env_path)
    try:
        raw = env_path.read_bytes()
    except OSError:
        _invalidate_env_cache(env_path)
        return {}
    with _ENV_FILE_CACHE_LOCK:
        if (cached := _ENV_FILE_CACHE.get(cache_key)) is not None and cached[0] == raw:
            _ENV_FILE_CACHE.move_to_end(cache_key)
            return dict(cached[1])
    raw_no_bom = raw.removeprefix(codecs.BOM_UTF8)
    try:
        text = raw_no_bom.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_no_bom.decode("latin-1")
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].lstrip() if line.startswith("export ") else line
        k, sep, val = line.partition("=")
        if sep and k.strip():
            parsed[k.strip()] = _parse_env_value(_strip_inline_comment(val))
    with _ENV_FILE_CACHE_LOCK:
        _ENV_FILE_CACHE[cache_key] = (raw, dict(parsed))
        _ENV_FILE_CACHE.move_to_end(cache_key)
        while len(_ENV_FILE_CACHE) > _ENV_FILE_CACHE_MAX:
            _ENV_FILE_CACHE.popitem(last=False)
    return parsed


load_env = read_env_values


def get_env_value(key: str, home: Path | str | None = None) -> Optional[str]:
    return v if (v := os.environ.get(key)) is not None else read_env_values(home).get(key)


def get_env_value_prefer_dotenv(key: str, home: Path | str | None = None) -> Optional[str]:
    return read_env_values(home).get(key) or os.environ.get(key)


def save_env_value(key: str, value: str, home: Path | str | None = None) -> None:
    validate_env_var_name_for_write(key)
    clean_value = str(value).replace("\n", "").replace("\r", "")
    resolved_home = _resolve_home(home)
    env_path = resolved_home / ".env"
    with _cross_process_lock(resolved_home / ".env.lock", _ENV_LOCK):
        if env_path.exists():
            with open(env_path, encoding="utf-8-sig", errors="replace") as fh:
                lines = _sanitize_env_lines(fh.readlines())
        else:
            lines = []
        entry = f"{key}={_quote_env_value(clean_value)}\n"
        idx = next((i for i, line in enumerate(lines) if _env_line_defines_key(line, key)), None)
        if idx is not None:
            lines[idx] = entry
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(entry)
        _atomic_write_file(env_path, lambda fh: fh.writelines(lines), prefix=".env_", mode=0o600, preserve_existing_mode=False)
    _publish_env_value(key, clean_value)
    _invalidate_env_cache(env_path)
    _LOAD_CONFIG_CACHE.pop(str(resolved_home / "config.yaml"), None)


def remove_env_value(key: str, home: Path | str | None = None) -> bool:
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    resolved_home = _resolve_home(home)
    env_path = resolved_home / ".env"
    if not env_path.exists():
        _publish_env_value(key, None)
        return False
    with _cross_process_lock(resolved_home / ".env.lock", _ENV_LOCK):
        if not env_path.exists():
            found = False
        else:
            with open(env_path, encoding="utf-8-sig", errors="replace") as fh:
                lines = _sanitize_env_lines(fh.readlines())
            new_lines = [line for line in lines if not _env_line_defines_key(line, key)]
            found = len(new_lines) < len(lines)
            if found:
                _atomic_write_file(env_path, lambda fh: fh.writelines(new_lines), prefix=".env_", mode=0o600, preserve_existing_mode=False)
    _publish_env_value(key, None)
    _invalidate_env_cache(env_path)
    _LOAD_CONFIG_CACHE.pop(str(resolved_home / "config.yaml"), None)
    return found


def invalidate_caches() -> None:
    with _CONFIG_LOCK:
        _RAW_CONFIG_CACHE.clear()
        _LOAD_CONFIG_CACHE.clear()
        _LAST_GOOD_CONFIG_BY_PATH.clear()
    _invalidate_env_cache()


__all__ = [
    "DEFAULT_CONFIG", "InvalidConfigError", "get_env_value", "get_env_value_prefer_dotenv",
    "invalidate_caches", "load_config", "load_env", "read_env_values", "read_raw_config",
    "remove_env_value", "save_config", "save_env_value", "validate_env_var_name_for_write",
]
