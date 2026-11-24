"""Drawing with the image model set in 模型, for the app's own pictures (a new look for the
companion, the icon set): one picture from a prompt, and its plain background cut away.

Standalone (no imports from the plugin), so tools/draw_icons.py can use it outside the server.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Optional

from PIL import Image

logger = logging.getLogger(__name__)


def generate(prompt: str, reference: Optional[str] = None) -> tuple[Optional[bytes], str]:
    """The picture's bytes (or None) and, when it failed, why. ``reference``: a picture to keep to."""
    from muse.media.image_gen import _handle_image_generate

    args: dict[str, Any] = {"prompt": prompt, "aspect_ratio": "square"}
    if reference:
        args["image_url"] = reference
    try:
        result = json.loads(_handle_image_generate(args))
    except Exception as exc:
        return None, str(exc) or type(exc).__name__
    image = result.get("image") if isinstance(result, dict) and result.get("success") else None
    if not isinstance(image, str) or not image:
        return None, str((result or {}).get("error") or "生图模型没有返回图片")[:200]
    from .models import record_media

    record_media("image")
    if image.startswith("data:"):
        import base64

        return base64.b64decode(image.split(",", 1)[1]), ""
    if image.startswith(("http://", "https://")):
        try:
            from .net_safety import fetch_bytes

            raw_bytes, _ = fetch_bytes(image, max_bytes=20_000_000, timeout=60)
            return raw_bytes, ""
        except ImportError:
            with urllib.request.urlopen(image, timeout=60) as response:  # noqa: S310 - standalone fallback
                return response.read(20_000_000), ""
        except Exception as exc:
            return None, str(exc) or type(exc).__name__
    return Path(image).read_bytes(), ""


def cut_out(raw: bytes, side: int) -> Image.Image:
    """The plain light background flooded away from the edges; trimmed and centred on a square."""
    rgba = Image.open(io.BytesIO(raw)).convert("RGBA")
    rgba.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    clear_background(rgba)
    return square(rgba, rgba.getbbox(), side)


def square(rgba: Image.Image, box: Optional[tuple[int, int, int, int]], side: int) -> Image.Image:
    """``box`` of ``rgba`` centred on a transparent square, ``side`` pixels."""
    rgba = rgba.crop(box or (0, 0, *rgba.size))
    size = int(max(rgba.size) * 1.1)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(rgba, ((size - rgba.width) // 2, (size - rgba.height) // 2))
    return out.resize((side, side), Image.Resampling.LANCZOS)


def clear_background(rgba: Image.Image) -> None:
    """Makes the plain light background (what touches the edges) transparent, in place."""
    width, height = rgba.size
    pixels = rgba.load()
    seen: set[tuple[int, int]] = set()
    stack = [(x, y) for x in range(width) for y in (0, height - 1)] + [(x, y) for y in range(height) for x in (0, width - 1)]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < width and 0 <= y < height):
            continue
        seen.add((x, y))
        r, g, b, _ = pixels[x, y]
        if min(r, g, b) < 232:
            continue
        pixels[x, y] = (255, 255, 255, 0)
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
