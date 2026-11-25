"""创意: the files the companion made or sent to the user, filed by kind (ported from nanobot).

Files live under ``<companion dir>/创意/图片``, ``视频``, ``文档`` and ``音频``. They arrive:

- from generation tools (image/video/audio/music/speech tools), right when the
  tool returns — links are downloaded before they expire (``on_post_tool_call``);
- as files the companion sends in the chat (images, videos, documents);
- as documents it writes straight into ``创意/文档`` (its persona asks for that).

Pictures it merely found on the web, and web-page screenshots, stay out. An
index beside the files keeps titles and where each came from; removing a file
from the list only hides it — the file stays on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from .paths import get_webui_dir

logger = logging.getLogger(__name__)

CREATIONS_DIR = "创意"
KIND_DIRS = {"image": "图片", "video": "视频", "document": "文档", "audio": "音频"}
_DIR_KINDS = {folder: kind for kind, folder in KIND_DIRS.items()}
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".svg"}
_VIDEO = {".mp4", ".mov", ".webm", ".m4v"}
_AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
_INDEX = ".index.json"
_MEDIA_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+")
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|*?]+\.[A-Za-z0-9]{2,5}")
_SPACED_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\r\n\"'`<>|*?]*?\.[A-Za-z0-9]{2,5}(?=$|[\s\"'`),;])")
_QUOTED_LOCAL_PATH = re.compile(r"[\"'`]((?:[A-Za-z]:[\\/]|/)[^\"'`<>|*?\r\n]+\.[A-Za-z0-9]{2,5})[\"'`]")
_MEDIA_TAG_LINE = re.compile(r"MEDIA:\s*([^\r\n]+)")
_JSON_MEDIA_KEYS = frozenset({
    "image", "video", "video_path", "file_path", "audio_path", "audio",
    "path", "output_path", "media_tag", "url", "image_url", "video_url", "audio_url",
})
_MEDIA_TOOL = re.compile(r"(image|video|audio|music|speech|tts)", re.IGNORECASE)
_SCREENSHOT = re.compile(r"screenshot|^page-\d", re.IGNORECASE)
_MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 180
_MAX_REDIRECTS = 3
_LOCK = threading.Lock()


def creations_root() -> Path:
    return get_webui_dir() / CREATIONS_DIR


def kind_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE:
        return "image"
    if suffix in _VIDEO:
        return "video"
    if suffix in _AUDIO:
        return "audio"
    return "document"


# ---------- index ----------


def _read_index(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / _INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("entries", {})
    data.setdefault("imported", {})
    return data


def _write_index(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{_INDEX}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / _INDEX)


def safe_title(text: str, limit: int = 40) -> str:
    title = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text).strip(" .")
    return title[:limit].strip() or "未命名"


def _display_title(stem: str) -> str:
    """A file name without the "YYYY-MM-DD " it was filed under; the list already groups by day."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}[ _-]*", "", stem).strip() or stem


def _place(root: Path, kind: str, title: str, suffix: str) -> Path:
    folder = root / KIND_DIRS[kind]
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{datetime.now():%Y-%m-%d} {safe_title(title)}"
    target = folder / f"{base}{suffix}"
    counter = 2
    while target.exists():
        target = folder / f"{base} ({counter}){suffix}"
        counter += 1
    return target


def import_file(source: Path, *, key: str, title: str, origin: str, prompt: Optional[str] = None,
                created_at: Optional[str] = None, move: bool = False) -> Optional[str]:
    """File ``source`` in its kind folder once per ``key``; returns its id (path under 创意)."""
    from .secrets_guard import is_secret_path

    if is_secret_path(source):
        return None
    root = creations_root()
    with _LOCK:
        data = _read_index(root)
        if key in data["imported"]:
            return data["imported"][key]
        if not source.is_file():
            return None
        target = _place(root, kind_of(source.name), title, source.suffix.lower())
        if move:
            shutil.move(str(source), target)
        else:
            shutil.copy2(source, target)
        rel = target.relative_to(root).as_posix()
        data["imported"][key] = rel
        data["entries"][rel] = {
            "title": title, "origin": origin, "prompt": prompt,
            "created_at": created_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _write_index(root, data)
        return rel


def hide_creation(creation_id: str) -> None:
    """Take a file off the 创意 list; the file itself stays on disk."""
    root = creations_root()
    path = (root / creation_id).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise ValueError("找不到这个作品")
    with _LOCK:
        data = _read_index(root)
        data["entries"].setdefault(creation_id, {})["hidden"] = True
        _write_index(root, data)


# ---------- sources ----------


def is_screenshot(path: Path) -> bool:
    return bool(_SCREENSHOT.search(path.name)) or "screenshots" in {part.lower() for part in path.parts}


def collect_sent_file(path: Path, *, title: str) -> None:
    """File what the companion sent in the chat (web-page screenshots stay out)."""
    from .secrets_guard import is_secret_path

    if is_secret_path(path) or not path.is_file() or is_screenshot(path):
        return
    try:
        if path.resolve().is_relative_to(creations_root().resolve()):
            return  # already in 创意
        stat = path.stat()
        import_file(path, key=f"file:{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}",
                    title=title or path.stem, origin="chat")
    except OSError as exc:
        logger.warning("companion: could not file %s in 创意: %s", path.name, exc)


def reply_title(text: str) -> str:
    """A short title from the reply that carried the file."""
    for line in (text or "").splitlines():
        plain = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|https?://\S+|[*_`#>]", r"\1", line)
        plain = re.split(r"[：:。！？!?；;]", plain, maxsplit=1)[0].strip(" ，,")
        if plain:
            return safe_title(plain, 30)
    return ""


def _media_in(text: str) -> tuple[list[str], list[Path]]:
    """Media links and existing local media files mentioned in a tool result."""
    from .secrets_guard import is_secret_path

    urls: list[str] = []
    files: list[Path] = []
    raw_text = text or ""

    def _add_candidate(raw_val: str) -> None:
        cand = raw_val.strip().strip("\"'`")
        if cand.upper().startswith("MEDIA:"):
            cand = cand[6:].strip().strip("\"'`")
        if not cand:
            return
        if cand.lower().startswith(("http://", "https://")):
            url = cand.rstrip(".,;:!?\"')")
            if kind_of(urlparse(url).path) in ("image", "video", "audio") and url not in urls:
                urls.append(url)
            return
        with_slashes = cand.replace("\\\\", "\\")
        for variant in (cand, with_slashes):
            try:
                path = Path(variant)
                if not is_secret_path(path) and kind_of(path.name) in ("image", "video", "audio") and path.is_file():
                    if path not in files:
                        files.append(path)
                    return
            except (OSError, ValueError):
                continue

    def _walk_json(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and str(k).lower() in _JSON_MEDIA_KEYS:
                    _add_candidate(v)
                elif isinstance(v, (dict, list)):
                    _walk_json(v)
                elif isinstance(k, str) and str(k).lower() in _JSON_MEDIA_KEYS and isinstance(v, list):
                    for item in v:
                        if isinstance(item, str):
                            _add_candidate(item)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    _add_candidate(item)
                elif isinstance(item, (dict, list)):
                    _walk_json(item)

    stripped = raw_text.strip()
    if stripped.startswith(("{", "[")):
        try:
            _walk_json(json.loads(stripped))
        except ValueError:
            pass

    for match in _MEDIA_TAG_LINE.finditer(raw_text):
        _add_candidate(match.group(1))
    for match in _QUOTED_LOCAL_PATH.finditer(raw_text):
        _add_candidate(match.group(1))
    for match in _MEDIA_URL.finditer(raw_text):
        _add_candidate(match.group(0))
    for match in _SPACED_LOCAL_PATH.finditer(raw_text):
        _add_candidate(match.group(0))
    for match in _LOCAL_PATH.finditer(raw_text):
        _add_candidate(match.group(0))
    return urls, files


async def _download(url: str, target_dir: Path) -> Optional[Path]:
    """Fetch a public media URL, re-checking every redirect; None when not allowed or too big."""
    from .net_safety import async_download_to_file

    downloaded, _ = await async_download_to_file(
        url, target_dir, max_bytes=_MAX_DOWNLOAD_BYTES, timeout=_DOWNLOAD_TIMEOUT_S, max_redirects=_MAX_REDIRECTS
    )
    return downloaded


async def collect_generated(text: str, *, title: str, prompt: Optional[str]) -> None:
    """File the media a generation tool returned: local files are copied, links downloaded."""
    root = creations_root()
    urls, files = _media_in(text)
    fallback = {"image": "图片", "video": "视频", "audio": "语音"}
    for path in files:
        if is_screenshot(path):
            continue
        stat = path.stat()
        import_file(path, key=f"file:{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}",
                    title=title or fallback[kind_of(path.name)], origin="generated", prompt=prompt)
    for url in urls:
        key = f"url:{url.split('?', 1)[0]}"
        with _LOCK:
            if key in _read_index(root)["imported"]:
                continue
        try:
            downloaded = await _download(url, root)
        except Exception as exc:
            logger.warning("companion: could not download generated media (%s)", type(exc).__name__)
            continue
        if downloaded is None:
            continue
        named = downloaded.with_name(downloaded.name + Path(urlparse(url).path).suffix.lower())
        downloaded.rename(named)
        try:
            import_file(named, key=key, title=title or fallback[kind_of(named.name)],
                        origin="generated", prompt=prompt, move=True)
        finally:
            named.unlink(missing_ok=True)


def on_generation_result(tool_name: str, args: Any, result: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """From the plugin's post_tool_call hook (agent thread): file what a generation tool made."""
    if loop is None or not isinstance(result, str) or not _MEDIA_TOOL.search(tool_name or ""):
        return
    from . import computer

    if computer.is_browser_tool(tool_name) or tool_name.startswith("browser") or "browser_" in tool_name or "vision" in tool_name:
        return  # screenshots and looking at pictures are not creations
    raw = args.get("prompt") if isinstance(args, dict) else None
    prompt = raw.strip() if isinstance(raw, str) and raw.strip() else None
    asyncio.run_coroutine_threadsafe(
        collect_generated(result, title=safe_title(prompt, 30) if prompt else "", prompt=prompt), loop)


# ---------- listing ----------


THUMB_SIDE = 480


def thumbnail(path: Path) -> Path:
    """A small WebP of a picture for 资源库's grid (its longest side ``THUMB_SIDE``), made once and
    kept beside the companion's data; the picture itself when it can't be made. A grid of 1–2 MB
    originals was 20 MB to open on a phone."""
    import hashlib

    stat = path.stat()
    key = hashlib.sha1(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()[:24]
    target = get_webui_dir() / "thumbs" / f"{key}.webp"
    if target.is_file():
        return target
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((THUMB_SIDE, THUMB_SIDE))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            image.save(tmp, "WEBP", quality=80)
            os.replace(tmp, target)
        return target
    except Exception as exc:  # an odd picture: the grid shows the original
        logger.info("companion: no thumbnail for %s: %s", path.name, exc)
        return path


def list_creations(sign: Callable[[str], str]) -> dict[str, Any]:
    """Everything filed under 创意 (newest first) with counts per kind; hidden files left out."""
    root = creations_root()
    entries = _read_index(root)["entries"]
    items: list[dict[str, Any]] = []
    for folder, kind in _DIR_KINDS.items():
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = path.relative_to(root).as_posix()
            meta = entries.get(rel, {})
            if meta.get("hidden"):
                continue
            stat = path.stat()
            created = meta.get("created_at") or datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
            items.append({
                "id": rel, "kind": kind, "name": path.name,
                "title": meta.get("title") or _display_title(path.stem),
                "origin": meta.get("origin") or "workspace", "prompt": meta.get("prompt"),
                "size": stat.st_size, "created_at": created, "url": sign(rel),
            })
    items.sort(key=lambda item: item["created_at"], reverse=True)
    counts = {kind: sum(1 for item in items if item["kind"] == kind) for kind in KIND_DIRS}
    return {"items": items, "counts": counts}
