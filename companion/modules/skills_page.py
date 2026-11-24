"""技能: what the companion knows how to do step by step (Hermes skills), shown and edited in the app.

A skill is a SKILL.md under ``<HERMES_HOME>/skills``: a name, a one-line description and the steps.
The companion writes its own when a way of doing something worked (``skill_manage``); the user can
read, change, try or delete them here, and teach it new ones by doing a task once in the browser
(教它做: the browser view records the steps, ``draft_from_steps`` turns them into a skill to review).

The 60-odd skills that come with Hermes are listed apart (``bundled``): they are generic and mostly
for developers. Writes from here go through Hermes's own skill tool, past its approval gate (the
user making the change is the approval), so its checks on names, size and content still apply.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _meta(text: str) -> dict[str, str]:
    match = _FRONT.match(text)
    meta: dict[str, str] = {}
    for line in (match.group(1) if match else "").splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta


def list_skills() -> dict[str, Any]:
    from muse.skills import SkillRegistry

    return SkillRegistry().list_for_page()


def read_skill(name: str) -> dict[str, Any]:
    from muse.skills import SkillRegistry

    rec = SkillRegistry().find_skill(name)
    if rec is None:
        raise ValueError("没有这个技能")
    return {
        "name": rec.name,
        "content": rec.skill_file.read_text(encoding="utf-8-sig"),
        "bundled": rec.bundled,
    }


def save_skill(name: str, content: str, new: bool = False) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("技能内容是空的")
    from muse.skills import SkillRegistry

    reg = SkillRegistry()
    reg.save_skill(name, content, create=new, trusted_user=True)
    return reg.list_for_page()


def delete_skill(name: str) -> dict[str, Any]:
    from muse.skills import SkillRegistry

    reg = SkillRegistry()
    if name in reg.bundled_names():
        raise ValueError("自带的技能不能删")
    rec = reg.find_skill(name)
    if rec is None:
        raise ValueError("没有这个技能")
    reg.delete_skill(name, forbid_bundled=True)
    return reg.list_for_page()


# ── 教它做: recorded steps → a skill draft ───────────────────────────────────────────────────────

_DRAFT_PROMPT = """用户在浏览器里亲手做了一遍一件事，下面是记录下来的操作。把它写成一个可以复用的技能（SKILL.md），以后你能照着做。

要求：
- 开头是 YAML frontmatter：name（小写英文和连字符，2~5 个词，描述这件事）、description（一句中文：什么时候用这个技能）
- 正文用中文：「## 做什么」一两句；「## 步骤」编号列出，每步写清楚在哪个网页、点哪个按钮/在哪个框里填什么（用界面上的文字指认，不用坐标）；
  每次会变的内容（比如搜索词、日期、金额）写成「〈要填的东西〉」；「## 注意」写容易出错的地方、需要用户确认的步骤（付款、发送、删除前要先问用户）
- 记录里的密码已被隐去，技能里也不能出现密码、验证码
- 只输出 SKILL.md 的内容"""


def _steps_text(steps: list[dict[str, Any]]) -> str:
    lines = []
    for i, step in enumerate(steps[:120], 1):
        kind = step.get("do")
        where = f"（在 {step['url']}）" if step.get("url") else ""
        if kind == "open":
            lines.append(f"{i}. 打开 {step.get('url', '')}")
        elif kind == "click":
            lines.append(f"{i}. 点击 {step.get('target') or '页面某处'}{where}")
        elif kind == "type":
            lines.append(f"{i}. 在 {step.get('target') or '输入框'} 里输入「{step.get('text', '')}」{where}")
        elif kind == "key":
            lines.append(f"{i}. 按 {step.get('key', '')} 键")
        elif kind == "back":
            lines.append(f"{i}. 返回上一页")
    return "\n".join(lines)


def draft_from_steps(goal: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    from .llm import ask

    if not steps:
        raise ValueError("没有记录到操作")
    goal = " ".join(str(goal or "").split())[:200]
    content = ask(_DRAFT_PROMPT, f"这件事是：{goal or '（用户没说，从操作里推断）'}\n"
                                 f"今天是 {datetime.now():%Y-%m-%d}\n\n操作记录：\n{_steps_text(steps)}",
                  max_tokens=1800, temperature=0.2).strip()
    content = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", content).strip() + "\n"
    name = _meta(content).get("name") or "taught-task"
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "taught-task"
    return {"name": name, "content": content}
