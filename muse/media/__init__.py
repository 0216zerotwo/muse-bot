"""Muse media catalog, image/video/vision/TTS/STT backends, and tool registration."""

from __future__ import annotations

from typing import Any

from muse.media.catalog import (
    DEFAULT_FAL_IMAGE_MODEL,
    DEFAULT_FAL_VIDEO_MODEL,
    FAL_IMAGE_MODELS,
    FAL_VIDEO_MODELS,
    backend_models,
    fetch_deepinfra_video_models,
    list_fal_image_models,
    list_fal_video_models,
)
from muse.media.image_gen import IMAGE_GENERATE_SCHEMA, _handle_image_generate
from muse.media.stt import (
    TranscriptionProvider,
    TranscriptionStreamSession,
    get_transcription_provider,
    list_transcription_providers,
    register_transcription_provider,
    transcribe_audio,
)
from muse.media.tts import TEXT_TO_SPEECH_SCHEMA, _handle_text_to_speech, text_to_speech
from muse.media.video import (
    VIDEO_GENERATE_SCHEMA,
    VideoGenProvider,
    _handle_video_generate,
    error_response,
    get_video_gen_provider,
    list_video_gen_providers,
    register_video_gen_provider,
    save_url_video,
    success_response,
)
from muse.media.vision import VISION_ANALYZE_SCHEMA, _handle_vision_analyze, vision_analyze_tool


def register_media_tools(registry: Any) -> None:
    """Register `vision_analyze`, `image_generate`, `video_generate`, and `text_to_speech`."""
    registry.register(
        name="vision_analyze",
        toolset="vision",
        description=VISION_ANALYZE_SCHEMA["description"],
        parameters=VISION_ANALYZE_SCHEMA["parameters"],
        handler=_handle_vision_analyze,
        is_async=True,
        emoji="👁️",
    )
    registry.register(
        name="image_generate",
        toolset="image_gen",
        description=IMAGE_GENERATE_SCHEMA["description"],
        parameters=IMAGE_GENERATE_SCHEMA["parameters"],
        handler=_handle_image_generate,
        is_async=False,
        emoji="🎨",
    )
    registry.register(
        name="video_generate",
        toolset="video_gen",
        description=VIDEO_GENERATE_SCHEMA["description"],
        parameters=VIDEO_GENERATE_SCHEMA["parameters"],
        handler=_handle_video_generate,
        is_async=False,
        emoji="🎬",
    )
    registry.register(
        name="text_to_speech",
        toolset="tts",
        description=TEXT_TO_SPEECH_SCHEMA["description"],
        parameters=TEXT_TO_SPEECH_SCHEMA["parameters"],
        handler=_handle_text_to_speech,
        is_async=False,
        emoji="🔊",
    )


__all__ = [
    "DEFAULT_FAL_IMAGE_MODEL",
    "DEFAULT_FAL_VIDEO_MODEL",
    "FAL_IMAGE_MODELS",
    "FAL_VIDEO_MODELS",
    "IMAGE_GENERATE_SCHEMA",
    "TEXT_TO_SPEECH_SCHEMA",
    "TranscriptionProvider",
    "TranscriptionStreamSession",
    "VIDEO_GENERATE_SCHEMA",
    "VISION_ANALYZE_SCHEMA",
    "VideoGenProvider",
    "_handle_image_generate",
    "_handle_text_to_speech",
    "_handle_video_generate",
    "_handle_vision_analyze",
    "backend_models",
    "error_response",
    "fetch_deepinfra_video_models",
    "get_transcription_provider",
    "get_video_gen_provider",
    "list_fal_image_models",
    "list_fal_video_models",
    "list_transcription_providers",
    "list_video_gen_providers",
    "register_media_tools",
    "register_transcription_provider",
    "register_video_gen_provider",
    "save_url_video",
    "success_response",
    "text_to_speech",
    "transcribe_audio",
    "vision_analyze_tool",
]
