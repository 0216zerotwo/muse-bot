"""Image generation subsystem: 5 backends (openai, openai-codex, xai, openrouter, fal)."""

from __future__ import annotations

import abc
import base64
import json
import logging
import os
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from muse.config_store import load_config
from muse.media._common import (
    DEFAULT_OPENROUTER_BASE_URL,
    codex_headers,
    fetch_url_bytes,
    get_secret,
    image_to_data_url,
    load_media_section,
    load_source_image_bytes,
    resolve_codex_credentials,
    resolve_xai_credentials,
    save_b64_media,
    save_bytes_media,
)

logger = logging.getLogger(__name__)

_IMAGE_CACHE_SUBDIR = "generated/images"
DEFAULT_ASPECT_RATIO = "landscape"
_FAL_QUEUE_ORIGIN = "https://queue.fal.run"

_GPT_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
_XAI_ASPECTS = {"square": "1:1", "landscape": "16:9", "portrait": "9:16", "4:3": "4:3", "3:4": "3:4"}
_OR_ASPECTS = {"square": "1:1", "landscape": "16:9", "portrait": "9:16"}
_GPT_TIERS = {
    "gpt-image-2-low": ("gpt-image-2", "low"),
    "gpt-image-2-medium": ("gpt-image-2", "medium"),
    "gpt-image-2-high": ("gpt-image-2", "high"),
    "gpt-image-2": ("gpt-image-2", "medium"),
}
_OPENROUTER_IMAGE_API_MODELS = frozenset({
    "google/gemini-3.1-flash-lite-image",
    "google/gemini-3.1-flash-image",
    "openai/gpt-image-2",
    "openai/gpt-image-1-mini",
    "microsoft/mai-image-2.5",
    "microsoft/mai-image-2.5-pro",
    "x-ai/grok-imagine-image-quality",
    "krea/krea-2-medium",
    "krea/krea-2-medium-turbo",
    "qwen/qwen-image-3-pro",
})


def resolve_aspect_ratio(value: Optional[str]) -> str:
    raw = str(value or DEFAULT_ASPECT_RATIO).strip().lower()
    if raw in ("square", "1:1"):
        return "square"
    if raw in ("portrait", "9:16", "2:3", "3:4"):
        return "portrait"
    return "landscape"


def save_b64_image(b64_data: str, *, prefix: str = "image", extension: str = "png") -> Path:
    return save_b64_media(_IMAGE_CACHE_SUBDIR, b64_data, prefix=prefix, extension=extension)


def save_url_image(url: str, *, prefix: str = "image") -> Path:
    raw, headers = fetch_url_bytes(url, max_bytes=30 * 1024 * 1024, timeout=60.0)
    ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    return save_bytes_media(_IMAGE_CACHE_SUBDIR, raw, prefix=prefix, extension=ext_map.get(ctype, "png"))


def image_success(
    *, image: str, model: str, prompt: str, aspect_ratio: str, provider: str,
    modality: str = "text", extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": True, "image": image, "model": model, "prompt": prompt,
        "aspect_ratio": aspect_ratio, "provider": provider, "modality": modality,
    }
    for k, v in (extra or {}).items():
        out.setdefault(k, v)
    return out


def image_error(
    *, error: str, error_type: str = "api_error", provider: str = "", model: str = "",
    prompt: str = "", aspect_ratio: str = DEFAULT_ASPECT_RATIO,
) -> dict[str, Any]:
    return {
        "success": False, "image": None, "error": error, "error_type": error_type,
        "provider": provider, "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio,
    }


def _collect_sources(image_url: Optional[str], refs: Optional[list[str]], limit: int = 16) -> list[str]:
    out = [image_url.strip()] if isinstance(image_url, str) and image_url.strip() else []
    out.extend(r.strip() for r in (refs or ()) if isinstance(r, str) and r.strip())
    return out[:limit]


def _err_summary(text: str, limit: int = 400) -> str:
    try:
        err = (json.loads(text) or {}).get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"].strip()[:limit]
        if isinstance(err, str) and err.strip():
            return err.strip()[:limit]
    except Exception:
        pass
    return str(text or "")[:limit]


def _materialize(b64_val: Any, url_val: Any, *, prefix: str) -> Optional[str]:
    if isinstance(b64_val, str) and b64_val.strip():
        return str(save_b64_image(b64_val.strip(), prefix=prefix))
    if isinstance(url_val, str) and url_val.strip():
        try:
            return str(save_url_image(url_val.strip(), prefix=prefix))
        except Exception as exc:
            logger.debug("muse.media.image_gen: cache failed (%s)", type(exc).__name__)
            return url_val.strip()
    return None


def _as_url_or_data(ref: str) -> str:
    return ref if ref.lower().startswith(("http://", "https://", "data:")) else image_to_data_url(ref)


def _png_size(raw: bytes) -> Optional[str]:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", raw[16:24])
    return f"{w}x{h}"


def _fal_catalog() -> tuple[dict[str, dict[str, Any]], str]:
    try:
        from muse.media.catalog import DEFAULT_FAL_IMAGE_MODEL, FAL_IMAGE_MODELS

        return FAL_IMAGE_MODELS, DEFAULT_FAL_IMAGE_MODEL
    except ImportError:
        default_id = "fal-ai/flux-2/klein/9b"
        return {
            default_id: {
                "size_style": "image_size_preset",
                "sizes": {"landscape": "landscape_16_9", "square": "square_hd", "portrait": "portrait_16_9"},
                "defaults": {"num_inference_steps": 4, "output_format": "png"},
                "supports": {"prompt", "image_size", "num_inference_steps", "seed", "output_format"},
                "edit_endpoint": "fal-ai/flux-2/klein/9b/edit",
                "edit_supports": {"prompt", "image_urls", "num_inference_steps", "seed", "output_format"},
            }
        }, default_id


def _pick_model(section_key: str, env_var: str, explicit: Optional[str], default: str) -> str:
    sec = load_media_section("image_gen")
    sub = sec.get(section_key) if isinstance(sec.get(section_key), dict) else {}
    for cand in (explicit, os.environ.get(env_var), sub.get("model"), sec.get("model"), default):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return default


class ImageGenProvider(abc.ABC):
    name: str = ""
    display_name: str = ""

    def _fail(
        self, msg: str, kind: str = "api_error", *,
        model: str = "", prompt: str = "", aspect: str = DEFAULT_ASPECT_RATIO,
    ):
        return image_error(
            error=msg, error_type=kind, provider=self.name, model=model, prompt=prompt, aspect_ratio=aspect
        )

    @abc.abstractmethod
    def is_available(self) -> bool: ...

    @abc.abstractmethod
    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]: ...


class OpenAIImageGenProvider(ImageGenProvider):
    name, display_name = "openai", "OpenAI"

    def _endpoint(self) -> tuple[str, str]:
        sec = load_media_section("image_gen")
        cfg = sec.get("openai") if isinstance(sec.get("openai"), dict) else {}
        named = str(cfg.get("provider") or "").strip()
        n_base, n_key = "", ""
        if named:
            entry = (load_config().get("providers") or {}).get(named)
            if isinstance(entry, dict):
                n_base = str(entry.get("base_url") or "").strip().rstrip("/")
                ke = str(entry.get("key_env") or "").strip()
                n_key = str(entry.get("api_key") or "").strip() or (get_secret(ke) if ke else "")
        base = (
            str(cfg.get("base_url") or "").strip().rstrip("/")
            or n_base
            or get_secret("OPENAI_BASE_URL").rstrip("/")
            or "https://api.openai.com/v1"
        )
        ke = str(cfg.get("key_env") or "").strip()
        key = (
            (get_secret(ke) if ke else "")
            or str(cfg.get("api_key") or "").strip()
            or n_key
            or get_secret("OPENAI_API_KEY")
        )
        return base, key

    def is_available(self) -> bool:
        return bool(self._endpoint()[1])

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        prompt, aspect = str(prompt or "").strip(), resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return self._fail("prompt is required", "invalid_request", aspect=aspect)
        base_url, api_key = self._endpoint()
        if not api_key:
            return self._fail("OPENAI_API_KEY not set", "auth_required", aspect=aspect)
        tier_id = _pick_model("openai", "OPENAI_IMAGE_MODEL", model, "gpt-image-2-medium")
        api_model, quality = _GPT_TIERS.get(tier_id, (tier_id, None))
        size = _GPT_SIZES.get(aspect, "1024x1024")
        sources = _collect_sources(image_url, reference_image_urls, 16)
        headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Project": ""}
        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0)) as client:
                if sources:
                    files = [
                        ("image[]" if len(sources) > 1 else "image", (fn, raw, mime))
                        for raw, fn, mime in (load_source_image_bytes(r) for r in sources)
                    ]
                    form = {"model": api_model, "prompt": prompt, "size": size, "n": "1"}
                    if quality:
                        form["quality"] = quality
                    resp = client.post(f"{base_url}/images/edits", headers=headers, data=form, files=files)
                else:
                    body: dict[str, Any] = {"model": api_model, "prompt": prompt, "size": size, "n": 1}
                    if quality:
                        body["quality"] = quality
                    resp = client.post(
                        f"{base_url}/images/generations",
                        headers={**headers, "Content-Type": "application/json"},
                        json=body,
                    )
            if resp.status_code >= 400:
                return self._fail(
                    f"OpenAI HTTP {resp.status_code}: {_err_summary(resp.text)}",
                    model=tier_id, prompt=prompt, aspect=aspect,
                )
            data = (resp.json() or {}).get("data") or []
        except Exception as exc:
            return self._fail(f"OpenAI image failed: {exc}", model=tier_id, prompt=prompt, aspect=aspect)
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        ref = _materialize(first.get("b64_json"), first.get("url"), prefix=f"openai_{tier_id}")
        if not ref:
            return self._fail(
                "OpenAI returned no image data", "empty_response",
                model=tier_id, prompt=prompt, aspect=aspect,
            )
        return image_success(
            image=ref, model=tier_id, prompt=prompt, aspect_ratio=aspect, provider=self.name,
            modality="image" if sources else "text",
            extra={"size": size, **({"quality": quality} if quality else {})},
        )


class OpenAICodexImageGenProvider(ImageGenProvider):
    """ChatGPT/Codex OAuth native ``{codex_base_url}/images/{generations,edits}`` backend."""

    name, display_name = "openai-codex", "OpenAI (Codex auth)"

    def is_available(self) -> bool:
        return bool(resolve_codex_credentials()[0])

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        prompt, aspect = str(prompt or "").strip(), resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return self._fail("prompt is required", "invalid_request", aspect=aspect)
        token, base_url = resolve_codex_credentials()
        if not token:
            return self._fail("No Codex/ChatGPT OAuth credentials available.", "auth_required", aspect=aspect)
        tier_id = _pick_model("openai-codex", "OPENAI_IMAGE_MODEL", model, "gpt-image-2-medium")
        quality = (_GPT_TIERS.get(tier_id) or ("gpt-image-2", "medium"))[1]
        size = _GPT_SIZES.get(aspect, "1024x1024")
        sources = _collect_sources(image_url, reference_image_urls, 16)
        try:
            input_images = [{"image_url": image_to_data_url(ref)} for ref in sources]
        except Exception as exc:
            return self._fail(
                f"Invalid Codex image input: {exc}", "invalid_image_input",
                model=tier_id, prompt=prompt, aspect=aspect,
            )
        body: dict[str, Any] = {
            "prompt": prompt, "model": "gpt-image-2", "n": 1, "quality": quality,
            "size": size, "background": "opaque",
        }
        if input_images:
            body["images"] = input_images
            endpoint = f"{base_url}/images/edits"
        else:
            endpoint = f"{base_url}/images/generations"
        headers = {
            **codex_headers(token, base_url=base_url),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-codex-image-turn-id": str(uuid.uuid4()),
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), headers=headers) as client:
                resp = client.post(endpoint, json=body)
            if resp.status_code >= 400:
                return self._fail(
                    f"Codex images HTTP {resp.status_code}: {_err_summary(resp.text)}",
                    model=tier_id, prompt=prompt, aspect=aspect,
                )
            payload = resp.json()
            req_id = resp.headers.get("x-codex-imagegen-request-id")
        except Exception as exc:
            return self._fail(f"Codex image failed: {exc}", model=tier_id, prompt=prompt, aspect=aspect)
        data = payload.get("data") if isinstance(payload, dict) else None
        first_row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        b64 = first_row.get("b64_json") if first_row else None
        if not isinstance(b64, str) or not b64.strip():
            return self._fail(
                "Codex returned no image data", "empty_response",
                model=tier_id, prompt=prompt, aspect=aspect,
            )
        saved = save_b64_image(b64, prefix=f"openai_codex_{tier_id}")
        return image_success(
            image=str(saved), model=tier_id, prompt=prompt, aspect_ratio=aspect, provider=self.name,
            modality="image" if input_images else "text",
            extra={
                "size": size, "quality": quality, "input_image_count": len(input_images),
                "pixel_size": _png_size(base64.b64decode(b64)),
                "reported_quality": payload.get("quality"), "reported_size": payload.get("size"),
                "imagegen_request_id": req_id,
            },
        )


class XAIImageGenProvider(ImageGenProvider):
    name, display_name = "xai", "xAI (Grok)"

    def is_available(self) -> bool:
        return bool(resolve_xai_credentials().get("api_key"))

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        prompt, aspect = str(prompt or "").strip(), resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return self._fail("prompt is required", "invalid_request", aspect=aspect)
        creds = resolve_xai_credentials()
        api_key, base_url = creds.get("api_key", ""), creds.get("base_url", "https://api.x.ai/v1")
        if not api_key:
            return self._fail("No xAI credentials found.", "auth_required", aspect=aspect)
        sources = _collect_sources(image_url, reference_image_urls, 3)
        is_edit = bool(sources)
        default_m = "grok-imagine-image-quality" if is_edit else "grok-imagine-image"
        model_id = _pick_model("xai", "XAI_IMAGE_MODEL", model, default_m)
        headers = {
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "User-Agent": "muse-bot/2.0",
        }
        try:
            if is_edit:
                imgs = [{"url": _as_url_or_data(r), "type": "image_url"} for r in sources]
                body: dict[str, Any] = {"model": model_id, "prompt": prompt}
                body["image" if len(imgs) == 1 else "images"] = imgs[0] if len(imgs) == 1 else imgs
                endpoint = f"{base_url}/images/edits"
            else:
                body = {
                    "model": model_id, "prompt": prompt,
                    "aspect_ratio": _XAI_ASPECTS.get(aspect, "1:1"), "resolution": "1k",
                }
                endpoint = f"{base_url}/images/generations"
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(endpoint, headers=headers, json=body)
            if resp.status_code >= 400:
                return self._fail(
                    f"xAI images HTTP {resp.status_code}: {_err_summary(resp.text)}",
                    model=model_id, prompt=prompt, aspect=aspect,
                )
            data = (resp.json() or {}).get("data") or []
        except Exception as exc:
            return self._fail(f"xAI image failed: {exc}", model=model_id, prompt=prompt, aspect=aspect)
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        ref = _materialize(first.get("b64_json"), first.get("url"), prefix=f"xai_{model_id}")
        if not ref:
            return self._fail(
                "xAI returned no image data", "empty_response",
                model=model_id, prompt=prompt, aspect=aspect,
            )
        return image_success(
            image=ref, model=model_id, prompt=prompt, aspect_ratio=aspect, provider=self.name,
            modality="image" if is_edit else "text",
        )


class OpenRouterImageGenProvider(ImageGenProvider):
    name, display_name = "openrouter", "OpenRouter"

    def _creds(self) -> tuple[str, str]:
        base = (get_secret("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")
        return get_secret("OPENROUTER_API_KEY"), base

    def is_available(self) -> bool:
        return bool(self._creds()[0])

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        prompt, aspect = str(prompt or "").strip(), resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return self._fail("prompt is required", "invalid_request", aspect=aspect)
        api_key, base_url = self._creds()
        if not api_key:
            return self._fail("OPENROUTER_API_KEY not set", "auth_required", aspect=aspect)
        model_id = _pick_model("openrouter", "OPENROUTER_IMAGE_MODEL", model, "openai/gpt-5.4-image-2")
        sources = _collect_sources(image_url, reference_image_urls, 16)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Muse Bot",
        }
        ratio = _OR_ASPECTS.get(aspect, "1:1")
        use_images = model_id in _OPENROUTER_IMAGE_API_MODELS
        try:
            ref_urls = [_as_url_or_data(r) for r in sources]
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
                if use_images:
                    body: dict[str, Any] = {"model": model_id, "prompt": prompt, "aspect_ratio": ratio}
                    if ref_urls:
                        body["input_references"] = [
                            {"type": "image_url", "image_url": {"url": u}} for u in ref_urls
                        ]
                    resp = client.post(f"{base_url}/images/generations", headers=headers, json=body)
                else:
                    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                    content.extend({"type": "image_url", "image_url": {"url": u}} for u in ref_urls[:3])
                    body = {
                        "model": model_id, "modalities": ["image", "text"],
                        "messages": [{"role": "user", "content": content}],
                        "image_config": {"aspect_ratio": ratio},
                    }
                    resp = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
            if resp.status_code >= 400:
                return self._fail(
                    f"OpenRouter HTTP {resp.status_code}: {_err_summary(resp.text)}",
                    model=model_id, prompt=prompt, aspect=aspect,
                )
            payload = resp.json()
        except Exception as exc:
            return self._fail(f"OpenRouter image failed: {exc}", model=model_id, prompt=prompt, aspect=aspect)
        ref: Optional[str] = None
        if use_images:
            rows = (payload or {}).get("data") or []
            first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            ref = _materialize(first.get("b64_json"), first.get("url"), prefix=f"openrouter_{model_id}")
        else:
            for ch in (payload or {}).get("choices") or ():
                for img in ((ch.get("message") or {}).get("images") or ()) if isinstance(ch, dict) else ():
                    u = (img.get("image_url") or {}).get("url") if isinstance(img, dict) else None
                    if isinstance(u, str) and u.strip():
                        if u.startswith("data:"):
                            ref = str(save_b64_image(u, prefix="openrouter_gen"))
                        else:
                            ref = _materialize(None, u, prefix="openrouter_gen")
                        break
                if ref:
                    break
        if not ref:
            return self._fail(
                "OpenRouter returned no image", "empty_response",
                model=model_id, prompt=prompt, aspect=aspect,
            )
        return image_success(
            image=ref, model=model_id, prompt=prompt, aspect_ratio=aspect, provider=self.name,
            modality="image" if sources else "text",
        )


class FALImageGenProvider(ImageGenProvider):
    name, display_name = "fal", "FAL"
    _poll_s, _deadline_s = 1.5, 180.0

    def is_available(self) -> bool:
        return bool(get_secret("FAL_KEY"))

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[list[str]] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        prompt, aspect = str(prompt or "").strip(), resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return self._fail("prompt is required", "invalid_request", aspect=aspect)
        api_key = get_secret("FAL_KEY")
        if not api_key:
            return self._fail("FAL_KEY not set", "auth_required", aspect=aspect)
        catalog, default_id = _fal_catalog()
        chosen = _pick_model("fal", "FAL_IMAGE_MODEL", model, default_id)
        model_id = chosen if chosen in catalog else default_id
        meta = catalog[model_id]
        max_refs = int(meta.get("max_reference_images") or 4)
        sources = _collect_sources(image_url, reference_image_urls, max_refs)
        is_edit = bool(sources and meta.get("edit_endpoint"))
        endpoint = str(meta["edit_endpoint"]) if is_edit else model_id
        supports = set(meta.get("edit_supports") or ()) if is_edit else set(meta.get("supports") or ())
        payload: dict[str, Any] = {**dict(meta.get("defaults") or {}), "prompt": prompt}
        required = {"prompt"}
        if is_edit:
            urls = [_as_url_or_data(r) for r in sources]
            param = str(meta.get("edit_image_param") or "image_urls")
            payload[param] = urls[0] if param != "image_urls" else urls
            required.add(param)
        style_map = {
            "image_size_preset": "image_size",
            "gpt_literal": "image_size",
            "aspect_ratio": "aspect_ratio",
        }
        skey = style_map.get(str(meta.get("size_style") or "image_size_preset"))
        sizes = meta.get("sizes") if isinstance(meta.get("sizes"), dict) else {}
        if skey and aspect in sizes and (not is_edit or skey in supports):
            payload[skey] = sizes[aspect]
        if isinstance(kwargs.get("seed"), int):
            payload["seed"] = kwargs["seed"]
        body = {k: v for k, v in payload.items() if k in supports or k in required}
        auth = {"Authorization": f"Key {api_key}"}
        post_headers = {**auth, "Content-Type": "application/json", "x-idempotency-key": str(uuid.uuid4())}
        try:
            with httpx.Client(timeout=60.0) as client:
                sub = client.post(f"{_FAL_QUEUE_ORIGIN}/{endpoint}", headers=post_headers, json=body)
                if sub.status_code >= 400:
                    return self._fail(
                        f"FAL HTTP {sub.status_code}: {_err_summary(sub.text)}",
                        model=model_id, prompt=prompt, aspect=aspect,
                    )
                result = sub.json()
                status_url = result.get("status_url") if isinstance(result, dict) else None
                resp_url = result.get("response_url") if isinstance(result, dict) else None
                if status_url and not result.get("images"):
                    deadline = time.monotonic() + self._deadline_s
                    while True:
                        if time.monotonic() > deadline:
                            return self._fail(
                                "FAL image timed out", "timeout",
                                model=model_id, prompt=prompt, aspect=aspect,
                            )
                        st = client.get(status_url, headers=auth)
                        st.raise_for_status()
                        sb = st.json()
                        state = str(sb.get("status") or "").upper()
                        if state == "COMPLETED":
                            r = client.get(resp_url or sb.get("response_url"), headers=auth)
                            r.raise_for_status()
                            result = r.json()
                            break
                        if state in ("FAILED", "CANCELLED"):
                            return self._fail(
                                str(sb.get("error") or f"FAL {state}"),
                                model=model_id, prompt=prompt, aspect=aspect,
                            )
                        time.sleep(self._poll_s)
        except Exception as exc:
            return self._fail(f"FAL image failed: {exc}", model=model_id, prompt=prompt, aspect=aspect)
        imgs = result.get("images") if isinstance(result, dict) else None
        first = imgs[0] if isinstance(imgs, list) and imgs else None
        url = first.get("url") if isinstance(first, dict) else (first if isinstance(first, str) else None)
        if not url:
            return self._fail(
                "FAL returned no image URL", "empty_response",
                model=model_id, prompt=prompt, aspect=aspect,
            )
        ref = _materialize(None, url, prefix=f"fal_{model_id.rsplit('/', 1)[-1]}") or url
        return image_success(
            image=ref, model=model_id, prompt=prompt, aspect_ratio=aspect, provider=self.name,
            modality="image" if is_edit else "text",
        )


_PROVIDERS: dict[str, ImageGenProvider] = {}


def _ensure_providers() -> None:
    if not _PROVIDERS:
        for cls in (
            OpenAIImageGenProvider,
            OpenAICodexImageGenProvider,
            XAIImageGenProvider,
            OpenRouterImageGenProvider,
            FALImageGenProvider,
        ):
            inst = cls()
            _PROVIDERS[inst.name] = inst


def register_image_gen_provider(provider: ImageGenProvider) -> None:
    _ensure_providers()
    _PROVIDERS[provider.name] = provider


def get_image_gen_provider(name: str) -> Optional[ImageGenProvider]:
    _ensure_providers()
    return _PROVIDERS.get(str(name or "").strip().lower())


def list_image_gen_providers() -> list[ImageGenProvider]:
    _ensure_providers()
    return list(_PROVIDERS.values())


def get_active_image_provider() -> Optional[ImageGenProvider]:
    _ensure_providers()
    configured = str(load_media_section("image_gen").get("provider") or "").strip().lower()
    if configured:
        return _PROVIDERS.get(configured)
    for name in ("fal", "openai-codex", "openai", "openrouter", "xai"):
        if _PROVIDERS[name].is_available():
            return _PROVIDERS[name]
    return _PROVIDERS["fal"]


def image_generate(
    prompt: str, *, aspect_ratio: str = DEFAULT_ASPECT_RATIO, image_url: Optional[str] = None,
    reference_image_urls: Optional[list[str]] = None, provider: Optional[str] = None,
    model: Optional[str] = None, **kwargs: Any,
) -> dict[str, Any]:
    prov = get_image_gen_provider(provider) if provider else get_active_image_provider()
    if prov is None:
        return image_error(
            error=f"No image generation provider found for {provider!r}",
            error_type="provider_not_registered", provider=str(provider or ""),
            prompt=prompt, aspect_ratio=resolve_aspect_ratio(aspect_ratio),
        )
    return prov.generate(
        prompt, aspect_ratio=aspect_ratio, model=model, image_url=image_url,
        reference_image_urls=reference_image_urls, **kwargs,
    )


def _handle_image_generate(args: dict[str, Any], **_: Any) -> str:
    refs_raw = args.get("reference_image_urls")
    refs = (
        [str(r).strip() for r in refs_raw if isinstance(r, str) and r.strip()]
        if isinstance(refs_raw, list)
        else None
    )
    res = image_generate(
        str(args.get("prompt") or "").strip(),
        aspect_ratio=str(args.get("aspect_ratio") or DEFAULT_ASPECT_RATIO).strip(),
        image_url=str(args.get("image_url") or "").strip() or None,
        reference_image_urls=refs,
        seed=args.get("seed"),
    )
    return json.dumps(res, ensure_ascii=False)


IMAGE_GENERATE_SCHEMA: dict[str, Any] = {
    "name": "image_generate",
    "description": "Generate or edit an image from a text prompt using the configured image model.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Detailed description of the image to generate."},
            "aspect_ratio": {
                "type": "string",
                "enum": ["landscape", "square", "portrait"],
                "default": "landscape",
            },
            "image_url": {"type": "string", "description": "Optional source image path or URL to edit."},
            "reference_image_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["prompt"],
    },
}

__all__ = [
    "DEFAULT_ASPECT_RATIO", "FALImageGenProvider", "IMAGE_GENERATE_SCHEMA", "ImageGenProvider",
    "OpenAICodexImageGenProvider", "OpenAIImageGenProvider", "OpenRouterImageGenProvider",
    "XAIImageGenProvider", "_handle_image_generate", "get_active_image_provider",
    "get_image_gen_provider", "image_error", "image_generate", "image_success",
    "list_image_gen_providers", "register_image_gen_provider", "resolve_aspect_ratio",
    "save_b64_image", "save_url_image",
]
