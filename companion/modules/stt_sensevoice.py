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
    from hermes_cli.config import read_raw_config, save_config

    config = read_raw_config()
    stt = config.setdefault("stt", {})
    if stt.get("provider") != NAME:
        stt["provider"] = NAME
        stt["enabled"] = True
        save_config(config)
        logger.info("companion: speech-to-text now runs on this server (SenseVoice)")
    return True


def register(ctx: Any) -> None:
    from agent.transcription_provider import TranscriptionProvider

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

        def transcribe(self, file_path: str, *, model: Optional[str] = None, language: Optional[str] = None,
                       **extra: Any) -> dict[str, Any]:
            return transcribe(file_path)

    ctx.register_transcription_provider(SenseVoice())
