"""Speech-to-text subsystem: TranscriptionProvider ABC, local STT, and 6 cloud backends.

Cloud backends: ``openai``, ``groq``, ``mistral``, ``xai``, ``elevenlabs``, ``deepinfra``.
Error prefix contracts strictly preserved for ``companion/server.py:1269``:
``("No STT provider", "STT is disabled", "not installed", "No installed local STT")``.
"""

from __future__ import annotations

import abc
import importlib.util
import ipaddress
import logging
import os
import re
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from muse.media._common import (
    get_secret,
    is_secret_path,
    load_media_section,
    resolve_xai_credentials,
)

logger = logging.getLogger(__name__)

OPENAI_STT_BASE_URL = "https://api.openai.com/v1"
GROQ_STT_BASE_URL = "https://api.groq.com/openai/v1"
MISTRAL_STT_BASE_URL = "https://api.mistral.ai/v1"
XAI_STT_BASE_URL = "https://api.x.ai/v1"
ELEVENLABS_STT_BASE_URL = "https://api.elevenlabs.io/v1"
DEEPINFRA_STT_BASE_URL = "https://api.deepinfra.com/v1/openai"

DEFAULT_MODELS: dict[str, str] = {
    "openai": "whisper-1",
    "groq": "whisper-large-v3-turbo",
    "mistral": "voxtral-mini-latest",
    "xai": "grok-stt-2.0",
    "elevenlabs": "scribe_v1",
    "deepinfra": "openai/whisper-large-v3-turbo",
    "local": "base",
}
BUILTIN_STT_PROVIDERS = frozenset(
    {"local", "local_command", "openai", "groq", "mistral", "xai", "elevenlabs", "deepinfra"}
)

_ASR_TEXT_RE = re.compile(
    r"\s*language\s+[\w.-]+(?:\s*<audio_language>[^<]*</audio_language>)?\s*<asr_text>\s*(?P<text>.*)",
    flags=re.IGNORECASE | re.DOTALL,
)
_LOCAL_MODEL_LOCK = threading.Lock()
_LOCAL_WHISPER_CACHE: tuple[str, Any] = ("", None)


class TranscriptionStreamSession(abc.ABC):
    """Live audio -> transcript session for streaming-capable providers."""

    @abc.abstractmethod
    def push_audio(self, chunk: bytes) -> None:
        """Feed one chunk of 16 kHz mono s16le PCM."""

    @abc.abstractmethod
    def finalize(self) -> dict[str, Any]:
        """End the session and return the transcription result envelope."""

    def partial_transcript(self) -> str:
        return ""


class TranscriptionProvider(abc.ABC):
    """Drop-in ABC for pluggable STT backends (e.g. ``companion/modules/stt_sensevoice.py``)."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. ``'sensevoice'``)."""

    @property
    def display_name(self) -> str:
        return self.name

    def is_available(self) -> bool:
        return False

    def list_models(self) -> list[dict[str, Any]]:
        return []

    def default_model(self) -> Optional[str]:
        models = self.list_models()
        return str(models[0]["id"]) if models and isinstance(models[0], dict) else None

    @property
    def streaming_capable(self) -> bool:
        return False

    def open_stream_session(
        self,
        *,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> TranscriptionStreamSession:
        raise NotImplementedError(f"{self.name} does not support live streaming transcription")

    @abc.abstractmethod
    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Transcribe ``file_path`` into ``{"success", "transcript", "provider", "error"?}``."""


_CUSTOM_PROVIDERS: dict[str, TranscriptionProvider] = {}


def register_transcription_provider(provider: TranscriptionProvider) -> None:
    pname = str(provider.name or "").strip().lower()
    if not pname:
        raise ValueError("TranscriptionProvider.name must be non-empty")
    if pname in BUILTIN_STT_PROVIDERS:
        raise ValueError(f"Cannot override built-in STT provider {pname!r}")
    _CUSTOM_PROVIDERS[pname] = provider


def get_transcription_provider(name: str) -> Optional[TranscriptionProvider]:
    key = str(name or "").strip().lower()
    found = _CUSTOM_PROVIDERS.get(key)
    if found is None and key == "sensevoice":
        try:
            from companion.modules.stt_sensevoice import register_native

            register_native()
            found = _CUSTOM_PROVIDERS.get(key)
        except Exception:
            found = None
    return found


def list_transcription_providers() -> list[TranscriptionProvider]:
    return list(_CUSTOM_PROVIDERS.values())


def _ok(transcript: str, provider: str) -> dict[str, Any]:
    return {"success": True, "transcript": transcript, "provider": provider}


def _err(message: str, *, provider: str = "", no_speech: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"success": False, "transcript": "", "error": message}
    if provider:
        out["provider"] = provider
    if no_speech:
        out["no_speech"] = True
    return out


def _is_truthy(val: Any, *, default: bool = True) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    text = str(val).strip().lower()
    if text in ("false", "0", "no", "off", ""):
        return False
    if text in ("true", "1", "yes", "on"):
        return True
    return default


def is_stt_enabled(stt_config: Optional[dict[str, Any]] = None) -> bool:
    cfg = load_media_section("stt") if stt_config is None else stt_config
    return _is_truthy(cfg.get("enabled"), default=True)


def _section(stt_cfg: dict[str, Any], key: str) -> dict[str, Any]:
    val = stt_cfg.get(key)
    return dict(val) if isinstance(val, dict) else {}


def _resolve_language(provider: str, stt_cfg: dict[str, Any], explicit: Optional[str]) -> Optional[str]:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    psec = _section(stt_cfg, provider)
    for candidate in (
        psec.get("language"),
        psec.get("language_code"),
        stt_cfg.get("language"),
        get_secret("MUSE_LOCAL_STT_LANGUAGE"),
        get_secret("HERMES_LOCAL_STT_LANGUAGE"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _resolve_local_command(stt_cfg: dict[str, Any]) -> str:
    local_sec = _section(stt_cfg, "local")
    for candidate in (
        get_secret("MUSE_LOCAL_STT_COMMAND"),
        get_secret("HERMES_LOCAL_STT_COMMAND"),
        get_secret("HERMES_STT_COMMAND"),
        local_sec.get("command"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _has_faster_whisper() -> bool:
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def _is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        if host == "localhost" or host.endswith((".local", ".lan", ".internal")):
            return True
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private
    except Exception:
        return False


def _resolve_cloud_key(provider: str, env_var: str, stt_cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = stt_cfg if stt_cfg is not None else load_media_section("stt")
    psec = _section(cfg, provider)
    for candidate in (psec.get("api_key"), cfg.get("api_key"), get_secret(env_var)):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _has_openai_stt(stt_cfg: Optional[dict[str, Any]] = None) -> bool:
    cfg = stt_cfg if stt_cfg is not None else load_media_section("stt")
    osec = _section(cfg, "openai")
    if _resolve_cloud_key("openai", "VOICE_TOOLS_OPENAI_KEY", cfg) or get_secret("OPENAI_API_KEY"):
        return True
    base = str(osec.get("base_url") or "").strip()
    return bool(base and _is_local_url(base))


def _has_xai_stt(stt_cfg: Optional[dict[str, Any]] = None) -> bool:
    if _resolve_cloud_key("xai", "XAI_API_KEY", stt_cfg):
        return True
    try:
        from muse.llm.oauth import get_xai_oauth_auth_status

        if get_xai_oauth_auth_status().get("logged_in"):
            return True
    except Exception:
        pass
    return bool(resolve_xai_credentials(prefer_api_key=True).get("api_key"))


# Compatible with ``companion/modules/stt_sensevoice.py:147``.
_CLOUD_PROVIDER_SPECS: dict[str, tuple[Callable[[], bool], Callable[[], bool]]] = {
    "groq": (
        lambda: bool(_resolve_cloud_key("groq", "GROQ_API_KEY")),
        lambda: bool(_resolve_cloud_key("groq", "GROQ_API_KEY")),
    ),
    "openai": (_has_openai_stt, _has_openai_stt),
    "mistral": (
        lambda: bool(_resolve_cloud_key("mistral", "MISTRAL_API_KEY")),
        lambda: bool(_resolve_cloud_key("mistral", "MISTRAL_API_KEY")),
    ),
    "xai": (_has_xai_stt, _has_xai_stt),
    "elevenlabs": (
        lambda: bool(_resolve_cloud_key("elevenlabs", "ELEVENLABS_API_KEY")),
        lambda: bool(_resolve_cloud_key("elevenlabs", "ELEVENLABS_API_KEY")),
    ),
    "deepinfra": (
        lambda: bool(_resolve_cloud_key("deepinfra", "DEEPINFRA_API_KEY")),
        lambda: bool(_resolve_cloud_key("deepinfra", "DEEPINFRA_API_KEY")),
    ),
}


def _clean_transcript(text: str) -> str:
    stripped = str(text or "").strip()
    match = _ASR_TEXT_RE.match(stripped)
    return match.group("text").strip() if match else stripped


def _extract_response_text(resp: httpx.Response) -> str:
    ctype = resp.headers.get("content-type", "").lower()
    if "json" in ctype:
        body = resp.json()
        if isinstance(body, dict):
            if isinstance(body.get("text"), str):
                return _clean_transcript(body["text"])
            err = body.get("error") or body.get("detail")
            raise RuntimeError(str(err) if err else "STT response contained no text")
    return _clean_transcript(resp.text)


def _transcribe_openai_compat(
    *,
    provider: str,
    file_path: str,
    api_key: str,
    base_url: str,
    model: str,
    language: Optional[str],
    prompt: Optional[str],
    timeout: float = 90.0,
) -> dict[str, Any]:
    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with open(file_path, "rb") as fh, httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/audio/transcriptions",
                headers=headers,
                data=data,
                files={"file": (Path(file_path).name, fh, "application/octet-stream")},
            )
        if resp.status_code >= 400:
            return _err(f"{provider} STT HTTP {resp.status_code}: {resp.text[:300]}", provider=provider)
        text = _extract_response_text(resp)
        return _ok(text, provider)
    except Exception as exc:
        return _err(f"{provider} STT failed: {exc}", provider=provider)


def _transcribe_xai(
    file_path: str,
    stt_cfg: dict[str, Any],
    *,
    language: Optional[str],
) -> dict[str, Any]:
    creds = resolve_xai_credentials(prefer_api_key=True)
    api_key = creds.get("api_key", "")
    if not api_key:
        return _err("No STT provider configured (XAI_API_KEY not set)", provider="xai")
    xsec = _section(stt_cfg, "xai")
    base_url = (
        str(xsec.get("base_url") or "").strip()
        if creds.get("provider") != "xai-oauth"
        else ""
    ) or creds.get("base_url") or XAI_STT_BASE_URL
    model = str(xsec.get("model") or DEFAULT_MODELS["xai"]).strip()
    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = language
        if _is_truthy(xsec.get("format"), default=True):
            data["format"] = "true"
    if _is_truthy(xsec.get("diarize"), default=False):
        data["diarize"] = "true"
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "muse-bot/2.0"}
    try:
        with open(file_path, "rb") as fh, httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/stt",
                headers=headers,
                data=data,
                files={"file": (Path(file_path).name, fh, "application/octet-stream")},
            )
        if resp.status_code >= 400:
            return _err(f"xAI STT HTTP {resp.status_code}: {resp.text[:300]}", provider="xai")
        text = _extract_response_text(resp)
        if text:
            return _ok(text, "xai")
        return _err("xAI STT returned empty transcript", provider="xai", no_speech=True)
    except Exception as exc:
        return _err(f"xAI STT failed: {exc}", provider="xai")


def _transcribe_elevenlabs(
    file_path: str,
    stt_cfg: dict[str, Any],
    *,
    language: Optional[str],
) -> dict[str, Any]:
    api_key = _resolve_cloud_key("elevenlabs", "ELEVENLABS_API_KEY", stt_cfg)
    if not api_key:
        return _err("No STT provider configured (ELEVENLABS_API_KEY not set)", provider="elevenlabs")
    esec = _section(stt_cfg, "elevenlabs")
    base_url = str(
        esec.get("base_url") or get_secret("ELEVENLABS_STT_BASE_URL") or ELEVENLABS_STT_BASE_URL
    ).strip()
    model = str(esec.get("model") or DEFAULT_MODELS["elevenlabs"]).strip()
    data: dict[str, str] = {
        "model_id": model,
        "tag_audio_events": str(_is_truthy(esec.get("tag_audio_events"), default=False)).lower(),
        "diarize": str(_is_truthy(esec.get("diarize"), default=False)).lower(),
    }
    if language:
        data["language_code"] = language
    try:
        with open(file_path, "rb") as fh, httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/speech-to-text",
                headers={"xi-api-key": api_key},
                data=data,
                files={"file": (Path(file_path).name, fh, "application/octet-stream")},
            )
        if resp.status_code >= 400:
            return _err(f"ElevenLabs STT HTTP {resp.status_code}: {resp.text[:300]}", provider="elevenlabs")
        text = _extract_response_text(resp)
        return (
            _ok(text, "elevenlabs")
            if text
            else _err("ElevenLabs returned empty transcript", provider="elevenlabs", no_speech=True)
        )
    except Exception as exc:
        return _err(f"ElevenLabs STT failed: {exc}", provider="elevenlabs")


def _transcribe_local_whisper(
    file_path: str,
    stt_cfg: dict[str, Any],
    *,
    language: Optional[str],
) -> dict[str, Any]:
    global _LOCAL_WHISPER_CACHE
    if not _has_faster_whisper():
        return _err("faster-whisper is not installed", provider="local")
    lsec = _section(stt_cfg, "local")
    model_name = str(lsec.get("model") or DEFAULT_MODELS["local"]).strip()
    try:
        from faster_whisper import WhisperModel

        with _LOCAL_MODEL_LOCK:
            if _LOCAL_WHISPER_CACHE[0] != model_name or _LOCAL_WHISPER_CACHE[1] is None:
                loaded = WhisperModel(model_name, device="auto", compute_type="default")
                _LOCAL_WHISPER_CACHE = (model_name, loaded)
            model = _LOCAL_WHISPER_CACHE[1]
        kwargs: dict[str, Any] = {}
        if language:
            kwargs["language"] = language
        segments, _ = model.transcribe(file_path, **kwargs)
        text = " ".join(str(seg.text).strip() for seg in segments if str(seg.text).strip()).strip()
        return _ok(text, "local")
    except Exception as exc:
        return _err(f"Local STT failed: {exc}", provider="local")


def _transcribe_local_command(file_path: str, cmd_template: str) -> dict[str, Any]:
    if not cmd_template:
        return _err("No installed local STT command configured", provider="local_command")
    try:
        parts = shlex.split(cmd_template, posix=os.name != "nt")
        if os.name == "nt":
            parts = [
                p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in ('"', "'") else p
                for p in parts
            ]
        replaced = False
        argv: list[str] = []
        for token in parts:
            if "{file}" in token or "{audio}" in token:
                argv.append(token.replace("{file}", file_path).replace("{audio}", file_path))
                replaced = True
            else:
                argv.append(token)
        if not replaced:
            argv.append(file_path)
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            return _err(f"Local STT command failed ({proc.returncode}): {detail}", provider="local_command")
        return _ok((proc.stdout or "").strip(), "local_command")
    except Exception as exc:
        return _err(f"Local STT command error: {exc}", provider="local_command")


def _dispatch_cloud(
    provider: str,
    file_path: str,
    stt_cfg: dict[str, Any],
    *,
    language: Optional[str],
    prompt: Optional[str],
) -> dict[str, Any]:
    lang = _resolve_language(provider, stt_cfg, language)
    if provider == "xai":
        return _transcribe_xai(file_path, stt_cfg, language=lang)
    if provider == "elevenlabs":
        return _transcribe_elevenlabs(file_path, stt_cfg, language=lang)

    psec = _section(stt_cfg, provider)
    model = str(psec.get("model") or stt_cfg.get("model") or DEFAULT_MODELS[provider]).strip()
    if provider == "openai":
        base_url = str(psec.get("base_url") or OPENAI_STT_BASE_URL).strip()
        api_key = (
            _resolve_cloud_key("openai", "VOICE_TOOLS_OPENAI_KEY", stt_cfg)
            or get_secret("OPENAI_API_KEY")
            or ("not-needed" if _is_local_url(base_url) else "")
        )
        if not api_key:
            return _err("No STT provider configured (OPENAI_API_KEY not set)", provider="openai")
        return _transcribe_openai_compat(
            provider="openai", file_path=file_path, api_key=api_key, base_url=base_url,
            model=model, language=lang, prompt=prompt,
        )
    if provider == "groq":
        api_key = _resolve_cloud_key("groq", "GROQ_API_KEY", stt_cfg)
        if not api_key:
            return _err("No STT provider configured (GROQ_API_KEY not set)", provider="groq")
        return _transcribe_openai_compat(
            provider="groq", file_path=file_path, api_key=api_key,
            base_url=str(psec.get("base_url") or GROQ_STT_BASE_URL),
            model=model, language=lang, prompt=prompt,
        )
    if provider == "mistral":
        api_key = _resolve_cloud_key("mistral", "MISTRAL_API_KEY", stt_cfg)
        if not api_key:
            return _err("No STT provider configured (MISTRAL_API_KEY not set)", provider="mistral")
        return _transcribe_openai_compat(
            provider="mistral", file_path=file_path, api_key=api_key,
            base_url=str(psec.get("base_url") or MISTRAL_STT_BASE_URL),
            model=model, language=lang, prompt=prompt,
        )
    if provider == "deepinfra":
        api_key = _resolve_cloud_key("deepinfra", "DEEPINFRA_API_KEY", stt_cfg)
        if not api_key:
            return _err("No STT provider configured (DEEPINFRA_API_KEY not set)", provider="deepinfra")
        return _transcribe_openai_compat(
            provider="deepinfra", file_path=file_path, api_key=api_key,
            base_url=str(psec.get("base_url") or DEEPINFRA_STT_BASE_URL),
            model=model, language=lang, prompt=prompt,
        )
    return _err(f"No STT provider registered for {provider!r}")


def transcribe_audio(
    file_path: str,
    *,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    source: str = "gateway",
) -> dict[str, Any]:
    """Transcribe an audio file using configured/auto-detected local or cloud STT."""
    del source
    if is_secret_path(file_path):
        return _err("Refusing to transcribe protected secret file path")

    stt_cfg = load_media_section("stt")
    if not is_stt_enabled(stt_cfg):
        return _err("STT is disabled in configuration")

    path = Path(str(file_path or "")).expanduser()
    if not path.is_file():
        return _err(f"Audio file not found: {file_path}")
    if path.stat().st_size <= 0:
        return _err(f"Audio file is empty: {file_path}")

    explicit = str(stt_cfg.get("provider") or "").strip().lower()
    if explicit == "nous":
        explicit = "openai"

    if explicit:
        custom = get_transcription_provider(explicit)
        if custom is not None:
            if not custom.is_available():
                return _err(f"STT provider {explicit!r} is not installed or ready", provider=explicit)
            return custom.transcribe(str(path), language=language, prompt=prompt)
        if explicit == "local":
            if _has_faster_whisper():
                return _transcribe_local_whisper(str(path), stt_cfg, language=language)
            cmd = _resolve_local_command(stt_cfg)
            if cmd:
                return _transcribe_local_command(str(path), cmd)
            return _err("No installed local STT backend (faster-whisper not installed)", provider="local")
        if explicit == "local_command":
            cmd = _resolve_local_command(stt_cfg)
            if cmd:
                return _transcribe_local_command(str(path), cmd)
            if _has_faster_whisper():
                return _transcribe_local_whisper(str(path), stt_cfg, language=language)
            return _err("No installed local STT command configured", provider="local_command")
        if explicit in _CLOUD_PROVIDER_SPECS:
            return _dispatch_cloud(explicit, str(path), stt_cfg, language=language, prompt=prompt)
        return _err(f"No STT provider registered for {explicit!r}")

    # Auto-detect priority: registered custom (incl. lazy SenseVoice) -> faster_whisper -> local cmd -> cloud.
    get_transcription_provider("sensevoice")
    for custom in _CUSTOM_PROVIDERS.values():
        if custom.is_available():
            return custom.transcribe(str(path), language=language, prompt=prompt)
    if _has_faster_whisper():
        return _transcribe_local_whisper(str(path), stt_cfg, language=language)
    cmd = _resolve_local_command(stt_cfg)
    if cmd:
        return _transcribe_local_command(str(path), cmd)
    for cname, (_, auto_probe) in _CLOUD_PROVIDER_SPECS.items():
        if auto_probe():
            return _dispatch_cloud(cname, str(path), stt_cfg, language=language, prompt=prompt)
    return _err("No STT provider configured or available")


__all__ = [
    "BUILTIN_STT_PROVIDERS",
    "DEFAULT_MODELS",
    "TranscriptionProvider",
    "TranscriptionStreamSession",
    "_CLOUD_PROVIDER_SPECS",
    "get_transcription_provider",
    "is_stt_enabled",
    "list_transcription_providers",
    "register_transcription_provider",
    "transcribe_audio",
]
