"""Video generation ABC, registry, and 4 built-in backends (xai, openrouter, fal, deepinfra)."""

from __future__ import annotations

import abc
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from muse.media._common import (
    DEFAULT_OPENROUTER_BASE_URL,
    fetch_url_bytes,
    get_secret,
    image_to_data_url,
    load_media_section,
    resolve_xai_credentials,
    save_b64_media,
    save_bytes_media,
)

logger = logging.getLogger(__name__)

_VIDEO_SUBDIR = "generated/videos"
COMMON_ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9")
DEFAULT_ASPECT_RATIO = "16:9"
COMMON_RESOLUTIONS: tuple[str, ...] = ("480p", "540p", "720p", "768p", "1080p")
DEFAULT_RESOLUTION = "720p"
_VIDEO_CTYPES = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
}
_FAL_QUEUE_ORIGIN = "https://queue.fal.run"


def save_b64_video(b64_data: str, *, prefix: str = "video", extension: str = "mp4") -> Path:
    return save_b64_media(_VIDEO_SUBDIR, b64_data, prefix=prefix, extension=extension)


def save_bytes_video(raw: bytes, *, prefix: str = "video", extension: str = "mp4") -> Path:
    return save_bytes_media(_VIDEO_SUBDIR, raw, prefix=prefix, extension=extension)


def save_url_video(
    url: str, *, prefix: str = "video", timeout: float = 180.0, max_bytes: int = 200 * 1024 * 1024,
    headers: Optional[dict[str, str]] = None, require_video_content_type: bool = False,
    trusted_origin: bool = False,
) -> Path:
    raw, resp_h = fetch_url_bytes(
        url, max_bytes=max_bytes, timeout=timeout, headers=headers, trusted_origin=trusted_origin
    )
    if not raw:
        raise ValueError(f"Video at {url} was empty (0 bytes).")
    ctype = resp_h.get("content-type", "").split(";", 1)[0].strip().lower()
    if require_video_content_type and ctype and ctype not in _VIDEO_CTYPES:
        raise ValueError(f"Unexpected video content-type: {ctype}")
    return save_bytes_video(raw, prefix=prefix, extension=_VIDEO_CTYPES.get(ctype, "mp4"))


def success_response(
    *, video: str, model: str, prompt: str, modality: str = "text", aspect_ratio: str = "",
    duration: int = 0, provider: str, extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": True, "video": video, "model": model, "prompt": prompt,
        "modality": modality, "aspect_ratio": aspect_ratio,
        "duration": int(duration) if duration else 0, "provider": provider,
    }
    for k, v in (extra or {}).items():
        out.setdefault(k, v)
    return out


def error_response(
    *, error: str, error_type: str = "provider_error", provider: str = "", model: str = "",
    prompt: str = "", aspect_ratio: str = "",
) -> dict[str, Any]:
    return {
        "success": False, "video": None, "error": error, "error_type": error_type,
        "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "provider": provider,
    }


def _norm_img(ref: Optional[str]) -> Optional[str]:
    if not ref or not str(ref).strip():
        return None
    s = str(ref).strip()
    return s if s.lower().startswith(("http://", "https://", "data:")) else image_to_data_url(s)


def _poll_json(
    client: httpx.Client, url: str, headers: dict[str, str], *,
    terminal: set[str], interval_s: float, deadline_s: float,
    status_fn: Callable[[dict[str, Any]], str] = lambda d: str(d.get("status") or "").lower(),
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + deadline_s
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"Video job timed out after {int(deadline_s)}s")
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        status = status_fn(body)
        if status in terminal:
            return status, body
        time.sleep(interval_s)


def _fal_video_catalog() -> tuple[dict[str, dict[str, Any]], str]:
    try:
        from muse.media.catalog import DEFAULT_FAL_VIDEO_MODEL, FAL_VIDEO_MODELS

        return FAL_VIDEO_MODELS, DEFAULT_FAL_VIDEO_MODEL
    except ImportError:
        default_id = "pixverse-v6"
        return {
            default_id: {
                "display": "Pixverse v6",
                "text_endpoint": "fal-ai/pixverse/v6/text-to-video",
                "image_endpoint": "fal-ai/pixverse/v6/image-to-video",
                "aspect_ratios": None, "resolutions": ("360p", "540p", "720p", "1080p"),
                "durations": (1, 15), "audio": True, "negative": True, "seed": True,
            }
        }, default_id


class VideoGenProvider(abc.ABC):
    """Drop-in ABC for video providers (incl. ``companion/modules/video_backends.py``)."""

    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name

    def is_available(self) -> bool:
        return False

    def list_models(self) -> list[dict[str, Any]]:
        return []

    def default_model(self) -> Optional[str]:
        rows = self.list_models()
        return str(rows[0]["id"]) if rows and isinstance(rows[0], dict) else None

    def capabilities(self) -> dict[str, Any]:
        return {
            "modalities": ["text"], "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": list(COMMON_RESOLUTIONS), "max_duration": 10, "min_duration": 1,
            "supports_audio": False, "supports_negative_prompt": False, "supports_seed": False,
            "supports_upscale": False, "max_reference_images": 0,
        }

    def _fail(
        self, msg: str, kind: str = "provider_error", *,
        model: str = "", prompt: str = "", ar: str = "",
    ):
        return error_response(
            error=msg, error_type=kind, provider=self.name, model=model, prompt=prompt, aspect_ratio=ar
        )

    @abc.abstractmethod
    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]: ...


class XAIVideoGenProvider(VideoGenProvider):
    name, _poll_s, _deadline_s = "xai", 5.0, 240.0

    @property
    def display_name(self) -> str:
        return "xAI"

    def is_available(self) -> bool:
        return bool(resolve_xai_credentials().get("api_key"))

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": "grok-imagine-video", "display": "Grok Imagine Video"},
            {"id": "grok-imagine-video-1.5", "display": "Grok Imagine Video 1.5"},
        ]

    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del negative_prompt, audio, seed, kwargs
        prompt = str(prompt or "").strip()
        if not prompt:
            return self._fail("prompt is required", "missing_prompt")
        creds = resolve_xai_credentials()
        api_key, base_url = creds.get("api_key", ""), creds.get("base_url", "https://api.x.ai/v1")
        if not api_key:
            return self._fail("No xAI credentials found", "auth_required", prompt=prompt)
        img = _norm_img(image_url)
        refs = [r for r in (_norm_img(x) for x in (reference_image_urls or ())) if r]
        modality = "reference" if refs else ("image" if img else "text")
        model_id = (
            "grok-imagine-video-1.5"
            if modality == "image"
            else (str(model or "").strip() or "grok-imagine-video")
        )
        secs = max(1, min(10 if refs else 15, int(duration or 8)))
        body: dict[str, Any] = {
            "model": model_id, "prompt": prompt, "duration": secs, "aspect_ratio": aspect_ratio,
            "resolution": resolution.lower() if resolution.lower() in ("480p", "720p") else "720p",
        }
        if img:
            body["image"] = {"url": img}
        if refs:
            body["reference_images"] = [{"url": r} for r in refs[:7]]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "muse-bot/2.0",
        }
        try:
            with httpx.Client(timeout=60.0) as client:
                sub = client.post(
                    f"{base_url}/videos/generations",
                    headers={**headers, "x-idempotency-key": str(uuid.uuid4())}, json=body,
                )
                sub.raise_for_status()
                req_id = str(sub.json().get("request_id") or "")
                if not req_id:
                    return self._fail("xAI missing request_id", model=model_id, prompt=prompt)
                status, res = _poll_json(
                    client, f"{base_url}/videos/{req_id}", headers,
                    terminal={"done", "failed", "error", "expired", "cancelled"},
                    interval_s=self._poll_s, deadline_s=self._deadline_s,
                )
        except Exception as exc:
            return self._fail(f"xAI video failed: {exc}", model=model_id, prompt=prompt)
        if status != "done":
            err = res.get("error")
            msg = err.get("message") if isinstance(err, dict) else None
            return self._fail(str(msg or f"xAI video status={status!r}"), model=model_id, prompt=prompt)
        vid = res.get("video") if isinstance(res.get("video"), dict) else {}
        url = str((vid.get("file_output") or {}).get("public_url") or vid.get("url") or "").strip()
        if not url:
            return self._fail(
                "xAI completed without a video URL", "empty_response", model=model_id, prompt=prompt
            )
        return success_response(
            video=url, model=str(res.get("model") or model_id), prompt=prompt, modality=modality,
            aspect_ratio=aspect_ratio, duration=int(vid.get("duration") or secs), provider=self.name,
            extra={"request_id": req_id},
        )


class OpenRouterVideoGenProvider(VideoGenProvider):
    name, _poll_s, _deadline_s = "openrouter", 10.0, 900.0

    @property
    def display_name(self) -> str:
        return "OpenRouter"

    def _creds(self) -> tuple[str, str]:
        base = (get_secret("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")
        return get_secret("OPENROUTER_API_KEY"), base

    def is_available(self) -> bool:
        return bool(self._creds()[0])

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "minimax/hailuo-3-max", "display": "MiniMax: Hailuo 3 Max"}]

    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del negative_prompt, kwargs
        prompt = str(prompt or "").strip()
        model_id = str(model or "").strip() or "minimax/hailuo-3-max"
        if not prompt:
            return self._fail("prompt is required", "invalid_request", model=model_id)
        api_key, base_url = self._creds()
        if not api_key:
            return self._fail("OPENROUTER_API_KEY not set", "auth_required", model=model_id, prompt=prompt)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Muse Bot",
        }
        body: dict[str, Any] = {
            "model": model_id, "prompt": prompt, "aspect_ratio": aspect_ratio, "resolution": resolution,
        }
        if duration:
            body["duration"] = int(duration)
        img = _norm_img(image_url)
        if img:
            body["frame_images"] = [
                {"type": "image_url", "image_url": {"url": img}, "frame_type": "first_frame"}
            ]
        refs = [r for r in (_norm_img(x) for x in (reference_image_urls or ())) if r]
        if refs:
            body["input_references"] = [{"type": "image_url", "image_url": {"url": r}} for r in refs[:3]]
        if audio is not None:
            body["generate_audio"] = bool(audio)
        if seed is not None:
            body["seed"] = int(seed)
        try:
            with httpx.Client(timeout=60.0) as client:
                sub = client.post(f"{base_url}/videos", headers=headers, json=body)
                sub.raise_for_status()
                job_id = str(sub.json().get("id") or "")
                if not job_id:
                    return self._fail("OpenRouter missing job id", model=model_id, prompt=prompt)
                status, job = _poll_json(
                    client, f"{base_url}/videos/{job_id}", headers,
                    terminal={"completed", "failed", "cancelled", "canceled", "expired"},
                    interval_s=self._poll_s, deadline_s=self._deadline_s,
                )
            if status != "completed":
                err_txt = str(job.get("error") or f"OpenRouter status={status!r}")
                return self._fail(err_txt, model=model_id, prompt=prompt)
            vpath = str(
                save_url_video(
                    f"{base_url}/videos/{job_id}/content",
                    prefix="openrouter",
                    headers=headers,
                    trusted_origin=True,
                )
            )
        except Exception as exc:
            return self._fail(f"OpenRouter video failed: {exc}", model=model_id, prompt=prompt)
        return success_response(
            video=vpath, model=model_id, prompt=prompt, modality="image" if img else "text",
            aspect_ratio=aspect_ratio, duration=int(duration or 0),
            provider=self.name, extra={"job_id": job_id},
        )


class FALVideoGenProvider(VideoGenProvider):
    name, _poll_s, _deadline_s = "fal", 3.0, 360.0

    @property
    def display_name(self) -> str:
        return "FAL"

    def is_available(self) -> bool:
        return bool(get_secret("FAL_KEY"))

    def list_models(self) -> list[dict[str, Any]]:
        families, _ = _fal_video_catalog()
        return [{"id": fid, "display": str(m.get("display") or fid)} for fid, m in families.items()]

    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del reference_image_urls, kwargs
        prompt = str(prompt or "").strip()
        if not prompt:
            return self._fail("prompt is required", "missing_prompt")
        api_key = get_secret("FAL_KEY")
        if not api_key:
            return self._fail("FAL_KEY not set", "auth_required", prompt=prompt)
        families, default_id = _fal_video_catalog()
        sec = load_media_section("video_gen")
        fcfg = sec.get("fal") if isinstance(sec.get("fal"), dict) else {}
        cands = (model, os.environ.get("FAL_VIDEO_MODEL"), fcfg.get("model"), sec.get("model"))
        chosen = next((str(c).strip() for c in cands if isinstance(c, str) and c.strip()), default_id)
        fid = chosen if chosen in families else default_id
        fam = families[fid]
        img = _norm_img(image_url)
        modality = "image" if img else "text"
        endpoint = fam.get(f"{modality}_endpoint")
        if not endpoint:
            return self._fail(
                f"FAL {fid} lacks {modality}-to-video", "modality_unsupported", model=fid, prompt=prompt
            )
        payload: dict[str, Any] = {"prompt": prompt}
        if img:
            payload[str(fam.get("image_param_key") or "image_url")] = img
        if fam.get("aspect_ratios") and aspect_ratio in fam["aspect_ratios"]:
            payload["aspect_ratio"] = aspect_ratio
        res = (fam.get("resolution_aliases") or {}).get(resolution.lower(), resolution)
        if fam.get("resolutions") and res in fam["resolutions"]:
            payload["resolution"] = res
        if duration is not None and fam.get("durations"):
            lo, hi = fam["durations"]
            clamped = max(lo, min(hi, int(duration)))
            suffix = fam.get("duration_suffix", "")
            payload["duration"] = clamped if fam.get("duration_int") else f"{clamped}{suffix}"
        if fam.get("audio") and audio is not None:
            payload[str(fam.get("audio_param_key") or "generate_audio")] = bool(audio)
        if fam.get("negative") and negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if fam.get("seed") and seed is not None:
            payload["seed"] = int(seed)
        if img:
            for k in fam.get("image_drop_keys") or ():
                payload.pop(k, None)
        for k, v in (fam.get("static_payload") or {}).items():
            payload.setdefault(k, v)
        auth = {"Authorization": f"Key {api_key}"}
        post_h = {**auth, "Content-Type": "application/json", "x-idempotency-key": str(uuid.uuid4())}
        try:
            with httpx.Client(timeout=60.0) as client:
                sub = client.post(f"{_FAL_QUEUE_ORIGIN}/{endpoint}", headers=post_h, json=payload)
                sub.raise_for_status()
                result = sub.json()
                status_url = result.get("status_url") if isinstance(result, dict) else None
                resp_url = result.get("response_url") if isinstance(result, dict) else None
                if status_url and not result.get("video"):
                    state, sb = _poll_json(
                        client, status_url, auth, terminal={"completed", "failed", "cancelled"},
                        interval_s=self._poll_s, deadline_s=self._deadline_s,
                    )
                    if state != "completed":
                        return self._fail(str(sb.get("error") or f"FAL {state}"), model=fid, prompt=prompt)
                    r = client.get(resp_url or sb.get("response_url"), headers=auth)
                    r.raise_for_status()
                    result = r.json()
        except Exception as exc:
            return self._fail(f"FAL video failed: {exc}", model=fid, prompt=prompt)
        vid = result.get("video") if isinstance(result, dict) else None
        url = vid.get("url") if isinstance(vid, dict) else (vid if isinstance(vid, str) else None)
        if not url:
            return self._fail("FAL returned no video URL", "empty_response", model=fid, prompt=prompt)
        return success_response(
            video=url, model=fid, prompt=prompt, modality=modality, aspect_ratio=aspect_ratio,
            duration=int(duration or 0), provider=self.name, extra={"endpoint": endpoint},
        )


class DeepInfraVideoGenProvider(VideoGenProvider):
    name, _default_base, _poll_s, _deadline_s = "deepinfra", "https://api.deepinfra.com/v1/openai", 5.0, 900.0

    @property
    def display_name(self) -> str:
        return "DeepInfra"

    def is_available(self) -> bool:
        return bool(get_secret("DEEPINFRA_API_KEY"))

    def list_models(self) -> list[dict[str, Any]]:
        try:
            from muse.media.catalog import fetch_deepinfra_video_models

            return fetch_deepinfra_video_models()
        except Exception:
            return []

    def generate(
        self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del reference_image_urls, audio, kwargs
        prompt = str(prompt or "").strip()
        if not prompt:
            return self._fail("prompt is required", "invalid_request")
        api_key = get_secret("DEEPINFRA_API_KEY")
        if not api_key:
            return self._fail("DEEPINFRA_API_KEY not set", "missing_credentials", prompt=prompt)
        model_id = str(model or "").strip() or (self.default_model() or "")
        if not model_id:
            return self._fail("No DeepInfra video model configured", "invalid_request", prompt=prompt)
        base_url = (get_secret("DEEPINFRA_BASE_URL") or self._default_base).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body: dict[str, Any] = {
            "model": model_id, "prompt": prompt, "aspect_ratio": aspect_ratio, "resolution": resolution,
        }
        if duration:
            body["seconds"] = str(int(duration))
        img = _norm_img(image_url)
        if img:
            body["image_url"] = img
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if seed is not None:
            body["seed"] = int(seed)
        try:
            with httpx.Client(timeout=60.0) as client:
                sub = client.post(f"{base_url}/videos", headers=headers, json=body)
                sub.raise_for_status()
                job = sub.json()
                job_id = str(job.get("id") or "")
                if not job_id:
                    return self._fail("DeepInfra missing video job id", model=model_id, prompt=prompt)
                status = str(job.get("status") or "").lower()
                term = {"completed", "succeeded", "failed", "error", "cancelled", "canceled"}
                if status not in term:
                    status, job = _poll_json(
                        client, f"{base_url}/videos/{job_id}", headers,
                        terminal=term, interval_s=self._poll_s, deadline_s=self._deadline_s,
                    )
            if status not in ("completed", "succeeded"):
                err_txt = str(job.get("error") or f"DeepInfra status={status!r}")
                return self._fail(err_txt, model=model_id, prompt=prompt)
            vpath = str(
                save_url_video(
                    f"{base_url}/videos/{job_id}/content", prefix="deepinfra",
                    headers={"Authorization": f"Bearer {api_key}"}, trusted_origin=True,
                )
            )
        except Exception as exc:
            return self._fail(f"DeepInfra video failed: {exc}", model=model_id, prompt=prompt)
        return success_response(
            video=vpath, model=model_id, prompt=prompt, modality="image" if img else "text",
            aspect_ratio=aspect_ratio, duration=int(duration or 0),
            provider=self.name, extra={"job_id": job_id},
        )


_VIDEO_PROVIDERS: dict[str, VideoGenProvider] = {}
_COMPANION_LOADED = False


def _ensure_video_providers() -> None:
    global _COMPANION_LOADED
    if not _VIDEO_PROVIDERS:
        classes = (
            XAIVideoGenProvider,
            OpenRouterVideoGenProvider,
            FALVideoGenProvider,
            DeepInfraVideoGenProvider,
        )
        for cls in classes:
            inst = cls()
            _VIDEO_PROVIDERS[inst.name] = inst
    if not _COMPANION_LOADED:
        _COMPANION_LOADED = True
        try:
            from companion.modules.video_backends import BACKENDS as companion_backends

            for cls in companion_backends:
                inst = cls()
                _VIDEO_PROVIDERS.setdefault(inst.name, inst)
        except Exception:
            pass


def register_video_gen_provider(provider: VideoGenProvider) -> None:
    _ensure_video_providers()
    _VIDEO_PROVIDERS[provider.name] = provider


def get_video_gen_provider(name: str) -> Optional[VideoGenProvider]:
    _ensure_video_providers()
    return _VIDEO_PROVIDERS.get(str(name or "").strip().lower())


def list_video_gen_providers() -> list[VideoGenProvider]:
    _ensure_video_providers()
    return list(_VIDEO_PROVIDERS.values())


def get_active_video_provider() -> Optional[VideoGenProvider]:
    _ensure_video_providers()
    configured = str(load_media_section("video_gen").get("provider") or "").strip().lower()
    if configured:
        return _VIDEO_PROVIDERS.get(configured)
    return next((p for p in _VIDEO_PROVIDERS.values() if p.is_available()), None)


def video_generate(
    prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
    reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO, resolution: str = DEFAULT_RESOLUTION,
    negative_prompt: Optional[str] = None, audio: Optional[bool] = None,
    seed: Optional[int] = None, provider: Optional[str] = None, **kwargs: Any,
) -> dict[str, Any]:
    prov = get_video_gen_provider(provider) if provider else get_active_video_provider()
    if prov is None:
        return error_response(
            error="No video generation backend is configured.", error_type="no_provider_configured",
            prompt=prompt, aspect_ratio=aspect_ratio,
        )
    cfg_model = str(load_media_section("video_gen").get("model") or "").strip()
    resolved = model or cfg_model or prov.default_model()
    return prov.generate(
        prompt, model=resolved, image_url=image_url, reference_image_urls=reference_image_urls,
        duration=duration, aspect_ratio=aspect_ratio, resolution=resolution,
        negative_prompt=negative_prompt, audio=audio, seed=seed, **kwargs,
    )


def _handle_video_generate(args: dict[str, Any], **_: Any) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        err = error_response(error="prompt is required", error_type="invalid_request")
        return json.dumps(err, ensure_ascii=False)
    refs_raw = args.get("reference_image_urls")
    refs = (
        [str(r).strip() for r in refs_raw if isinstance(r, str) and r.strip()]
        if isinstance(refs_raw, list)
        else None
    )
    dur_raw = args.get("duration")
    dur = int(dur_raw) if dur_raw is not None and str(dur_raw).strip() != "" else None
    res = video_generate(
        prompt,
        image_url=str(args.get("image_url") or "").strip() or None,
        reference_image_urls=refs,
        duration=dur,
        aspect_ratio=str(args.get("aspect_ratio") or DEFAULT_ASPECT_RATIO).strip(),
        resolution=str(args.get("resolution") or DEFAULT_RESOLUTION).strip(),
        negative_prompt=str(args.get("negative_prompt") or "").strip() or None,
        audio=args.get("audio") if isinstance(args.get("audio"), bool) else None,
        seed=int(args["seed"]) if isinstance(args.get("seed"), int) else None,
    )
    return json.dumps(res, ensure_ascii=False)


VIDEO_GENERATE_SCHEMA: dict[str, Any] = {
    "name": "video_generate",
    "description": "Generate a video from a text prompt or animate a still image.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Description of the desired video and motion."},
            "image_url": {
                "type": "string",
                "description": "Optional first-frame image path, data URL, or URL.",
            },
            "reference_image_urls": {"type": "array", "items": {"type": "string"}},
            "duration": {"type": "integer", "description": "Desired duration in seconds."},
            "aspect_ratio": {"type": "string", "enum": list(COMMON_ASPECT_RATIOS), "default": "16:9"},
            "resolution": {"type": "string", "enum": list(COMMON_RESOLUTIONS), "default": "720p"},
            "negative_prompt": {"type": "string"},
            "audio": {"type": "boolean"},
            "seed": {"type": "integer"},
        },
        "required": ["prompt"],
    },
}

__all__ = [
    "COMMON_ASPECT_RATIOS", "COMMON_RESOLUTIONS", "DEFAULT_ASPECT_RATIO", "DEFAULT_RESOLUTION",
    "DeepInfraVideoGenProvider", "FALVideoGenProvider", "OpenRouterVideoGenProvider",
    "VIDEO_GENERATE_SCHEMA", "VideoGenProvider", "XAIVideoGenProvider", "_handle_video_generate",
    "error_response", "get_active_video_provider", "get_video_gen_provider",
    "list_video_gen_providers", "register_video_gen_provider", "save_b64_video",
    "save_bytes_video", "save_url_video", "success_response", "video_generate",
]
