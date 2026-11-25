"""Provider resolution, parameter pre-normalization, and wire-protocol routing."""

from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

BUILTIN_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "api_mode": "chat_completions"},
    "openai-api": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "api_mode": "chat_completions"},
    "openai-codex": {"base_url": "https://chatgpt.com/backend-api/codex", "api_mode": "codex_responses", "oauth": True},
    "xai-oauth": {"base_url": "https://api.x.ai/v1", "api_mode": "codex_responses", "oauth": True},
    "anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY", "api_mode": "anthropic_messages"},
    "deepseek": {"base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "api_mode": "chat_completions"},
    "google": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "api_mode": "chat_completions"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "api_mode": "chat_completions"},
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY", "api_mode": "chat_completions"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY", "api_mode": "chat_completions"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "key_env": "MOONSHOT_API_KEY", "api_mode": "chat_completions"},
    "kimi-coding": {"base_url": "https://api.moonshot.ai/v1", "key_env": "KIMI_API_KEY", "api_mode": "chat_completions"},
    "kimi-coding-cn": {"base_url": "https://api.moonshot.cn/v1", "key_env": "KIMI_CN_API_KEY", "api_mode": "chat_completions"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "api_mode": "chat_completions"},
    "minimax": {"base_url": "https://api.minimax.io/anthropic", "key_env": "MINIMAX_API_KEY", "api_mode": "anthropic_messages"},
    "minimax-cn": {"base_url": "https://api.minimaxi.com/anthropic", "key_env": "MINIMAX_API_KEY", "api_mode": "anthropic_messages"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "key_env": "ZHIPU_API_KEY", "api_mode": "chat_completions"},
    "alibaba": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY", "api_mode": "chat_completions"},
    "volcengine": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "key_env": "ARK_API_KEY", "api_mode": "chat_completions"},
}

_ANTHROPIC_MODES = frozenset({"anthropic", "anthropic-messages", "anthropic_messages", "messages"})
_RESPONSES_MODES = frozenset({"responses", "openai-responses", "openai_responses", "codex_responses", "codex"})
_CHAT_MODES = frozenset({"openai", "chat", "chat-completions", "chat_completions", "openai-chat"})
_OPENAI_REASONING_RE = re.compile(r"^(?:openai/)?(?:o1(?:$|[-.])|o3(?:$|[-.])|o4(?:$|[-.])|gpt-5(?:$|[-.]))", re.IGNORECASE)
_ENV_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")


def _read_config() -> dict[str, Any]:
    try:
        from muse.config_store import load_config

        data = load_config()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_env(name: str) -> str:
    if not name:
        return ""
    try:
        from muse.config_store import get_env_value

        val = get_env_value(name)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return str(os.environ.get(name, "") or "").strip()


def _expand_str(val: Any) -> str:
    s = str(val or "").strip()
    if "${" not in s:
        return s

    def _repl(match: re.Match[str]) -> str:
        ref = match.group(1).strip()
        var_name = ref[4:].strip() if ref.startswith("env:") else ref
        resolved = _read_env(var_name) if var_name else ""
        return resolved if resolved else match.group(0)

    return _ENV_TEMPLATE_RE.sub(_repl, s).strip()


def canonical_api_mode(raw: Any) -> Optional[str]:
    mode = str(raw or "").strip().lower()
    if mode in _ANTHROPIC_MODES:
        return "anthropic_messages"
    if mode in _RESPONSES_MODES:
        return "codex_responses"
    if mode in _CHAT_MODES:
        return "chat_completions"
    return None


def _is_anthropic_endpoint(base_url: str | None, api_mode: str | None = None) -> bool:
    """True for explicit Anthropic api_mode, ``*.anthropic.com`` (not ``/openai``), ``.../anthropic``, or ``api.kimi.com/coding``."""
    if canonical_api_mode(api_mode) == "anthropic_messages":
        return True
    raw = str(base_url or "").strip().lower().rstrip("/")
    if not raw:
        return False
    parsed = urlparse(raw)
    host, path = (parsed.hostname or "").lower(), (parsed.path or "").rstrip("/")
    if (host == "anthropic.com" or host.endswith(".anthropic.com")) and "/openai" not in path:
        return True
    if path.endswith(("/anthropic", "/anthropic/v1", "/anthropic/v1/messages")):
        return True
    return host == "api.kimi.com" and (path == "/coding" or path.startswith("/coding/"))


is_anthropic_endpoint = _is_anthropic_endpoint


def normalize_anthropic_messages_url(base_url: str) -> str:
    base = str(base_url or "https://api.anthropic.com").strip().rstrip("/") or "https://api.anthropic.com"
    low = base.lower()
    if low.endswith("/v1/messages") or low.endswith("/messages"):
        return base
    return f"{base}/messages" if low.endswith("/v1") else f"{base}/v1/messages"


def normalize_chat_completions_url(base_url: str) -> str:
    base = str(base_url or "https://api.openai.com/v1").strip().rstrip("/") or "https://api.openai.com/v1"
    return base if base.lower().endswith("/chat/completions") else f"{base}/chat/completions"


def normalize_responses_url(base_url: str) -> str:
    base = str(base_url or "https://api.openai.com/v1").strip().rstrip("/") or "https://api.openai.com/v1"
    return base if base.lower().endswith("/responses") else f"{base}/responses"


def is_openai_reasoning_model(model: str | None) -> bool:
    """True for ``o1``/``o3``/``o4*``/``gpt-5*`` requiring ``max_completion_tokens`` and no ``temperature``."""
    return bool(model and _OPENAI_REASONING_RE.match(str(model).strip()))


def prenormalize_provider_params(
    *, extra_body: Optional[dict[str, Any]] = None, max_tokens: Optional[int] = None, temperature: Optional[float] = None,
) -> tuple[Optional[int], Optional[float], dict[str, Any]]:
    """Pop ``max_tokens`` and ``temperature`` from ``providers["m-*"]["extra_body"]`` into standard top-level fields."""
    cleaned = dict(extra_body) if isinstance(extra_body, dict) else {}
    raw_max = max_tokens if max_tokens is not None else cleaned.pop("max_tokens", None)
    raw_temp = temperature if temperature is not None else cleaned.pop("temperature", None)
    cleaned.pop("max_tokens", None)
    cleaned.pop("temperature", None)
    eff_max = int(raw_max) if raw_max is not None and not isinstance(raw_max, bool) else None
    eff_temp = float(raw_temp) if raw_temp is not None and not isinstance(raw_temp, bool) else None
    return eff_max, eff_temp, cleaned


def detect_api_mode(*, provider: str = "", base_url: str = "", api_mode: Optional[str] = None) -> str:
    explicit = canonical_api_mode(api_mode)
    if explicit:
        return explicit
    if str(provider or "").strip().lower() in ("openai-codex", "xai-oauth"):
        return "codex_responses"
    if _is_anthropic_endpoint(base_url, api_mode):
        return "anthropic_messages"
    parsed = urlparse(str(base_url or "").strip().lower().rstrip("/"))
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/responses") or ((parsed.hostname or "") == "chatgpt.com" and path.startswith("/backend-api/codex")):
        return "codex_responses"
    return "chat_completions"


def resolve_runtime_provider(
    requested: Optional[str] = None, *, target_model: Optional[str] = None, config: Optional[dict[str, Any]] = None,
    base_url: Optional[str] = None, api_key: Optional[str] = None, api_mode: Optional[str] = None,
    allow_oauth_refresh: bool = True,
) -> dict[str, Any]:
    """Resolve ``m-*`` presets, OAuth subscriptions, or built-in/custom providers into transport kwargs."""
    cfg = config if isinstance(config, dict) else _read_config()
    raw_model_cfg = cfg.get("model")
    if isinstance(raw_model_cfg, dict):
        model_cfg: dict[str, Any] = raw_model_cfg
    elif isinstance(raw_model_cfg, str) and raw_model_cfg.strip():
        model_cfg = {"default": _expand_str(raw_model_cfg)}
    else:
        model_cfg = {}
    providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    reasoning_overrides = (cfg.get("agent") or {}).get("reasoning_overrides") if isinstance(cfg.get("agent"), dict) else {}

    provider_id = str(requested or model_cfg.get("provider") or "").strip()
    if provider_id.lower().startswith("custom:"):
        provider_id = provider_id[len("custom:"):].strip()
    if provider_id.lower() in ("", "auto"):
        provider_id = str(model_cfg.get("provider") or "").strip()
        if provider_id.lower() in ("", "auto"):
            provider_id = next(iter(providers_cfg), "") or "openai"

    prov_lower = provider_id.lower()
    entry = providers_cfg.get(provider_id) or providers_cfg.get(prov_lower)
    entry = entry if isinstance(entry, dict) else {}

    if prov_lower in ("openai-codex", "xai-oauth"):
        from muse.llm.oauth import (
            DEFAULT_CODEX_BASE_URL, DEFAULT_XAI_OAUTH_BASE_URL,
            resolve_codex_runtime_credentials, resolve_xai_oauth_runtime_credentials,
        )
        creds: dict[str, Any] = {}
        explicit_key = str(api_key or "").strip()
        if not explicit_key:
            creds = (
                resolve_codex_runtime_credentials(refresh_if_expiring=allow_oauth_refresh, read_only=not allow_oauth_refresh)
                if prov_lower == "openai-codex"
                else resolve_xai_oauth_runtime_credentials(refresh_if_expiring=allow_oauth_refresh)
            )
        default_url = DEFAULT_CODEX_BASE_URL if prov_lower == "openai-codex" else DEFAULT_XAI_OAUTH_BASE_URL
        resolved_model = _expand_str(target_model or entry.get("model") or model_cfg.get("default") or "")
        return {
            "provider": prov_lower, "model": resolved_model,
            "base_url": _expand_str(base_url or creds.get("base_url") or default_url).rstrip("/"),
            "api_key": str(explicit_key or creds.get("api_key") or "").strip(),
            "api_mode": canonical_api_mode(api_mode) or "codex_responses",
            "extra_body": {}, "max_tokens": None, "temperature": None, "extra_headers": {},
            "context_length": entry.get("context_length"),
            "reasoning_effort": reasoning_overrides.get(resolved_model) if isinstance(reasoning_overrides, dict) else None,
            "auth_mode": "chatgpt" if prov_lower == "openai-codex" else "oauth_device_code",
            "source": creds.get("source") or "muse-auth-store",
        }

    builtin = BUILTIN_PROVIDER_DEFAULTS.get(prov_lower, {})
    same_cfg = str(model_cfg.get("provider") or "").strip().lower() == prov_lower
    resolved_url = _expand_str(
        base_url or entry.get("base_url") or (model_cfg.get("base_url") if same_cfg else "") or builtin.get("base_url") or ""
    ).rstrip("/")
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or builtin.get("key_env") or "").strip()
    resolved_key = str(
        (str(api_key).strip() if api_key else "")
        or (_read_env(key_env) if key_env else "")
        or _expand_str(entry.get("api_key") or "")
    ).strip()
    raw_mode = api_mode or entry.get("api_mode") or (model_cfg.get("api_mode") if same_cfg else None)
    resolved_mode = detect_api_mode(provider=provider_id, base_url=resolved_url, api_mode=raw_mode)
    if not raw_mode and resolved_mode == "chat_completions" and builtin.get("api_mode") and not base_url and not entry.get("base_url"):
        resolved_mode = str(builtin["api_mode"])

    lifted_max, lifted_temp, cleaned_extra = prenormalize_provider_params(
        extra_body=entry.get("extra_body") if isinstance(entry.get("extra_body"), dict) else None,
        max_tokens=entry.get("max_tokens"), temperature=entry.get("temperature"),
    )
    resolved_model = _expand_str(target_model or entry.get("model") or model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name") or "")
    raw_headers = entry.get("extra_headers")
    return {
        "provider": provider_id, "model": resolved_model, "base_url": resolved_url, "api_key": resolved_key,
        "api_mode": resolved_mode, "extra_body": cleaned_extra, "max_tokens": lifted_max, "temperature": lifted_temp,
        "extra_headers": {str(k): _expand_str(v) for k, v in raw_headers.items() if k and v is not None} if isinstance(raw_headers, dict) else {},
        "context_length": entry.get("context_length"),
        "reasoning_effort": reasoning_overrides.get(resolved_model) if isinstance(reasoning_overrides, dict) else None,
        "auth_mode": None, "source": "config" if entry else ("builtin" if builtin else "explicit"),
    }
