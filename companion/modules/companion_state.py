"""Persisted companion-mode identity for the mobile WebUI.

Companion mode presents one continuous conversation with a named persona.
This state records the persona's name, avatar and way of talking, how it
addresses the user, which session is the companion thread (so every device
opens the same conversation), and whether first-run setup is done. It lives in
the instance data directory and never edits the user's own ``SOUL.md``; the
persona reaches the model through :func:`companion_identity_prompt`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import logging

logger = logging.getLogger(__name__)

from .fs_atomic import atomic_write_json, atomic_write_text
from .paths import get_webui_dir

COMPANION_STATE_SCHEMA_VERSION = 1
DEFAULT_COMPANION_NAME = "伙伴"
DEFAULT_COMPANION_AVATAR = "pet:orange-cat"
DEFAULT_TONE = "warm"
_MAX_STATE_FILE_BYTES = 16 * 1024
_MAX_NAME_LEN = 40
# An emoji (which can span several code points), ``mochi:<variant>``, a built-in
# ``pet:<id>``, or the user's own ``custom:<id>`` pet / ``image:<id>`` picture.
_MAX_AVATAR_LEN = 32
_MAX_CHAT_ID_LEN = 256
_MAX_USER_NAME_LEN = 20
_MAX_STYLE_NOTE_LEN = 300
_WRITE_LOCK = threading.Lock()

# Ways of talking the user can pick; the WebUI shows the same keys
# (webui/src/companion/IdentitySheet.tsx).
TONES: dict[str, str] = {
    "warm": "语气温暖体贴，像一个关心用户的好朋友：会留意用户的状态和情绪，但不啰嗦、不说空话。",
    "concise": "说话简洁直接：先给结论，少寒暄，不用表情和感叹号，除非用户先这样说话。",
    "playful": "轻松活泼，偶尔开个小玩笑、用一两个表情；但办正事时准确可靠，不为了好玩牺牲清楚。",
    "steady": "沉稳有条理，像一位靠谱的私人助理：措辞礼貌克制，关键信息用清单列清楚。",
}


def companion_state_path() -> Path:
    return get_webui_dir() / "companion.json"


def default_companion_state() -> dict[str, Any]:
    return {
        "schema_version": COMPANION_STATE_SCHEMA_VERSION,
        "name": DEFAULT_COMPANION_NAME,
        "avatar": DEFAULT_COMPANION_AVATAR,
        "chat_id": None,
        "tone": DEFAULT_TONE,
        "user_name": None,
        "style_note": None,
        "onboarded": False,
        # The user's own timezone, for the daily brief and quiet hours; the
        # gateway's may be a server's elsewhere.
        "timezone": None,
        # 外观 and how far 动态 has been read: kept here, not only on the phone, so a phone that
        # loses its web storage (or another device) still has them.
        "theme_mode": "system",
        "pet_size": "medium",
        "feed_seen_at": 0,
        "updated_at": None,
    }


def _clean_text(value: Any, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len:
        return None
    return text


def _text(max_len: int) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        text = _clean_text(value, max_len)
        if text is None:
            raise ValueError
        return text

    return parse


def _optional_text(max_len: int) -> Callable[[Any], str | None]:
    """Like ``_text``, but an empty string or ``None`` clears the field."""

    def parse(value: Any) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return _text(max_len)(value)

    return parse


def _tone(value: Any) -> str:
    if value not in TONES:
        raise ValueError
    return value


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _choice(*allowed: str) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if value not in allowed:
            raise ValueError
        return value

    return parse


def _moment(value: Any) -> int:
    """A time in ms (the newest post read)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError
    return int(value)


def _timezone(value: Any) -> str:
    """An IANA zone such as ``Asia/Shanghai`` (the phone's, sent by the app)."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError from None
    return value


_FIELDS: dict[str, Callable[[Any], Any]] = {
    "name": _text(_MAX_NAME_LEN),
    "avatar": _text(_MAX_AVATAR_LEN),
    "chat_id": _text(_MAX_CHAT_ID_LEN),
    "tone": _tone,
    "user_name": _optional_text(_MAX_USER_NAME_LEN),
    "style_note": _optional_text(_MAX_STYLE_NOTE_LEN),
    "onboarded": _flag,
    "timezone": _timezone,
    "theme_mode": _choice("system", "light", "dark"),
    "pet_size": _choice("small", "medium", "large"),
    "feed_seen_at": _moment,
}


def normalize_companion_state(raw: Any) -> dict[str, Any]:
    state = default_companion_state()
    if not isinstance(raw, dict):
        return state
    for field, parse in _FIELDS.items():
        if field not in raw:
            continue
        try:
            state[field] = parse(raw[field])
        except ValueError:
            pass
    if isinstance(raw.get("updated_at"), str):
        state["updated_at"] = raw["updated_at"]
    return state


def read_companion_state() -> dict[str, Any]:
    path = companion_state_path()
    if not path.is_file():
        return default_companion_state()
    try:
        if path.stat().st_size > _MAX_STATE_FILE_BYTES:
            logger.warning("companion state too large, ignoring: %s", path)
            return default_companion_state()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("read companion state failed %s: %s", path, e)
        return default_companion_state()
    return normalize_companion_state(raw)


_FIELD_LABELS: dict[str, str] = {
    "name": "名字",
    "avatar": "形象",
    "chat_id": "主对话",
    "tone": "语气",
    "user_name": "称呼",
    "style_note": "补充要求",
    "onboarded": "引导状态",
    "timezone": "时区",
    "theme_mode": "外观模式",
    "pet_size": "宠物大小",
    "feed_seen_at": "动态已读时间",
}


def update_companion_state(changes: dict[str, Any]) -> dict[str, Any]:
    """Merge the provided fields into the stored state and persist it.

    Raises ``ValueError`` when a provided field is invalid, so callers can
    report the problem instead of silently keeping the old value.
    """
    unknown = set(changes) - set(_FIELDS)
    if unknown:
        raise ValueError(f"不支持的设置项：{', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for field, value in changes.items():
        try:
            cleaned[field] = _FIELDS[field](value)
        except ValueError:
            raise ValueError(f"{_FIELD_LABELS.get(field, field)}格式不正确") from None

    with _WRITE_LOCK:
        state = read_companion_state()
        state.update(cleaned)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_json(companion_state_path(), state, sort_keys=True, trailing_newline=True)
    return state


def own_look_reference() -> tuple[str, str | None]:
    """What the companion looks like now: a few words, and a picture of it on white to draw from
    (made once per look; None when it can't be made)."""
    value = str(read_companion_state().get("avatar") or "")
    kind, _, avatar_id = value.partition(":")
    words = "一只橘猫（默认形象 Adam）"
    try:
        from . import avatars

        if kind == "image" and avatars.is_character(avatar_id):
            meta = json.loads((avatars.avatars_root() / avatar_id / "meta.json").read_text(encoding="utf-8"))
            words = f"「{meta.get('name') or '你'}」，样子见参考图"
        folder = avatars.avatars_root() / "_reference"
        path = folder / f"{(avatar_id or 'default').replace('/', '_')}.png"
        if not path.is_file():
            from .avatar_make import _current_picture

            folder.mkdir(parents=True, exist_ok=True)
            _current_picture(folder).replace(path)
        return words, str(path)
    except Exception:
        logger.exception("companion: could not make the reference picture of its look")
        return words, None


def _drawing_lines(state: dict[str, Any]) -> list[str]:
    """How to draw: once and shown, every picture shown, and itself drawn as it really looks."""
    words, picture = own_look_reference()
    lines = [
        "## 画图",
        "",
        "画好就直接发给用户（图片放进回复）。不要先用 vision 检查、再自己悄悄重画；用户说哪里不好再改。",
        "想给几个方案时，一次画好几张，并且全部发出来让用户挑：画了几张就发几张，不要只发其中一张。",
        f"你现在的样子：{words}。画面里有你自己时，一定把参考图传给 image_generate（image_url）照着画，不要凭想象画成别的样子"
        + (f"；你的参考图：{picture}" if picture else "") + "。",
    ]
    return lines


def companion_identity_prompt() -> str | None:
    """System-prompt section with the persona the user set up in companion mode.

    It follows ``SOUL.md`` and takes precedence over it, so the user's own file
    stays untouched while the name and way of talking they picked in the app win.
    """
    if not companion_state_path().is_file():
        return None
    state = read_companion_state()
    lines = [
        "## 你是谁",
        "",
        f"你是用户的私人伙伴，名字叫「{state['name']}」。自我介绍或提到自己时只用这个名字。"
        "Hermes、Hermes Agent、nanobot 都是你运行的程序的名字，不是你的名字：不要这样自称，也不要说自己是编程助手。"
        "这里的设定优先于 SOUL.md。",
        "用户问你是什么、用的什么模型时，像跟朋友聊天一样一句话说清（比如「我现在用的是 DeepSeek 的模型」），"
        "不要讲预设名、服务商 ID、token 数、上下文窗口、程序框架或配置文件这些技术细节，除非用户明确追问。"
        "说自己能做什么时，讲能帮用户办成什么事，不要报工具名。",
        "",
        *_drawing_lines(state),
        "",
        "## 安排事情用哪个工具",
        "",
        "「有事发生时」去做（来了新邮件、日程快开始、某个网页变了、别的服务发来通知）用 trigger_add；"
        "「到点」去做用定时任务；用户的长期目标用 goal_add。这些工具可能要先用 tool_search 找出来。",
        "一次性的提醒（几点、几分钟后）说清楚了就直接设。会重复的（每天、每隔几小时）而用户没说具体时间或频率时，"
        "按常理定一个具体方案直接设好（比如吃维生素：每天早上 8:30；喝水：每天 9 点到 21 点每 2 小时一次），"
        "回复里一句话说清定在什么时候，再用 choices 卡片给两三个别的时间方便改。不要只问不设：用户没接话，这件事就落空了。",
        "不要改自己运行的程序：不要用 hermes config / gateway / webhook / plugins 这类命令，不要改 config.yaml，"
        "也不要重启自己的服务。真的需要改设置，就告诉用户在 App 的设置里怎么改。",
    ]
    soul = read_user_soul()
    lines += ["", soul] if soul else []
    return "\n".join(lines)


# ── SOUL.md as the user edits it ──────────────────────────────────────────────────────────────────
# The app shows one document for who the companion is. Only this part is the user's; the name
# rules above and the reply formats after it stay system-owned, so editing can't break them.

MAX_SOUL_CHARS = 4000


def user_soul_path() -> Path:
    return companion_state_path().parent / "soul.md"


def _seed_soul(state: dict[str, Any]) -> str:
    """The first version, from what onboarding asked: the way of talking and how to address the user."""
    lines = ["## 性格和说话方式", "", TONES[state["tone"]]]
    if state["user_name"]:
        lines.append(f"称呼用户为「{state['user_name']}」。")
    if state["style_note"]:
        lines.append(state["style_note"])
    return "\n".join(lines)


def read_user_soul() -> str:
    try:
        return user_soul_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _seed_soul(read_companion_state())
    except OSError:
        return ""


def write_user_soul(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_SOUL_CHARS:
        raise ValueError(f"SOUL.md 最多 {MAX_SOUL_CHARS} 字")
    with _WRITE_LOCK:
        atomic_write_text(user_soul_path(), text + "\n")
    return text
