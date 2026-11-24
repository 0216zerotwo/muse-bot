"""Built-in static FAL image (25) & video (20) model catalogs plus DeepInfra dynamic video model fetcher."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── FAL Image Models (25 entries) ────────────────────────────────────────────

_PRESET_SIZES = {"landscape": "landscape_16_9", "square": "square_hd", "portrait": "portrait_16_9"}
_ASPECT_SIZES = {"landscape": "16:9", "square": "1:1", "portrait": "9:16"}
_GPT2_SIZES = {"landscape": "landscape_4_3", "square": "square_hd", "portrait": "portrait_4_3"}
_DEFAULT_SIZES = {"image_size_preset": _PRESET_SIZES, "aspect_ratio": _ASPECT_SIZES}


def _img(
    display: str, speed: str, strengths: str, price: str, defaults: dict[str, Any], supports: str,
    *, style: str = "image_size_preset", sizes: Optional[dict[str, Any]] = None,
    edit_ep: Optional[str] = None, edit_sup: Optional[str] = None, max_ref: Optional[int] = None, edit_param: Optional[str] = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "display": display, "speed": speed, "strengths": strengths, "price": price,
        "size_style": style, "sizes": sizes if sizes is not None else _DEFAULT_SIZES[style],
        "defaults": defaults, "supports": set(supports.split()), "upscale": False,
    }
    if edit_ep:
        e.update(edit_endpoint=edit_ep, edit_supports=set((edit_sup or "").split()), max_reference_images=max_ref)
        if edit_param:
            e["edit_image_param"] = edit_param
    return e


_S_GPT25 = "prompt image_size quality num_images output_format background output_compression sync_mode"
_S_GPT25_E = "prompt image_urls image_size quality num_images output_format background output_compression sync_mode mask_url input_fidelity"

FAL_IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "fal-ai/flux-2/klein/9b": _img(
        "FLUX 2 Klein 9B", "<1s", "Fast, crisp text", "$0.006/MP",
        {"num_inference_steps": 4, "output_format": "png", "enable_safety_checker": False},
        "prompt image_size num_inference_steps seed output_format enable_safety_checker",
        edit_ep="fal-ai/flux-2/klein/9b/edit", edit_sup="prompt image_urls num_inference_steps seed output_format enable_safety_checker", max_ref=9,
    ),
    "fal-ai/flux-2-pro": _img(
        "FLUX 2 Pro", "~6s", "Studio photorealism", "$0.03/MP",
        {"num_inference_steps": 50, "guidance_scale": 4.5, "num_images": 1, "output_format": "png", "enable_safety_checker": False, "safety_tolerance": "5", "sync_mode": True},
        "prompt image_size num_inference_steps guidance_scale num_images output_format enable_safety_checker safety_tolerance sync_mode seed",
        edit_ep="fal-ai/flux-2-pro/edit", edit_sup="prompt image_urls num_inference_steps guidance_scale num_images output_format enable_safety_checker safety_tolerance sync_mode seed", max_ref=9,
    ),
    "fal-ai/z-image/turbo": _img(
        "Z-Image Turbo", "~2s", "Bilingual EN/CN, 6B", "$0.005/MP",
        {"num_inference_steps": 8, "num_images": 1, "output_format": "png", "enable_safety_checker": False, "enable_prompt_expansion": False},
        "prompt image_size num_inference_steps num_images seed output_format enable_safety_checker enable_prompt_expansion",
    ),
    "fal-ai/nano-banana-pro": _img(
        "Nano Banana Pro (Gemini 3 Pro Image)", "~8s", "Gemini 3 Pro, reasoning depth, text rendering", "$0.15/image (1K)",
        {"num_images": 1, "output_format": "png", "safety_tolerance": "5", "resolution": "1K"},
        "prompt aspect_ratio num_images output_format safety_tolerance seed sync_mode resolution enable_web_search limit_generations",
        style="aspect_ratio", edit_ep="fal-ai/nano-banana-pro/edit",
        edit_sup="prompt image_urls aspect_ratio num_images output_format safety_tolerance seed sync_mode resolution enable_web_search limit_generations", max_ref=2,
    ),
    "fal-ai/nano-banana-2": _img(
        "Nano Banana 2 (Gemini 3.1 Flash Image)", "~3s", "Fast reasoning, multilingual text, infographics", "Lower-cost Flash tier",
        {"num_images": 1, "output_format": "png", "safety_tolerance": "4", "resolution": "1K", "limit_generations": True},
        "prompt aspect_ratio num_images output_format safety_tolerance seed sync_mode system_prompt resolution enable_web_search limit_generations thinking_level",
        style="aspect_ratio", edit_ep="fal-ai/nano-banana-2/edit",
        edit_sup="prompt image_urls aspect_ratio num_images output_format safety_tolerance seed sync_mode system_prompt resolution enable_web_search limit_generations thinking_level", max_ref=14,
    ),
    "fal-ai/gpt-image-1.5": _img(
        "GPT Image 1.5", "~15s", "Prompt adherence", "$0.034/image",
        {"quality": "medium", "num_images": 1, "output_format": "png"},
        "prompt image_size quality num_images output_format background sync_mode",
        style="gpt_literal", sizes={"landscape": "1536x1024", "square": "1024x1024", "portrait": "1024x1536"},
        edit_ep="fal-ai/gpt-image-1.5/edit", edit_sup="prompt image_urls image_size quality num_images output_format sync_mode", max_ref=16,
    ),
    "fal-ai/gpt-image-2": _img(
        "GPT Image 2", "~20s", "SOTA text rendering + CJK, world-aware photorealism", "$0.04–0.06/image",
        {"quality": "medium", "num_images": 1, "output_format": "png"},
        "prompt image_size quality num_images output_format sync_mode", sizes=_GPT2_SIZES,
        edit_ep="openai/gpt-image-2/edit", edit_sup="prompt image_urls quality num_images output_format sync_mode mask_image_url", max_ref=16,
    ),
    "openai/gpt-image-2.5/flare/text-to-image": _img(
        "GPT Image 2.5 Flare", "Fast", "Everyday creation, natural lighting and textures", "Token-based pricing",
        {"quality": "medium", "num_images": 1, "output_format": "png"}, _S_GPT25, sizes=_GPT2_SIZES,
        edit_ep="openai/gpt-image-2.5/flare/edit", edit_sup=_S_GPT25_E, max_ref=16,
    ),
    "openai/gpt-image-2.5/sunburst/text-to-image": _img(
        "GPT Image 2.5 Sunburst", "Slower", "Precision editing, subject and composition consistency", "Token-based pricing",
        {"quality": "medium", "num_images": 1, "output_format": "png"}, _S_GPT25, sizes=_GPT2_SIZES,
        edit_ep="openai/gpt-image-2.5/sunburst/edit", edit_sup=_S_GPT25_E, max_ref=16,
    ),
    "fal-ai/ideogram/v3": _img(
        "Ideogram V3", "~5s", "Best typography", "$0.03-0.09/image",
        {"rendering_speed": "BALANCED", "expand_prompt": True, "style": "AUTO"},
        "prompt image_size rendering_speed expand_prompt style seed",
        edit_ep="fal-ai/ideogram/v3/edit", edit_sup="prompt image_urls rendering_speed expand_prompt style seed", max_ref=1,
    ),
    "fal-ai/recraft/v4/pro/text-to-image": _img(
        "Recraft V4 Pro", "~8s", "Design, brand systems, production-ready", "$0.25/image",
        {"enable_safety_checker": False}, "prompt image_size enable_safety_checker colors background_color",
    ),
    "fal-ai/qwen-image": _img(
        "Qwen Image", "~12s", "LLM-based, complex text", "$0.02/MP",
        {"num_inference_steps": 30, "guidance_scale": 2.5, "num_images": 1, "output_format": "png", "acceleration": "regular"},
        "prompt image_size num_inference_steps guidance_scale num_images output_format acceleration seed sync_mode",
        edit_ep="fal-ai/qwen-image-2/pro/edit", edit_sup="prompt image_urls num_inference_steps guidance_scale num_images output_format acceleration seed sync_mode", max_ref=3,
    ),
    "fal-ai/krea/v2/medium/text-to-image": _img(
        "Krea 2 Medium", "~15-25s", "Illustration, anime, painting, expressive/artistic styles", "$0.030 (text) / $0.035 (style refs)",
        {"creativity": "medium"}, "prompt aspect_ratio creativity seed image_style_references", style="aspect_ratio",
    ),
    "fal-ai/krea/v2/large/text-to-image": _img(
        "Krea 2 Large", "~25-60s", "Photorealism, raw textured looks (motion blur, grain, film)", "$0.060 (text) / $0.065 (style refs)",
        {"creativity": "medium"}, "prompt aspect_ratio creativity seed image_style_references", style="aspect_ratio",
    ),
    "bytedance/seedream/v5/pro/text-to-image": _img(
        "Seedream 5.0 Pro", "~10s", "ByteDance flagship, dense layouts, native text in 14 languages", "$0.0675/image (≤1536²)",
        {"num_images": 1, "output_format": "png", "enable_safety_checker": False},
        "prompt image_size num_images output_format sync_mode enable_safety_checker",
        sizes={"landscape": {"width": 2048, "height": 1152}, "square": {"width": 1536, "height": 1536}, "portrait": {"width": 1152, "height": 2048}},
        edit_ep="bytedance/seedream/v5/pro/edit", edit_sup="prompt image_urls image_size num_images output_format sync_mode enable_safety_checker", max_ref=10,
    ),
    "bytedance/seedream/v5/lite/text-to-image": _img(
        "Seedream 5.0 Lite", "~5s", "Fast/cheap Seedream tier, high-res output", "$0.035/image",
        {"num_images": 1, "enable_safety_checker": False}, "prompt image_size num_images max_images sync_mode enable_safety_checker",
    ),
    "ideogram/v4/instant": _img(
        "Ideogram V4 (Instant)", "<1s", "Latest Ideogram typography, posters/logos, instant", "$0.0075/MP",
        {"expansion_model": "Medium", "output_format": "png", "enable_safety_checker": False},
        "prompt image_size expansion_model num_images seed sync_mode enable_safety_checker output_format",
    ),
    "ideogram/v4/fast": _img(
        "Ideogram V4 (Fast)", "~1s", "Ideogram V4 quality tiers via rendering_speed", "$0.005-0.018/MP",
        {"expansion_model": "Medium", "rendering_speed": "BALANCED"}, "prompt image_size expansion_model rendering_speed num_images seed sync_mode",
    ),
    "alibaba/qwen-image-3/text-to-image": _img(
        "Qwen Image 3", "~8s", "Complex CN/EN text rendering, prompt-guided resolution", "$0.04 (1K) / $0.075 (2K) per image",
        {"num_images": 1, "output_format": "png", "enable_prompt_expansion": False, "enable_safety_checker": False},
        "prompt negative_prompt image_size num_images seed sync_mode output_format enable_prompt_expansion enable_safety_checker",
        edit_ep="alibaba/qwen-image-3/edit", edit_sup="prompt image_urls negative_prompt num_images seed sync_mode output_format enable_prompt_expansion enable_safety_checker", max_ref=3,
    ),
    "microsoft/mai-image-2.5-pro": _img(
        "MAI Image 2.5 Pro", "~10s", "Microsoft flagship, hero imagery, precise typography", "~$0.17/image",
        {"num_images": 1, "output_format": "png"}, "prompt aspect_ratio num_images output_format sync_mode", style="aspect_ratio",
    ),
    "google/nano-banana-2-lite": _img(
        "Nano Banana 2 Lite", "<2s", "Gemini image family, sub-2s, 14 aspect ratios incl. extreme", "~$0.04/image (1K fixed)",
        {"num_images": 1, "output_format": "png", "safety_tolerance": "5"},
        "prompt aspect_ratio num_images seed output_format safety_tolerance sync_mode system_prompt limit_generations thinking_level",
        style="aspect_ratio", edit_ep="google/nano-banana-2-lite/edit",
        edit_sup="prompt image_urls aspect_ratio num_images seed output_format safety_tolerance sync_mode system_prompt", max_ref=4,
    ),
    "fal-ai/recraft/v4.1/text-to-image": _img(
        "Recraft V4.1", "~8s", "Design-first raster, brand systems, editorial", "$0.035/image",
        {"enable_safety_checker": False}, "prompt image_size enable_safety_checker colors background_color",
    ),
    "xai/grok-imagine-image/v2.0/text-to-image": _img(
        "Grok Imagine Image 2.0", "~5s", "xAI. Design-grade typography/layout, instruction following", "$0.06/image (1K medium)",
        {"num_images": 1, "output_format": "png", "resolution": "1k", "quality": "medium"},
        "prompt aspect_ratio num_images output_format resolution quality sync_mode", style="aspect_ratio",
        edit_ep="xai/grok-imagine-image/v2.0/edit", edit_sup="prompt image_urls num_images output_format resolution quality sync_mode", max_ref=3,
    ),
    "fal-ai/kling-image/v3/text-to-image": _img(
        "Kling Image v3", "~10s", "Kuaishou. Realistic detail, cheap native 2K, wide AR set", "$0.028/image",
        {"num_images": 1, "output_format": "png", "resolution": "2K"},
        "prompt aspect_ratio num_images output_format resolution negative_prompt sync_mode", style="aspect_ratio",
        edit_ep="fal-ai/kling-image/v3/image-to-image", edit_sup="prompt image_url aspect_ratio num_images output_format resolution sync_mode", max_ref=1, edit_param="image_url",
    ),
    "meta/muse-image/text-to-image": _img(
        "Meta Muse Image", "~5s", "Meta. Realism + typography at commodity price", "$0.01/image",
        {"num_images": 1, "output_format": "png"}, "prompt aspect_ratio num_images output_format sync_mode", style="aspect_ratio",
        edit_ep="meta/muse-image/edit", edit_sup="prompt image_urls num_images output_format sync_mode", max_ref=10,
    ),
}
DEFAULT_FAL_IMAGE_MODEL = "fal-ai/flux-2/klein/9b"

# ── FAL Video Models (20 entries) ────────────────────────────────────────────

_SIX_AR = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
_H3_A = {"480p": "768P", "540p": "768P", "720p": "768P", "768p": "768P", "1080p": "2K", "2k": "2K", "4k": "4K", "2160p": "4K"}
_H3M_A = {"480p": "480P", "540p": "480P", "720p": "768P", "768p": "768P", "1080p": "768P", "2k": "768P", "4k": "768P", "2160p": "768P"}
_H3T_A = {"480p": "480P", "540p": "480P", "720p": "768P", "768p": "768P", "1080p": "1080P", "2k": "1080P", "4k": "1080P", "2160p": "1080P"}


def _vid(display: str, speed: str, tier: str, strengths: str, text: Optional[str], image: str, **caps: Any) -> dict[str, Any]:
    return {"display": display, "speed": speed, "price": tier, "tier": tier, "strengths": strengths, "text_endpoint": text, "image_endpoint": image, "aspect_ratios": None, "resolutions": None, "durations": None, "audio": False, "negative": False, "seed": False, **caps}


FAL_VIDEO_MODELS: dict[str, dict[str, Any]] = {
    "ltx-2.3": _vid("LTX 2.3 (22B)", "~30-60s", "cheap", "22B model with native audio generation. Affordable.", "fal-ai/ltx-2.3-22b/text-to-video", "fal-ai/ltx-2.3-22b/image-to-video", audio=True, negative=True, seed=True),
    "ltx-2.5": _vid("LTX 2.5", "~30-90s", "cheap", "Lightricks open-source audio-video model. Native audio, up to 20s / 4K (i2v), camera-motion presets.", "lightricks/ltx-2.5/text-to-video/fast", "lightricks/ltx-2.5/image-to-video/fast", duration_int=True, aspect_ratios=("16:9", "9:16"), resolutions=("720p", "1080p", "1440p", "2160p"), resolution_aliases={"2k": "1440p", "4k": "2160p"}, durations=(6, 20), duration_enum=tuple(range(6, 21, 2)), duration_cap_by_resolution={"1440p": 10, "2160p": 10}, audio=True),
    "pixverse-v6": _vid("Pixverse v6", "~30-90s", "cheap", "Affordable. Negative prompts. 1-15s durations.", "fal-ai/pixverse/v6/text-to-video", "fal-ai/pixverse/v6/image-to-video", resolutions=("360p", "540p", "720p", "1080p"), durations=(1, 15), audio=True, negative=True, seed=True),
    "seedance-2.0-mini": _vid("Seedance 2.0 Mini", "~30-90s", "cheap", "ByteDance. Faster/cheaper Seedance tier, audio + lip-sync, 4-15s.", "bytedance/seedance-2.0/mini/text-to-video", "bytedance/seedance-2.0/mini/image-to-video", aspect_ratios=_SIX_AR, resolutions=("480p", "720p"), durations=(4, 15), audio=True),
    "veo3.1": _vid("Veo 3.1", "~60-120s", "premium", "Google DeepMind. Cinematic, native audio, strong prompt adherence.", "fal-ai/veo3.1", "fal-ai/veo3.1/image-to-video", aspect_ratios=("16:9", "9:16"), resolutions=("720p", "1080p", "4k"), durations=(4, 8), duration_enum=(4, 6, 8), duration_suffix="s", audio=True, negative=True, seed=True),
    "seedance-2.0": _vid("Seedance 2.0", "~60-120s", "premium", "ByteDance. Cinematic, synchronized audio + lip-sync, 4-15s.", "bytedance/seedance-2.0/text-to-video", "bytedance/seedance-2.0/image-to-video", aspect_ratios=_SIX_AR, resolutions=("480p", "720p", "1080p"), durations=(4, 15), audio=True),
    "seedance-2.5": _vid("Seedance 2.5", "~60-180s", "premium", "ByteDance flagship. Native 30s single-pass, audio in the same latent space, lip-sync.", "bytedance/seedance-2.5/text-to-video", "bytedance/seedance-2.5/image-to-video", aspect_ratios=_SIX_AR, image_drop_keys=("aspect_ratio",), resolutions=("480p", "720p"), durations=(4, 30), audio=True),
    "minimax-h3": _vid("MiniMax H3", "~60-180s", "premium", "MiniMax frontier. Native 2K (up to 4K), 5-15s, seven aspect ratios.", "minimax/h3/text-to-video", "minimax/h3/image-to-video", duration_int=True, image_drop_keys=("aspect_ratio",), aspect_ratios=_SIX_AR, resolutions=("768P", "2K", "4K"), resolution_aliases=_H3_A, durations=(5, 15), audio_native=True),
    "minimax-h3-max": _vid("MiniMax H3 Max (fal post-train)", "~5-30s", "premium", "fal's post-trained MiniMax H3. Top-ranked quality/prompt adherence/aesthetics, 768p in seconds, 5-15s.", "minimax/h3-max/text-to-video", "minimax/h3-max/image-to-video", duration_int=True, image_drop_keys=("aspect_ratio",), aspect_ratios=_SIX_AR, resolutions=("480P", "768P"), resolution_aliases=_H3M_A, durations=(5, 15), static_payload={"prompt_expansion_mode": "balanced"}, audio_native=True, seed=True),
    "minimax-h3-max-turbo": _vid("MiniMax H3 Max Turbo (fal post-train)", "~5-20s", "premium", "fal's throughput-tuned H3 Max variant. Near-Max quality at a fraction of the price/latency, 480P-1080P, 5-15s.", "minimax/h3-max-turbo/text-to-video", "minimax/h3-max-turbo/image-to-video", duration_int=True, image_drop_keys=("aspect_ratio",), aspect_ratios=_SIX_AR, resolutions=("480P", "768P", "1080P"), resolution_aliases=_H3T_A, durations=(5, 15), static_payload={"prompt_expansion_mode": "balanced"}, audio_native=True, seed=True),
    "flux-3": _vid("FLUX 3 (via FAL)", "~60-120s", "premium", "Black Forest Labs frontier video. Native audio, 5-20s, 8 aspect ratios.", "blackforestlabs/flux-3/text-to-video", "blackforestlabs/flux-3/image-to-video", duration_int=True, aspect_ratios=("21:9", "2:1", "16:9", "4:3", "1:1", "3:4", "9:16"), resolutions=("720p", "1080p"), durations=(5, 20), audio=True),
    "grok-imagine-1.5": _vid("Grok Imagine 1.5 (via FAL)", "~30-90s", "premium", "xAI. Fast stylized video with audio, 1-15s, cheap per second.", "xai/grok-imagine-video/v1.5/text-to-video", "xai/grok-imagine-video/v1.5/image-to-video", duration_int=True, image_drop_keys=("aspect_ratio",), aspect_ratios=("16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16"), resolutions=("480p", "720p", "1080p"), durations=(1, 15), audio_native=True),
    "gemini-omni-flash": _vid("Gemini Omni Flash 1.1 (via FAL)", "~60-120s", "premium", "Google. Text & image to video with native audio, physics-grounded motion, up to 4K, 3-10s.", "google/gemini-omni-flash/v1.1/text-to-video", "google/gemini-omni-flash/v1.1/image-to-video", duration_int=True, aspect_ratios=("16:9", "9:16"), resolutions=("360p", "720p", "1080p", "4k"), durations=(3, 10), audio_native=True),
    "kling-v3": _vid("Kling 3.0 (Standard)", "~60-180s", "premium", "Kuaishou frontier core model. Cinematic motion, native audio, 3-15s.", "fal-ai/kling-video/v3/standard/text-to-video", "fal-ai/kling-video/v3/standard/image-to-video", image_param_key="start_image_url", image_drop_keys=("aspect_ratio",), aspect_ratios=("16:9", "9:16", "1:1"), durations=(3, 15), audio=True, negative=True),
    "kling-v3-pro": _vid("Kling 3.0 Pro", "~60-180s", "premium", "Kling 3.0 top quality tier. Cinematic motion, native audio, 3-15s.", "fal-ai/kling-video/v3/pro/text-to-video", "fal-ai/kling-video/v3/pro/image-to-video", image_param_key="start_image_url", image_drop_keys=("aspect_ratio",), aspect_ratios=("16:9", "9:16", "1:1"), durations=(3, 15), audio=True, negative=True),
    "kling-v3-4k": _vid("Kling v3 4K", "~120-300s", "premium", "4K output, native audio (Chinese/English), 3-15s.", "fal-ai/kling-video/v3/4k/text-to-video", "fal-ai/kling-video/v3/4k/image-to-video", image_param_key="start_image_url", aspect_ratios=("16:9", "9:16", "1:1"), durations=(3, 15), audio=True, negative=True, seed=True),
    "kling-o3": _vid("Kling O3 (Standard)", "~60-180s", "premium", "Kuaishou frontier. Multi-shot native storytelling, optional audio, 3-15s.", "fal-ai/kling-video/o3/standard/text-to-video", "fal-ai/kling-video/o3/standard/image-to-video", image_drop_keys=("aspect_ratio",), aspect_ratios=("16:9", "9:16", "1:1"), durations=(3, 15), audio=True),
    "wan-3.0": _vid("Wan 3.0", "~60-180s", "premium", "Alibaba latest gen. 2-30s clips, native audio, up to 1080p, lip-sync.", "alibaba/wan-3.0/text-to-video", "alibaba/wan-3.0/image-to-video", image_param_key="start_image_url", duration_int=True, audio_param_key="audio", aspect_ratios=("16:9", "4:3", "1:1", "3:4", "9:16"), resolutions=("480p", "720p", "1080p"), durations=(2, 30), audio=True, seed=True),
    "wan-3.0-prime": _vid("Wan 3.0 Prime", "~60-180s", "premium", "Alibaba premium tier. Faster iteration, higher fidelity, 2-30s, native audio.", "alibaba/wan-3.0-prime/text-to-video", "alibaba/wan-3.0-prime/image-to-video", image_param_key="start_image_url", duration_int=True, audio_param_key="audio", aspect_ratios=("16:9", "4:3", "1:1", "3:4", "9:16"), resolutions=("480p", "720p", "1080p"), durations=(2, 30), audio=True, seed=True),
    "happy-horse": _vid("Happy Horse 1.1", "~60-120s", "premium", "Alibaba flagship. 1080p, native audio + multilingual lip-sync, 3-15s, nine aspect ratios.", "alibaba/happy-horse/v1.1/text-to-video", "alibaba/happy-horse/v1.1/image-to-video", duration_int=True, image_drop_keys=("aspect_ratio",), aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21", "5:4", "4:5"), resolutions=("720p", "1080p"), durations=(3, 15), audio_native=True, seed=True),
}
FAL_FAMILIES = FAL_VIDEO_MODELS
DEFAULT_FAL_VIDEO_MODEL = "pixverse-v6"

# ── DeepInfra Dynamic Video Models ───────────────────────────────────────────

DEEPINFRA_MODELS_LIST_URL = "https://api.deepinfra.com/models/list"
_deepinfra_video_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
_deepinfra_neg_until: float = 0.0
_DEEPINFRA_CACHE_TTL, _DEEPINFRA_NEG_TTL = 600.0, 60.0


def reset_deepinfra_cache() -> None:
    global _deepinfra_video_cache, _deepinfra_neg_until
    _deepinfra_video_cache, _deepinfra_neg_until = None, 0.0


def fetch_deepinfra_video_models(*, timeout: float = 5.0, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch DeepInfra models tagged ``"video-gen"`` with offline/timeout ``[]`` fallback."""
    global _deepinfra_video_cache, _deepinfra_neg_until
    now = time.monotonic()
    if not force_refresh:
        if _deepinfra_video_cache is not None and (now - _deepinfra_video_cache[0]) < _DEEPINFRA_CACHE_TTL:
            return list(_deepinfra_video_cache[1])
        if now < _deepinfra_neg_until:
            return []
    headers = {"Accept": "application/json"}
    if api_key := os.environ.get("DEEPINFRA_API_KEY", "").strip():
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = httpx.get(DEEPINFRA_MODELS_LIST_URL, headers=headers, timeout=httpx.Timeout(float(timeout), connect=min(float(timeout), 5.0)), follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.info("muse.media.catalog: DeepInfra video catalog unavailable (%s)", type(exc).__name__)
        _deepinfra_neg_until = time.monotonic() + _DEEPINFRA_NEG_TTL
        return []
    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), list) else [])
    matched, seen = [], set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        tags_raw = item.get("tags") if isinstance(item.get("tags"), list) else meta.get("tags")
        if "video-gen" not in ([str(t).strip().lower() for t in tags_raw] if isinstance(tags_raw, list) else []):
            continue
        mid = str(item.get("model_name") or item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        display = str(item.get("display") or mid.split("/")[-1])
        note = " ".join(str(item.get("description") or meta.get("description") or "").split())[:60]
        matched.append({"id": mid, "name": display, "display": display, "note": note, "strengths": note})
    _deepinfra_video_cache, _deepinfra_neg_until = (time.monotonic(), matched), 0.0
    return list(matched)


def _catalog_rows(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": mid, "name": str(m.get("display") or mid), "display": str(m.get("display") or mid),
         "note": str(m.get("strengths") or "")[:60], "strengths": str(m.get("strengths") or "")}
        for mid, m in table.items()
    ]


def list_fal_image_models() -> list[dict[str, Any]]:
    return _catalog_rows(FAL_IMAGE_MODELS)


def list_fal_video_models() -> list[dict[str, Any]]:
    return _catalog_rows(FAL_VIDEO_MODELS)


def backend_models(kind: str, backend: str) -> list[dict[str, Any]]:
    """Drop-in replacement for ``companion/modules/model_catalog.py:_backend_models``."""
    k, b = str(kind or "").strip().lower(), str(backend or "").strip().lower()
    if k == "image" and b == "fal":
        return list_fal_image_models()
    if k == "video" and b == "fal":
        return list_fal_video_models()
    if k == "video" and b == "deepinfra":
        return fetch_deepinfra_video_models()
    return []
