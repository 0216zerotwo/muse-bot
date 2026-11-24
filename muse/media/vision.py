"""Multimodal image analysis tool (``vision_analyze``) with SSRF and secret-path guards."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from typing import Any, Optional

from PIL import Image

from muse.media._common import is_secret_path, load_source_image_bytes, sniff_image_mime

logger = logging.getLogger(__name__)

_MAX_VISION_DIM = 2048
_MAX_VISION_BYTES = 5 * 1024 * 1024
_DEFAULT_PROMPT = "Describe this image in detail."

# Module attribute so tests can patch ``muse.media.vision.async_call_llm`` directly.
async_call_llm: Any = None


def _get_async_call_llm() -> Any:
    if async_call_llm is not None:
        return async_call_llm
    from muse.llm.auxiliary import async_call_llm as _aux_call

    return _aux_call


def _normalize_image_for_vision(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Ensure image fits within vision token/byte limits and uses a standard MIME."""
    supported = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if mime in supported and len(raw) <= _MAX_VISION_BYTES:
        try:
            with Image.open(io.BytesIO(raw)) as img:
                if max(img.size) <= _MAX_VISION_DIM:
                    return raw, mime
        except Exception:
            return raw, mime

    try:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) > _MAX_VISION_DIM:
                img.thumbnail((_MAX_VISION_DIM, _MAX_VISION_DIM), Image.Resampling.LANCZOS)
            rgb = img.convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=88, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, mime if mime.startswith("image/") else "image/png"


def _load_and_encode_image(image_url: str) -> str:
    ref = str(image_url or "").strip()
    if not ref:
        raise ValueError("image_url is required")
    if not ref.lower().startswith(("http://", "https://", "data:")) and is_secret_path(ref):
        raise PermissionError("Refusing to analyze protected secret file")
    raw, _, mime = load_source_image_bytes(ref, max_bytes=50 * 1024 * 1024)
    detected = sniff_image_mime(raw) or mime
    norm_bytes, norm_mime = _normalize_image_for_vision(raw, detected)
    return f"data:{norm_mime};base64,{base64.b64encode(norm_bytes).decode('ascii')}"


def _extract_response_text(resp: Any) -> tuple[str, str]:
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        msg = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        text = str(msg.get("content") or resp.get("content") or "").strip()
        return text, str(resp.get("model") or "")
    choices = getattr(resp, "choices", None) or []
    if choices:
        msg = getattr(choices[0], "message", None)
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip(), str(getattr(resp, "model", "") or "")
    content = getattr(resp, "content", None)
    return str(content or "").strip(), str(getattr(resp, "model", "") or "")


async def vision_analyze_tool(
    image_url: str,
    user_prompt: str = _DEFAULT_PROMPT,
    **kwargs: Any,
) -> str:
    """Analyze an image from a local path, data URI, or SSRF-checked HTTP(S) URL.

    Returns JSON ``{"success": True, "analysis": ..., "model": ...}`` compatible with
    ``companion/modules/avatar_make.py``.
    """
    prompt = str(user_prompt or _DEFAULT_PROMPT).strip() or _DEFAULT_PROMPT
    try:
        data_url = await asyncio.to_thread(_load_and_encode_image, image_url)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    try:
        caller = _get_async_call_llm()
        resp = await caller(
            task="vision",
            messages=messages,
            max_tokens=kwargs.get("max_tokens", 1024),
            timeout=kwargs.get("timeout", 60.0),
        )
        analysis, model_name = _extract_response_text(resp)
    except Exception as exc:
        logger.warning("muse.media.vision: analysis failed (%s)", type(exc).__name__)
        return json.dumps({"success": False, "error": f"Vision analysis failed: {exc}"}, ensure_ascii=False)

    if not analysis:
        return json.dumps(
            {"success": False, "error": "Vision model returned empty analysis"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"success": True, "analysis": analysis, "model": model_name},
        ensure_ascii=False,
    )


async def _handle_vision_analyze(args: dict[str, Any], **kwargs: Any) -> str:
    image_url = str(args.get("image_url") or args.get("path") or "").strip()
    prompt = str(args.get("user_prompt") or args.get("prompt") or _DEFAULT_PROMPT).strip()
    return await vision_analyze_tool(image_url=image_url, user_prompt=prompt, **kwargs)


VISION_ANALYZE_SCHEMA: dict[str, Any] = {
    "name": "vision_analyze",
    "description": "Analyze an image from a local file path or public HTTP(S) URL using a vision model.",
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Local image path, data URI, or public HTTP(S) URL.",
            },
            "user_prompt": {
                "type": "string",
                "description": "Question or instruction for analyzing the image.",
                "default": _DEFAULT_PROMPT,
            },
        },
        "required": ["image_url"],
    },
}


__all__ = [
    "VISION_ANALYZE_SCHEMA",
    "_handle_vision_analyze",
    "async_call_llm",
    "vision_analyze_tool",
]
