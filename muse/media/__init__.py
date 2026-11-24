"""Muse media catalog and backend registry."""

from __future__ import annotations

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

__all__ = [
    "DEFAULT_FAL_IMAGE_MODEL",
    "DEFAULT_FAL_VIDEO_MODEL",
    "FAL_IMAGE_MODELS",
    "FAL_VIDEO_MODELS",
    "backend_models",
    "fetch_deepinfra_video_models",
    "list_fal_image_models",
    "list_fal_video_models",
]
