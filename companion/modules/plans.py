"""Goals the user is working towards, with steps and an optional check-in (shown in 任务).

Hermes's ``/goal`` keeps the agent working on one thing until it is done; these
are the user's own longer goals ("三个月减 5 斤", "年底前把驾照考下来"). Each has
steps the companion marks as it helps, and optionally a check-in cadence: at that
time a scheduled job sends one short question about progress and does nothing
else.

Each goal keeps a work record (``log``, newest last): steps started, done or stuck, work handed to a
helper for it and what came back, and files a step produced — the 目标 page shows it as the goal's
work history.

The companion never rewrites a plan on its own. ``goal_propose_plan`` is held as
an approval card (see ``approvals``); only when the user picks 用新计划 do the
remaining steps change, and finished steps are always kept.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .companion_state import read_companion_state
from .fs_atomic import atomic_write_json
from .paths import get_webui_dir

TOOLSET = "companion"
STATUSES = ("pending", "doing", "done", "blocked")
# Life areas, as nanoMuse's goals page groups them (vendor/nanomuse-web/web/src/goals.tsx).
CATEGORIES = ("health", "finance", "career", "learning", "relationships", "family", "home", "travel", "creative", "other")
CHECKIN_JOB_PREFIX = "目标跟进："
_MAX_STEPS = 12
_MAX_LOG = 40
_STEP_LOG = {"doing": "开始做", "done": "做完了", "blocked": "卡住了", "pending": "改回没做"}
_LOCK = threading.Lock()
_WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
_CADENCE = re.compile(
    r"^(?:(daily|weekdays)|weekly (mon|tue|wed|thu|fri|sat|sun)|monthly ([1-9]|1\d|2[0-8])) "
    r"([01]\d|2[0-3]):([0-5]\d)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _path() -> Path:
    return get_webui_dir() / "goals.json"


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []


def _write(goals: list[dict[str, Any]]) -> None:
    atomic_write_json(_path(), goals)


def _find(goals: list[dict[str, Any]], goal_id: Any) -> dict[str, Any]:
    goal = next((g for g in goals if g.get("id") == goal_id), None)
    if goal is None:
        raise ValueError(f"没有这个目标：{goal_id}")
    return goal


def _log(goal: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
    entry = {"at": int(time.time() * 1000), "kind": kind, "text": " ".join(str(text).split())[:200],
             **{k: v for k, v in extra.items() if v}}
    goal["log"] = [*(goal.get("log") or []), entry][-_MAX_LOG:]


def log_event(goal_id: str, kind: str, text: str, **extra: Any) -> None:
    """Add to a goal's work record from elsewhere (a helper's work for it). Unknown goals are ignored."""
    with _LOCK:
        goals = _read()
        goal = next((g for g in goals if g.get("id") == goal_id), None)
        if goal is None:
            return
        _log(goal, kind, text, **extra)
        _write(goals)


def _steps(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("步骤列表格式不对")
    steps = [{"text": " ".join(str(s).split())[:120], "status": "pending"} for s in raw if str(s).strip()]
    return steps[:_MAX_STEPS]


def cron_expr(cadence: str) -> str:
    """``daily 08:00`` / ``weekdays 07:30`` / ``weekly mon 09:00`` / ``monthly 1 09:00`` as cron."""
    match = _CADENCE.match(cadence or "")
    if not match:
        raise ValueError("跟进时间格式不对（例如 daily 08:00、weekdays 07:30、weekly mon 09:00 或 monthly 1 09:00）")
    every, weekday, day, hour, minute = match.groups()
    hour, minute = int(hour), int(minute)
    if every == "daily":
        return f"{minute} {hour} * * *"
    if every == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if weekday:
        return f"{minute} {hour} * * {_WEEKDAYS[weekday]}"
    return f"{minute} {hour} {int(day)} * *"


def _checkin_prompt(goal: dict[str, Any]) -> str:
    nxt = next((s["text"] for s in goal["steps"] if s["status"] != "done"), None)
    return (
        "[这条消息不会直接给用户看。现在是约好的目标跟进时间。]\n\n"
        f"用户的目标：「{goal['title']}」" + (f"，下一步是「{nxt}」" if nxt else "") + "。\n"
        "只发一条简短的消息，用你平时的口吻问问进展；不要做任何别的事，不要调用工具，不要列计划。"
    )


def _sync_job(goal: dict[str, Any]) -> None:
    """Keep the goal's check-in job in step with its cadence (none once it is done)."""
    from muse.cron.jobs import create_job, remove_job

    if goal.get("job_id"):
        remove_job(goal["job_id"])
        goal["job_id"] = None
    chat_id = read_companion_state().get("chat_id")
    if goal.get("checkin") and not goal.get("done") and not goal.get("paused") and chat_id:
        job = create_job(prompt=_checkin_prompt(goal), schedule=cron_expr(goal["checkin"]),
                         name=CHECKIN_JOB_PREFIX + goal["title"], deliver=f"companion:{chat_id}",
                         attach_to_session=True, failure_deliver="local")
        goal["job_id"] = job.get("id") if isinstance(job, dict) else None


def ensure_jobs() -> None:
    """Create missing goal check-in cron jobs once ``chat_id`` is populated."""
    from muse.cron.jobs import get_job

    with _LOCK:
        goals = _read()
        changed = False
        for goal in goals:
            if not goal.get("checkin") or goal.get("done") or goal.get("paused"):
                continue
            job_id = goal.get("job_id")
            if job_id and get_job(job_id) is not None:
                continue
            goal["job_id"] = None
            _sync_job(goal)
            if goal.get("job_id"):
                changed = True
        if changed:
            _write(goals)


def clear_job_ref(job_id: str) -> None:
    """Clean up goal check-in reference when its cron job is deleted from the 任务 page."""
    if not job_id:
        return
    with _LOCK:
        goals = _read()
        changed = False
        for goal in goals:
            if goal.get("job_id") == job_id:
                goal["job_id"] = None
                goal["checkin"] = None
                changed = True
        if changed:
            _write(goals)


def _next_run_ms(job_id: Optional[str]) -> Optional[int]:
    if not job_id:
        return None
    try:
        from muse.cron.jobs import get_job

        job = get_job(job_id) or {}
    except Exception:
        return None
    raw = job.get("next_run_at")
    if isinstance(raw, str):
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            return None
    return None


# ── reads and the app's edits ─────────────────────────────────────────────────────────────────────

def list_goals() -> dict[str, Any]:
    goals = _read()
    for goal in goals:
        goal["next_checkin_ms"] = _next_run_ms(goal.get("job_id"))
    # Open goals first, then by due date, newest last.
    goals.sort(key=lambda g: (bool(g.get("done")), g.get("due") or "9999", g.get("created_at", 0)))
    return {"goals": goals}


def add_goal(title: str, steps: Any = None, checkin: Optional[str] = None, due: Optional[str] = None,
             category: Optional[str] = None) -> dict[str, Any]:
    title = " ".join(str(title or "").split())[:60]
    if not title:
        raise ValueError("请填写目标名称")
    if checkin:
        cron_expr(checkin)
    if due and not _DATE.match(due):
        raise ValueError("截止日期格式应为 YYYY-MM-DD")
    if category and category not in CATEGORIES:
        category = "other"
    goal = {"id": f"g_{uuid.uuid4().hex[:8]}", "title": title, "steps": _steps(steps or []),
            "checkin": checkin or None, "due": due or None, "done": False, "paused": False,
            "category": category or None,
            "created_at": int(time.time() * 1000), "job_id": None}
    with _LOCK:
        goals = _read()
        _sync_job(goal)
        goals.append(goal)
        _write(goals)
    return goal


def update_goal(goal_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """``done`` / ``paused`` / ``checkin`` / ``due`` / ``title`` / ``category`` / ``add_step``, and one
    step's ``step`` + ``status`` (+ ``note``)."""
    with _LOCK:
        goals = _read()
        goal = _find(goals, goal_id)
        if "title" in changes:
            goal["title"] = " ".join(str(changes["title"]).split())[:60] or goal["title"]
        if "due" in changes:
            due = changes["due"] or None
            if due and not _DATE.match(due):
                raise ValueError("截止日期格式应为 YYYY-MM-DD")
            goal["due"] = due
        if "category" in changes:
            category = changes["category"] or None
            if category is not None and category not in CATEGORIES:
                raise ValueError("不支持这个目标分类")
            goal["category"] = category
        if changes.get("add_step"):
            if len(goal["steps"]) >= _MAX_STEPS:
                raise ValueError(f"一个目标最多设置 {_MAX_STEPS} 步")
            goal["steps"] += _steps([changes["add_step"]])
        if "step" in changes:
            index = changes["step"]
            if not isinstance(index, int) or not 1 <= index <= len(goal["steps"]):
                raise ValueError("找不到对应的步骤编号")
            status = changes.get("status")
            if status not in STATUSES:
                raise ValueError("步骤状态无效")
            goal["steps"][index - 1]["status"] = status
            if changes.get("note"):
                goal["steps"][index - 1]["note"] = str(changes["note"])[:120]
            _log(goal, "step", f"{_STEP_LOG[status]}第 {index} 步：{goal['steps'][index - 1]['text']}",
                 status=status, note=str(changes.get("note") or "")[:120])
        if changes.get("output"):
            # A file this goal produced (its name in 资源库).
            _log(goal, "file", str(changes["output"])[:120])
        reschedule = False
        if "checkin" in changes:
            checkin = changes["checkin"] or None
            if checkin:
                cron_expr(checkin)
            goal["checkin"], reschedule = checkin, True
        if "done" in changes:
            goal["done"], reschedule = bool(changes["done"]), True
            _log(goal, "goal", "目标完成" if goal["done"] else "目标重新打开")
        if "paused" in changes:
            # A paused goal keeps its plan but asks nothing until it is resumed.
            goal["paused"], reschedule = bool(changes["paused"]), True
        if reschedule or "title" in changes or "step" in changes or "add_step" in changes:
            # The check-in prompt names the title and next step, so it follows them.
            _sync_job(goal)
        _write(goals)
    return goal


def remove_goal(goal_id: str) -> None:
    with _LOCK:
        goals = _read()
        goal = _find(goals, goal_id)
        if goal.get("job_id"):
            from muse.cron.jobs import remove_job

            remove_job(goal["job_id"])
        _write([g for g in goals if g is not goal])


def check_in_now(goal_id: str) -> None:
    """Send the check-in question right away (the 现在问问我 button)."""
    goal = _find(_read(), goal_id)
    if not goal.get("job_id"):
        raise ValueError("这个目标没有设定跟进时间")
    from muse.cron.jobs import trigger_job

    trigger_job(goal["job_id"])


def apply_plan(goal_id: str, steps: Any) -> dict[str, Any]:
    """Replace the steps not yet done; finished steps stay, in their order."""
    new = _steps(steps)
    if not new:
        raise ValueError("the new plan has no steps")
    with _LOCK:
        goals = _read()
        goal = _find(goals, goal_id)
        goal["steps"] = [s for s in goal["steps"] if s["status"] == "done"] + new
        goal["steps"] = goal["steps"][:_MAX_STEPS]
        _sync_job(goal)
        _write(goals)
    return goal


# ── the companion's tools ─────────────────────────────────────────────────────────────────────────

def _result(fn) -> str:
    try:
        return json.dumps({"success": True, **fn()}, ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def _brief(goal: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "title", "checkin", "due", "done")
    return {**{k: goal.get(k) for k in keep},
            "steps": [f"{i}. [{s['status']}] {s['text']}" for i, s in enumerate(goal["steps"], 1)]}


def handle_goal_list(args: dict[str, Any], **_: Any) -> str:
    return _result(lambda: {"goals": [_brief(g) for g in _read()]})


def handle_goal_add(args: dict[str, Any], **_: Any) -> str:
    return _result(lambda: {"goal": _brief(add_goal(args.get("title", ""), args.get("steps"),
                                                     args.get("checkin"), args.get("due"), args.get("category")))})


def handle_goal_update(args: dict[str, Any], **_: Any) -> str:
    changes = {k: args[k] for k in ("title", "due", "checkin", "done", "step", "status", "note", "output") if k in args}
    return _result(lambda: {"goal": _brief(update_goal(args.get("goal_id", ""), changes))})


def handle_goal_propose_plan(args: dict[str, Any], **_: Any) -> str:
    # Runs only after the user picks 用新计划 on the approval card (the pre_tool_call gate holds it).
    return _result(lambda: {"goal": _brief(apply_plan(args.get("goal_id", ""), args.get("steps")))})


_GOAL_ID = {"type": "string", "description": "目标的 id（goal_list 里的 id）"}
_CHECKIN = {"type": "string", "description": "跟进时间：daily 08:00 / weekdays 07:30 / weekly mon 09:00 / monthly 1 09:00；不需要跟进就不填"}
TOOLS = (
    ("goal_list", "列出用户的长期目标、每一步的进度和跟进时间。", {}, [], handle_goal_list),
    ("goal_add", "用户说出一个要长期推进的目标（减重、考证、学一样东西…）时，记成目标，拆成几步。创建前先跟用户确认步骤和是否需要定期跟进。",
     {"title": {"type": "string", "description": "简短的目标，20 字以内"},
      "steps": {"type": "array", "items": {"type": "string"}, "description": "3~6 个具体步骤"},
      "checkin": _CHECKIN, "due": {"type": "string", "description": "截止日期 YYYY-MM-DD，可选"},
      "category": {"type": "string", "enum": list(CATEGORIES), "description": "属于生活的哪个方面"}},
     ["title", "steps"], handle_goal_add),
    ("goal_update", "更新目标：标记某一步的进度（step 是从 1 开始的序号，status 是 pending/doing/done/blocked，note 写一句结果），"
                    "或改跟进时间、截止日期，或 done=true 表示目标完成。不能用它改计划里的步骤。推进目标时每做完一步都要标记；"
                    "这一步做出了文件（存在资源库里的），把文件名填进 output。交给帮手做的部分，用 helper_ask 时带上 goal_id。",
     {"goal_id": _GOAL_ID, "step": {"type": "integer"}, "status": {"type": "string", "enum": list(STATUSES)},
      "note": {"type": "string"}, "checkin": _CHECKIN, "due": {"type": "string"}, "done": {"type": "boolean"},
      "title": {"type": "string"}, "output": {"type": "string", "description": "这一步产出的文件名（资源库里的）"}},
     ["goal_id"], handle_goal_update),
    ("goal_propose_plan", "觉得计划需要调整时，提出新的剩余步骤（已完成的步骤会保留）。不会直接生效：用户会看到确认卡片，"
                          "选「用新计划」才会改，选「保持原样」就不改。",
     {"goal_id": _GOAL_ID, "steps": {"type": "array", "items": {"type": "string"}, "description": "新的剩余步骤"},
      "reason": {"type": "string", "description": "为什么要改，一句话"}},
     ["goal_id", "steps", "reason"], handle_goal_propose_plan),
)


def register_tools(ctx: Any) -> None:
    for name, description, properties, required, handler in TOOLS:
        schema = {"name": name, "description": description,
                  "parameters": {"type": "object", "properties": properties, "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description,
                          emoji="🎯")


def goal_for_approval(goal_id: Any) -> Optional[dict[str, Any]]:
    """The goal a held plan proposal is about, for its card."""
    return next((g for g in _read() if g.get("id") == goal_id), None)
