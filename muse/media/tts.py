"""Text-to-speech subsystem supporting OpenAI-compatible TTS and optional ``edge-tts``."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from muse.media._common import (
    get_media_cache_dir,
    get_secret,
    is_secret_path,
    load_media_section,
    save_bytes_media,
)

logger = logging.getLogger(__name__)

_AUDIO_CACHE_SUBDIR = "audio"
DEFAULT_OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_TTS_VOICE = "alloy"
DEFAULT_EDGE_TTS_VOICE = "zh-CN-XiaoxiaoNeural"


def _has_edge_tts() -> bool:
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except Exception:
        return False


def _resolve_output_path(output_path: Optional[str], extension: str) -> Path:
    if output_path:
        if is_secret_path(output_path):
            raise PermissionError("Refusing to write TTS output to a protected secret path")
        target = Path(output_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    folder = get_media_cache_dir(_AUDIO_CACHE_SUBDIR)
    # Create via save_bytes_media when writing, or return target path for edge-tts.
    import uuid
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"tts_{stamp}_{uuid.uuid4().hex[:8]}.{extension}"


def _synthesize_openai(
    text: str,
    tts_cfg: dict[str, Any],
    *,
    voice: Optional[str],
    model: Optional[str],
    speed: Optional[float],
    output_path: Optional[str],
) -> dict[str, Any]:
    osec = tts_cfg.get("openai") if isinstance(tts_cfg.get("openai"), dict) else {}
    api_key = (
        str(osec.get("api_key") or "").strip()
        or get_secret("VOICE_TOOLS_OPENAI_KEY")
        or get_secret("OPENAI_API_KEY")
    )
    base_url = str(osec.get("base_url") or "https://api.openai.com/v1").strip().rstrip("/")
    if not api_key:
        return {"success": False, "error": "OPENAI_API_KEY not set for OpenAI TTS"}
    chosen_model = str(model or osec.get("model") or DEFAULT_OPENAI_TTS_MODEL).strip()
    chosen_voice = str(voice or osec.get("voice") or DEFAULT_OPENAI_TTS_VOICE).strip()
    body: dict[str, Any] = {
        "model": chosen_model,
        "input": text,
        "voice": chosen_voice,
        "response_format": "mp3",
    }
    if speed is not None:
        body["speed"] = max(0.25, min(4.0, float(speed)))
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{base_url}/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code >= 400:
            return {"success": False, "error": f"OpenAI TTS HTTP {resp.status_code}: {resp.text[:300]}"}
        if output_path:
            target = _resolve_output_path(output_path, "mp3")
            target.write_bytes(resp.content)
        else:
            target = save_bytes_media(_AUDIO_CACHE_SUBDIR, resp.content, prefix="tts_openai", extension="mp3")
        return {
            "success": True,
            "file_path": str(target),
            "media_tag": f"MEDIA:{target}",
            "provider": "openai",
            "voice": chosen_voice,
            "model": chosen_model,
        }
    except Exception as exc:
        return {"success": False, "error": f"OpenAI TTS failed: {exc}"}


def _synthesize_edge(
    text: str,
    tts_cfg: dict[str, Any],
    *,
    voice: Optional[str],
    output_path: Optional[str],
) -> dict[str, Any]:
    if not _has_edge_tts():
        return {"success": False, "error": "edge-tts package is not installed"}
    esec = tts_cfg.get("edge") if isinstance(tts_cfg.get("edge"), dict) else {}
    chosen_voice = str(voice or esec.get("voice") or DEFAULT_EDGE_TTS_VOICE).strip()
    try:
        import edge_tts

        target = _resolve_output_path(output_path, "mp3")

        async def _run() -> None:
            comm = edge_tts.Communicate(text, chosen_voice)
            await comm.save(str(target))

        try:
            asyncio.get_running_loop()
            # Called from inside an active loop: run on a worker thread.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(_run())).result(timeout=90.0)
        except RuntimeError:
            asyncio.run(_run())
        return {
            "success": True,
            "file_path": str(target),
            "media_tag": f"MEDIA:{target}",
            "provider": "edge",
            "voice": chosen_voice,
        }
    except Exception as exc:
        return {"success": False, "error": f"edge-tts failed: {exc}"}


def text_to_speech(
    text: str,
    *,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    speed: Optional[float] = None,
    output_path: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Synthesize speech audio to disk and return ``{success, file_path, media_tag, provider}``."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return {"success": False, "error": "text is required for text_to_speech"}
    if output_path and is_secret_path(output_path):
        return {"success": False, "error": "Refusing to write TTS output to a protected secret path"}

    tts_cfg = load_media_section("tts")
    chosen = str(provider or tts_cfg.get("provider") or "").strip().lower()
    if not chosen:
        if get_secret("VOICE_TOOLS_OPENAI_KEY") or get_secret("OPENAI_API_KEY"):
            chosen = "openai"
        elif _has_edge_tts():
            chosen = "edge"
        else:
            chosen = "openai"

    if chosen == "edge":
        return _synthesize_edge(cleaned, tts_cfg, voice=voice, output_path=output_path)
    if chosen in ("openai", "nous"):
        return _synthesize_openai(
            cleaned, tts_cfg, voice=voice, model=model, speed=speed, output_path=output_path
        )
    return {"success": False, "error": f"Unsupported TTS provider: {chosen}"}


def _handle_text_to_speech(args: dict[str, Any], **_: Any) -> str:
    result = text_to_speech(
        str(args.get("text") or ""),
        voice=str(args.get("voice") or "").strip() or None,
        model=str(args.get("model") or "").strip() or None,
        speed=float(args["speed"]) if args.get("speed") is not None else None,
        output_path=str(args.get("output_path") or "").strip() or None,
        provider=str(args.get("provider") or "").strip() or None,
    )
    return json.dumps(result, ensure_ascii=False)


TEXT_TO_SPEECH_SCHEMA: dict[str, Any] = {
    "name": "text_to_speech",
    "description": "Convert text into a spoken audio file (MP3) using OpenAI TTS or edge-tts.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to synthesize into speech."},
            "voice": {"type": "string", "description": "Optional voice name."},
            "speed": {"type": "number", "description": "Optional speech speed multiplier (0.25 to 4.0)."},
            "output_path": {"type": "string", "description": "Optional target output file path."},
        },
        "required": ["text"],
    },
}


__all__ = [
    "DEFAULT_EDGE_TTS_VOICE",
    "DEFAULT_OPENAI_TTS_MODEL",
    "DEFAULT_OPENAI_TTS_VOICE",
    "TEXT_TO_SPEECH_SCHEMA",
    "_handle_text_to_speech",
    "text_to_speech",
]
