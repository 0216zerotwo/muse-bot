"""Contextual follow-up chips (`GET /api/sessions/{key}/followups`).

Extracts the latest (or requested `turn_id`) exchange from a chat transcript, caches
`(chat_id, turn_id)` results in a thread-safe 128-entry `OrderedDict` LRU, and generates
3 concise natural Chinese follow-up prompts via `quick_model` (with an instant, feature-aware
heuristic fallback when no model API key is configured or the fast call times out).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, Optional

CACHE_MAX_SIZE = 128

_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple[str, str], list[str]] = OrderedDict()

_CARD_BLOCK_RE = re.compile(r"```(?:card|ui|json)[ \t]*\n[\s\S]*?(?:\n```|$)", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?!(?:card|ui|json)\b)[^\n]*\n[\s\S]*?\n```", re.IGNORECASE)
_CODE_SIGNAL_RE = re.compile(
    r"(?:\b(?:def|class|function|const|import|from|SELECT|pytest|npm|git|docker|curl)\b|Traceback|Error:|Exception)",
    re.IGNORECASE,
)
_ERROR_SIGNAL_RE = re.compile(r"(?:Traceback|Error:|Exception|报错|失败|异常|bug|修复|排查)", re.IGNORECASE)
_COMPARE_SIGNAL_RE = re.compile(r"(?:对比|区别|优缺点|优势|劣势|方案\s*[A-Z1-9]|哪个更|相比|versus|\bvs\.?\b)", re.IGNORECASE)
_STEP_SIGNAL_RE = re.compile(r"(?:(?:^|\n)\s*(?:1\.|2\.|3\.|第一步|步骤)|清单|流程|checklist|\|\s*---)", re.IGNORECASE)
_SUMMARY_SIGNAL_RE = re.compile(r"(?:总结|摘要|纪要|提炼|润色|改写|翻译|大纲|邮件|稿件|文案)", re.IGNORECASE)
_URL_OR_CITE_RE = re.compile(r"(?:https?://|\[\d{1,2}\]|参考来源|调研|研究报告|信源)", re.IGNORECASE)
_LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*•·]+|\d+[.、)）])\s*")


def clear_cache() -> None:
    """Clear the in-process follow-up LRU cache (primarily for tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(key: tuple[str, str]) -> Optional[list[str]]:
    with _CACHE_LOCK:
        val = _CACHE.get(key)
        if val is None:
            return None
        _CACHE.move_to_end(key)
        return list(val)


def _cache_set(key: tuple[str, str], suggestions: list[str]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = list(suggestions)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX_SIZE:
            _CACHE.popitem(last=False)


def _extract_turn(
    events: list[dict[str, Any]],
    turn_id: Optional[str] = None,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Return `(eff_turn_id, user_text, assistant_text, sources)` for the target or latest turn."""
    wanted = str(turn_id or "").strip()

    def _is_user(ev: dict[str, Any]) -> bool:
        return ev.get("event") == "user_message" and (ev.get("source") or {}).get("kind") != "notice"

    def _is_assistant(ev: dict[str, Any]) -> bool:
        return (
            ev.get("event") == "message"
            and ev.get("kind") != "tool_hint"
            and (ev.get("source") or {}).get("kind") != "notice"
        )

    eff_turn = wanted
    if not eff_turn:
        for ev in reversed(events):
            tid = str(ev.get("turn_id") or "").strip()
            if tid and _is_assistant(ev) and str(ev.get("text") or "").strip():
                eff_turn = tid
                break
        if not eff_turn:
            for ev in reversed(events):
                tid = str(ev.get("turn_id") or "").strip()
                if tid and _is_user(ev) and str(ev.get("text") or "").strip():
                    eff_turn = tid
                    break
    if not eff_turn:
        return "", "", "", []

    user_parts: list[str] = []
    assistant_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    for ev in events:
        if str(ev.get("turn_id") or "").strip() != eff_turn:
            continue
        if _is_user(ev):
            txt = str(ev.get("text") or "").strip()
            if txt:
                user_parts.append(txt)
        elif _is_assistant(ev):
            txt = str(ev.get("text") or "").strip()
            if txt:
                assistant_parts.append(txt)
            raw_sources = ev.get("sources")
            if isinstance(raw_sources, list):
                sources.extend(s for s in raw_sources if isinstance(s, dict))

    if not user_parts and not assistant_parts:
        return "", "", "", []
    return eff_turn, "\n".join(user_parts), "\n\n".join(assistant_parts), sources


def _strip_cards(text: str) -> str:
    return _CARD_BLOCK_RE.sub(" ", text or "").strip()


def heuristic_followups(
    user_text: str,
    assistant_text: str,
    *,
    sources: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """Generate 3 contextual, natural Chinese follow-up suggestions without calling an LLM."""
    combined = f"{user_text}\n{assistant_text}"
    plain_reply = _strip_cards(assistant_text)

    has_code = bool(_CODE_FENCE_RE.search(assistant_text) or _CODE_SIGNAL_RE.search(plain_reply))
    has_error = bool(_ERROR_SIGNAL_RE.search(combined))
    has_sources = bool(sources) or bool(_URL_OR_CITE_RE.search(plain_reply))
    has_compare = bool(_COMPARE_SIGNAL_RE.search(combined))
    has_steps = bool(_STEP_SIGNAL_RE.search(plain_reply))
    has_summary = bool(_SUMMARY_SIGNAL_RE.search(combined))

    if has_code and has_error:
        return ["解释报错背后的根本原因", "给出可直接运行的修复代码", "如何写测试防止同类问题复发"]
    if has_code:
        return ["帮我补上关键边界的单元测试", "这段实现还有哪些性能或坑？", "能再精简一下核心代码吗？"]
    if has_sources:
        return ["对比不同来源里的关键分歧", "挑出最值得深入核实的细节", "整理成一页可直接分享的摘要"]
    if has_compare:
        return ["按我的场景直接给个首选方案", "列出各方案容易忽略的隐性成本", "选定之后第一步怎么落地？"]
    if has_steps:
        return ["把第一步拆成更具体的操作", "执行过程中最容易踩什么坑？", "帮我整理成一份待办检查清单"]
    if has_summary:
        return ["再提炼成三条最核心的结论", "换成更简洁自然的口语表达", "针对最关键的一点展开细说"]
    return ["举个具体的实际例子说明一下", "这里还有哪些容易忽略的细节？", "接下来我最适合先做哪一步？"]


def _resolve_quick_model_target() -> Optional[dict[str, Any]]:
    """Return `{"provider", "model"}` if a usable configured model with credentials exists, else None."""
    try:
        from companion.modules import models as models_mod
        from muse.llm.providers import resolve_runtime_provider
    except Exception:
        return None

    target: dict[str, Any] = {}
    try:
        data = models_mod.read_models()
        presets = data.get("presets") or {}
        if presets:
            name = models_mod.quick_model(data, list(presets))
            if name and name in presets:
                target = {
                    "provider": models_mod.runtime_provider(name, presets[name]),
                    "model": presets[name]["model"],
                }
    except Exception:
        target = {}

    try:
        resolved = resolve_runtime_provider(
            requested=target.get("provider"),
            target_model=target.get("model"),
        )
    except Exception:
        return None

    api_key = str(resolved.get("api_key") or "").strip()
    model = str(target.get("model") or resolved.get("model") or "").strip()
    base_url = str(resolved.get("base_url") or "").strip()
    if not api_key or not model or not base_url:
        return None
    return {
        "provider": str(target.get("provider") or resolved.get("provider") or ""),
        "model": model,
    }


def _parse_suggestions(raw: Optional[str]) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    items: list[Any] = []
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                items = parsed
        except ValueError:
            items = []
    if not items:
        items = [line for line in text.splitlines() if line.strip()]

    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in items:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("prompt") or entry.get("question") or ""
        cand = _LEADING_BULLET_RE.sub("", str(entry or "").strip()).strip(" \"'「」『』《》，。")
        cand = " ".join(cand.split())
        if len(cand) < 4:
            continue
        if len(cand) > 24:
            cand = cand[:22].rstrip("，、；： ")
        key = cand.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(cand)
        if len(cleaned) >= 3:
            break
    return cleaned


async def _generate_via_llm(
    target: dict[str, Any],
    user_text: str,
    assistant_text: str,
    *,
    sources: Sequence[dict[str, Any]] = (),
) -> list[str]:
    from muse.llm.auxiliary import async_call_llm
    from muse.llm.types import RetryPolicy

    source_hint = f"\n（本轮引用了 {len(sources)} 个外部信源）" if sources else ""
    messages = [
        {
            "role": "system",
            "content": (
                "根据刚才的对话，站在用户的角度生成 3 条简短自然的后续追问建议。"
                "每条 6~18 个字，具体实用、不套话、不重复。只输出一个 JSON 字符串数组，不要解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户：{user_text[:400] or '（无）'}\n"
                f"回答：{_strip_cards(assistant_text)[:1200] or '（无）'}{source_hint}"
            ),
        },
    ]
    response = await asyncio.wait_for(
        async_call_llm(
            messages=messages,
            max_tokens=120,
            temperature=0.3,
            timeout=5,
            reasoning_config={"enabled": False},
            retry_policy=RetryPolicy(max_retries=0),
            **target,
        ),
        timeout=5.5,
    )
    content = response.choices[0].message.content if response and response.choices else ""
    return _parse_suggestions(content)


async def get_followups(
    store: Any,
    chat_id: str,
    *,
    turn_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return 3 contextual follow-up suggestions for `chat_id` (latest or specified `turn_id`)."""
    events = await asyncio.to_thread(store.events, chat_id)
    eff_turn_id, user_text, assistant_text, sources = _extract_turn(events, turn_id=turn_id)
    if not eff_turn_id:
        return {"chat_id": chat_id, "turn_id": "", "suggestions": []}

    cache_key = (chat_id, eff_turn_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"chat_id": chat_id, "turn_id": eff_turn_id, "suggestions": cached}

    suggestions: list[str] = []
    target = await asyncio.to_thread(_resolve_quick_model_target)
    if target is not None:
        try:
            suggestions = await _generate_via_llm(target, user_text, assistant_text, sources=sources)
        except Exception:
            suggestions = []

    if len(suggestions) < 3:
        fallback = heuristic_followups(user_text, assistant_text, sources=sources)
        seen = {s.casefold() for s in suggestions}
        for item in fallback:
            if item.casefold() not in seen:
                suggestions.append(item)
                seen.add(item.casefold())
            if len(suggestions) >= 3:
                break

    final = suggestions[:3]
    _cache_set(cache_key, final)
    return {"chat_id": chat_id, "turn_id": eff_turn_id, "suggestions": final}


__all__ = ["CACHE_MAX_SIZE", "clear_cache", "get_followups", "heuristic_followups"]
