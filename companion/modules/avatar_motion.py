"""The companion's look in motion, as Muse does it: from the still picture of a look, the video
model set in 模型 makes one short clip per state — 待机 (idle), 思考 (thinking), 干活 (working),
等回复 (waiting) — inferring how the hair sways, the eyes blink, the head tilts; the app then plays
the clip that matches what the companion is doing. Each clip starts from the picture of that state
when the look has one (干活 from the busy picture, at its little laptop), else from the main one.

Each clip is turned into a looping animated picture with a transparent background (an animated
WebP: a phone's browser can't play a video with see-through parts, Safari least of all): the
frames are taken at a steady rate, the plain light background cut away from each (the same area
for every frame, so the character doesn't jitter), played forward then back so it loops without
a jump. They go into the look's folder (``motion_<state>.webp``); until they are ready the look
shows its still pictures, and a grey line in the main chat says when it starts moving. The clips
themselves are filed in 资源库 (视频), next to the look's pictures.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import avatars
from .drawing import clear_background, square

logger = logging.getLogger(__name__)

TOOLSET = "companion"
FPS = 12
SIDE = 320
# Seconds of each clip kept: played forward then back, an 8-second loop that stays small on a phone.
KEEP_S = 4
# 768P is plenty: the face is at most ~100 points wide on a phone (~300 px on a 3x screen), and the
# frames are made at 320 px; 1080P would mostly cost more.
RESOLUTION = "768p"
# What every clip must keep, so its background can be cut away and it loops: a locked camera, the
# character whole and in place, the plain white background untouched, nothing new appearing.
_KEEP = ("[Static shot] Locked-off camera: no zoom, no pan, no camera movement. The same character, same design "
         "and colors, stays centered and fully in frame (whole body, in place), never walks or turns away. "
         "Seamless pure white background (#FFFFFF): no floor, no shadow, no scenery, no light change. No new objects "
         "(only what is already in the picture), no other characters, no text. Small, gentle motion; the character "
         "ends in the same pose it starts in.")
STATES = {
    "idle": "Idle: the character breathes softly, blinks slowly once or twice, its hair and clothes sway gently "
            "as in a light breeze.",
    "thinking": "Thinking: the character glances up to one side, tilts its head slightly, raises one hand to its chin, "
                "blinks, then looks back.",
    "working": "Working at the computer: the character, focused, types on the laptop it already holds, its fingers "
               "tapping the keys, eyes moving across the screen, nodding very slightly as it reads; the laptop stays "
               "where it is, its screen facing the character.",
    "waiting": "Waiting for a reply: the character looks at the viewer with a friendly, expectant smile, tilts its head "
               "a little from side to side, blinks.",
}

# The picture each state's clip starts from, when the look has it (else the main picture).
_START = {"working": "working.webp"}

LABELS = {"idle": "待机", "thinking": "思考", "working": "干活", "waiting": "等回复"}
# What each clip shows, in words for 资源库 (the prompt itself is for the video model).
SAYS = {"idle": "待机：轻轻呼吸、慢慢眨眼，头发和衣服微微摆动",
        "thinking": "思考：抬眼看向一侧、歪头、手托下巴",
        "working": "干活：专注地敲电脑键盘、看屏幕、轻轻点头",
        "waiting": "等回复：看着你、期待地微笑、左右轻轻歪头"}

_adapter: Any = None
_loop: Any = None
_running: set[str] = set()
_LOCK = threading.Lock()


def bind(adapter: Any, loop: Any) -> None:
    global _adapter, _loop
    _adapter, _loop = adapter, loop


def video_model() -> Optional[str]:
    """The video model chosen in 模型, if any."""
    from .models import read_models

    return read_models()["media"]["video"].get("active") or None


def _first_frame(folder: Path, state: str = "idle") -> str:
    """The state's still picture on white, as the clip's first frame (sent inline: the companies'
    services can't read a file on this computer)."""
    start = folder / _START.get(state, "image.webp")
    with Image.open(start if start.is_file() else folder / "image.webp") as raw_img:
        picture = raw_img.convert("RGBA")
    canvas = Image.new("RGB", picture.size, "white")
    canvas.paste(picture, mask=picture)
    canvas.thumbnail((768, 768))
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def _clip(prompt: str, first_frame: str) -> tuple[Optional[Path], str]:
    from muse.media.video import _handle_video_generate

    try:
        result = json.loads(_handle_video_generate({
            "prompt": prompt, "image_url": first_frame, "duration": KEEP_S, "aspect_ratio": "1:1",
            "resolution": RESOLUTION}))
    except Exception as exc:
        return None, str(exc) or type(exc).__name__
    video = result.get("video") if isinstance(result, dict) and result.get("success") else None
    if not isinstance(video, str) or not video:
        return None, str((result or {}).get("error") or "视频模型没有返回视频")[:200]
    from .models import record_media

    record_media("video", seconds=KEEP_S, resolution=RESOLUTION)
    if video.startswith(("http://", "https://")):
        from . import net_safety

        try:
            raw_video, _ = net_safety.fetch_bytes(video, max_bytes=200_000_000, timeout=120)
        except Exception as exc:
            return None, str(exc) or type(exc).__name__
        target = Path(tempfile.mkdtemp(prefix="companion-motion-")) / "clip.mp4"
        target.write_bytes(raw_video)
        return target, ""
    return Path(video), ""


def loop_from(video: Path, side: int = SIDE) -> bytes:
    """A clip as a looping animated WebP with its background cut away."""
    with tempfile.TemporaryDirectory(prefix="companion-frames-") as tmp:
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-t", str(KEEP_S), "-i", str(video),
                        "-vf", f"fps={FPS},scale={side * 2}:-2", str(Path(tmp) / "f%04d.png")],
                       check=True, capture_output=True, timeout=300)
        frames = []
        for path in sorted(Path(tmp).glob("f*.png")):
            with Image.open(path) as raw_frame:
                frame = raw_frame.convert("RGBA")
            clear_background(frame)
            frames.append(frame)
    if not frames:
        raise ValueError("视频里没有画面")
    # One crop for every frame (the area the character ever covers), so it doesn't jump around.
    boxes = [f.getbbox() for f in frames if f.getbbox()]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)) \
        if boxes else None
    frames = [square(f, box, side) for f in frames]
    # Forward then back: the end meets the start, so it loops without a jump.
    frames = frames + frames[-2:0:-1]
    out = io.BytesIO()
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=round(1000 / FPS), loop=0,
                   quality=65, alpha_quality=30, method=4)
    return out.getvalue()


def animate(avatar_id: str, states: Optional[list[str]] = None) -> dict[str, Any]:
    """Makes the clips of a look (blocking: a few minutes) and keeps those that worked; ``states``:
    only these (default all)."""
    states = [s for s in (states or list(STATES)) if s in STATES]
    folder = avatars.avatars_root() / avatar_id
    if not (folder / "image.webp").is_file():
        raise ValueError("这个形象没有图片")
    if not video_model():
        raise ValueError("还没有视频模型：在「模型」里添加一个（比如即梦 Seedance、海螺、万相），形象才能动起来")
    if shutil.which("ffmpeg") is None:
        raise ValueError("这台电脑上没有 ffmpeg，没法把视频做成动效")
    def one(state: str) -> tuple[str, Optional[bytes], str]:
        prompt = f"{STATES[state]} {_KEEP}"
        video, why = _clip(prompt, _first_frame(folder, state))
        if video is None:
            return state, None, why
        file_clip(avatar_id, state, video)
        try:
            return state, loop_from(video), ""
        except Exception as exc:
            return state, None, str(exc) or type(exc).__name__

    with ThreadPoolExecutor(len(states)) as pool:
        results = list(pool.map(one, states))
    made = {state: data for state, data, _ in results if data}
    for state, data in made.items():
        (folder / f"motion_{state}.webp").write_bytes(data)
    avatars.add_files(avatar_id, {f"motion_{state}": f"motion_{state}.webp" for state in made})
    failed = {state: why for state, data, why in results if not data}
    for state, why in failed.items():
        logger.info("companion: the %s clip of look %s failed (%s)", state, avatar_id, why)
    return {"made": list(made), "failed": failed}


def file_clip(avatar_id: str, state: str, video: Path) -> None:
    """Puts a state's clip in 资源库 (视频), titled after the look; a remade clip is filed again."""
    from .creations import import_file

    name = str(avatars._meta(avatar_id)[1].get("name") or "形象")[:20]
    try:
        import_file(video, key=f"avatar:{avatar_id}:clip_{state}:{video.stem}", title=f"形象动作·{LABELS[state]}：{name}",
                    origin="generated", prompt=SAYS[state])
    except Exception:
        logger.exception("companion: could not file the %s clip of look %s in 资源库", state, avatar_id)


def animate_later(avatar_id: str) -> bool:
    """Starts making the clips in the background; False when it can't (no video model) or already is."""
    if not video_model():
        return False
    with _LOCK:
        if avatar_id in _running:
            return False
        _running.add(avatar_id)

    def run() -> None:
        text = ""
        try:
            avatars.set_flag(avatar_id, "motion_pending", True)
            result = animate(avatar_id)
            text = ("形象动起来了：待机、思考、干活、等回复各有一段动作。" if not result["failed"]
                    else f"形象动起来了一部分（{len(result['made'])}/{len(STATES)} 段），其余的没生成出来。" if result["made"]
                    else "形象的动作没生成出来，先用静态的。")
        except Exception as exc:
            logger.exception("companion: animating look %s failed", avatar_id)
            text = f"形象的动作没生成出来：{exc}"
        finally:
            with _LOCK:
                _running.discard(avatar_id)
            try:
                avatars.set_flag(avatar_id, "motion_pending", False)
            except Exception:
                pass
        if text:
            _notice(text)

    threading.Thread(target=run, name=f"companion-motion-{avatar_id}", daemon=True).start()
    return True


def _notice(text: str) -> None:
    import asyncio

    from .companion_state import read_companion_state

    chat = read_companion_state().get("chat_id")
    if _adapter is None or _loop is None or not chat:
        return
    try:
        asyncio.run_coroutine_threadsafe(_adapter.post_notice(chat, text), _loop).result(15)
    except Exception:
        logger.exception("companion: could not say the look's motion is ready")


# ── the companion's tool: animate the look it wears now ─────────────────────────────────────────

def _handle_animate(args: dict[str, Any], **_: Any) -> str:
    from .companion_state import read_companion_state

    kind, _, avatar_id = str(read_companion_state().get("avatar") or "").partition(":")
    if kind != "image" or not avatars.is_character(avatar_id):
        return json.dumps({"success": False, "error": "现在的形象不是画出来的，先用 avatar_create 画一个"}, ensure_ascii=False)
    if not video_model():
        return json.dumps({"success": False, "error": "还没有视频模型：请用户在「模型」里添加一个视频模型"}, ensure_ascii=False)
    started = animate_later(avatar_id)
    return json.dumps({"success": True, "note": ("开始给形象做动作了，要几分钟；做好了聊天里会有提示。用一句话告诉用户。"
                                                  if started else "已经在做了，等它做完。")}, ensure_ascii=False)


_SCHEMA = {
    "name": "avatar_animate",
    "description": "让你现在的形象动起来（用视频模型生成待机、思考、干活、等回复的动作，要几分钟，在后台做）。用户想让形象会动时用。",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register_tools(ctx: Any) -> None:
    ctx.register_tool(name="avatar_animate", toolset=TOOLSET, schema=_SCHEMA, handler=_handle_animate,
                      description=_SCHEMA["description"], emoji="🎞️")
