"""Ready-made model services for 添加模型: pick a company, paste its API key, pick a model.

Which models each company has, and what they can do (see pictures, think, how long a context,
the price), comes from models.dev — the open model registry Hermes itself keeps cached
(``agent.models_dev``), so the list follows new releases without an app update. This module only
adds what models.dev does not say: each company's OpenAI-compatible address, where to get a key,
its look, and the drawing and video models (and which Hermes backend runs them).

Kinds: ``text`` (the chat models), ``image`` (drawing: Hermes's ``image_gen``), ``video``
(``video_gen``).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

KINDS = ("text", "image", "video")
USD_TO_CNY = 7.2  # prices shown and stored in 元, like the ones typed in by hand
_RECOMMENDED = 3

# Groups in 添加模型, in this order.
GROUPS = {"sub": "用订阅账号登录", "cn": "国内", "global": "海外", "hub": "聚合平台"}


def _v(id: str, name: str, dev: str, base_url: str, key_env: str, console: str, domain: str, color: str,
       group: str, **extra: Any) -> dict[str, Any]:
    return {"id": id, "name": name, "dev": dev, "base_url": base_url, "key_env": key_env, "console": console,
            "domain": domain, "color": color, "group": group, **extra}


# id, name, models.dev provider, OpenAI-compatible address, key variable, where to get a key, look,
# group, and for drawing/video: the Hermes backend plus models models.dev does not list. A
# subscription (``login``) has no address or key: it signs in (see ``oauth_login``) and its models
# run on the Hermes provider named by ``provider``.
VENDORS: list[dict[str, Any]] = [
    # -- subscriptions --
    {"id": "chatgpt", "name": "ChatGPT 订阅", "dev": "openai", "login": "chatgpt", "provider": "openai-codex",
     "group": "sub", "domain": "chatgpt.com", "color": "#0D0D0D", "console": "",
     "note": "用 ChatGPT Plus / Pro 账号登录，按会员额度用，不用另外买 API",
     "image": {"backend": "openai-codex", "models": [
         {"id": "gpt-image-2-medium", "name": "GPT Image 2", "note": "会员额度内画图"},
         {"id": "gpt-image-2-high", "name": "GPT Image 2 高质量", "note": "更精细，更慢"}]}},
    {"id": "supergrok", "name": "SuperGrok 订阅", "dev": "xai", "login": "grok", "provider": "xai-oauth",
     "group": "sub", "domain": "grok.com", "color": "#111111", "console": "",
     "note": "用 SuperGrok 或 X Premium+ 账号登录，按会员额度用",
     "image": {"backend": "xai", "models": []}, "video": {"backend": "xai", "models": []}},
    # -- in China --
    _v("deepseek", "DeepSeek", "deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY",
       "https://platform.deepseek.com/api_keys", "deepseek.com", "#4D6BFE", "cn",
       pricing={"all": ["https://api-docs.deepseek.com/zh-cn/quick_start/pricing"]}),
    _v("volcengine", "火山方舟", "volcengine", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY",
       "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey", "volcengine.com", "#3370FF", "cn",
       image={"backend": "openai", "models": [
           {"id": "doubao-seedream-4-0-250828", "name": "Seedream 4.0", "note": "中文海报、写实照片都擅长"}]},
       video={"backend": "companion-ark", "models": [
           {"id": "doubao-seedance-2-0-260128", "name": "Seedance 2.0", "note": "字节的视频模型，电影感、带声音"},
           {"id": "doubao-seedance-2-0-fast-260128", "name": "Seedance 2.0 Fast", "note": "更快更便宜"},
           {"id": "doubao-seedance-2-0-mini-260615", "name": "Seedance 2.0 Mini", "note": "最便宜"}]},
       note="模型要先在方舟控制台「开通管理」里开通",
       pricing={"all": ["https://www.volcengine.com/docs/82379/1544106"]}),
    _v("alibaba", "阿里云百炼", "alibaba-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
       "https://bailian.console.aliyun.com/?tab=model#/api-key", "aliyun.com", "#FF6A00", "cn",
       video={"backend": "companion-dashscope", "models": [
           {"id": "wan3.0-video", "name": "万相 3.0", "note": "最长 30 秒、带声音，720P 约 0.6 元/秒"},
           {"id": "wan3.0-video-prime", "name": "万相 3.0 Prime", "note": "效果一样，出得更快"}]},
       pricing={"all": ["https://help.aliyun.com/zh/model-studio/model-pricing"]}),
    _v("moonshot", "Kimi", "moonshotai-cn", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY",
       "https://platform.moonshot.cn/console/api-keys", "moonshot.cn", "#16191E", "cn",
       pricing={"all": ["https://platform.moonshot.cn/docs/pricing/chat"]}),
    _v("zhipu", "智谱 GLM", "zhipuai", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY",
       "https://bigmodel.cn/usercenter/proj-mgmt/apikeys", "bigmodel.cn", "#3859FF", "cn",
       image={"backend": "openai", "models": [
           {"id": "cogview-4-250304", "name": "CogView-4", "note": "能写中文字"},
           {"id": "cogview-3-flash", "name": "CogView-3 Flash", "note": "免费"}]},
       pricing={"all": ["https://open.bigmodel.cn/pricing"]}),
    _v("minimax", "MiniMax", "minimax-cn", "https://api.minimaxi.com/v1", "MINIMAX_API_KEY",
       "https://platform.minimax.cn/user-center/basic-information/interface-key", "minimaxi.com", "#F23F5D", "cn",
       video={"backend": "companion-minimax", "models": [
           {"id": "MiniMax-H3", "name": "海螺 H3", "note": "MiniMax 最新的视频模型，带声音"},
           {"id": "MiniMax-H3-Lite", "name": "海螺 H3 Lite", "note": "便宜一半，适合短动作"},
           {"id": "MiniMax-H3-Max", "name": "海螺 H3 Max", "note": "效果最好"},
           {"id": "MiniMax-Hailuo-2.3", "name": "海螺 2.3", "note": "动作、表情细腻"}]},
       pricing={"all": ["https://platform.minimax.cn/docs/guides/pricing-paygo"]}),
    _v("stepfun", "阶跃星辰", "stepfun", "https://api.stepfun.com/v1", "STEPFUN_API_KEY",
       "https://platform.stepfun.com/interface-key", "stepfun.com", "#1D4ED8", "cn",
       pricing={"all": ["https://platform.stepfun.com/docs/pricing/details"]}),
    _v("xiaomi", "小米 MiMo", "xiaomi", "https://api.xiaomimimo.com/v1", "XIAOMI_API_KEY",
       "https://platform.xiaomimimo.com/#/console/api-keys", "xiaomimimo.com", "#FF6900", "cn"),
    _v("tencent", "腾讯混元", "tencent-tokenhub", "https://tokenhub.tencentmaas.com/v1", "TENCENT_TOKENHUB_API_KEY",
       "https://cloud.tencent.com/document/product/1823/130050", "cloud.tencent.com", "#0052D9", "cn"),
    _v("sensenova", "商汤日日新", "sensenova", "https://token.sensenova.cn/v1", "SENSENOVA_API_KEY",
       "https://platform.sensenova.cn/docs", "sensenova.cn", "#5B4BFF", "cn"),
    _v("longcat", "美团 LongCat", "longcat", "https://api.longcat.chat/openai", "LONGCAT_API_KEY",
       "https://longcat.chat/platform/api_keys", "longcat.chat", "#FFC300", "cn"),
    _v("modelscope", "魔搭 ModelScope", "modelscope", "https://api-inference.modelscope.cn/v1", "MODELSCOPE_API_KEY",
       "https://modelscope.cn/my/myaccesstoken", "modelscope.cn", "#624AFF", "cn",
       note="每天有免费额度"),
    # -- outside China --
    _v("openai", "OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
       "https://platform.openai.com/api-keys", "openai.com", "#0D0D0D", "global",
       image={"backend": "openai", "models": []},
       pricing={"all": ["https://platform.openai.com/docs/pricing"]}),
    _v("anthropic", "Claude", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY",
       "https://console.anthropic.com/settings/keys", "anthropic.com", "#D97757", "global",
       pricing={"all": ["https://docs.claude.com/en/docs/about-claude/pricing"]}),
    _v("google", "Gemini", "google", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
       "https://aistudio.google.com/apikey", "gemini.google.com", "#1A73E8", "global",
       pricing={"all": ["https://ai.google.dev/gemini-api/docs/pricing"]}),
    _v("xai", "xAI Grok", "xai", "https://api.x.ai/v1", "XAI_API_KEY",
       "https://console.x.ai", "x.ai", "#111111", "global",
       image={"backend": "xai", "models": []}, video={"backend": "xai", "models": []},
       pricing={"all": ["https://docs.x.ai/docs/models"]}),
    _v("mistral", "Mistral", "mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY",
       "https://console.mistral.ai/api-keys", "mistral.ai", "#FA520F", "global",
       pricing={"all": ["https://mistral.ai/pricing"]}),
    _v("perplexity", "Perplexity", "perplexity-agent", "https://api.perplexity.ai/v1", "PERPLEXITY_API_KEY",
       "https://www.perplexity.ai/account/api/keys", "perplexity.ai", "#1F7A8C", "global",
       note="自带联网搜索"),
    _v("cohere", "Cohere", "cohere", "https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY",
       "https://dashboard.cohere.com/api-keys", "cohere.com", "#39594D", "global"),
    _v("groq", "Groq", "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
       "https://console.groq.com/keys", "groq.com", "#F55036", "global", note="开源模型，速度极快"),
    _v("nvidia", "NVIDIA", "nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
       "https://build.nvidia.com/settings/api-keys", "nvidia.com", "#76B900", "global", note="有免费额度"),
    _v("together", "Together AI", "togetherai", "https://api.together.xyz/v1", "TOGETHER_API_KEY",
       "https://api.together.ai/settings/api-keys", "together.ai", "#0F6FFF", "global"),
    _v("fireworks", "Fireworks", "fireworks-ai", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY",
       "https://fireworks.ai/account/api-keys", "fireworks.ai", "#5019C5", "global"),
    _v("deepinfra", "DeepInfra", "deepinfra", "https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY",
       "https://deepinfra.com/dash/api_keys", "deepinfra.com", "#2A6CF6", "global",
       video={"backend": "deepinfra", "models": []}),
    _v("ollama", "Ollama Cloud", "ollama-cloud", "https://ollama.com/v1", "OLLAMA_API_KEY",
       "https://ollama.com/settings/keys", "ollama.com", "#111111", "global"),
    # -- one key, many companies --
    _v("openrouter", "OpenRouter", "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
       "https://openrouter.ai/settings/keys", "openrouter.ai", "#6467F2", "hub",
       image={"backend": "openrouter", "models": []},
       video={"backend": "openrouter", "models": [
           {"id": "minimax/hailuo-3-max", "name": "海螺 Hailuo 3 Max", "note": "MiniMax 的视频模型"}]},
       note="一个 key 就能用几百个模型，包括可灵、海螺、Seedance、Veo 等视频模型"),
    {"id": "fal", "name": "fal", "dev": "", "base_url": "https://fal.run", "key_env": "FAL_KEY",
     "console": "https://fal.ai/dashboard/keys", "domain": "fal.ai", "color": "#111111", "group": "hub",
     "media_only": True, "note": "只画图和做视频：海螺 H3、Seedance、万相、可灵、Veo 都能用一个 key",
     "image": {"backend": "fal", "models": []}, "video": {"backend": "fal", "models": [
         {"id": "minimax-h3", "name": "海螺 H3", "note": "MiniMax 最新，原生 2K、带声音"},
         {"id": "seedance-2.0", "name": "Seedance 2.0", "note": "字节，电影感、口型同步"},
         {"id": "seedance-2.5", "name": "Seedance 2.5", "note": "字节旗舰，一次 30 秒"},
         {"id": "wan-3.0", "name": "万相 3.0", "note": "阿里，2~30 秒、带声音"},
         {"id": "kling-v3", "name": "可灵 3.0", "note": "快手，动作自然"},
         {"id": "veo3.1", "name": "Veo 3.1", "note": "Google，电影感、带声音"}]}},
    _v("siliconflow", "硅基流动", "siliconflow-cn", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY",
       "https://cloud.siliconflow.cn/account/ak", "siliconflow.cn", "#7C3AED", "hub"),
    _v("302ai", "302.AI", "302ai", "https://api.302.ai/v1", "COMPANION_302AI_KEY",
       "https://dash.302.ai/apis/list", "302.ai", "#8E47F0", "hub", note="国内可直接付款，各家模型都有"),
    _v("aihubmix", "AIHubMix", "aihubmix", "https://aihubmix.com/v1", "AIHUBMIX_API_KEY",
       "https://aihubmix.com/token", "aihubmix.com", "#2563EB", "hub", note="国内可直接付款，各家模型都有"),
    _v("novita", "Novita AI", "novita-ai", "https://api.novita.ai/openai", "NOVITA_API_KEY",
       "https://novita.ai/settings/key-management", "novita.ai", "#22C55E", "hub"),
]
CUSTOM = {"id": "custom", "name": "其他服务", "kinds": ["text", "image"], "group": "hub",
          "note": "任何兼容 OpenAI 接口的服务：填地址、key 和模型 ID"}


def vendor(vendor_id: str) -> Optional[dict[str, Any]]:
    return next((v for v in VENDORS if v["id"] == vendor_id), None)


def vendor_for_base_url(base_url: str) -> Optional[dict[str, Any]]:
    url = (base_url or "").rstrip("/")
    return next((v for v in VENDORS if v.get("base_url") and v["base_url"].rstrip("/") == url), None)


def vendor_for_login(provider: str) -> Optional[dict[str, Any]]:
    """The subscription whose models run on this Hermes provider (openai-codex, xai-oauth)."""
    return next((v for v in VENDORS if v.get("provider") == provider), None)


def kinds_of(entry: dict[str, Any]) -> list[str]:
    return [*([] if entry.get("media_only") else ["text"]), *(kind for kind in ("image", "video") if kind in entry)]


def _backend_models(kind: str, backend: str) -> list[dict[str, Any]]:
    """The models one of Hermes's drawing/video backends lists itself (fal's families, DeepInfra's
    live catalog); empty when it cannot be loaded."""
    import importlib

    try:
        module = importlib.import_module(f"plugins.{kind}_gen.{backend}")
        provider = next(v() for v in vars(module).values()
                        if isinstance(v, type) and v.__module__ == module.__name__ and hasattr(v, "list_models")
                        and not getattr(v, "__abstractmethods__", None))
        rows = provider.list_models()
    except Exception as exc:
        logger.info("companion: %s %s models unavailable (%s)", backend, kind, exc)
        return []
    return [{"id": str(row["id"]), "name": str(row.get("display") or row["id"]),
             "note": str(row.get("strengths") or "")[:60]} for row in rows if isinstance(row, dict) and row.get("id")]


def _registry() -> dict[str, Any]:
    try:
        from agent.models_dev import fetch_models_dev

        return fetch_models_dev(allow_network=False) or fetch_models_dev(allow_network=True) or {}
    except Exception as exc:
        logger.info("companion: model registry unavailable (%s)", exc)
        return {}


def _price(cost: Any, key: str) -> Optional[float]:
    value = cost.get(key) if isinstance(cost, dict) else None
    return round(float(value) * USD_TO_CNY, 2) if isinstance(value, (int, float)) else None


def tier_for(price_in: Optional[float]) -> str:
    """Rough class by input price (元/百万 token): a hint for 自动选择, editable later."""
    if price_in is None:
        return "balanced"
    return "cheap" if price_in <= 2 else "strong" if price_in >= 15 else "balanced"


def _entry(model_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    modalities = raw.get("modalities") or {}
    limit = raw.get("limit") or {}
    price_in, price_out = _price(raw.get("cost"), "input"), _price(raw.get("cost"), "output")
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "note": " ".join(str(raw.get("description") or "").split())[:80],
        "vision": "image" in (modalities.get("input") or []),
        "reasoning": bool(raw.get("reasoning")),
        "context": limit.get("context"),
        "price_in": price_in,
        "price_out": price_out,
        "tier": tier_for(price_in),
        "released": raw.get("release_date") or "",
    }


def _outputs(raw: dict[str, Any]) -> list[str]:
    return list((raw.get("modalities") or {}).get("output") or [])


def _registry_models(dev_id: str, kind: str) -> list[dict[str, Any]]:
    models = ((_registry().get(dev_id) or {}).get("models") or {})
    found = []
    for model_id, raw in models.items():
        if not isinstance(raw, dict) or raw.get("status") == "deprecated":
            continue
        outputs = _outputs(raw)
        if kind == "text":
            # The companion works with tools, so a chat model must call them.
            if outputs != ["text"] or not raw.get("tool_call"):
                continue
        elif kind not in outputs:
            continue
        found.append(_entry(model_id, raw))
    found.sort(key=lambda m: m["released"], reverse=True)
    return found


def _openrouter_videos() -> list[dict[str, Any]]:
    """OpenRouter's video models, live from its public catalog (no key needed)."""
    import httpx

    try:
        response = httpx.get("https://openrouter.ai/api/v1/videos/models", timeout=8)
        response.raise_for_status()
        rows = response.json().get("data") or []
    except Exception as exc:
        logger.info("companion: OpenRouter video catalog unavailable (%s)", exc)
        return []
    return [{"id": row["id"], "name": row.get("name") or row["id"],
             "note": " ".join(str(row.get("description") or "").split())[:80]}
            for row in rows if isinstance(row, dict) and row.get("id")]


def vendor_models(vendor_id: str, kind: str) -> dict[str, Any]:
    """The models a company offers for one kind: recommended ones first (the newest, one per
    family), then the rest. Empty when the company has none of that kind."""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")
    entry = vendor(vendor_id)
    if entry is None:
        raise ValueError("没有这个服务商")
    models = _registry_models(entry["dev"], kind)
    if entry.get("login") == "chatgpt" and kind == "text":
        from .oauth_login import models as plan_models

        known = {m["id"]: m for m in _registry_models("openai", "text")}
        models = [known.get(model_id) or {"id": model_id, "name": model_id, "note": "", "vision": True,
                                          "reasoning": True, "context": None, "tier": "balanced", "released": ""}
                  for model_id in plan_models("chatgpt")]
    if entry.get("login"):
        models = [{**m, "price_in": None, "price_out": None} for m in models]
    if kind != "text":
        # The hand-picked ones first, then the registry's, then a live catalog; each model once.
        blank = {"vision": False, "reasoning": False, "context": None, "price_in": None, "price_out": None,
                 "tier": "balanced", "released": ""}
        curated = [{**blank, **m} for m in (entry.get(kind) or {}).get("models") or []]
        backend = (entry.get(kind) or {}).get("backend")
        live = ([{**blank, **m} for m in _openrouter_videos()] if (kind, vendor_id) == ("video", "openrouter")
                else [{**blank, **m} for m in _backend_models(kind, backend)] if backend in ("fal", "deepinfra") else [])
        seen: set[str] = set()
        models = [m for m in [*curated, *models, *live] if not (m["id"] in seen or seen.add(m["id"]))]
    for model in models:
        # One shape for every price (see model_check): the registry's are per million tokens.
        model.setdefault("price", {"unit": "mtok", "rows": [{"spec": "", "input": model["price_in"],
                                                             "output": model["price_out"], "cached": None}]}
                         if model.get("price_in") is not None and model.get("price_out") is not None else None)
    models, found = _with_checked(vendor_id, kind, models)
    recommended, families = [], set()
    for model in models:
        family = model["id"].rsplit("-", 1)[0] if kind == "text" else model["id"]
        if family in families:
            continue
        families.add(family)
        recommended.append(model["id"])
        if len(recommended) == _RECOMMENDED:
            break
    for model in models:
        model["recommended"] = model["id"] in recommended
    models.sort(key=lambda m: not m["recommended"])
    return {"vendor": vendor_id, "kind": kind, "models": models,
            "checked": {"at": found["checked_at"], "sources": found["sources"]} if found else None,
            "can_check": not entry.get("login")}


def _with_checked(vendor_id: str, kind: str, models: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[dict]]:
    """The list as the company's platform showed it at the last 检测: its models first, with their
    current IDs and prices; then the others (a drawing/video model it no longer lists is marked)."""
    from .model_check import checked, text_prices

    found = checked(vendor_id, kind)
    if not found:
        return models, None
    known = {m["id"]: m for m in models}
    blank = {"note": "", "vision": False, "reasoning": False, "context": None, "tier": "balanced", "released": ""}
    fresh = []
    for item in found["models"]:
        base = known.get(item["id"]) or {**blank, "id": item["id"], "name": item["name"]}
        price = item.get("price") or base.get("price")
        price_in, price_out, _ = text_prices(price)
        fresh.append({**base, "note": base.get("note") or item.get("note") or "", "price": price, "checked": True,
                      "price_in": price_in, "price_out": price_out,
                      **({"tier": tier_for(price_in)} if kind == "text" and item["id"] not in known else {})})
    listed = {m["id"] for m in fresh}
    rest = [{**m, **({"missing": True} if kind != "text" else {})} for m in models if m["id"] not in listed]
    return fresh + rest, found


def model_facts(vendor_id: str, model_id: str) -> Optional[dict[str, Any]]:
    """What the registry knows of one model (to fill in vision, price and context on adding it)."""
    entry = vendor(vendor_id)
    raw = (((_registry().get(entry["dev"]) or {}).get("models") or {}).get(model_id)) if entry else None
    return _entry(model_id, raw) if isinstance(raw, dict) else None


def catalog_payload(saved_keys: set[str]) -> dict[str, Any]:
    """The companies for 添加模型 by group, with which kinds each offers and whether its key is
    saved (a subscription: whether it is signed in)."""
    from .oauth_login import local_login, signed_in

    def ready(v: dict[str, Any]) -> bool:
        return signed_in(v["login"]) if v.get("login") else v["base_url"].rstrip("/") in saved_keys

    return {
        "groups": [{"id": key, "name": name} for key, name in GROUPS.items()],
        "vendors": [
            {"id": v["id"], "name": v["name"], "kinds": kinds_of(v), "console": v["console"], "group": v["group"],
             "login": v.get("login"), "local_login": bool(v.get("login")) and local_login(v["login"]), "logo_url": f"https://www.google.com/s2/favicons?domain={v['domain']}&sz=64",
             "color": v["color"], "note": v.get("note", ""), "has_key": ready(v)}
            for v in VENDORS
        ] + [{**CUSTOM, "console": "", "login": None, "logo_url": None, "color": None, "has_key": False}],
    }
