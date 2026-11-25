"""语音转文字 on this server, open source: SenseVoice-Small (FunASR's model, Chinese, English,
Cantonese, Japanese, Korean, with punctuation) run by sherpa-onnx on the CPU — no cloud, no key.

Registered as Hermes's transcription provider ``sensevoice``; it serves the app's voice button
(``transcribe_audio``) when ``stt.provider`` is ``sensevoice``, which ``enable()`` sets once the
model is in place. The phone records webm/opus or mp4/aac; ffmpeg turns that into 16 kHz mono
PCM first. The model lives in ``<companion dir>/stt/sensevoice/`` (``model.int8.onnx`` and
``tokens.txt``), loaded once and kept.

Its packages (sherpa-onnx, numpy) are in ``<companion dir>/stt/pylib``, put on the import path
here: Hermes swaps its own dependency environment in at start-up (and rebuilds it on updates), so
they can't live in it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .paths import get_webui_dir

logger = logging.getLogger(__name__)

NAME = "sensevoice"
_LOCK = threading.Lock()
_recognizer: Any = None


def model_dir() -> Path:
    return get_webui_dir() / "stt" / "sensevoice"


def _use_own_packages() -> None:
    import sys

    folder = str(get_webui_dir() / "stt" / "pylib")
    if folder not in sys.path and Path(folder).is_dir():
        sys.path.append(folder)


def missing() -> list[str]:
    """What is not there yet for it to work (empty: ready)."""
    import importlib.util

    _use_own_packages()
    out = []
    if importlib.util.find_spec("sherpa_onnx") is None:
        out.append("sherpa-onnx")
    if importlib.util.find_spec("numpy") is None:
        out.append("numpy")
    if not (model_dir() / "model.int8.onnx").is_file() or not (model_dir() / "tokens.txt").is_file():
        out.append("SenseVoice 模型")
    if shutil.which("ffmpeg") is None:
        out.append("ffmpeg")
    return out


def _load() -> Any:
    global _recognizer
    with _LOCK:
        if _recognizer is None:
            import sherpa_onnx

            folder = model_dir()
            _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(folder / "model.int8.onnx"), tokens=str(folder / "tokens.txt"),
                language="auto", use_itn=True, num_threads=2)
        return _recognizer


def _pcm(file_path: str) -> Any:
    """The recording as 16 kHz mono float samples."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", file_path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(file_path: str) -> dict[str, Any]:
    lacking = missing()
    if lacking:
        return {"success": False, "transcript": "", "provider": NAME, "error": "语音识别还缺：" + "、".join(lacking)}
    try:
        recognizer = _load()
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, _pcm(file_path))
        recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
    except Exception as exc:
        logger.warning("companion: SenseVoice could not transcribe (%s)", exc)
        return {"success": False, "transcript": "", "provider": NAME, "error": str(exc) or type(exc).__name__}
    return {"success": True, "transcript": text, "provider": NAME}


def enable() -> bool:
    """Make it the app's speech-to-text once it can run (start-up); True when it is."""
    lacking = missing()
    if lacking:
        logger.info("companion: local speech-to-text not on yet, it still needs %s", "、".join(lacking))
        return False
    from muse.config_store import read_raw_config, save_config

    config = read_raw_config()
    stt = config.setdefault("stt", {})
    if stt.get("provider") != NAME:
        stt["provider"] = NAME
        stt["enabled"] = True
        save_config(config)
        logger.info("companion: speech-to-text now runs on this server (SenseVoice)")
    return True


_CLOUD_PROVIDERS: dict[str, tuple[str, Optional[str]]] = {
    "groq": ("GROQ_API_KEY", None),
    "openai": ("OPENAI_API_KEY", None),
    "mistral": ("MISTRAL_API_KEY", None),
    "xai": ("XAI_API_KEY", None),
    "elevenlabs": ("ELEVENLABS_API_KEY", None),
    "deepinfra": ("DEEPINFRA_API_KEY", None),
}


def _raw_stt_config() -> dict[str, Any]:
    try:
        from muse.config_store import load_config

        raw = load_config()
        stt = raw.get("stt") if isinstance(raw, dict) else None
        return stt if isinstance(stt, dict) else {}
    except Exception:
        return {}


def _is_local_url(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
        if host == "localhost" or host.endswith((".local", ".lan", ".internal")):
            return True
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private
    except Exception:
        return False


def _cloud_ready(provider: str, stt: dict[str, Any]) -> bool:
    import sys

    transcription_tools = sys.modules.get("muse.media.stt")
    if transcription_tools is not None:
        try:
            if provider == "openai" and transcription_tools._has_openai_stt(stt):
                return True
            if provider == "xai" and transcription_tools._has_xai_stt(stt):
                return True
            spec = getattr(transcription_tools, "_CLOUD_PROVIDER_SPECS", {}).get(provider)
            if spec is not None and callable(spec[1]) and spec[1]():
                return True
        except Exception:
            pass
    env_var, _ = _CLOUD_PROVIDERS.get(provider, ("", None))
    if not env_var:
        return False
    section = stt.get(provider)
    sec_dict = section if isinstance(section, dict) else {}

    def _env(name: str) -> Optional[str]:
        val = os.environ.get(name)
        if not val:
            try:
                from muse.config_store import get_env_value

                val = get_env_value(name)
            except Exception:
                val = None
        return val

    key = sec_dict.get("api_key") or stt.get("api_key") or _env(env_var)
    if not key and provider == "openai":
        key = _env("VOICE_TOOLS_OPENAI_KEY")
    if str(key or "").strip():
        return True
    if provider == "openai":
        base = str(sec_dict.get("base_url") or "").strip()
        return bool(base and _is_local_url(base))
    if provider == "xai":
        try:
            from muse.paths import get_muse_home

            if (get_muse_home() / "auth.json").is_file():
                from muse.llm.oauth import get_xai_oauth_auth_status

                return bool(get_xai_oauth_auth_status().get("logged_in"))
        except Exception:
            return False
    return False


def stt_available() -> bool:
    """Whether voice speech-to-text can run right now (SenseVoice or another configured STT provider)."""
    import importlib.util
    from muse.config_store import get_env_value

    stt = _raw_stt_config()
    enabled = stt.get("enabled", True)
    if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in ("false", "0", "no", "off")):
        return False
    sensevoice_ready = not missing()
    provider = str(stt.get("provider") or "").strip().lower()
    if provider == NAME:
        return sensevoice_ready
    if not provider and sensevoice_ready:
        return True
    local_section = stt.get("local")
    local_cmd = bool(
        get_env_value("MUSE_LOCAL_STT_COMMAND")
        or get_env_value("HERMES_LOCAL_STT_COMMAND")
        or get_env_value("HERMES_STT_COMMAND")
        or (local_section.get("command") if isinstance(local_section, dict) else None)
    )
    faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    if provider == "local":
        return faster_whisper or local_cmd
    if provider == "local_command":
        return local_cmd or faster_whisper
    if provider in ("openai", "nous"):
        return _cloud_ready("openai", stt)
    if provider in _CLOUD_PROVIDERS:
        return _cloud_ready(provider, stt)
    if provider:
        custom = stt.get("providers")
        return isinstance(custom, dict) and isinstance(custom.get(provider), dict)
    return faster_whisper or local_cmd or any(_cloud_ready(name, stt) for name in _CLOUD_PROVIDERS)


def _build_provider() -> Any:
    try:
        from muse.media.stt import TranscriptionProvider
    except ImportError:
        return None

    class SenseVoice(TranscriptionProvider):
        @property
        def name(self) -> str:
            return NAME

        @property
        def display_name(self) -> str:
            return "SenseVoice（本机）"

        def is_available(self) -> bool:
            return not missing()

        def list_models(self) -> list[dict[str, Any]]:
            return [{"id": "sensevoice-small", "display": "SenseVoice-Small int8"}]

        def transcribe(
            self,
            file_path: str,
            *,
            model: Optional[str] = None,
            language: Optional[str] = None,
            **extra: Any,
        ) -> dict[str, Any]:
            return transcribe(file_path)

    return SenseVoice()


def register_native() -> None:
    provider = _build_provider()
    if provider is None:
        return
    try:
        from muse.media.stt import register_transcription_provider

        register_transcription_provider(provider)
    except Exception:
        pass


def register(ctx: Any) -> None:
    provider = _build_provider()
    if provider is None:
        return
    register_native()
    reg_fn = getattr(ctx, "register_transcription_provider", None)
    if callable(reg_fn):
        reg_fn(provider)
