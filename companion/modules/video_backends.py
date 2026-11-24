"""Video models straight from the companies in China: MiniMax (海螺 / H3), 火山方舟 (Seedance) and
阿里云百炼 (万相 Wan). Hermes's own video backends reach these only through fal or OpenRouter;
these three call each company's own API with the key the user added for it.

All three are the same shape: create a task, poll it until it is done, then save the video.
The model and the service (address + key variable) come from ``video_gen.<backend>`` in
config.yaml, which the model page writes (``models.media_settings``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

from . import net_safety

try:
    from muse.media._common import get_secret as get_secret_str
except ImportError:
    def get_secret_str(name: str) -> str:
        return os.environ.get(name, "")

try:
    from muse.media.video import (
        VideoGenProvider,
        error_response,
        register_video_gen_provider,
        save_url_video,
        success_response,
    )
    _HAS_VIDEO_PROVIDER = True
except ImportError:
    _HAS_VIDEO_PROVIDER = False

    class VideoGenProvider:  # type: ignore[no-redef]
        pass

    def error_response(**kwargs: Any) -> dict[str, Any]:
        return {"success": False, **kwargs}

    def success_response(**kwargs: Any) -> dict[str, Any]:
        return {"success": True, **kwargs}

    def save_url_video(url: str, *, prefix: str = "video") -> str:
        raise RuntimeError("video_gen_provider unavailable")

    def register_video_gen_provider(provider: Any) -> None:
        return None

logger = logging.getLogger(__name__)

_POLL_S = 8.0
_DEADLINE_S = 900.0


def _settings(backend: str) -> dict[str, Any]:
    try:
        from muse.config_store import load_config

        section = (load_config().get("video_gen") or {}).get(backend)
    except Exception:
        section = None
    return section if isinstance(section, dict) else {}


class _TaskVideo(VideoGenProvider):
    """Create → poll → save; a backend fills in the three company-specific steps."""

    name = ""
    label = ""
    key_env = ""
    default_base_url = ""
    models: list[dict[str, Any]] = []
    image_models: frozenset[str] = frozenset()

    @property
    def display_name(self) -> str:
        return self.label

    def _key(self) -> str:
        return get_secret_str(str(_settings(self.name).get("key_env") or self.key_env)).strip()

    def _base(self) -> str:
        return str(_settings(self.name).get("base_url") or self.default_base_url).rstrip("/")

    def is_available(self) -> bool:
        return bool(self._key())

    def list_models(self) -> list[dict[str, Any]]:
        return list(self.models)

    def default_model(self) -> Optional[str]:
        return str(_settings(self.name).get("model") or (self.models[0]["id"] if self.models else "")) or None

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text", "image"], "aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                "resolutions": ["480p", "720p", "1080p"], "max_duration": 15, "min_duration": 3,
                "supports_audio": True, "supports_negative_prompt": False, "supports_seed": False,
                "supports_upscale": False, "max_reference_images": 0}

    # -- the company-specific steps --
    def _create(self, client: httpx.Client, model: str, prompt: str, image_url: Optional[str],
                duration: int, aspect_ratio: str, resolution: str, audio: Optional[bool]) -> str:
        raise NotImplementedError

    def _poll(self, client: httpx.Client, task: str) -> tuple[str, Optional[str], str]:
        """(state: "running" | "done" | "failed", the video's address when done, why it failed)."""
        raise NotImplementedError

    def _cleanup(self, task: str) -> None:
        pass

    def generate(self, prompt: str, *, model: Optional[str] = None, image_url: Optional[str] = None,
                 reference_image_urls: Optional[list[str]] = None, duration: Optional[int] = None,
                 aspect_ratio: str = "16:9", resolution: str = "720p", negative_prompt: Optional[str] = None,
                 audio: Optional[bool] = None, seed: Optional[int] = None, **kwargs: Any) -> dict[str, Any]:
        model = model or self.default_model() or ""
        failed = lambda why, kind="provider_error": error_response(  # noqa: E731
            error=why, error_type=kind, provider=self.name, model=model, prompt=prompt, aspect_ratio=aspect_ratio)
        key = self._key()
        if not key:
            return failed(f"{self.label} 还没有 API key，在「模型」里添加时填上", "auth_required")
        seconds = max(3, min(15, int(duration or 5)))
        task: Optional[str] = None
        try:
            with httpx.Client(base_url=self._base(), timeout=60,
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}) as client:
                task = self._create(client, model, prompt, image_url, seconds, aspect_ratio, resolution, audio)
                deadline = time.monotonic() + _DEADLINE_S
                while True:
                    state, url, why = self._poll(client, task)
                    if state == "done" and url:
                        break
                    if state == "failed":
                        return failed(f"{self.label} 没生成出来：{why or '未知原因'}")
                    if time.monotonic() > deadline:
                        return failed(f"{self.label} 15 分钟还没生成完（任务 {task}）", "timeout")
                    time.sleep(_POLL_S)
        except httpx.HTTPStatusError as exc:
            return failed(f"{self.label} 返回 {exc.response.status_code}：{exc.response.text[:300]}")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return failed(f"{self.label} 调用失败：{exc}")
        finally:
            if task:
                self._cleanup(task)
        if not net_safety.is_safe_url(url):
            return failed("返回的视频地址不安全，已拦截")
        try:
            video = str(save_url_video(url, prefix=self.name))
        except Exception as exc:  # the company's link still works for a while
            logger.info("companion video: could not keep %s's video (%s)", self.name, exc)
            video = url
        return success_response(video=video, model=model, prompt=prompt, modality="image" if image_url else "text",
                                aspect_ratio=aspect_ratio, duration=seconds, provider=self.name)


def _checked(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("返回的不是 JSON 对象")
    return data


class MiniMaxVideo(_TaskVideo):
    """MiniMax 海螺: POST /v1/video_generation → GET /v1/query/video_generation → GET /v1/files/retrieve.
    The H3 family (H3, H3-Lite, H3-Max, H3-Highspeed) only takes the newer interface:
    POST /v2/video_generation with a ``content`` list (the text, and the first frame as an
    ``image_url`` with role ``first_frame``) → GET /v2/query/video_generation/{task_id}, the video at
    ``task.content.url``."""

    name = "companion-minimax"
    label = "MiniMax 海螺"
    key_env = "MINIMAX_API_KEY"
    default_base_url = "https://api.minimaxi.com/v1"
    models = [{"id": "MiniMax-H3", "display": "海螺 H3"},
              {"id": "MiniMax-H3-Lite", "display": "海螺 H3 Lite"},
              {"id": "MiniMax-H3-Max", "display": "海螺 H3 Max"},
              {"id": "MiniMax-Hailuo-2.3", "display": "海螺 2.3"},
              {"id": "MiniMax-Hailuo-02", "display": "海螺 02"}]

    _v2_tasks: set[str] = set()

    def __init__(self) -> None:
        super().__init__()
        self._v2_tasks = set()

    def _cleanup(self, task: str) -> None:
        self._v2_tasks.discard(task)

    def _v2(self, path: str) -> str:
        """An address of the newer interface (the service's address ends in /v1)."""
        base = self._base()
        return (base[: -len("/v1")] if base.endswith("/v1") else base) + path

    def _create(self, client, model, prompt, image_url, duration, aspect_ratio, resolution, audio):
        if model.startswith("MiniMax-H3"):
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            if image_url:
                content.append({"type": "image_url", "image_url": {"url": image_url}, "role": "first_frame"})
            low = "480P" if resolution == "480p" and model in ("MiniMax-H3-Lite", "MiniMax-H3-Max") else "768P"
            body = {"model": model, "content": content, "duration": max(5 if model == "MiniMax-H3-Max" else 4, duration),
                    "resolution": "2K" if resolution == "1080p" and model == "MiniMax-H3" else low,
                    "ratio": "adaptive" if image_url else aspect_ratio}
            task = str(_checked(client.post(self._v2("/v2/video_generation"), json=body))["task_id"])
            self._v2_tasks.add(task)
            return task
        body: dict[str, Any] = {"model": model, "prompt": prompt, "duration": 10 if duration > 6 else 6,
                                "resolution": "1080P" if resolution == "1080p" else "768P"}
        if image_url:
            body["first_frame_image"] = image_url
        data = _checked(client.post("/video_generation", json=body))
        status = data.get("base_resp") or {}
        if status.get("status_code") not in (0, None):
            raise ValueError(status.get("status_msg") or str(status))
        return str(data["task_id"])

    def _poll(self, client, task):
        if task in self._v2_tasks:
            found = _checked(client.get(self._v2(f"/v2/query/video_generation/{task}"))).get("task") or {}
            status = str(found.get("status") or "")
            if status == "succeeded":
                self._v2_tasks.discard(task)
                return "done", str((found.get("content") or {}).get("url") or ""), ""
            if status in ("failed", "cancelled"):
                self._v2_tasks.discard(task)
                error = found.get("error") or {}
                return "failed", None, str(error.get("message") or error.get("code") or status)
            return "running", None, ""
        data = _checked(client.get("/query/video_generation", params={"task_id": task}))
        status = str(data.get("status") or "").lower()
        if status == "success":
            if data.get("video_url"):
                return "done", str(data["video_url"]), ""
            file = _checked(client.get("/files/retrieve", params={"file_id": data["file_id"]}))
            return "done", str((file.get("file") or {}).get("download_url") or ""), ""
        if status in ("fail", "failed"):
            return "failed", None, str((data.get("base_resp") or {}).get("status_msg") or "")
        return "running", None, ""


class ArkVideo(_TaskVideo):
    """火山方舟 Seedance: POST /contents/generations/tasks → GET /contents/generations/tasks/{id}."""

    name = "companion-ark"
    label = "火山方舟 Seedance"
    key_env = "ARK_API_KEY"
    default_base_url = "https://ark.cn-beijing.volces.com/api/v3"
    models = [{"id": "doubao-seedance-2-0-260128", "display": "Seedance 2.0"},
              {"id": "doubao-seedance-2-0-fast-260128", "display": "Seedance 2.0 Fast"},
              {"id": "doubao-seedance-2-0-mini-260615", "display": "Seedance 2.0 Mini"}]

    def _create(self, client, model, prompt, image_url, duration, aspect_ratio, resolution, audio):
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url}, "role": "first_frame"})
        body: dict[str, Any] = {"model": model, "content": content, "duration": duration,
                                "ratio": "adaptive" if image_url else aspect_ratio,
                                "resolution": resolution, "watermark": False}
        if audio is not None:
            body["generate_audio"] = bool(audio)
        return str(_checked(client.post("/contents/generations/tasks", json=body))["id"])

    def _poll(self, client, task):
        data = _checked(client.get(f"/contents/generations/tasks/{task}"))
        status = str(data.get("status") or "")
        if status == "succeeded":
            return "done", str((data.get("content") or {}).get("video_url") or ""), ""
        if status in ("failed", "cancelled", "expired"):
            return "failed", None, str((data.get("error") or {}).get("message") or status)
        return "running", None, ""


class WanVideo(_TaskVideo):
    """阿里云百炼 万相: POST /services/aigc/video-generation/video-synthesis (async) → GET /tasks/{id}."""

    name = "companion-dashscope"
    label = "阿里云百炼 万相"
    key_env = "DASHSCOPE_API_KEY"
    default_base_url = "https://dashscope.aliyuncs.com/api/v1"
    models = [{"id": "wan3.0-video", "display": "万相 3.0"},
              {"id": "wan3.0-video-prime", "display": "万相 3.0 Prime"}]

    def _base(self) -> str:
        # The service saved for 百炼's chat models is its OpenAI-compatible address; video is on /api/v1.
        base = super()._base()
        return base.replace("/compatible-mode/v1", "/api/v1")

    def _create(self, client, model, prompt, image_url, duration, aspect_ratio, resolution, audio):
        body: dict[str, Any] = {
            "model": model, "input": {"prompt": prompt},
            "parameters": {"resolution": resolution.upper(), "ratio": "adaptive" if image_url else aspect_ratio,
                           "duration": duration, "watermark": False}}
        if image_url:
            body["input"]["media"] = [{"type": "first_frame", "url": image_url}]
        if audio is not None:
            body["parameters"]["audio"] = bool(audio)
        data = _checked(client.post("/services/aigc/video-generation/video-synthesis", json=body,
                                    headers={"X-DashScope-Async": "enable"}))
        if not (data.get("output") or {}).get("task_id"):
            raise ValueError(data.get("message") or str(data)[:300])
        return str(data["output"]["task_id"])

    def _poll(self, client, task):
        output = _checked(client.get(f"/tasks/{task}")).get("output") or {}
        status = str(output.get("task_status") or "")
        if status == "SUCCEEDED":
            return "done", str(output.get("video_url") or ""), ""
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            return "failed", None, str(output.get("message") or output.get("code") or status)
        return "running", None, ""


BACKENDS = (MiniMaxVideo, ArkVideo, WanVideo)


def register_native() -> None:
    if not _HAS_VIDEO_PROVIDER:
        return
    for backend in BACKENDS:
        try:
            register_video_gen_provider(backend())
        except Exception as exc:
            logger.info("companion: could not add native %s video backend (%s)", backend.name, type(exc).__name__)


def register(ctx: Any) -> None:
    if not _HAS_VIDEO_PROVIDER:
        return
    register_native()
    reg_fn = getattr(ctx, "register_video_gen_provider", None)
    if not callable(reg_fn):
        return
    from muse.engine import adapt_video_provider_for_hermes

    for backend in BACKENDS:
        try:
            reg_fn(adapt_video_provider_for_hermes(backend()))
        except Exception as exc:  # an older Hermes without video backends: the rest still works
            logger.info("companion: could not add the %s video backend (%s)", backend.name, type(exc).__name__)
