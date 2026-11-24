"""Canonical status & error notice keys, allowed format placeholders, and Chinese templates."""

from __future__ import annotations

import json
import logging
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .paths import get_muse_home

logger = logging.getLogger(__name__)

_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    "gateway.stop.stopped": ((), "好，先停下了。需要的话随时接着说。"),
    "gateway.stop.stopped_pending": ((), "好，先停下了。需要的话随时接着说。"),
    "gateway.goal.set": (("budget", "goal"), "好，这件事我会一直做到办完：{goal}\n中途需要你的时候我会说，也可以随时点上面的「停下」。"),
    "gateway.goal.paused": (("goal",), "⏸ 先停下了：{goal}"),
    "gateway.goal.resumed": (("goal",), "▶ 接着做：{goal}"),
    "gateway.goal.no_goal_set": ((), "现在没有在办的事。"),
    "gateway.busy.ack_detail_wrap": (("detail",), ""),
    "gateway.busy.steered_head": ((), "收到，我边做边按你说的来"),
    "gateway.busy.steered_tail": ((), "。"),
    "gateway.busy.steered_subagents_head": ((), "收到，我和帮手们边做边按你说的来"),
    "gateway.busy.steered_subagents_tail": ((), "。"),
    "gateway.busy.redirected_head": ((), "好，按你刚说的调整"),
    "gateway.busy.redirected_tail": ((), "。"),
    "gateway.busy.queued_head": ((), "收到，手上这件做完就回你"),
    "gateway.busy.queued_tail": ((), "。"),
    "gateway.busy.interrupting_head": ((), "好，先放下手上的事回你"),
    "gateway.busy.interrupting_tail": ((), "。"),
    "gateway.busy.subagent_working_head": ((), "帮手们还在忙"),
    "gateway.busy.compressing_head": ((), "我在整理前面聊过的内容"),
    "gateway.busy.demoted_tail": ((), "，你这条我记下了，忙完就处理。"),
    "gateway.busy.action_restarting": ((), "重启"),
    "gateway.busy.action_shutting_down": ((), "关一下"),
    "gateway.busy.drain_queued": (("action",), "我正在{action}，你这条记下了，回来就处理。"),
    "gateway.busy.drain_rejected": (("action",), "我正在{action}，稍等一下再发给我。"),
    "gateway.busy.drain_rejected_new_work": (("action",), "我正在{action}，稍等一下再交给我。"),
    "gateway.busy.another_turn_running": ((), "我还在回上一条，等我说完再发一次吧。"),
    "gateway.busy.draining_maintenance": ((), "我在做一点维护，过一会儿再发给我。"),
    "gateway.shutdown.notice_shutdown": ((), "我要先关一下，手上的事会停下。回来后跟我说一声，我接着做。"),
    "gateway.shutdown.notice_restart": ((), "我重启一下，手上的事会先停下。好了之后跟我说一声，我接着做。"),
    "gateway.reset.header_default": ((), "好，开始新的对话。我记得的关于你的事都还在。"),
    "gateway.reset.header_new": ((), "好，开始新的对话。我记得的关于你的事都还在。"),
    "gateway.reset.tip": (("tip",), ""),
    "gateway.session.info_model": (("model",), ""),
    "gateway.session.info_provider": (("provider",), ""),
    "gateway.session.info_context": (("source", "tokens"), ""),
    "gateway.session.info_acting_model": (("model", "provider"), ""),
    "gateway.session.info_endpoint": (("url",), ""),
    "gateway.startup.restarted": ((), "我回来了。"),
    "gateway.startup.online": ((), "我回来了。"),
    "gateway.restart.restarting": ((), "我重启一下，马上回来。"),
    "gateway.restart.in_progress": ((), "正在重启，马上回来。"),
    "gateway.errors.connection_interrupted": ((), "刚才和模型那边的连接断了一下，再试一次就好（可以点「重新生成」）。"),
    "gateway.errors.interrupted_before_start": ((), "这条还没来得及处理就被打断了，再发一次吧。"),
    "gateway.errors.rate_limited": ((), "模型那边现在请求太多，等一会儿再试（可以点「重新生成」）。"),
    "gateway.errors.auth_failed": ((), "登录模型服务失败了。去「设置 → 模型」重新登录，或换一个模型。"),
    "gateway.errors.bad_request": ((), "模型这次拒绝了这个请求。换个说法试试，或去「设置 → 模型」换一个。"),
    "gateway.errors.unreachable": ((), "现在连不上模型服务，可能是网络或服务暂时出了问题。等一会儿再试；一直不行就去「设置 → 模型」换一个。"),
    "gateway.errors.connection_unknown": ((), "现在连不上模型服务。等一会儿再试；一直不行就去「设置 → 模型」换一个。"),
    "gateway.errors.context_overflow": ((), "这段对话太长了，我一次读不完。开一个新的旁聊接着聊吧。"),
    "gateway.errors.usage_limit_resets": (("window",), "模型的用量到上限了，{window} 后恢复。着急的话去「设置 → 模型」换一个。"),
    "gateway.errors.provider_kept_failing": ((), "模型服务一直出错。等一会儿再试，或去「设置 → 模型」换一个。"),
    "gateway.errors.generic_failed": ((), "这次没能回完，出了点问题。再试一次吧（可以点「重新生成」）。"),
    "gateway.errors.generic_failed_with_hint": (("hint",), "这次没能回完。{hint}\n再试一次吧（可以点「重新生成」）。"),
    "gateway.errors.stopped_before_finishing": (("reason",), "我没做完就停下了{reason}。再试一次吧；聊得很久的话，开个新的旁聊会更顺。"),
    "gateway.errors.no_response": ((), "这次没有生成回复，可能是临时出错，再发一次试试。"),
    "gateway.errors.unexpected_silence": ((), "这次模型没给出回复。换个说法，或再试一次。"),
    "gateway.errors.no_credentials": ((), "我连不上模型服务，这条没有处理。去「设置 → 模型」登录或换一个模型。"),
    "gateway.errors.hint_auth": (("relogin",), " 模型服务的登录过期了，或者密钥不对。"),
    "gateway.errors.hint_quota": ((), " 模型服务的余额或额度用完了：去那家服务的网站充值，或在「设置 → 模型」换一个。"),
    "gateway.errors.hint_overloaded": ((), " 模型服务现在太忙了。"),
}

NOTICE_PLACEHOLDERS: dict[str, frozenset[str]] = {k: frozenset(p) for k, (p, _) in _SPECS.items()}
DEFAULT_NOTICES_ZH: dict[str, str] = {k: txt for k, (_, txt) in _SPECS.items()}
NOTICE_KEYS: frozenset[str] = frozenset(NOTICE_PLACEHOLDERS)


def extract_placeholders(template: str) -> set[str]:
    """Extract top-level ``{field}`` placeholder names from a ``str.format`` template."""
    return {
        root
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name and (root := field_name.split(".", 1)[0].split("[", 1)[0])
    }


def allowed_placeholders(key: str) -> frozenset[str]:
    return NOTICE_PLACEHOLDERS[key]


def validate_notice_template(key: str, template: str) -> None:
    if key not in NOTICE_PLACEHOLDERS:
        raise KeyError(f"Unknown notice key: {key!r}")
    disallowed = extract_placeholders(template) - NOTICE_PLACEHOLDERS[key]
    if disallowed:
        raise ValueError(f"Notice {key!r} uses unsupported placeholders {sorted(disallowed)}")


def validate_overlay(overlay: Mapping[str, str]) -> list[str]:
    issues: list[str] = []
    for key, template in overlay.items():
        try:
            validate_notice_template(key, template)
        except (KeyError, ValueError) as exc:
            issues.append(str(exc))
    return issues


def _load_home_overlay(home: Path | None = None, lang: str = "zh") -> dict[str, str]:
    path = Path(home if home is not None else get_muse_home()) / "locales" / f"{lang}.yaml"
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        if raw.lstrip().startswith("{"):
            parsed = json.loads(raw)
        else:
            from ruamel.yaml import YAML

            parsed = YAML(typ="safe").load(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}
    except Exception as exc:
        logger.debug("Could not read locale overlay %s: %s", path, exc)
    return {}


def render_notice(
    key: str,
    overlay: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    lang: str = "zh",
    **format_kwargs: Any,
) -> str:
    """Render a canonical notice key with optional format arguments, never raising on missing kwargs."""
    template = (overlay[key] if overlay is not None and key in overlay else None)
    if template is None:
        template = _load_home_overlay(home=home, lang=lang).get(key)
    if template is None:
        template = DEFAULT_NOTICES_ZH.get(key, key)
    if not format_kwargs or not template:
        return template
    try:
        return template.format(**format_kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Notice format fallback for key=%r fields=%r: %s", key, sorted(format_kwargs), exc)
        return template


__all__ = [
    "DEFAULT_NOTICES_ZH", "NOTICE_KEYS", "NOTICE_PLACEHOLDERS", "allowed_placeholders",
    "extract_placeholders", "render_notice", "validate_notice_template", "validate_overlay",
]
