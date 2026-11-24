"""Suggestions the companion offers on its own (the 灵感 tab; ported from nanobot).

One model call turns long-term memory and the companion chat's recent messages
into a few concrete things it could do for the user now. Ideas are cached and
only regenerated when stale or on request, so the tab never spends tokens in
the background.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .paths import get_webui_dir

IDEAS_TTL_S = 12 * 3600
_MAX_IDEAS = 5
_RECENT_MESSAGES = 30
_MEMORY_CHARS = 6000
_LOCK = asyncio.Lock()
_ABILITIES = "联网搜索和阅读网页、设定提醒和定时任务、整理和总结信息、长期推进一个目标、画图、读邮件并在用户确认后发送"


def _cache_path() -> Path:
    return get_webui_dir() / "companion-ideas.json"


def _read_cache() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("ideas"), list) else None


def _write_cache(data: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _memory_text() -> str:
    from muse.paths import get_muse_home

    parts = []
    for name in ("USER.md", "MEMORY.md"):
        path = get_muse_home() / "memories" / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").replace("\n§\n", "\n"))
    return "\n\n".join(parts)[:_MEMORY_CHARS]


def _recent(events: list[dict[str, Any]]) -> list[str]:
    lines = []
    for event in events:
        if event.get("event") == "user_message":
            speaker = "用户"
        elif event.get("event") == "message" and not event.get("kind") and event.get("text"):
            speaker = "你"
        else:
            continue
        lines.append(f"{speaker}：{str(event.get('text') or '').strip()[:300]}")
    return lines[-_RECENT_MESSAGES:]


def _prompt(memory: str, recent: list[str]) -> list[dict[str, str]]:
    conversation = "\n".join(recent) or "（还没有聊过）"
    return [
        {
            "role": "system",
            "content": (
                "你是用户的私人 AI 伙伴，会主动替用户着想。根据你对用户的了解和最近的对话，"
                f"提出 {_MAX_IDEAS - 1}~{_MAX_IDEAS} 件你现在就能主动帮用户做的具体的事。"
                f"你能做的事包括：{_ABILITIES}。只提你真的做得到的事；"
                "了解不多时，提一些能帮你更了解用户、或通用但实用的事。\n"
                "只输出一个 JSON 数组，每项包含：\n"
                '- "title"：12 字以内，动词开头，例如「整理本周的待办」\n'
                '- "reason"：为什么现在建议，24 字以内\n'
                '- "prompt"：用户点「去做」后发给你的话，用用户的口吻，具体可执行'
            ),
        },
        {"role": "user", "content": f"## 关于用户的长期记忆\n{memory or '（暂无）'}\n\n## 最近的对话\n{conversation}"},
    ]


def _parse(content: Optional[str]) -> list[dict[str, str]]:
    text = content or ""
    match = re.search(r"\[.*\]", text, re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("ideas", [])
    ideas = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        title, prompt = str(item.get("title") or "").strip(), str(item.get("prompt") or "").strip()
        if title and prompt:
            ideas.append({"title": title[:40], "reason": str(item.get("reason") or "").strip()[:80], "prompt": prompt[:500]})
    return ideas[:_MAX_IDEAS]


async def get_ideas(events: list[dict[str, Any]], *, refresh: bool) -> dict[str, Any]:
    """Cached ideas, regenerated when stale or when ``refresh`` is requested."""
    from muse.llm.auxiliary import async_call_llm

    from .llm import chat_target

    async with _LOCK:
        cached = _read_cache()
        fresh = cached is not None and time.time() - float(cached.get("generated_at", 0)) < IDEAS_TTL_S
        if cached is not None and fresh and not refresh:
            return cached
        target = chat_target()
        response = await async_call_llm(
            messages=_prompt(await asyncio.to_thread(_memory_text), _recent(events)),
            max_tokens=4000, timeout=90, **target,
        )
        ideas = _parse(response.choices[0].message.content if response and response.choices else None)
        if not ideas:
            if cached is not None:
                return cached
            raise RuntimeError("没有想出合适的建议，稍后再试")
        result = {"generated_at": time.time(), "ideas": ideas}
        _write_cache(result)
        return result
