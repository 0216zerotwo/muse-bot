"""The user's own looks for the companion: uploaded pictures and imported pets (from nanobot).

A picture (uploaded, or taken from 创意) is re-encoded to WebP, at most 1024 px,
which also drops EXIF data such as where a phone photo was taken. A pet is a
Codex pet sprite atlas (1536×1872: 8 columns × 9 rows of 192×208 cells, as
made by Codex's hatch-pet skill), optionally with our errands atlas
(1536×1248: 6 more rows). Each lives in its own folder under
``<webui>/avatars/<id>/`` with a small ``meta.json``.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .companion_state import DEFAULT_COMPANION_AVATAR, read_companion_state, update_companion_state
from .paths import get_webui_dir

CELL_WIDTH, CELL_HEIGHT, COLUMNS = 192, 208, 8
SHEET_SIZE = (COLUMNS * CELL_WIDTH, 9 * CELL_HEIGHT)
ACTIONS_SIZE = (COLUMNS * CELL_WIDTH, 6 * CELL_HEIGHT)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_PICTURE_SIDE = 1024
_MAX_NAME_LEN = 40
_ID = re.compile(r"^[0-9a-f]{12}$")
_DATA_URL = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)


def avatars_root() -> Path:
    return get_webui_dir() / "avatars"


def _decode(data: Any, field: str) -> bytes:
    """Bytes from a ``data:image/...;base64,`` URL (or bare base64)."""
    if not isinstance(data, str) or not data:
        raise ValueError(f"{field} is required")
    payload = _DATA_URL.sub("", data, count=1)
    if len(payload) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
        raise ValueError(f"{field} is too large")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{field} is not valid base64 image data") from None


def _open(data: bytes, field: str) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ValueError(f"{field} is too large")
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError(f"{field} is not an image") from None
    return image


def _name(value: Any, fallback: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text[:_MAX_NAME_LEN] or fallback


def _save_meta(folder: Path, meta: dict[str, Any]) -> None:
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_folder() -> tuple[str, Path]:
    avatar_id = uuid.uuid4().hex[:12]
    folder = avatars_root() / avatar_id
    folder.mkdir(parents=True, exist_ok=False)
    return avatar_id, folder


def _picture(image: Image.Image) -> Image.Image:
    # Phone photos carry their rotation in EXIF; apply it, then save without EXIF.
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA" if "A" in image.getbands() or image.mode == "P" else "RGB")
    image.thumbnail((MAX_PICTURE_SIDE, MAX_PICTURE_SIDE), Image.Resampling.LANCZOS)
    return image


def save_picture(data: bytes, *, name: str) -> dict[str, Any]:
    image = _picture(_open(data, "image"))
    avatar_id, folder = _new_folder()
    image.save(folder / "image.webp", "WEBP", quality=90, method=6)
    meta = {"id": avatar_id, "kind": "image", "name": name, "created_at": _now(), "files": {"image": "image.webp"}}
    _save_meta(folder, meta)
    return meta


def save_character(pictures: dict[str, Image.Image], *, name: str) -> dict[str, Any]:
    """A look drawn from a description (``avatar_make``): ``image``, plus the moods it has
    (``working``, ``happy``), each on a transparent background."""
    avatar_id, folder = _new_folder()
    files = {}
    for mood, picture in pictures.items():
        picture.save(folder / f"{mood}.webp", "WEBP", quality=90, method=6)
        files[mood] = f"{mood}.webp"
    meta = {"id": avatar_id, "kind": "image", "character": True, "name": _name(name, "新形象"),
            "created_at": _now(), "files": files}
    _save_meta(folder, meta)
    return meta


def _meta(avatar_id: str) -> tuple[Path, dict[str, Any]]:
    folder = _folder(avatar_id)
    return folder, json.loads((folder / "meta.json").read_text(encoding="utf-8"))


def add_files(avatar_id: str, files: dict[str, str]) -> None:
    """More files of a look (its motion clips, ``avatar_motion``), by role → file name."""
    folder, meta = _meta(avatar_id)
    meta["files"] = {**meta.get("files", {}), **files}
    _save_meta(folder, meta)


def set_flag(avatar_id: str, key: str, value: bool) -> None:
    folder, meta = _meta(avatar_id)
    if value:
        meta[key] = True
    else:
        meta.pop(key, None)
    _save_meta(folder, meta)


def save_pet(sheet: bytes, actions: bytes | None, *, name: str) -> dict[str, Any]:
    atlas = _open(sheet, "sheet")
    if atlas.size != SHEET_SIZE:
        raise ValueError(f"a pet sheet must be {SHEET_SIZE[0]}×{SHEET_SIZE[1]} (Codex pet spritesheet)")
    extra = _open(actions, "actions") if actions else None
    if extra is not None and extra.size != ACTIONS_SIZE:
        raise ValueError(f"an errands sheet must be {ACTIONS_SIZE[0]}×{ACTIONS_SIZE[1]}")
    avatar_id, folder = _new_folder()
    files = {"sheet": "spritesheet.webp"}
    atlas.convert("RGBA").save(folder / "spritesheet.webp", "WEBP", lossless=True, method=6)
    if extra is not None:
        extra.convert("RGBA").save(folder / "actions.webp", "WEBP", lossless=True, method=6)
        files["actions"] = "actions.webp"
    meta = {"id": avatar_id, "kind": "pet", "name": name, "created_at": _now(), "files": files}
    _save_meta(folder, meta)
    return meta


def upload(payload: dict[str, Any]) -> dict[str, Any]:
    """``{"kind": "image", "name", "image"}`` or ``{"kind": "pet", "name", "sheet", "actions"?}`` (data URLs)."""
    kind = payload.get("kind")
    if kind == "image":
        return save_picture(_decode(payload.get("image"), "image"), name=_name(payload.get("name"), "我的图片"))
    if kind == "pet":
        actions = payload.get("actions")
        return save_pet(
            _decode(payload.get("sheet"), "sheet"),
            _decode(actions, "actions") if actions else None,
            name=_name(payload.get("name"), "我的宠物"),
        )
    raise ValueError("kind must be image or pet")


def from_creation(path: Path, *, name: str) -> dict[str, Any]:
    """A picture from 创意 (``path`` already resolved inside it by the caller)."""
    if not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("unknown or too large picture")
    return save_picture(path.read_bytes(), name=_name(name, "我的图片"))


def _folder(avatar_id: str) -> Path:
    if not isinstance(avatar_id, str) or not _ID.match(avatar_id):
        raise ValueError("unknown avatar")
    folder = avatars_root() / avatar_id
    if not (folder / "meta.json").is_file():
        raise ValueError("unknown avatar")
    return folder


def delete(avatar_id: str) -> None:
    """Remove a look; if the companion was wearing it, it goes back to the default pet."""
    shutil.rmtree(_folder(avatar_id))
    if read_companion_state().get("avatar") in (f"custom:{avatar_id}", f"image:{avatar_id}"):
        update_companion_state({"avatar": DEFAULT_COMPANION_AVATAR})


def is_character(avatar_id: str) -> bool:
    """A look drawn from a description (not a picture or pet the user brought)."""
    try:
        return bool(json.loads((_folder(avatar_id) / "meta.json").read_text(encoding="utf-8")).get("character"))
    except (ValueError, OSError):
        return False


def avatar_value(meta: dict[str, Any]) -> str:
    """How the companion state refers to it."""
    return f"{'custom' if meta['kind'] == 'pet' else 'image'}:{meta['id']}"


def list_avatars(sign: Callable[[Path], str | None]) -> dict[str, Any]:
    root = avatars_root()
    items = []
    for meta_path in root.glob("*/meta.json") if root.is_dir() else []:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not _ID.match(str(meta.get("id", ""))) or meta.get("kind") not in ("image", "pet"):
            continue
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}

        def url(key: str) -> str | None:
            name = files.get(key)
            path = meta_path.parent / name if isinstance(name, str) else None
            if path is None or not path.is_file():
                return None
            signed = sign(path)
            # The files are cached for good; a remade one (a clip done again) gets a new link.
            return f"{signed}?v={path.stat().st_mtime_ns // 1_000_000}" if signed else None

        items.append(
            {
                "id": meta["id"],
                "kind": meta["kind"],
                "name": meta.get("name") or "",
                "created_at": meta.get("created_at") or "",
                "image_url": url("image"),
                "sheet_url": url("sheet"),
                "actions_url": url("actions"),
                # A drawn character: shown whole (not cropped round), with its moods.
                "character": bool(meta.get("character")),
                "working_url": url("working"),
                "happy_url": url("happy"),
                # Its motion clips by state (idle, thinking, working, waiting), as they are made.
                "motions": {key[len("motion_"):]: url(key) for key in files if key.startswith("motion_") and url(key)},
                "motion_pending": bool(meta.get("motion_pending")),
            }
        )
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"items": items}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
