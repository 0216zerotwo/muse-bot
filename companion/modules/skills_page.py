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

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _root() -> Path:
    from muse.paths import get_muse_home

    return get_muse_home() / "skills"


def _bundled() -> set[str]:
    try:
        lines = (_root() / ".bundled_manifest").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.split(":", 1)[0].strip() for line in lines if line.strip()}


def _meta(text: str) -> dict[str, str]:
    match = _FRONT.match(text)
    meta: dict[str, str] = {}
    for line in (match.group(1) if match else "").splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta


def list_skills() -> dict[str, Any]:
    bundled = _bundled()
    learned, builtin = [], []
    for path in _root().glob("**/SKILL.md"):
        if any(part.startswith(".") for part in path.relative_to(_root()).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _meta(text)
        name = meta.get("name") or path.parent.name
        row = {"name": name, "description": meta.get("description", "")[:200],
               "category": path.parent.parent.name if path.parent.parent != _root() else "",
               "updated_at": int(path.stat().st_mtime * 1000)}
        (builtin if name in bundled or path.parent.name in bundled else learned).append(row)
    learned.sort(key=lambda r: r["updated_at"], reverse=True)
    builtin.sort(key=lambda r: r["name"])
    return {"learned": learned, "bundled": builtin}


def read_skill(name: str) -> dict[str, Any]:
    from tools.skill_manager_tool import _find_skill

    found = _find_skill(name)
    if not found:
        raise ValueError("没有这个技能")
    path = Path(found["path"])
    path = path / "SKILL.md" if path.is_dir() else path
    return {"name": name, "content": path.read_text(encoding="utf-8"), "bundled": name in _bundled()}


def _manage(action: str, name: str, content: Optional[str] = None) -> dict[str, Any]:
    from tools.skill_manager_tool import _skill_gate_bypass, skill_manage

    token = _skill_gate_bypass.set(True)
    try:
        result = json.loads(skill_manage(action=action, name=name, content=content))
    finally:
        _skill_gate_bypass.reset(token)
    if not result.get("success", False):
        raise ValueError(str(result.get("error") or result.get("message") or "没有改成"))
    return result


def save_skill(name: str, content: str, new: bool = False) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("技能内容是空的")
    _manage("create" if new else "edit", name, content)
    return list_skills()


def delete_skill(name: str) -> dict[str, Any]:
    if name in _bundled():
        raise ValueError("自带的技能不能删")
    _manage("delete", name)
    return list_skills()


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
