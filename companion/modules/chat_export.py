"""Clean Chinese Markdown and structured JSON conversation export for Web, iOS, and Android."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from muse.interceptors import scrub_url_secrets

from . import companion_state

_CARD_FENCE_RE = re.compile(r"```(card|json|ui)[ \t]*\n([\s\S]*?)\n```")
_CARD_TYPES = frozenset({"choices", "buttons", "widget", "pictures", "confirm", "approval"})
_SAFE_FILENAME_RE = re.compile(r"[\\/:*?\"<>|\r\n\t]+")


def _strip_internal_cards(text: str) -> str:
    """Remove internal ``card``/``ui`` blocks while keeping legitimate ``json`` and code fences."""
    if not text or "```" not in text:
        return (text or "").strip()

    def _replace(match: re.Match[str]) -> str:
        lang = match.group(1).lower()
        body = match.group(2)
        if lang in ("card", "ui"):
            return ""
        # ``json`` block: only strip if it is actually a companion interactive card object.
        try:
            parsed = json.loads(body)
        except ValueError:
            return match.group(0)
        if isinstance(parsed, dict) and str(parsed.get("type") or "") in _CARD_TYPES:
            return ""
        return match.group(0)

    cleaned = _CARD_FENCE_RE.sub(_replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return scrub_url_secrets(cleaned.strip())


def _normalize_media(raw_media: Any) -> list[dict[str, str]]:
    if not isinstance(raw_media, list):
        return []
    kind_labels = {"image": "图片", "video": "视频", "audio": "音频", "file": "文件"}
    items: list[dict[str, str]] = []
    for entry in raw_media:
        if isinstance(entry, dict):
            url = scrub_url_secrets(str(entry.get("url") or "").strip())
            name = str(entry.get("name") or entry.get("title") or "").strip()
            if not name and url:
                name = Path(urlparse(url).path).name
            kind = str(entry.get("kind") or "file").strip().lower()
            items.append({
                "kind": kind,
                "label": kind_labels.get(kind, "附件"),
                "name": name or "未命名附件",
                "url": url,
            })
        elif isinstance(entry, str) and entry.strip():
            url = scrub_url_secrets(entry.strip())
            name = Path(urlparse(url).path).name or "附件"
            items.append({"kind": "file", "label": "附件", "name": name, "url": url})
    return items


def _normalize_sources(raw_sources: Any) -> list[dict[str, str]]:
    if not isinstance(raw_sources, list):
        return []
    seen_urls: set[str] = set()
    sources: list[dict[str, str]] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        url = scrub_url_secrets(str(item.get("url") or "").strip())
        title = " ".join(str(item.get("title") or "").split())
        domain = str(item.get("domain") or "").strip()
        if not domain and url.startswith(("http://", "https://")):
            domain = urlparse(url).netloc
        key = url or title
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        entry: dict[str, str] = {"title": title or domain or url, "url": url}
        if domain:
            entry["domain"] = domain
        snippet = " ".join(str(item.get("snippet") or "").split())
        if snippet:
            entry["snippet"] = scrub_url_secrets(snippet)
        sources.append(entry)
    return sources


def _format_media_lines(media_items: list[dict[str, str]]) -> str:
    if not media_items:
        return ""
    return "\n".join(f"> 📎 {m['label']}：{m['name']}" for m in media_items)


def _format_sources_block(sources: list[dict[str, str]]) -> str:
    if not sources:
        return ""
    lines = ["**参考来源：**"]
    for idx, src in enumerate(sources, 1):
        title = src.get("title") or src.get("domain") or src.get("url") or f"来源 {idx}"
        url = src.get("url") or ""
        if url:
            lines.append(f"{idx}. [{title}]({url})")
        else:
            lines.append(f"{idx}. {title}")
    return "\n".join(lines)


def export_filename(title: str, chat_id: str, ext: str = "md") -> str:
    clean = _SAFE_FILENAME_RE.sub(" ", str(title or "").strip()).strip(" .")[:40]
    base = clean or f"chat-{chat_id[:8]}"
    return f"{base}.{ext.lstrip('.')}"


def export_chat(store: Any, chat_id: str) -> dict[str, Any]:
    """Build both structured ``turns`` and clean Chinese Markdown for ``chat_id``."""
    state = companion_state.read_companion_state()
    main_chat_id = state.get("chat_id")
    companion_name = str(state.get("name") or "").strip() or "伙伴"

    raw_title = store.title(chat_id).strip()
    if raw_title:
        title = raw_title
    elif chat_id and chat_id == main_chat_id:
        title = "主要聊天"
    else:
        title = "对话记录"

    now_dt = datetime.now().astimezone()
    exported_at = now_dt.isoformat(timespec="seconds")
    exported_display = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    turns: list[dict[str, Any]] = []
    turns_by_id: dict[str, dict[str, Any]] = {}

    for ev in store.events(chat_id):
        event_type = ev.get("event")
        if event_type not in ("user_message", "message"):
            continue
        if ev.get("kind") == "tool_hint" or ev.get("turn_phase") == "activity":
            continue
        source_meta = ev.get("source") if isinstance(ev.get("source"), dict) else {}
        if source_meta.get("kind") == "notice":
            continue

        tid = str(ev.get("turn_id") or "").strip()
        at_ms = int(ev.get("created_at_ms") or 0)
        text = _strip_internal_cards(str(ev.get("text") or ""))
        media = _normalize_media(ev.get("media_urls"))
        sources = _normalize_sources(ev.get("sources"))

        if event_type == "user_message":
            body_parts = [p for p in (text, _format_media_lines(media)) if p]
            user_body = "\n\n".join(body_parts)
            turn_entry: dict[str, Any] = {
                "turn_id": tid,
                "created_at_ms": at_ms,
                "user": user_body,
                "assistant": "",
                "speaker": companion_name,
                "media": list(media),
                "sources": list(sources),
            }
            turns.append(turn_entry)
            if tid:
                turns_by_id[tid] = turn_entry
            continue

        # Assistant / helper / proactive reply
        speaker = str(source_meta.get("label") or "").strip() or companion_name
        body_parts = [p for p in (text, _format_media_lines(media)) if p]
        reply_body = "\n\n".join(body_parts)
        if not reply_body and not sources:
            continue

        target_turn = turns_by_id.get(tid) if tid else None
        if target_turn is None:
            target_turn = {
                "turn_id": tid,
                "created_at_ms": at_ms,
                "user": "",
                "assistant": reply_body,
                "speaker": speaker,
                "media": list(media),
                "sources": list(sources),
            }
            turns.append(target_turn)
            if tid:
                turns_by_id[tid] = target_turn
        else:
            if reply_body:
                target_turn["assistant"] = (
                    f"{target_turn['assistant']}\n\n{reply_body}" if target_turn["assistant"] else reply_body
                )
            if speaker != companion_name:
                target_turn["speaker"] = speaker
            if media:
                target_turn["media"].extend(media)
            if sources:
                existing_urls = {s.get("url") or s.get("title") for s in target_turn["sources"]}
                for src in sources:
                    key = src.get("url") or src.get("title")
                    if key not in existing_urls:
                        existing_urls.add(key)
                        target_turn["sources"].append(src)

    filtered_turns: list[dict[str, Any]] = []
    for t in turns:
        if not t["user"] and not t["assistant"] and not t["sources"]:
            continue
        out_turn: dict[str, Any] = {
            "turn_id": t["turn_id"],
            "created_at_ms": t["created_at_ms"],
            "user": t["user"],
            "assistant": t["assistant"],
        }
        if t["media"]:
            out_turn["media"] = t["media"]
        if t["sources"]:
            out_turn["sources"] = t["sources"]
        out_turn["_speaker"] = t["speaker"]
        filtered_turns.append(out_turn)

    md_sections: list[str] = [
        f"# {title}\n\n- 导出时间：{exported_display}\n- 会话 ID：`{chat_id}`",
        "---",
    ]

    if not filtered_turns:
        md_sections.append("_（暂无对话内容）_")
    else:
        for idx, t in enumerate(filtered_turns, 1):
            speaker = t.pop("_speaker", companion_name)
            turn_blocks: list[str] = [f"## 第 {idx} 轮"]
            if t["user"]:
                turn_blocks.append(f"### 我\n\n{t['user']}")
            if t["assistant"] or t.get("sources"):
                assistant_parts = [p for p in (t["assistant"], _format_sources_block(t.get("sources") or [])) if p]
                turn_blocks.append(f"### {speaker}\n\n" + "\n\n".join(assistant_parts))
            md_sections.append("\n\n".join(turn_blocks))

    markdown = "\n\n".join(md_sections).rstrip() + "\n"
    return {
        "chat_id": chat_id,
        "title": title,
        "exported_at": exported_at,
        "markdown": markdown,
        "turns": filtered_turns,
    }
