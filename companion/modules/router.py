"""Pick how each answer is shown before the companion writes it (as OpenIntelligentUI's router does).

It runs before every reply, so it has to be quick. Like LiteLLM's auto-router, rules go first:
a short acknowledgement or a command (好的、谢谢、重新生成…) needs no form at all, and a request
whose words say it plainly (算一下房贷、走势、规划路线…) gets its form without asking a model.
Only the rest goes to the cheapest model, with thinking off and a one-line answer, for one form:
a short text reply, a native card, or a small generated app (```ui) — a chart, a calculator, a
map/itinerary, a tunable comparison… The choice is added to this turn's user message
(``pre_llm_call`` context), so the system prompt and its cache stay untouched. If the router
fails or is slow, nothing is added and the companion decides by its own rules.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_S = 6.0
_RECENT = 4
# Nothing to present: acknowledgements, small talk, and asks about the previous answer (重新生成
# keeps its form from the chat itself).
_NO_FORM = re.compile(
    r"^(?:好+的?|嗯+|哦+|噢+|行|可以|收到|ok|okay|谢谢|多谢|感谢|辛苦了|哈+|嘿+|晚安|早安?|午安|在吗|你好|您好|hi|hello"
    r"|先不管|先不用|不用了|算了|没事了?|就这样|好的?谢谢|重新生成|再来一[次个遍]|继续|接着说|展开说说|详细点)[!！。.~～?？\s]*$",
    re.IGNORECASE,
)
# Requests that say their form in so many words (checked in order; the first match wins).
_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # A calculator only where there are several things to adjust (rate, term, people…); a sum is text.
    (re.compile(r"房贷|车贷|贷款|月供|复利|分期|汇率换算|分账|AA制?|预算(?:表|规划)|个税|工资.*算|做个计算器"), "ui", "calculator"),
    (re.compile(r"(?:路线|行程|攻略|怎么走|逛一天|几日游|一日游|自驾|旅行计划)"), "ui", "itinerary"),
    (re.compile(r"走势|趋势|涨跌|行情|大盘|K线|占比|分布|曲线|图表|画个图|做个图"), "ui", "chart"),
    (re.compile(r"天气|下雨|气温|带伞"), "card", "weather"),
    (re.compile(r"车次|高铁|火车票|动车"), "card", "trains"),
    (re.compile(r"清单|待办|checklist|要带什么|准备什么"), "card", "checklist"),
)
_FORMS = {
    "text": "这一轮用文字回答：1~3 句话，不用卡片、不用界面。",
    "card": "这一轮用文字（一句）加一个原生卡片（```card）回答：{kind}。",
    "ui": "这一轮用一句话加一个生成界面（```ui）回答：做一个{kind}，在手机上一屏看完，可以直接点、拖、切换，本地即时计算；"
          "需要我接着办的事放成 data-send 按钮。卡片外的文字不超过一句。",
}
_KINDS_UI = {
    "chart": "图表", "calculator": "计算器/可调参数的小工具", "map": "地图（带编号地点）", "itinerary": "行程（地图+时间线）",
    "compare": "可切换偏好的对比", "plan": "可调整的方案/预算", "dashboard": "数据看板", "diagram": "流程图/示意图",
    "tool": "小工具",
}
_KINDS_CARD = {
    "buttons": "选项按钮（buttons）", "choices": "单选/多选（choices）", "weather": "天气卡片（weather）",
    "trains": "车次卡片（trains）", "places": "地点卡片（places）", "timeline": "时间线（timeline）",
    "checklist": "清单（checklist）", "table": "表格（table）", "form": "表单（form）", "compare": "对比表（compare）",
}

_PROMPT = """你是一个呈现方式路由器。用户在手机聊天 App 里提问，你只决定这一轮回复怎么呈现，不回答问题本身。
可选 form：
- text：闲聊、问候、简单事实、一句话能说清的回答、用户只是在确认或道谢。
- card：结果是一组选项、天气、车次、几个地点、简单时间线、清单、小表格、需要用户填几项信息；kind 取 {cards} 之一。
- ui：用户会想自己调参数、看变化或在图上看结果：图表/趋势/占比、计算器（房贷、预算、分账、换算…）、地图/行程规划、需要按偏好切换的多方案对比、方案/预算规划、数据看板、流程示意；kind 取 {uis} 之一。
大多数回复是 text。只有用户真的会去点、拖、切换，或者结果是一组结构化信息时才用 card/ui；一个数字、一句结论、一个算式的结果都是 text。拿不准就选 text。
只输出一行 JSON：{{"form": "text|card|ui", "kind": "…"}}"""


def _text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return " ".join(str(content or "").split())


# A sum, a unit or a single fact asked for: the answer is a number or a line.
# (a minus only with spaces round it: 2026-10-25 is a date)
_SIMPLE = re.compile(r"^[^\n]{0,40}?\d[\d.,]*\s*(?:[+*/×÷^]|\s-\s)\s*\d[\d\s.,+\-*/×÷^()]*[^\n]{0,24}$"
                     r"|多少钱$|几点了?$|是什么意思[？?]?$|怎么读[？?]?$")


# Asked to keep it short: text, whatever the subject (复利 alone would get a calculator).
_BRIEF = re.compile(r"一句话|一两句|两三句|简单(?:说|讲|解释|介绍)|简短|简要|长话短说")


def quick_choice(user_message: str) -> Optional[dict[str, str]]:
    """The form from the words alone: ``{"form": "none"}`` for nothing to present, a form for a
    plain request, None when a model has to judge."""
    text = user_message.strip()
    if _NO_FORM.match(text) or len(text) <= 2:
        return {"form": "none", "kind": ""}
    if _SIMPLE.search(text) or _BRIEF.search(text):
        return {"form": "text", "kind": ""}
    for pattern, form, kind in _RULES:
        if pattern.search(text):
            return {"form": form, "kind": kind}
    return None


def classify(user_message: str, history: list[Any]) -> Optional[dict[str, str]]:
    """``{"form", "kind"}`` for this turn, or None when the router can't say."""
    from agent.auxiliary_client import call_llm

    from .models import quick_model, read_models, runtime_provider

    data = read_models()
    names = list(data["presets"])
    cheap = quick_model(data, names) if names else None
    target = {"provider": runtime_provider(cheap, data["presets"][cheap]), "model": data["presets"][cheap]["model"]} if cheap else {}
    recent = []
    for message in history[-_RECENT:]:
        role = message.get("role") if isinstance(message, dict) else None
        if role in ("user", "assistant") and _text(message):
            recent.append(f"{'用户' if role == 'user' else '助手'}：{_text(message)[:200]}")
    prompt = _PROMPT.format(cards="、".join(_KINDS_CARD), uis="、".join(_KINDS_UI))
    user = ("最近的对话：\n" + "\n".join(recent) + "\n\n" if recent else "") + f"这一轮用户说：{user_message[:600]}"
    try:
        # One short line of JSON, no thinking first: this call sits in front of every reply.
        response = call_llm(
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            max_tokens=60, temperature=0, timeout=_TIMEOUT_S, reasoning_config={"enabled": False}, **target,
        )
        raw = response.choices[0].message.content or ""
    except Exception as exc:
        logger.info("companion: presentation router unavailable (%s)", type(exc).__name__)
        return None
    match = re.search(r"\{[^{}]*\}", raw)
    try:
        choice = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None
    form = choice.get("form")
    if form not in _FORMS:
        return None
    return {"form": form, "kind": str(choice.get("kind") or "")}


# Asked as soon as the app hands a message over, so the answer is ready (or nearly) when Hermes,
# after setting up the turn (~1–2 s), asks for it: message text -> its pending choice.
_pending: dict[str, Future] = {}
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="companion-router")


def prefetch(user_message: str, history: list[Any]) -> None:
    """Start judging a message the rules can't place, while the turn is still being set up."""
    text = _text(user_message)
    if not text or text.startswith(("【系统通知】", "/")) or quick_choice(text) is not None:
        return
    while len(_pending) >= 8:  # a turn that never reached the model leaves its entry behind
        _pending.pop(next(iter(_pending)))
    _pending[text] = _workers.submit(classify, text, history)


def instruction(choice: dict[str, str]) -> str:
    form, kind = choice["form"], choice.get("kind", "")
    label = (_KINDS_UI if form == "ui" else _KINDS_CARD).get(kind, kind or ("小工具" if form == "ui" else "合适的卡片"))
    return "【本轮呈现方式（App 自动判断）】" + _FORMS[form].format(kind=label)


def on_pre_llm_call(platform: str = "", user_message: Any = None, conversation_history: Any = None,
                    **kwargs: Any) -> Optional[dict[str, str]]:
    """Hermes hook: only for the companion app, only for something the user typed."""
    if platform != "companion":
        return None
    text = _text(user_message)
    if not text or text.startswith("【系统通知】") or text.startswith("/"):
        return None
    from .helpers import group_for_chat, helper_for_chat, is_desk
    from ..activity import _chat_for_session

    session = kwargs.get("session_id")
    chat = _chat_for_session(str(session)) if session else None
    if chat and (helper_for_chat(chat) or group_for_chat(chat) or is_desk(chat)):
        return None  # a helper answers in plain text; in a group the helpers speak, the host adds a line
    started = time.monotonic()
    choice = quick_choice(text)
    how = "rule"
    if choice is None:
        pending = _pending.pop(text, None)
        how = "prefetched" if pending else "model"
        if pending is None and _pending:
            logger.info("companion: router prefetch missed: %r not in %r", text[:40], [k[:40] for k in _pending])
        try:
            choice = pending.result(timeout=_TIMEOUT_S) if pending else classify(text, list(conversation_history or []))
        except Exception:  # timed out: the companion decides by its own rules
            choice = None
    if choice is None or choice["form"] == "none":
        return None
    logger.info("companion: presenting this turn as %s/%s (%s, %.1fs)", choice["form"], choice.get("kind", ""),
                how, time.monotonic() - started)
    return {"context": instruction(choice)}
