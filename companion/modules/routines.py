"""Things the companion does on its own: the daily brief and check-ins (ported from nanobot).

Both are ordinary Hermes scheduled jobs delivered into the companion chat, so
they also show in the 任务 page with every other scheduled task. The brief runs
once a day at the user's chosen time; check-ins run a few times during the day
(never at night) and stay silent unless something is worth saying — Hermes
drops a job's reply when it is ``[SILENT]``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .companion_state import read_companion_state

BRIEF_JOB_NAME = "每日早报"
BRIEF_PROMPT = (
    "给用户发今天的早报，用几条短消息：今天的日程（连了日历就去查）、"
    "值得处理的新邮件（连了邮箱就去看，只挑重要的）、今天该记得或跟进的事"
    "（从记忆和最近的聊天里找：生日、截止时间、约好的事）。"
    "某一项没有内容就跳过，不要编造。最后可以问一句今天想先做什么。"
)
CHECKIN_JOB_NAME = "主动关心"
# Every three hours from 9:00 to 21:00 in the user's timezone: quiet at night.
CHECKIN_SCHEDULE = "0 9,12,15,18,21 * * *"
SILENT = "[SILENT]"
CHECKIN_PROMPT = (
    "[这条消息不会直接给用户看。你在做定时的「主动关心」检查。]\n\n"
    "先回想你对用户的记忆，需要的话用会话搜索看看最近和用户聊了什么。"
    "然后想一想现在有没有值得主动找用户说的事，例如：\n"
    "- 接下来 24 小时内的生日、纪念日、截止时间、约好的事或出行\n"
    "- 用户提过想做、但还没有下文的事，现在适合跟进一下\n"
    "- 你答应过用户、还没完成的事\n\n"
    "如果有，直接写出要发给用户的那条消息：用你平时的口吻，简短，只说最重要的一件事，"
    "可以顺手问要不要你帮忙。\n"
    f"如果没有特别值得说的，只回复 {SILENT}。\n"
    "不要发送邮件、修改日程或做任何对外操作；需要时可以只读地查看日历或邮件。"
)
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _deliver(chat_id: str) -> str:
    return f"companion:{chat_id}"


def _ours(name: str, chat_id: str) -> list[dict[str, Any]]:
    from muse.cron.jobs import list_jobs

    return [job for job in list_jobs(include_disabled=True)
            if job.get("name") == name and job.get("deliver") == _deliver(chat_id)]


def _brief_time(job: Optional[dict[str, Any]]) -> Optional[str]:
    if job is None or job.get("enabled") is False:
        return None
    schedule = job.get("schedule")
    expr = schedule.get("expr") if isinstance(schedule, dict) else None
    parts = str(expr or "").split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return f"{int(parts[1]):02d}:{int(parts[0]):02d}"


def read_routines() -> dict[str, Any]:
    """``{"brief_time": "08:00" | None, "checkins": bool}``."""
    chat_id = read_companion_state().get("chat_id")
    if not chat_id:
        return {"brief_time": None, "checkins": False}
    briefs = _ours(BRIEF_JOB_NAME, chat_id)
    return {
        "brief_time": _brief_time(briefs[0] if briefs else None),
        "checkins": bool(_ours(CHECKIN_JOB_NAME, chat_id)),
    }


def update_routines(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply ``brief_time`` (``"HH:MM"`` or ``None``) and/or ``checkins`` (bool)."""
    unknown = set(changes) - {"brief_time", "checkins"}
    if unknown:
        raise ValueError(f"不支持的日常字段：{', '.join(sorted(unknown))}")
    chat_id = read_companion_state().get("chat_id")
    if not chat_id:
        raise ValueError("主对话还没有准备好")
    if "brief_time" in changes:
        at = changes["brief_time"]
        if at is not None and (not isinstance(at, str) or not _TIME.match(at)):
            raise ValueError("早报时间格式不正确（应为 HH:MM）")
        _replace(BRIEF_JOB_NAME, chat_id, None if at is None else _daily(at), BRIEF_PROMPT)
    if "checkins" in changes:
        if not isinstance(changes["checkins"], bool):
            raise ValueError("主动关心开关格式不正确")
        if changes["checkins"] != bool(_ours(CHECKIN_JOB_NAME, chat_id)):
            _replace(CHECKIN_JOB_NAME, chat_id, CHECKIN_SCHEDULE if changes["checkins"] else None, CHECKIN_PROMPT)
    return read_routines()


def _daily(at: str) -> str:
    hour, minute = (int(part) for part in at.split(":"))
    return f"{minute} {hour} * * *"


def _replace(name: str, chat_id: str, schedule: Optional[str], prompt: str) -> None:
    from muse.cron.jobs import create_job, remove_job

    for job in _ours(name, chat_id):
        remove_job(job["id"])
    if schedule is not None:
        # attach_to_session: the reply also lands in the chat's history, so the companion
        # remembers what it said when the user answers.
        create_job(prompt=prompt, schedule=schedule, name=name, deliver=_deliver(chat_id), attach_to_session=True,
                   failure_deliver="local")


def source_for_job(job_id: Optional[str]) -> Optional[dict[str, str]]:
    """How the app labels a message a scheduled job sent (动态 and push use it)."""
    if not job_id:
        return None
    try:
        from muse.cron.jobs import get_job

        job = get_job(str(job_id)) or {}
    except Exception:
        job = {}
    name = str(job.get("name") or "定时任务")
    return {"kind": "checkin" if name == CHECKIN_JOB_NAME else "cron", "label": name}
