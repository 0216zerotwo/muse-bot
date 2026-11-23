"""查找聊天记录: what was said, in any of the user's chats (the side menu's search, as WeChat's).

Each hit is one message — the user's or the companion's — with the words around the match, its chat
and its time; newest first. Cards and pages in a reply are left out of what is searched (their code
would match words the user never saw). Helpers' working chats and the browser desk are not searched.
"""

from __future__ import annotations

import re
from typing import Any

_CARD = re.compile(r"```(?:card|json|ui)[ \t]*\n[\s\S]*?(?:\n```|$)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AROUND = 28
_LIMIT = 40


def _plain(text: str) -> str:
    text = _CARD.sub(" ", text or "")
    text = _IMAGE.sub(r" \1 ", text)
    text = _LINK.sub(r"\1", text)
    return " ".join(text.replace("**", "").split())


def _snippet(text: str, at: int, length: int) -> str:
    start, end = max(0, at - _AROUND), min(len(text), at + length + _AROUND)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def search(store: Any, query: str, main_chat: str | None = None, limit: int = _LIMIT) -> dict[str, Any]:
    words = " ".join(str(query or "").split())
    if not words:
        return {"query": "", "hits": []}
    needle = words.casefold()
    tokens = [tok.casefold() for tok in words.split() if tok]
    hits: list[dict[str, Any]] = []
    for chat_id, meta in store._read_index().items():
        if meta.get("desk") or meta.get("helper"):
            continue
        title = "主要聊天" if chat_id == main_chat else (meta.get("title") or "旁聊")
        for event in store.events(chat_id):
            kind = event.get("event")
            if kind not in ("user_message", "message") or event.get("kind") == "tool_hint":
                continue
            if (event.get("source") or {}).get("kind") == "notice":
                continue
            text = _plain(str(event.get("text") or ""))
            folded = text.casefold()
            at = folded.find(needle)
            match_len = len(words)
            if at < 0 and len(tokens) > 1 and all(tok in folded for tok in tokens):
                at = folded.find(tokens[0])
                match_len = len(tokens[0])
            if at < 0:
                continue
            hits.append({"chat_id": chat_id, "chat_title": title, "role": "user" if kind == "user_message" else "assistant",
                         "text": _snippet(text, at, match_len), "turn_id": event.get("turn_id"),
                         "at_ms": event.get("created_at_ms") or 0})
    hits.sort(key=lambda hit: hit["at_ms"], reverse=True)
    return {"query": words, "hits": hits[:limit], "more": len(hits) > limit}
