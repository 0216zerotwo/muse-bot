"""The companion's 模型 and 用量 pages (ported from nanobot's companion_models / model_router).

The user's model presets live in ``<companion dir>/models.json``: a name, which
service it's on (a provider entry in Hermes's ``config.yaml``, holding the
address and the key's env var) and the model id, plus per-model limits.
:func:`apply_to_hermes` turns that into Hermes config:

* each preset becomes its own provider entry (``m-<name>``) so its reply length
  and temperature travel with it; thinking strength goes to
  ``agent.reasoning_overrides``; the step limit to ``agent.max_turns``;
* the chat model is the pinned one, else the default; fallbacks become
  ``fallback_providers``;
* with 自动选择 on, Hermes's small background calls (titles, summaries,
  approval checks…) use the cheapest model, and when the chat model can't see
  images the best model that can looks at them first.

The chat model only changes when the user changes it, so a conversation keeps
its prompt cache (the router that picked a model per message is gone).

Drawing and video models (``media``) are kept apart from the chat ones: one of each is in use,
written to Hermes's ``image_gen`` / ``video_gen`` (see :func:`_apply_media`). Models are added
from a company (``model_catalog``: its address, key variable, and what each model can do) or,
for any other OpenAI-compatible service, by address.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .fs_atomic import atomic_write_json
from .paths import get_webui_dir

logger = logging.getLogger(__name__)

TIERS = ("cheap", "balanced", "strong")
MEDIA_KINDS = ("image", "video")
PRUNE_AT_TOKENS = 40_000
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "adaptive")
MAX_TOKENS_RANGE = (256, 131_072)
TEMPERATURE_RANGE = (0.0, 2.0)
TOOL_STEPS_RANGE = (5, 1000)
_MAX_DESCRIPTION = 120
_CHECK_TIMEOUT_S = 20.0
_PROVIDER_PREFIX = "m-"
# Hermes's background calls that a cheap model does well.
_CHEAP_SLOTS = ("title_generation", "approval", "memory_query_rewrite", "goal_judge", "compression")
_LOCK = threading.Lock()


def _models_path() -> Path:
    return get_webui_dir() / "models.json"


def _health_path() -> Path:
    return get_webui_dir() / "model-health.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    atomic_write_json(path, data)


def read_models() -> dict[str, Any]:
    data = _read_json(_models_path(), {})
    data.setdefault("presets", {})
    data.setdefault("default", next(iter(data["presets"]), None))
    data.setdefault("fallbacks", [])
    data.setdefault("max_tool_iterations", 200)
    router = data.setdefault("router", {})
    router.setdefault("enabled", False)
    router.setdefault("fixed", None)
    router.setdefault("models", {})
    media = data.setdefault("media", {})
    for kind in MEDIA_KINDS:
        media.setdefault(kind, {}).setdefault("models", {})
        media[kind].setdefault("active", None)
    return data


def _info(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data["router"]["models"].get(name) or {}
    return {
        "description": raw.get("description", ""),
        "tier": raw.get("tier", "balanced"),
        "vision": bool(raw.get("vision", False)),
        "price_in": raw.get("price_in"),
        "price_out": raw.get("price_out"),
        "price_cached": raw.get("price_cached"),
    }


# ---------- health ----------


def read_health() -> dict[str, dict[str, Any]]:
    return _read_json(_health_path(), {})


def record_health(name: str, ok: bool, error: Optional[str] = None, latency_ms: Optional[int] = None) -> None:
    with _LOCK:
        health = read_health()
        health[name] = {"ok": ok, "error": error, "checked_at": int(time.time() * 1000), "latency_ms": latency_ms}
        _write_json(_health_path(), health)


def short_error(text: str, name: str) -> str:
    """A few words on why a model call failed, for the app to show."""
    match = re.search(r"['\"]code['\"]\s*:\s*['\"]([A-Za-z_.]+)['\"]", text)
    code = match.group(1) if match else ""
    if code == "ModelNotOpen":
        return f"{name} 在服务商那里还没开通"
    if code in {"InvalidApiKey", "AuthenticationError", "invalid_api_key"} or " 401" in f" {text[:200]}":
        return f"{name} 的 API key 无效"
    if code:
        return f"{name} 出错：{code}"
    flat = " ".join(text.split())
    return f"{name} 出错：{flat[:40]}" if flat else f"{name} 出错"


def _service(provider: str) -> dict[str, Any]:
    from muse.config_store import load_config

    service = (load_config().get("providers") or {}).get(provider)
    if not isinstance(service, dict) or not service.get("base_url"):
        raise ValueError(f"找不到这个服务：{provider}")
    return service


def _check_signed_in(name: str, preset: dict[str, Any]) -> None:
    """A one-token request through the signed-in provider (the plan's tokens live in auth.json)."""
    from muse.llm.auxiliary import call_llm

    from .oauth_login import keep_signed_in

    if preset["provider"] == "openai-codex":
        keep_signed_in()
    started = time.monotonic()
    try:
        call_llm(messages=[{"role": "user", "content": "ping"}], max_tokens=16, timeout=_CHECK_TIMEOUT_S,
                 provider=preset["provider"], model=preset["model"])
        record_health(name, True, latency_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:
        record_health(name, False, short_error(str(exc), name))


def _is_anthropic_service(service: dict[str, Any]) -> bool:
    from muse.llm.providers import _is_anthropic_endpoint

    return _is_anthropic_endpoint(service.get("base_url"), service.get("api_mode"))


async def check_models(names: Optional[list[str]] = None) -> dict[str, Any]:
    """Send each model a minimal request and remember whether the service answers."""
    import httpx

    data = read_models()
    for name in names or sorted(data["presets"]):
        preset = data["presets"].get(name)
        if preset is None:
            raise ValueError(f"找不到这个模型：{name}")
        if preset.get("auth"):
            await asyncio.to_thread(_check_signed_in, name, preset)
            continue
        try:
            from muse.config_store import get_env_value

            service = _service(preset["provider"])
            key_env = str(service.get("key_env") or "")
            key = (get_env_value(key_env) if key_env else None) or str(service.get("api_key") or "")
            base = str(service["base_url"]).rstrip("/")
            messages = [{"role": "user", "content": "ping"}]
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_S) as client:
                if _is_anthropic_service(service):
                    endpoint = f"{base}/messages" if (base.endswith("/v1") or "/v1/" in base) else f"{base}/v1/messages"
                    response = await client.post(
                        endpoint,
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                        json={"model": preset["model"], "messages": messages, "max_tokens": 1},
                    )
                else:
                    endpoint = f"{base}/chat/completions"
                    headers = {"Authorization": f"Bearer {key}"}
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json={"model": preset["model"], "messages": messages, "max_tokens": 1},
                    )
                    if response.status_code == 400 and any(
                        tok in response.text.lower() for tok in ("max_completion_tokens", "max_tokens")
                    ):
                        # OpenAI reasoning models (o1/o3/o4-mini/gpt-5) require max_completion_tokens.
                        started = time.monotonic()
                        response = await client.post(
                            endpoint,
                            headers=headers,
                            json={"model": preset["model"], "messages": messages, "max_completion_tokens": 16},
                        )
            if response.status_code >= 400:
                record_health(name, False, short_error(f"{response.status_code} {response.text}", name))
            else:
                # How long a one-token answer takes: the small calls in front of a reply go to a quick model.
                record_health(name, True, latency_ms=int((time.monotonic() - started) * 1000))
        except httpx.TimeoutException:
            record_health(name, False, f"{name} 没有在 {int(_CHECK_TIMEOUT_S)} 秒内回应")
        except Exception as exc:
            record_health(name, False, short_error(str(exc), name))
    return models_payload()


# ---------- the page ----------


def _services() -> dict[str, Any]:
    from muse.config_store import read_raw_config

    return {k: v for k, v in (read_raw_config().get("providers") or {}).items()
            if isinstance(v, dict) and not k.startswith(_PROVIDER_PREFIX)}


def saved_key_urls() -> set[str]:
    """Addresses whose key is already saved (添加模型 then lets the key field stay empty)."""
    from muse.config_store import get_env_value

    return {
        str(entry.get("base_url") or "").rstrip("/")
        for entry in _services().values()
        if entry.get("api_key") or (entry.get("key_env") and get_env_value(str(entry["key_env"])))
    }


def _vendor_of(services: dict[str, Any], provider: str) -> Optional[str]:
    from .model_catalog import vendor_for_base_url, vendor_for_login

    found = vendor_for_login(provider) or vendor_for_base_url(str((services.get(provider) or {}).get("base_url") or ""))
    return found["id"] if found else None


def models_payload() -> dict[str, Any]:
    data = read_models()
    services = _services()
    return {
        "presets": [
            {
                "name": name,
                "model": preset["model"],
                "provider": preset["provider"],
                "vendor": _vendor_of(services, preset["provider"]),
                "reasoning_effort": preset.get("reasoning_effort"),
                "max_tokens": preset.get("max_tokens", 8192),
                "temperature": preset.get("temperature", 0.1),
                "context_window_tokens": preset.get("context_window_tokens", 131_072),
            }
            for name, preset in sorted(data["presets"].items())
        ],
        "max_tool_iterations": data["max_tool_iterations"],
        "default": data["default"],
        "fallbacks": data["fallbacks"],
        "settings": {
            "enabled": data["router"]["enabled"],
            "router": None,
            "fixed": data["router"]["fixed"],
            "models": {name: _info(data, name) for name in data["presets"]},
        },
        "health": read_health(),
        # The model no longer changes per message, so there are no per-turn choices to list.
        "decisions": [],
        "media": {
            kind: {
                "active": data["media"][kind]["active"],
                "models": [{"name": name, "model": entry["model"], "vendor": entry.get("vendor"), "backend": entry["backend"],
                            "price": entry.get("price")}
                           for name, entry in sorted(data["media"][kind]["models"].items())],
            }
            for kind in MEDIA_KINDS
        },
    }


_NUMBER_LABELS = {
    "max_tokens": "最大回复长度",
    "temperature": "温度",
    "max_tool_iterations": "单次最多步数",
}


def _number(value: Any, bounds: tuple[float, float], field: str, *, integer: bool) -> float:
    label = _NUMBER_LABELS.get(field, field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not float(value).is_integer()):
        raise ValueError(f"{label}必须是{'整数' if integer else '数字'}")
    if not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{label}必须在 {bounds[0]:g} 到 {bounds[1]:g} 之间")
    return int(value) if integer else float(value)


def _price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("价格必须是不小于 0 的数字")
    return float(value)


def _model_info(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("模型信息格式不正确")
    description, tier, vision = raw.get("description", ""), raw.get("tier", "balanced"), raw.get("vision", False)
    if not isinstance(description, str) or len(description) > _MAX_DESCRIPTION:
        raise ValueError(f"模型说明最多 {_MAX_DESCRIPTION} 个字")
    if tier not in TIERS:
        raise ValueError(f"模型档位只能是 {', '.join(TIERS)} 之一")
    if not isinstance(vision, bool):
        raise ValueError("图片理解开关格式不正确")
    return {"description": description.strip(), "tier": tier, "vision": vision,
            "price_in": _price(raw.get("price_in")), "price_out": _price(raw.get("price_out")),
            "price_cached": _price(raw.get("price_cached"))}


def _apply_limits(data: dict[str, Any], limits: Any) -> None:
    if not isinstance(limits, dict):
        raise ValueError("模型参数格式不正确")
    for name, values in limits.items():
        preset = data["presets"].get(name)
        if preset is None:
            raise ValueError(f"找不到这个模型：{name}")
        if not isinstance(values, dict):
            raise ValueError(f"{name} 的参数格式不正确")
        unknown = set(values) - {"reasoning_effort", "max_tokens", "temperature"}
        if unknown:
            raise ValueError(f"不支持的模型参数：{', '.join(sorted(unknown))}")
        if "reasoning_effort" in values:
            effort = values["reasoning_effort"]
            if effort is not None and effort not in REASONING_EFFORTS:
                raise ValueError(f"思考强度只能是 {', '.join(REASONING_EFFORTS)} 之一")
            preset["reasoning_effort"] = effort
        if "max_tokens" in values:
            preset["max_tokens"] = _number(values["max_tokens"], MAX_TOKENS_RANGE, "max_tokens", integer=True)
        if "temperature" in values:
            preset["temperature"] = _number(values["temperature"], TEMPERATURE_RANGE, "temperature", integer=False)


def update_checked_prices(vendor_id: str, kind: str, prices: dict[str, dict[str, Any]]) -> None:
    """After 检测: the models already added from this company take the prices it found."""
    from .model_check import text_prices

    if not prices:
        return
    services = _services()
    with _LOCK:
        data = read_models()
        if kind == "text":
            for name, preset in data["presets"].items():
                if preset["model"] in prices and _vendor_of(services, preset["provider"]) == vendor_id:
                    price_in, price_out, price_cached = text_prices(prices[preset["model"]])
                    info = data["router"]["models"].setdefault(name, {"description": "", "tier": "balanced", "vision": False})
                    info.update(price_in=price_in, price_out=price_out, price_cached=price_cached)
        else:
            for entry in data["media"][kind]["models"].values():
                if entry.get("vendor") == vendor_id and entry["model"] in prices:
                    entry["price"] = prices[entry["model"]]
        _write_json(_models_path(), data)


def update_models(changes: dict[str, Any]) -> dict[str, Any]:
    """The page's changes: ``enabled``/``fixed`` (自动选择 or one pinned model), per-model ``models``
    info, ``fallbacks``, per-model ``presets`` limits and ``max_tool_iterations``."""
    unknown = set(changes) - {
        "enabled", "router", "fixed", "default", "models", "fallbacks", "presets", "max_tool_iterations", "media_active"}
    if unknown:
        raise ValueError(f"不支持的字段：{', '.join(sorted(unknown))}")
    with _LOCK:
        data = read_models()
        names = data["presets"]
        if "default" in changes:
            if changes["default"] not in names:
                raise ValueError(f"找不到这个模型：{changes['default']}")
            data["default"] = changes["default"]
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise ValueError("自动选择开关格式不正确")
            data["router"]["enabled"] = changes["enabled"]
        if "fixed" in changes:
            fixed = changes["fixed"] or None
            if fixed is not None and fixed not in names:
                raise ValueError(f"找不到这个模型：{fixed}")
            data["router"]["fixed"] = fixed
        if "models" in changes:
            if not isinstance(changes["models"], dict):
                raise ValueError("模型配置格式不正确")
            for name, info in changes["models"].items():
                if info is None:
                    data["router"]["models"].pop(str(name), None)
                else:
                    data["router"]["models"][str(name)] = _model_info(info)
        if "fallbacks" in changes:
            fallbacks = changes["fallbacks"]
            if not isinstance(fallbacks, list) or not all(isinstance(name, str) for name in fallbacks):
                raise ValueError("备用模型必须是模型名称列表")
            missing = [name for name in fallbacks if name not in names]
            if missing:
                raise ValueError(f"找不到这些模型：{', '.join(missing)}")
            data["fallbacks"] = list(dict.fromkeys(fallbacks))
        if "presets" in changes:
            _apply_limits(data, changes["presets"])
        if "max_tool_iterations" in changes:
            data["max_tool_iterations"] = _number(
                changes["max_tool_iterations"], TOOL_STEPS_RANGE, "max_tool_iterations", integer=True)
        if "media_active" in changes:
            active = changes["media_active"]
            if not isinstance(active, dict):
                raise ValueError("媒体模型配置格式不正确")
            for kind, name in active.items():
                if kind not in MEDIA_KINDS or (name is not None and name not in data["media"][kind]["models"]):
                    label = "画图" if kind == "image" else "视频" if kind == "video" else str(kind)
                    raise ValueError(f"找不到这个{label}模型：{name}")
                data["media"][kind]["active"] = name
        _write_json(_models_path(), data)
    apply_to_hermes()
    return models_payload()


# ---------- adding and removing models ----------

_NAME = re.compile(r"^[\w.\- 一-鿿]{1,40}$")


def _service_for(base_url: str, api_key: str, key_env: Optional[str]) -> str:
    """The service entry (config.yaml ``providers``) for an address, made on first use; a new key
    replaces the saved one. ``key_env`` names the variable for a new service (the company's usual
    one, like DEEPSEEK_API_KEY); a service already set up keeps its own."""
    from muse.config_store import read_raw_config, save_config, save_env_value

    config = read_raw_config()
    services = config.setdefault("providers", {})
    service = next((key for key, entry in services.items()
                    if not key.startswith(_PROVIDER_PREFIX) and isinstance(entry, dict)
                    and str(entry.get("base_url") or "").rstrip("/") == base_url), None)
    if service is None:
        if not api_key:
            raise ValueError("这个服务第一次用，需要填 API key")
        slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://", "", base_url).lower()).strip("-")[:30] or "service"
        service = f"svc-{slug}"
        env = key_env or f"COMPANION_KEY_{slug.upper().replace('-', '_')}"
        services[service] = {"base_url": base_url, "key_env": env}
        save_config(config)
        save_env_value(env, api_key)
    elif api_key:
        env = services[service].get("key_env") or key_env or f"COMPANION_KEY_{service.upper().replace('-', '_')}"
        if not services[service].get("key_env"):
            services[service]["key_env"] = env
            save_config(config)
        save_env_value(env, api_key)
    return service


def _share_key(service: str, env: str) -> None:
    """Hermes's OpenRouter, xAI, fal and DeepInfra drawing/video backends read their company's usual
    variable (OPENROUTER_API_KEY, XAI_API_KEY, FAL_KEY, DEEPINFRA_API_KEY): make sure it holds the key
    this service uses."""
    from muse.config_store import get_env_value, read_raw_config, save_env_value

    own = str(((read_raw_config().get("providers") or {}).get(service) or {}).get("key_env") or "")
    if own and own != env and (val := get_env_value(own)):
        save_env_value(env, val)


def _unique(name: str, taken: set[str]) -> str:
    name = " ".join(name.split())[:36] or "模型"
    if name not in taken:
        return name
    return next(f"{name} {i}" for i in range(2, 100) if f"{name} {i}" not in taken)


def add_preset(values: dict[str, Any]) -> dict[str, Any]:
    """A new model: from a company in the catalog (``vendor`` + ``model``, its key once), or any
    OpenAI-compatible service (``base_url``). ``kind`` is text (default), image or video. A service
    already set up at the same address keeps its key unless a new one is given. Returns the page
    payload with ``added``: the new model's name."""
    from .model_catalog import model_facts, tier_for, vendor

    kind = str(values.get("kind") or "text")
    vendor_id = str(values.get("vendor") or "").strip()
    model = str(values.get("model") or "").strip()
    api_key = str(values.get("api_key") or "").strip()
    entry = vendor(vendor_id) if vendor_id and vendor_id != "custom" else None
    if vendor_id and vendor_id != "custom" and entry is None:
        raise ValueError("没有这个服务商")
    login = (entry or {}).get("login")
    if login:
        from .oauth_login import signed_in

        if not signed_in(login):
            raise ValueError(f"先登录{entry['name']}")
    base_url = "" if login else entry["base_url"] if entry else str(values.get("base_url") or "").strip().rstrip("/")
    if kind not in ("text", *MEDIA_KINDS):
        raise ValueError("kind 只能是 text、image 或 video")
    if not login and not re.match(r"^https?://\S+$", base_url):
        raise ValueError("服务地址要以 http:// 或 https:// 开头")
    if not model or len(model) > 200:
        raise ValueError("请选一个模型，或填写模型 ID")
    backend = "openai"
    if kind != "text":
        backend = ((entry or {}).get(kind) or {}).get("backend") or ("openai" if kind == "image" and not entry else "")
        if not backend:
            raise ValueError(f"{(entry or {}).get('name', '这个服务')}没有{'画图' if kind == 'image' else '视频'}模型")
    facts = model_facts(entry["id"], model) if entry else None
    from .model_check import checked, text_prices

    found = next((m for m in ((checked(entry["id"], kind) if entry else None) or {}).get("models") or []
                  if m["id"] == model), None)
    checked_price = (found or {}).get("price")
    name = str(values.get("name") or "").strip() or (facts or {}).get("name") or model.rsplit("/", 1)[-1]
    if not _NAME.match(name):
        name = re.sub(r"[^\w.\- 一-鿿]+", "-", name).strip("-") or "模型"
    with _LOCK:
        data = read_models()
        taken = set(data["presets"]) | {n for k in MEDIA_KINDS for n in data["media"][k]["models"]}
        if values.get("name") and name in taken:
            raise ValueError(f"已经有叫 {name} 的模型了")
        name = _unique(name, taken)
        # A subscription runs on Hermes's signed-in provider; everything else on a service with a key.
        service = entry["provider"] if login else _service_for(base_url, api_key, entry["key_env"] if entry else None)
        if kind == "text":
            data["presets"][name] = {
                "provider": service, **({"auth": True} if login else {}),
                "model": model, "reasoning_effort": None, "max_tokens": 8192,
                "temperature": 0.1,
                "context_window_tokens": int((facts or {}).get("context") or 131_072)}
            price_in, price_out, price_cached = text_prices(checked_price)
            if facts or price_in is not None:
                # What the platform (检测) or the registry says, so 自动选择 and 用量 are right
                # without typing it in.
                if price_in is None:
                    price_in, price_out = facts["price_in"], facts["price_out"]
                data["router"]["models"][name] = {
                    "description": "", "tier": (facts or {}).get("tier") or tier_for(price_in),
                    "vision": bool((facts or {}).get("vision")),
                    "price_in": price_in, "price_out": price_out, "price_cached": price_cached}
            if not data["default"]:
                data["default"] = name
        else:
            if entry and not login and backend in ("openrouter", "xai", "fal", "deepinfra"):
                _share_key(service, entry["key_env"])
            data["media"][kind]["models"][name] = {
                "vendor": entry["id"] if entry else None, "backend": backend, "provider": service, "model": model,
                **({"price": checked_price} if checked_price else {})}
            if not data["media"][kind]["active"]:
                data["media"][kind]["active"] = name
        _write_json(_models_path(), data)
    apply_to_hermes()
    return {**models_payload(), "added": name}


def remove_preset(name: str) -> dict[str, Any]:
    with _LOCK:
        data = read_models()
        media_kind = next((k for k in MEDIA_KINDS if name in data["media"][k]["models"]), None)
        if media_kind:
            data["media"][media_kind]["models"].pop(name)
            if data["media"][media_kind]["active"] == name:
                data["media"][media_kind]["active"] = next(iter(data["media"][media_kind]["models"]), None)
            _write_json(_models_path(), data)
    if media_kind:
        apply_to_hermes()
        return models_payload()
    with _LOCK:
        data = read_models()
        if name not in data["presets"]:
            raise ValueError("这个模型已经不在了")
        if len(data["presets"]) == 1:
            raise ValueError("至少要留一个模型")
        data["presets"].pop(name)
        data["router"]["models"].pop(name, None)
        data["fallbacks"] = [n for n in data["fallbacks"] if n != name]
        if data["router"]["fixed"] == name:
            data["router"]["fixed"] = None
        if data["default"] == name:
            data["default"] = next(iter(data["presets"]))
        _write_json(_models_path(), data)
        health = read_health()
        if health.pop(name, None) is not None:
            _write_json(_health_path(), health)
    apply_to_hermes()
    return models_payload()


# ---------- Hermes config ----------


def _provider_key(name: str) -> str:
    ascii_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    if ascii_slug and all(ord(ch) < 128 for ch in name):
        return f"{_PROVIDER_PREFIX}{ascii_slug}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    if ascii_slug:
        return f"{_PROVIDER_PREFIX}{ascii_slug[:24]}-{digest}"
    return f"{_PROVIDER_PREFIX}{digest}"


def runtime_provider(name: str, preset: dict[str, Any]) -> str:
    """The Hermes provider a model is called through: its own entry (``m-<name>``), or for a
    subscription (``auth``) Hermes's signed-in provider itself (openai-codex, xai-oauth)."""
    return preset["provider"] if preset.get("auth") else _provider_key(name)


def chat_preset(data: dict[str, Any]) -> Optional[str]:
    fixed = data["router"]["fixed"]
    if not data["router"]["enabled"] and fixed in data["presets"]:
        return fixed
    return data["default"] if data["default"] in data["presets"] else next(iter(data["presets"]), None)


def quick_model(data: dict[str, Any], names: list[str]) -> Optional[str]:
    """For the small calls that hold up a reply (how to present it, titles, summarizing the chat):
    the quickest model that works, by its last measured answer time; among those within a second
    of each other, the cheaper. Without measurements, the cheapest."""
    health = read_health()
    usable = [n for n in names if health.get(n, {}).get("ok") is not False] or names
    timed = [n for n in usable if health.get(n, {}).get("latency_ms")]
    if not timed:
        return _pick(data, usable, cheapest=True)
    fastest = min(health[n]["latency_ms"] for n in timed)
    close = [n for n in timed if health[n]["latency_ms"] <= fastest + 1000]
    return _pick(data, close, cheapest=True)


def _pick(data: dict[str, Any], names: list[str], *, cheapest: bool) -> Optional[str]:
    usable = [n for n in names if read_health().get(n, {}).get("ok") is not False] or names
    rank = {tier: i for i, tier in enumerate(TIERS)}
    ordered = sorted(usable, key=lambda n: rank[_info(data, n)["tier"]], reverse=not cheapest)
    return ordered[0] if ordered else None


def hermes_settings(data: dict[str, Any], services: dict[str, Any]) -> dict[str, Any]:
    """The Hermes config the presets stand for (pure: easy to check)."""
    providers: dict[str, Any] = {}
    reasoning: dict[str, str] = {}
    for name, preset in data["presets"].items():
        if preset.get("auth"):
            if preset.get("reasoning_effort"):
                reasoning[preset["model"]] = preset["reasoning_effort"]
            continue
        service = services.get(preset["provider"])
        if not isinstance(service, dict):
            continue
        entry = {k: service[k] for k in ("base_url", "key_env", "api_key", "api_mode") if service.get(k)}
        entry["name"] = name
        entry["model"] = preset["model"]
        if preset.get("context_window_tokens"):
            entry["context_length"] = int(preset["context_window_tokens"])
        extra ={k: preset[k] for k in ("max_tokens", "temperature") if preset.get(k) is not None}
        if extra:
            entry["extra_body"] = extra
        providers[_provider_key(name)] = entry
        if preset.get("reasoning_effort"):
            reasoning[preset["model"]] = preset["reasoning_effort"]

    def slot(name: Optional[str]) -> dict[str, str]:
        if not name:
            return {}
        preset = data["presets"][name]
        return {"provider": runtime_provider(name, preset), "model": preset["model"]}

    chat = chat_preset(data)
    available = [n for n, preset in data["presets"].items() if preset.get("auth") or _provider_key(n) in providers]
    auxiliary: dict[str, dict[str, str]] = {}
    if data["router"]["enabled"] and chat in available:
        cheap = quick_model(data, available)
        if cheap and cheap != chat:
            for name in _CHEAP_SLOTS:
                auxiliary[name] = slot(cheap)
        if not _info(data, chat)["vision"]:
            seeing = _pick(data, [n for n in available if _info(data, n)["vision"]], cheapest=False)
            if seeing:
                auxiliary["vision"] = slot(seeing)
    return {
        "providers": providers,
        "model": slot(chat) if chat in available else {},
        "fallback_providers": [slot(n) for n in data["fallbacks"] if n in available and n != chat],
        "reasoning_overrides": reasoning,
        "max_turns": data["max_tool_iterations"],
        "auxiliary": auxiliary,
    }


def apply_to_hermes(timezone: Optional[str] = None) -> bool:
    """Write the presets (and the companion's other Hermes settings) into config.yaml — only the
    parts the app owns. True if anything changed."""
    from muse.config_store import read_raw_config, save_config

    config = read_raw_config()
    before = json.dumps(config, sort_keys=True, default=str)
    if timezone:
        # Scheduled tasks (早报, 主动关心) run on the phone's clock.
        config["timezone"] = timezone
    # Scheduled replies arrive as the companion's own messages, without Hermes's header.
    cron = config.setdefault("cron", {})
    cron["wrap_response"] = False
    # Its note on failed file edits is for coding sessions; the app never shows raw file work.
    display = config.setdefault("display", {})
    display["file_mutation_verifier"] = False
    # A reply's wait grows with the history sent each turn (about 2 s at 20K tokens, 3–8 s past
    # 50K), mostly old tool output (a page of quotes, a search). Hermes trims that without a model
    # call once the history passes PRUNE_AT_TOKENS — no stall, unlike summarizing the chat early.
    # A value the user set themselves stays.
    compression = config.setdefault("compression", {})
    if compression.get("threshold_tokens") == PRUNE_AT_TOKENS:
        compression.pop("threshold_tokens")  # an earlier version set this; it held replies up
    if compression.get("proactive_prune_tokens") in (None, 0, PRUNE_AT_TOKENS):
        compression["proactive_prune_tokens"] = PRUNE_AT_TOKENS
    data = read_models()
    if data["presets"]:
        _apply_presets(config, data)
    _apply_media(config, data)
    if json.dumps(config, sort_keys=True, default=str) == before:
        return False
    save_config(config)
    if timezone:
        from muse.time_utils import reset_cache

        reset_cache()
    return True


def _apply_presets(config: dict[str, Any], data: dict[str, Any]) -> None:
    services = {k: v for k, v in (config.get("providers") or {}).items() if not k.startswith(_PROVIDER_PREFIX)}
    wanted = hermes_settings(data, services)
    config["providers"] = {**services, **wanted["providers"]}
    if wanted["model"]:
        model = config.get("model") if isinstance(config.get("model"), dict) else {}
        config["model"] = {**model, "default": wanted["model"]["model"], "provider": wanted["model"]["provider"]}
    config["fallback_providers"] = wanted["fallback_providers"]
    agent = config.setdefault("agent", {})
    agent["reasoning_overrides"] = wanted["reasoning_overrides"]
    agent["max_turns"] = wanted["max_turns"]
    auxiliary = config.setdefault("auxiliary", {})
    for name in (*_CHEAP_SLOTS, "vision"):
        current = auxiliary.get(name) if isinstance(auxiliary.get(name), dict) else {}
        if name in wanted["auxiliary"]:
            auxiliary[name] = {**current, **wanted["auxiliary"][name]}
        elif str(current.get("provider", "")).startswith(_PROVIDER_PREFIX):
            # Ours from before: back to following the chat model.
            auxiliary[name] = {**current, "provider": "auto", "model": ""}


def media_settings(data: dict[str, Any], services: dict[str, Any]) -> dict[str, Optional[dict[str, Any]]]:
    """``image_gen`` / ``video_gen`` for the drawing and video models in use (pure: easy to check).
    None: nothing in use, so the companion's earlier setting is taken out again."""
    wanted: dict[str, Optional[dict[str, Any]]] = {}
    for kind, section in (("image", "image_gen"), ("video", "video_gen")):
        name = data["media"][kind]["active"]
        entry = data["media"][kind]["models"].get(name) if name else None
        if not entry:
            wanted[section] = None
            continue
        backend, model = entry["backend"], entry["model"]
        value: dict[str, Any] = {"provider": backend, "model": model, "managed_by": "companion"}
        # Drawing through an OpenAI-style service, and video straight from a company (the companion's
        # own backends, see video_backends): the service's address and key go with the model.
        if (kind, backend) == ("image", "openai") or backend.startswith("companion-"):
            service = services.get(entry["provider"]) or {}
            value[backend] = {"model": model, **{k: service[k] for k in ("base_url", "key_env") if service.get(k)}}
        elif kind == "image":
            value[backend] = {"model": model}
        wanted[section] = value
    return wanted


def _apply_media(config: dict[str, Any], data: dict[str, Any]) -> None:
    services = {k: v for k, v in (config.get("providers") or {}).items() if not k.startswith(_PROVIDER_PREFIX)}
    for section, value in media_settings(data, services).items():
        current = config.get(section) if isinstance(config.get(section), dict) else None
        if value is not None:
            config[section] = value
        elif current and current.get("managed_by") == "companion":
            config.pop(section)


# ---------- 用量 ----------


def _since(days: int, timezone: Optional[str]) -> float:
    zone = ZoneInfo(timezone) if timezone else None
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    return start.timestamp()


# A prompt the provider already has cached (the same opening as the turn before: instructions,
# tools, the conversation so far) is billed at about a tenth of fresh input (DeepSeek, Qwen, GLM).
CACHED_INPUT_RATE = 0.1


def _period(rows: list[tuple], data: dict[str, Any]) -> dict[str, Any]:
    """Per-model tokens and an estimated cost, plus tokens per source (chat, background, system).
    ``cached_tokens``: the part of the input that was a cache hit (billed for much less)."""
    by_model: dict[str, dict[str, Any]] = {}
    by_source: dict[str, int] = {}
    preset_of = {p["model"]: name for name, p in data["presets"].items()}
    for model, base_url, task, source, calls, tokens_in, tokens_out, cached in rows:
        entry = by_model.setdefault(model, {
            "provider": base_url or "", "model": model, "preset": preset_of.get(model),
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        })
        entry["calls"] += calls or 0
        entry["input_tokens"] += tokens_in or 0
        entry["output_tokens"] += tokens_out or 0
        entry["cached_tokens"] += cached or 0
        kind = "system" if task else ("cron" if source == "cron" else "user")
        by_source[kind] = by_source.get(kind, 0) + (tokens_in or 0) + (tokens_out or 0)
    total: Optional[float] = None
    for entry in by_model.values():
        # A subscription (会员账号登录) is paid for by the plan: nothing per token.
        entry["subscription"] = bool(entry["preset"] and (data["presets"].get(entry["preset"]) or {}).get("auth"))
        if entry["subscription"]:
            entry["cost"] = 0.0
            continue
        info = _info(data, entry["preset"]) if entry["preset"] else None
        if not info or info["price_in"] is None or info["price_out"] is None:
            entry["cost"] = None
            continue
        fresh = entry["input_tokens"] - entry["cached_tokens"]
        cached_price = info.get("price_cached") if info.get("price_cached") is not None else info["price_in"] * CACHED_INPUT_RATE
        entry["cost"] = round(fresh / 1e6 * info["price_in"] + entry["cached_tokens"] / 1e6 * cached_price
                              + entry["output_tokens"] / 1e6 * info["price_out"], 4)
        total = (total or 0.0) + entry["cost"]
    return {
        "by_model": sorted(by_model.values(), key=lambda e: -(e["input_tokens"] + e["output_tokens"])),
        "by_source": by_source,
        "cached_tokens": sum(e["cached_tokens"] for e in by_model.values()),
        "input_tokens": sum(e["input_tokens"] for e in by_model.values()),
        "cost": None if total is None else round(total, 4),
    }


# ---------- 用量 of pictures and videos ----------
# Token counts come from Hermes's own records; a picture or a clip is logged here as it is made
# (by a chat's tools or by the app itself: drawing a look, its motion), and priced from its model.

_MEDIA_LOCK = threading.Lock()


def _media_log() -> Path:
    return get_webui_dir() / "media-usage.jsonl"


def record_media(kind: str, *, seconds: Optional[float] = None, resolution: str = "", count: int = 1) -> None:
    """One picture or clip made with the active model of its kind."""
    try:
        data = read_models()
        name = data["media"][kind]["active"]
        entry = data["media"][kind]["models"].get(name) if name else None
        if not entry:
            return
        line = json.dumps({"t": time.time(), "kind": kind, "name": name, "model": entry["model"],
                           "seconds": seconds, "resolution": str(resolution or ""), "count": count}, ensure_ascii=False)
        with _MEDIA_LOCK:
            path = _media_log()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        logger.exception("companion: could not log a %s made", kind)


def _media_period(since: float, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pictures and clips made since then, per model, with what they cost (None without a price)."""
    from .model_catalog import vendor
    from .model_check import cost_of

    rows: dict[str, dict[str, Any]] = {}
    try:
        lines = _media_log().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or float(item.get("t") or 0) < since or item.get("kind") not in MEDIA_KINDS:
            continue
        name = str(item.get("name") or "")
        entry = data["media"][item["kind"]]["models"].get(name) or {}
        subscription = bool((vendor(entry.get("vendor") or "") or {}).get("login"))
        row = rows.setdefault(name, {"name": name, "kind": item["kind"], "model": item.get("model") or "",
                                     "count": 0, "seconds": 0.0, "cost": 0.0, "subscription": subscription})
        count = int(item.get("count") or 1)
        row["count"] += count
        row["seconds"] += float(item.get("seconds") or 0) * count
        cost = 0.0 if subscription else cost_of(entry.get("price"), count=count, seconds=item.get("seconds"),
                                                 resolution=item.get("resolution") or "")
        row["cost"] = None if cost is None or row["cost"] is None else round(row["cost"] + cost, 4)
    return sorted(rows.values(), key=lambda r: -r["count"])


def usage_summary(timezone: Optional[str] = None) -> dict[str, Any]:
    from muse.db import sync_from_legacy_state_db
    from muse.paths import get_muse_db_path
    from muse.runtime import use_shared_db

    data = read_models()
    periods = {label: _since(days, timezone) for label, days in (("today", 1), ("week", 7))}
    fetched: dict[str, list[tuple]] = {label: [] for label in periods}
    try:
        sync_from_legacy_state_db()
        muse_path = get_muse_db_path()
        if muse_path.is_file():
            with use_shared_db(db_path=muse_path) as db:
                for label, since in periods.items():
                    fetched[label] = db.query_usage_since(since)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.debug("companion: could not read usage from muse.db (%s)", type(exc).__name__)
    result = {}
    for label, since in periods.items():
        period = _period(fetched[label], data)
        period["media"] = _media_period(since, data)
        costs = [c for c in [period["cost"], *(r["cost"] for r in period["media"])] if c is not None]
        period["cost"] = round(sum(costs), 4) if costs else None
        result[label] = period
    return result


async def usage_summary_async(timezone: Optional[str] = None) -> dict[str, Any]:
    return await asyncio.to_thread(usage_summary, timezone)
