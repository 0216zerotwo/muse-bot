"""On-demand neural Read Aloud (TTS) API with speech text cleaning, SHA-256 disk cache, and signed URLs."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

MAX_TTS_CHARS = 4000
MAX_AUDIO_CACHE_FILES = 200
TTS_PREFIX = "/api/webui/companion/tts/file"

_SYNTH_LOCKS_GUARD = threading.Lock()
_SYNTH_LOCKS: dict[str, list[Any]] = {}  # digest -> [threading.Lock, active_refcount]

_MEDIA_LINE_RE = re.compile(r"^\s*MEDIA:.*$", re.MULTILINE)
_CARD_BLOCK_RE = re.compile(r"```(?:card|ui|json)[ \t]*\n[\s\S]*?(?:\n```|$)", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n([\s\S]*?)\n```")
_OPEN_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n[\s\S]*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"']+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CITATION_REF_RE = re.compile(r"\[\d{1,2}\]")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s*>\s*", re.MULTILINE)
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_INLINE_MD_RE = re.compile(r"[`*_~]+")
_MULTI_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")


class TtsError(ValueError):
    """Structured TTS error carrying an HTTP status code and user-facing Chinese message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message


def tts_available() -> bool:
    """Return True when ``edge_tts`` is importable or an OpenAI-compatible TTS key is configured."""
    try:
        from muse.media._common import get_secret, load_media_section
        from muse.media.tts import _has_edge_tts
    except Exception:
        return False

    try:
        tts_cfg = load_media_section("tts")
        if tts_cfg.get("enabled") is False:
            return False
        if _has_edge_tts():
            return True
        osec = tts_cfg.get("openai") if isinstance(tts_cfg.get("openai"), dict) else {}
        if str(osec.get("api_key") or tts_cfg.get("api_key") or "").strip():
            return True
        return bool(get_secret("VOICE_TOOLS_OPENAI_KEY") or get_secret("OPENAI_API_KEY"))
    except Exception:
        return False


def _replace_code_block(match: re.Match[str]) -> str:
    body = (match.group(1) or "").strip()
    if not body:
        return ""
    if len(body) > 80 or "\n" in body:
        return "，这里有一段代码，请查看屏幕。"
    return f" {body} "


def clean_text_for_speech(text: str) -> str:
    """Strip UI/card blocks, simplify long code blocks, remove URLs/Markdown, and cap to 4,000 chars."""
    raw = str(text or "")
    if not raw.strip():
        return ""
    out = _MEDIA_LINE_RE.sub("", raw)
    out = _CARD_BLOCK_RE.sub(" ", out)
    out = _CODE_BLOCK_RE.sub(_replace_code_block, out)
    out = _OPEN_CODE_BLOCK_RE.sub("，这里有一段代码，请查看屏幕。", out)
    out = _IMAGE_RE.sub(lambda m: f" {m.group(1).strip()} " if m.group(1).strip() else " ", out)
    out = _LINK_RE.sub(r"\1", out)
    out = _BARE_URL_RE.sub(" ", out)
    out = _HTML_TAG_RE.sub(" ", out)
    out = _CITATION_REF_RE.sub("", out)
    out = _TABLE_SEP_RE.sub("", out)
    out = _HEADING_RE.sub("", out)
    out = _QUOTE_RE.sub("", out)
    out = _HR_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = out.replace("|", "，")
    out = _INLINE_MD_RE.sub("", out)

    lines = [_MULTI_SPACE_RE.sub(" ", line).strip() for line in out.splitlines()]
    compact = _MULTI_NEWLINE_RE.sub("\n", "\n".join(line for line in lines if line)).strip()
    if len(compact) > MAX_TTS_CHARS:
        compact = compact[:MAX_TTS_CHARS].rstrip()
    return compact


def _resolve_provider_and_voice(payload: dict[str, Any]) -> tuple[str, str, Optional[str], Optional[float]]:
    from muse.media._common import get_secret, load_media_section
    from muse.media.tts import DEFAULT_EDGE_TTS_VOICE, DEFAULT_OPENAI_TTS_VOICE, _has_edge_tts

    tts_cfg = load_media_section("tts")
    provider = str(payload.get("provider") or tts_cfg.get("provider") or "").strip().lower()
    if not provider:
        osec = tts_cfg.get("openai") if isinstance(tts_cfg.get("openai"), dict) else {}
        has_openai = bool(
            str(osec.get("api_key") or "").strip()
            or get_secret("VOICE_TOOLS_OPENAI_KEY")
            or get_secret("OPENAI_API_KEY")
        )
        if has_openai:
            provider = "openai"
        elif _has_edge_tts():
            provider = "edge"
        else:
            provider = "openai"

    if provider not in ("edge", "openai", "nous"):
        raise TtsError(400, "不支持的语音朗读引擎")

    if provider == "edge":
        esec = tts_cfg.get("edge") if isinstance(tts_cfg.get("edge"), dict) else {}
        voice = str(payload.get("voice") or esec.get("voice") or DEFAULT_EDGE_TTS_VOICE).strip()
    else:
        osec = tts_cfg.get("openai") if isinstance(tts_cfg.get("openai"), dict) else {}
        voice = str(payload.get("voice") or osec.get("voice") or DEFAULT_OPENAI_TTS_VOICE).strip()

    model = str(payload.get("model") or "").strip() or None
    raw_speed = payload.get("speed")
    speed: Optional[float] = None
    if raw_speed is not None and raw_speed != "":
        try:
            speed = round(max(0.25, min(4.0, float(raw_speed))), 2)
        except (TypeError, ValueError):
            raise TtsError(400, "语速参数不正确") from None
    return provider, voice, model, speed


def _cache_key(provider: str, voice: str, clean_text: str, speed: Optional[float] = None) -> str:
    material = f"{provider}\0{voice}\0{clean_text}" if speed is None else f"{provider}\0{voice}\0{speed}\0{clean_text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _friendly_error_zh(raw_error: str) -> str:
    low = (raw_error or "").lower()
    if "not installed" in low:
        return "语音朗读组件尚未安装，请先安装 edge-tts 或配置 OpenAI 语音服务"
    if "api_key" in low or "not set" in low:
        return "还没有配置语音朗读服务，请先安装 edge-tts 或配置 OpenAI API Key"
    if "unsupported" in low:
        return "不支持的语音朗读引擎"
    if "timeout" in low or "timed out" in low:
        return "语音生成超时了，请稍后再试"
    return "语音生成失败，请稍后再试"


def audio_cache_dir() -> Path:
    """Return the cached TTS audio directory ``<MUSE_HOME>/cache/audio``."""
    from muse.media._common import get_media_cache_dir

    return get_media_cache_dir("audio")


@contextlib.contextmanager
def _digest_singleflight(digest: str):
    """Serialize concurrent TTS synthesis requests for identical ``digest`` keys."""
    with _SYNTH_LOCKS_GUARD:
        slot = _SYNTH_LOCKS.get(digest)
        if slot is None:
            slot = [threading.Lock(), 0]
            _SYNTH_LOCKS[digest] = slot
        slot[1] += 1
        lock = slot[0]
    try:
        with lock:
            yield
    finally:
        with _SYNTH_LOCKS_GUARD:
            slot[1] -= 1
            if slot[1] <= 0:
                _SYNTH_LOCKS.pop(digest, None)


def _prune_audio_cache(cache_dir: Path, *, max_files: int = MAX_AUDIO_CACHE_FILES, keep: Optional[Path] = None) -> None:
    """Retain at most ``max_files`` newest ``tts_*.mp3`` entries in ``cache_dir``."""
    if max_files <= 0 or not cache_dir.is_dir():
        return
    entries: list[tuple[int, str, Path]] = []
    for path in cache_dir.glob("tts_*.mp3"):
        if path.name.startswith("."):
            continue
        with contextlib.suppress(OSError):
            st = path.stat()
            if path.is_file():
                entries.append((st.st_mtime_ns, path.name, path))
    if len(entries) <= max_files:
        return
    entries.sort(key=lambda item: (item[0], item[1]))
    excess = len(entries) - max_files
    keep_resolved = keep.resolve() if keep is not None else None
    removed = 0
    for _, _, path in entries:
        if removed >= excess:
            break
        if keep_resolved is not None:
            with contextlib.suppress(OSError):
                if path.resolve() == keep_resolved:
                    continue
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
            removed += 1


def synthesize(adapter: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Synthesize (or reuse cached) MP3 speech for `payload['text']` and return a zero-copy signed URL."""
    if not isinstance(payload, dict):
        raise TtsError(400, "请求格式不正确")
    cleaned = clean_text_for_speech(str(payload.get("text") or ""))
    if not cleaned:
        raise TtsError(400, "没有可以朗读的文字内容")

    from muse.media.tts import text_to_speech

    provider, voice, model, speed = _resolve_provider_and_voice(payload)
    digest = _cache_key(provider, voice, cleaned, speed=speed)
    cache_dir = audio_cache_dir()
    cache_path = cache_dir / f"tts_{digest}.mp3"

    used_provider = provider
    with _digest_singleflight(digest):
        cached = cache_path.is_file() and cache_path.stat().st_size > 0
        if cached:
            with contextlib.suppress(OSError):
                os.utime(cache_path, None)
        else:
            tmp_path = cache_dir / f".tts_{digest[:16]}_{uuid.uuid4().hex[:8]}.mp3"
            try:
                res = text_to_speech(
                    cleaned,
                    voice=voice,
                    model=model,
                    speed=speed,
                    output_path=str(tmp_path),
                    provider=provider,
                )
                if not isinstance(res, dict) or not res.get("success"):
                    err_msg = str((res or {}).get("error") or "")
                    raise TtsError(503, _friendly_error_zh(err_msg))
                used_provider = str(res.get("provider") or provider)
                if tmp_path.is_file() and tmp_path.stat().st_size > 0:
                    try:
                        os.replace(tmp_path, cache_path)
                    except PermissionError:
                        if not (cache_path.is_file() and cache_path.stat().st_size > 0):
                            raise
                else:
                    alt = Path(str(res.get("file_path") or ""))
                    if not alt.is_file() or alt.stat().st_size == 0:
                        raise TtsError(503, "语音文件生成异常，请稍后再试")
                    shutil.copyfile(alt, tmp_path)
                    try:
                        os.replace(tmp_path, cache_path)
                    except PermissionError:
                        if not (cache_path.is_file() and cache_path.stat().st_size > 0):
                            raise
                _prune_audio_cache(cache_dir, max_files=MAX_AUDIO_CACHE_FILES, keep=cache_path)
            finally:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)

    url = adapter.media.sign(TTS_PREFIX, cache_path.name)
    if not url:
        raise TtsError(500, "签发语音地址失败，请稍后再试")
    return {
        "ok": True,
        "url": url,
        "cached": cached,
        "provider": used_provider,
        "voice": voice,
    }


__all__ = [
    "MAX_AUDIO_CACHE_FILES",
    "MAX_TTS_CHARS",
    "TTS_PREFIX",
    "TtsError",
    "audio_cache_dir",
    "clean_text_for_speech",
    "synthesize",
    "tts_available",
]
