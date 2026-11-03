"""帮手: helpers the companion calls on (as OpenBot's coworkers, or Grok Bot's Bots).

A helper is a durable profile — a name, an avatar (emoji), what it is for (``title``, one line), how
it should work (``instructions``) and the model it runs on (``model``: a preset from the 模型 page,
or empty for the main assistant's) — kept in ``<companion dir>/helpers.json`` and edited on the 帮手
page. Six of them (``TEMPLATES``) are there from the start; they stay as the user leaves them
(edited, deleted) and an edited one can go back to its default. The companion in a chat (the main
chat or a side chat) is in charge; helpers work for it:

- it hands one a piece of work with ``helper_ask``, on its own judgement (each turn lists the
  helpers there are) or because the user named one (``@记账 …`` in the message);
- the helper does the work in a working chat of its own (hidden from the 旁聊 list), with its role
  and its work notes carried on every turn (OpenBot sends the role with every run the same way),
  and its answer comes back as the tool's result;
- it keeps notes for its job itself (``helper_note``), apart from the companion's memory of the user.

Each helper is also a chat of its own (as each Grok Bot is): the user can open it from the 帮手 page
and talk to it directly; work the companion handed it shows there as coming from that chat. A new
helper starts blank and sets itself up by talking — it asks what it should take care of, then writes
its own profile (``helper_profile``); later the user just tells it how to change.

A helper may hand part of its work to another helper too (Grok Bot's chief of staff; the 管家
template does nothing else): the other one's answer shows in the asking helper's chat as its own
message. Chains are kept short and never loop back (``_asking``).

Group chats (群聊, ``groups``) put 2–6 helpers in one conversation: the companion coordinates (it
picks an owner for what the user says, or the one the user @ named), each helper's answer is posted
in the group as its own message, and helpers in a group can pass work to each other.

Each helper also learns the user's habits in its line of work (``habits``): it records one itself
when the user states a lasting preference (``helper_habit``), and ``modules.helper_learn`` reads the
chats every hour for preferences shown again and again. They go into each of its turns as defaults,
and the user sees, adds and drops them on its profile.

The browser belongs to the companion: helpers don't drive it themselves. A helper that needs
something done on a web page asks with ``browser_request``; the companion does it in a working chat
of its own (a browser desk, hidden, fresh for each request and removed after), one request at a
time, and hands back what it saw and did.

The runtime half (posting the task, waiting for the answer) lives on the adapter
(``CompanionAdapter.ask_helper``, ``CompanionAdapter.browser_for``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

from .paths import get_webui_dir

logger = logging.getLogger(__name__)

TOOLSET = "companion"
MAX_HELPERS = 12
_MAX_NOTES = 30
_MAX_NAME = 12
_MAX_TITLE = 16
# How many helpers deep work may be passed on (a helper asking one, which asks another).
_MAX_CHAIN = 3
# A helper's chat -> the chat waiting for its answer, while a helper asked it.
_asking: dict[str, str] = {}
_MAX_INSTRUCTIONS = 2000
_ASK_TIMEOUT_S = 15 * 60
_BACKGROUND_TIMEOUT_S = 60 * 60  # work handed off to run in the background (helper_ask wait=false)
_LOCK = threading.Lock()
_AVATAR = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
_MAX_AVATAR = 200_000
_MENTION = re.compile(r"@([^\s@，。,.!！?？:：]{1,12})")

# Starting points on the 帮手 page (OpenBot's catalog, Grok Bot's suggested roles).
TEMPLATES = (
    {"name": "研究员", "emoji": "🔎", "title": "把一个问题查清楚，给出处",
     "starters": ["新能源车买纯电还是插混好？", "最近有哪些值得关注的 AI 新闻？", "褪黑素长期吃安全吗？"],
     "instructions": "- 先想清楚要回答什么，再去查；至少找 3 个可靠来源，互相核对\n"
                     "- 结论放最前面，下面列依据，每条关键信息后面附上来源链接\n"
                     "- 查不到或说法不一的地方直接说明，不要编\n"
                     "- 篇幅按问题来：简单问题一两句，复杂的分点"},
    {"name": "记账", "emoji": "💰", "title": "记下每笔收支，按周按月算账",
     "starters": ["午饭 32 元", "这个月花了多少？", "上周钱都花在哪了？"],
     "instructions": "- 每笔记下：日期、金额、类别（餐饮/交通/购物/居住/娱乐/其他）、备注，存进资源库里的「账本」文件（没有就新建）\n"
                     "- 金额不清楚时先问，不要猜\n"
                     "- 要统计时给出总额、各类别占比，和上个周期比多了还是少了\n"
                     "- 只记账和分析，不替用户付款、转账"},
    {"name": "写手", "emoji": "✍️", "title": "写邮件、文案、总结，也帮忙改稿",
     "starters": ["帮我写一封请假邮件", "把这段话改得更简洁", "写一条朋友圈文案"],
     "instructions": "- 动笔前弄清：写给谁、要达到什么目的、什么语气、大概多长；缺的先问\n"
                     "- 语言自然、具体，不说空话套话\n"
                     "- 改稿时保留原意，最后用一两句说清改了哪里\n"
                     "- 写好的东西只交给用户，不替用户发出去"},
    {"name": "行程", "emoji": "🧭", "title": "规划出行和一天的安排",
     "starters": ["周末去杭州玩两天怎么安排？", "明天上午去机场，几点出门合适？", "规划一下今天下午的事"],
     "instructions": "- 按时间顺序排：几点在哪、怎么过去、路上多久\n"
                     "- 查好天气、营业时间、交通班次，需要提前预订或排队的地方单独标出\n"
                     "- 给一个主方案，必要时附一个下雨或延误时的备选\n"
                     "- 不替用户付款、下单、订票，只给方案和链接"},
    {"name": "比价", "emoji": "🛒", "title": "买东西前比较几个选项",
     "starters": ["2000 元左右的降噪耳机推荐", "iPad Air 和小米平板怎么选？", "扫地机器人哪款性价比高？"],
     "instructions": "- 先确认用户在意什么（价格、性能、外观、售后…）和预算\n"
                     "- 列出 3~5 个候选：价格、关键参数、真实评价里的优缺点，附购买链接\n"
                     "- 最后给出推荐和理由，说明各自适合什么人\n"
                     "- 只做比较，不下单"},
    {"name": "翻译", "emoji": "🌐", "title": "翻译外文，润色成自然的表达",
     "starters": ["把这段英文翻成中文", "“期待与您合作”用英文怎么说得自然？", "帮我润色这封英文邮件"],
     "instructions": "- 忠实原意，读起来像母语的人写的\n"
                     "- 人名、品牌、术语保留原文，第一次出现时在括号里给中文\n"
                     "- 用户没说目标语言时：中文翻成英文，外文翻成中文\n"
                     "- 只给译文；有拿不准的地方在最后单独说明"},
    {"name": "管家", "emoji": "🧑‍💼", "title": "统筹几个帮手，一起把事办完",
     "starters": ["这周帮我把出差安排好", "整理一下我这个月的开销和行程", "帮我准备下周的家庭聚餐"],
     "instructions": "- 一件事先拆成几块，每块交给最合适的帮手（helper_ask），自己不重复做它们的活\n"
                     "- 交代清楚：要做什么、背景、要交回什么样的结果\n"
                     "- 收回来后核对有没有矛盾、遗漏，汇总成一个结论和下一步\n"
                     "- 需要用户拍板的事列出来问，不替用户做决定"},
)

_adapter: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def bind(adapter: Any, loop: asyncio.AbstractEventLoop) -> None:
    """The running adapter: the tools post work through it (they run on worker threads)."""
    global _adapter, _loop
    _adapter, _loop = adapter, loop


# ── data ──────────────────────────────────────────────────────────────────────────────────────────

def _path():
    return get_webui_dir() / "helpers.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, list):  # the first format: just the helpers, before there were defaults
        data = {"helpers": data}
    data = data if isinstance(data, dict) else {}
    data["helpers"] = [h for h in data.get("helpers") or [] if isinstance(h, dict) and h.get("id")]
    data.setdefault("defaults", 0)
    return data


def _read() -> list[dict[str, Any]]:
    return _load()["helpers"]


def _write(rows: list[dict[str, Any]], defaults: Optional[int] = None) -> None:
    data = _load()
    data["helpers"] = rows
    data.pop("seeded", None)
    if defaults is not None:
        data["defaults"] = defaults
    _save(data)


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# What the first templates said (before the defaults): a helper made from one, never edited, is
# brought up to the default it now is.
_FIRST_TITLES = {"研究员": "查资料、做调研", "记账": "记录和统计花销", "写手": "写稿、改稿", "行程": "规划出行和日程",
                 "比价": "购物前比较", "翻译": "翻译和润色外文"}
DEFAULTS_VERSION = 1


def ensure_defaults(store: Any) -> None:
    """Once: the six default helpers. One the user already made from a template becomes that default
    (and, left as it was, gets the default's wording); after that what the user does with them stands."""
    with _LOCK:
        if _load()["defaults"] >= DEFAULTS_VERSION:
            return
        _write(_read(), defaults=DEFAULTS_VERSION)
    for template in TEMPLATES:
        existing = find(template["name"])
        try:
            if existing is None:
                create(store, {**template, "preset": template["name"]})
            elif not existing.get("preset"):
                untouched = existing.get("title") == _FIRST_TITLES.get(template["name"])
                with _LOCK:
                    rows = _read()
                    row = next(h for h in rows if h["id"] == existing["id"])
                    row["preset"] = template["name"]
                    if untouched:
                        row.update({k: template[k] for k in ("emoji", "title", "instructions")})
                    _write(rows)
        except (ValueError, StopIteration) as exc:
            logger.info("companion: default helper %s not added (%s)", template["name"], exc)


def template_for(helper: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next((t for t in TEMPLATES if t["name"] == helper.get("preset")), None)


def is_default(helper: dict[str, Any]) -> bool:
    """A preset helper still as it came (name, emoji, job, instructions, model)."""
    template = template_for(helper)
    return bool(template) and not helper.get("model") and all(helper.get(k) == template[k] for k in ("name", "emoji", "title", "instructions"))


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def all_helpers() -> list[dict[str, Any]]:
    return _read()


def find(name_or_id: str) -> Optional[dict[str, Any]]:
    wanted = _clean(name_or_id, 40).lstrip("@「」")
    rows = _read()
    return next((h for h in rows if wanted in (h["id"], h["name"])), None) \
        or next((h for h in rows if wanted and (wanted in h["name"] or wanted == h.get("title"))), None)


def helper_for_chat(chat_id: Optional[str]) -> Optional[dict[str, Any]]:
    return next((h for h in _read() if chat_id and h.get("chat_id") == chat_id), None)


def new_desk(store: Any) -> str:
    """A fresh working chat for one piece of browser work a helper asked for (hidden; removed when
    done), so each request starts with a clean context instead of every earlier one."""
    with _LOCK:
        data = _load()
        old = data.pop("desk_chat", None)  # the one shared desk chat of before
        if old:
            store.delete(old)
            _save(data)
    chat_id = store.create()
    store.update_meta(chat_id, desk=True)
    store.set_title(chat_id, "浏览器代办")
    return chat_id


def is_desk(chat_id: Optional[str]) -> bool:
    return bool(chat_id) and _adapter is not None and bool(_adapter.store.meta(chat_id).get("desk"))


def create_blank(store: Any, idea: str = "") -> dict[str, Any]:
    """新建帮手: a helper with no job yet, which sets itself up in its chat. ``idea``: a name or what it
    is for, as the user typed it ("邮件分拣", "盯着我的快递"); it proposes the rest from that."""
    taken = {h["name"] for h in _read()}
    idea = " ".join(str(idea or "").split())[:60]
    short = idea if idea and len(idea) <= _MAX_NAME and idea not in taken and not re.search(r"[，。,.!?？！]", idea) else ""
    name = short or next(n for n in ("新帮手", *(f"新帮手{i}" for i in range(2, 50))) if n not in taken)
    return create(store, {"name": name, "emoji": "🙂", "title": "", "instructions": "", "setup": True})


def _fields(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in values:
        out["name"] = _clean(values["name"], _MAX_NAME)
        if not out["name"]:
            raise ValueError("帮手要有名字")
    if "title" in values:
        out["title"] = _clean(values["title"], _MAX_TITLE)
    if "instructions" in values:
        out["instructions"] = str(values["instructions"] or "").strip()[:_MAX_INSTRUCTIONS]
    if "emoji" in values:
        out["emoji"] = str(values["emoji"] or "").strip()[:4] or "🤖"
    if "avatar" in values:
        # A photo for its face (small, made in the app or by generate_avatar); empty: back to the emoji.
        avatar = str(values["avatar"] or "")
        if avatar and (not _AVATAR.match(avatar) or len(avatar) > _MAX_AVATAR):
            raise ValueError("头像图片不对，换一张试试")
        out["avatar"] = avatar
    if "setup" in values:
        out["setup"] = bool(values["setup"])
    if "model" in values:
        model = str(values["model"] or "").strip()
        if model:
            from .models import read_models

            if model not in read_models()["presets"]:
                raise ValueError("模型页里没有这个模型")
        out["model"] = model
    return out


def create(store: Any, values: dict[str, Any]) -> dict[str, Any]:
    fields = _fields({"emoji": "🤖", "title": "", "instructions": "", "model": "", **values})
    preset = str(values.get("preset") or "")
    if not preset:
        # Added from a template in 新建帮手 (the app sends its fields): it is that default.
        template = next((t for t in TEMPLATES if t["name"] == fields["name"]), None)
        if template and all(fields.get(k) == template[k] for k in ("title", "instructions")):
            preset = template["name"]
    with _LOCK:
        rows = _read()
        if len(rows) >= MAX_HELPERS:
            raise ValueError(f"最多 {MAX_HELPERS} 个帮手")
        if any(h["name"] == fields["name"] for h in rows):
            raise ValueError("已经有叫这个名字的帮手了")
        helper_id = uuid.uuid4().hex[:10]
        chat_id = store.create()
        store.update_meta(chat_id, helper=helper_id)
        store.set_title(chat_id, fields["name"])
        row = {"id": helper_id, **fields, "chat_id": chat_id, "notes": [], "created_at": int(time.time() * 1000),
               **({"preset": preset} if preset else {})}
        _write([*rows, row])
    return row


def update(store: Any, helper_id: str, values: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(values)
    with _LOCK:
        rows = _read()
        row = next((h for h in rows if h["id"] == helper_id), None)
        if row is None:
            raise ValueError("没有这个帮手")
        if fields.get("name") and any(h["name"] == fields["name"] and h["id"] != helper_id for h in rows):
            raise ValueError("已经有叫这个名字的帮手了")
        if "notes" in values and isinstance(values["notes"], list):
            row["notes"] = [n for n in (_clean(n, 300) for n in values["notes"]) if n][-_MAX_NOTES:]
        row.update(fields)
        _write(rows)
    if fields.get("name"):
        store.set_title(row["chat_id"], fields["name"])
    return row


def generate_avatar(store: Any, helper_id: str, idea: str = "") -> dict[str, Any]:
    """A face for a helper drawn by the image model set in 模型, in the style of the app's own
    icons (blocking: a minute or so)."""
    import base64
    import io

    from .drawing import cut_out, generate

    row = next((h for h in _read() if h["id"] == helper_id), None)
    if row is None:
        raise ValueError("没有这个帮手")
    idea = _clean(idea, 80) or f"{row['name']}，{row.get('title') or '一个帮手'}"
    prompt = (f"A single cute kawaii sticker-style app icon for a helper: {idea}. Flat illustration with soft cel "
              "shading, warm cozy palette, chunky rounded shapes, a thin warm dark-brown outline, a friendly face if "
              "it fits. Centered, filling about 80% of the frame, on a plain pure white background. No text, no border.")
    raw, why = generate(prompt)
    if raw is None:
        raise ValueError(f"画头像没成功：{why[:120]}")
    out = io.BytesIO()
    cut_out(raw, 192).save(out, "PNG", optimize=True)
    return update(store, helper_id, {"avatar": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()})


def reset(store: Any, helper_id: str) -> dict[str, Any]:
    """A default helper back as it came (its notes and record stay)."""
    row = next((h for h in _read() if h["id"] == helper_id), None)
    template = template_for(row) if row else None
    if template is None:
        raise ValueError("只有默认帮手能恢复")
    return update(store, helper_id, {**template, "model": ""})


def model_override(helper: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Hermes's per-session model override for a helper's own model (None: the main assistant's)."""
    name = helper.get("model") or ""
    if not name:
        return None
    from hermes_cli.runtime_provider import resolve_runtime_provider

    from .models import read_models, runtime_provider

    preset = read_models()["presets"].get(name)
    if not preset:
        return None
    provider = runtime_provider(name, preset)
    runtime = resolve_runtime_provider(requested=provider, target_model=preset["model"])
    return {"model": preset["model"], "provider": provider, "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"), "api_mode": runtime.get("api_mode"),
            "request_overrides": {}, "capabilities": {}}


def delete(store: Any, helper_id: str) -> None:
    with _LOCK:
        rows = _read()
        row = next((h for h in rows if h["id"] == helper_id), None)
        _write([h for h in rows if h["id"] != helper_id])
        groups = _groups()
        for group in groups:
            group["members"] = [m for m in group.get("members") or [] if m != helper_id]
        # A group left with nobody in it goes too (one member stays: the user can add more).
        emptied = [g for g in groups if not g["members"]]
        _write_groups([g for g in groups if g["members"]])
    for group in emptied:
        store.delete(group["chat_id"])
    if row and row.get("chat_id"):
        from .triggers import remove_owned

        remove_owned(helper_id)
        _drop_jobs(row["chat_id"])
        store.delete(row["chat_id"])


def _drop_jobs(chat_id: str) -> None:
    """The scheduled jobs a helper set up from its chat (they report there) go with it."""
    try:
        from cron.jobs import list_jobs, remove_job

        from .automations import job_chat

        for job in list_jobs(include_disabled=True):
            if isinstance(job, dict) and job_chat(job) == chat_id:
                remove_job(job["id"])
    except Exception:
        logger.exception("companion: could not remove the jobs of helper chat %s", chat_id)


def add_note(helper_id: str, note: str) -> None:
    with _LOCK:
        rows = _read()
        row = next((h for h in rows if h["id"] == helper_id), None)
        if row is not None:
            notes = [n for n in row.get("notes") or [] if n != note]
            row["notes"] = [*notes, note][-_MAX_NOTES:]
            _write(rows)


MAX_HABITS = 20


def set_learned_until(ms: int) -> None:
    """Where the hourly habit pass (modules.helper_learn) read up to."""
    with _LOCK:
        data = _load()
        data["habits_learned_until_ms"] = ms
        _save(data)


def habits_of(helper: dict[str, Any]) -> list[dict[str, Any]]:
    return [h for h in helper.get("habits") or [] if isinstance(h, dict) and h.get("text")]


def add_habit(helper_id: str, text: str, by: str, replaces: Optional[str] = None) -> Optional[dict[str, Any]]:
    """A habit of the user's for this helper's work: ``by`` is "you" (typed on its profile), "chat"
    (the helper heard it said) or "learned" (the hourly pass). ``replaces``: one it supersedes."""
    text = _clean(text, 120)
    if not text:
        return None
    with _LOCK:
        rows = _read()
        row = next((h for h in rows if h["id"] == helper_id), None)
        if row is None:
            return None
        habits = [h for h in habits_of(row) if h.get("id") != replaces and h["text"] != text]
        habit = {"id": uuid.uuid4().hex[:8], "text": text, "by": by, "at": int(time.time() * 1000)}
        row["habits"] = [*habits, habit][-MAX_HABITS:]
        _write(rows)
    return habit


def notice(helper: dict[str, Any], text: str) -> None:
    """A grey line in the helper's chat, so the user sees what it just learned (from any thread)."""
    if _adapter is None or _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_adapter.post_notice(helper["chat_id"], text), _loop).result(15)
    except Exception:
        logger.exception("companion: could not show what %s learned", helper.get("name"))


def remove_habit(helper_id: str, habit_id: str) -> None:
    with _LOCK:
        rows = _read()
        row = next((h for h in rows if h["id"] == helper_id), None)
        if row is not None:
            row["habits"] = [h for h in habits_of(row) if h.get("id") != habit_id]
            _write(rows)


# ── 群聊: group chats of helpers ──────────────────────────────────────────────────────────────────

MIN_GROUP, MAX_GROUP = 2, 6


def _groups() -> list[dict[str, Any]]:
    groups = _load().get("groups") or []
    return [g for g in groups if isinstance(g, dict) and g.get("id")]


def _write_groups(groups: list[dict[str, Any]]) -> None:
    data = _load()
    data["groups"] = groups
    _save(data)


def group_for_chat(chat_id: Optional[str]) -> Optional[dict[str, Any]]:
    return next((g for g in _groups() if chat_id and g.get("chat_id") == chat_id), None)


def members_of(group: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {h["id"]: h for h in _read()}
    return [by_id[m] for m in group.get("members") or [] if m in by_id]


def _members(values: list[Any]) -> list[str]:
    known = {h["id"] for h in _read()}
    members = list(dict.fromkeys(str(m) for m in values if str(m) in known))
    if not MIN_GROUP <= len(members) <= MAX_GROUP:
        raise ValueError(f"群聊要有 {MIN_GROUP}~{MAX_GROUP} 个帮手")
    return members


def create_group(store: Any, name: str, members: list[Any]) -> dict[str, Any]:
    members = _members(members)
    name = _clean(name, 20) or "、".join(h["name"] for h in _read() if h["id"] in members)[:20]
    with _LOCK:
        groups = _groups()
        chat_id = store.create()
        store.update_meta(chat_id, group=True)
        store.set_title(chat_id, name)
        group = {"id": uuid.uuid4().hex[:10], "name": name, "members": members, "chat_id": chat_id,
                 "created_at": int(time.time() * 1000)}
        _write_groups([*groups, group])
    return group


def update_group(store: Any, group_id: str, name: Optional[str] = None, members: Optional[list[Any]] = None) -> None:
    with _LOCK:
        groups = _groups()
        group = next((g for g in groups if g["id"] == group_id), None)
        if group is None:
            raise ValueError("没有这个群聊")
        if members is not None:
            group["members"] = _members(members)
        if name is not None and _clean(name, 20):
            group["name"] = _clean(name, 20)
            store.set_title(group["chat_id"], group["name"])
        _write_groups(groups)


def delete_group(store: Any, group_id: str) -> None:
    with _LOCK:
        groups = _groups()
        group = next((g for g in groups if g["id"] == group_id), None)
        _write_groups([g for g in groups if g["id"] != group_id])
    if group:
        store.delete(group["chat_id"])


# ── the 帮手 page ─────────────────────────────────────────────────────────────────────────────────

def _work(store: Any, chat_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """What a helper was asked and what it answered, newest first."""
    tasks: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in store.events(chat_id):
        turn = str(event.get("turn_id") or "")
        if not turn:
            continue
        if event.get("event") == "user_message":
            source = ((event.get("provenance") or {}).get("session_message") or {}).get("session") or {}
            tasks[turn] = {"turn_id": turn, "at": int(event.get("created_at_ms") or 0), "from": source.get("name") or "你",
                           "task": str(event.get("text") or ""), "answer": "", "done": False}
            order.append(turn)
        elif turn in tasks and event.get("event") == "message" and event.get("turn_phase") == "answer":
            tasks[turn]["answer"] = (tasks[turn]["answer"] + "\n\n" + str(event.get("text") or "")).strip()
        elif turn in tasks and event.get("event") == "turn_end":
            tasks[turn]["done"] = True
    return [tasks[t] for t in reversed(order)][:limit]


def page(store: Any, busy: set[str]) -> dict[str, Any]:
    rows = []
    for helper in _read():
        work = _work(store, helper["chat_id"])
        rows.append({**{k: helper.get(k) for k in ("id", "name", "emoji", "title", "instructions", "chat_id", "notes")},
                     "avatar": helper.get("avatar") or "", "habits": habits_of(helper), "created_at": helper.get("created_at") or 0,
                     "last": _last(store, helper["chat_id"], helper["name"]),
                     "model": helper.get("model") or "", "preset": helper.get("preset") or "",
                     "setup": bool(helper.get("setup")),
                     "is_default": is_default(helper), "working": helper["chat_id"] in busy, "work": work})
    groups = [{**g, "last": _last(store, g["chat_id"], None)} for g in _groups()]
    return {"helpers": rows, "templates": list(TEMPLATES), "max": MAX_HELPERS, "groups": groups,
            "max_group": MAX_GROUP}


_CARD = re.compile(r"```(?:card|json|ui)[\s\S]*?(?:```|$)")
_MARKS = re.compile(r"\*\*|__|`|~~|^#{1,6}\s+|^>\s?", re.M)


def _last(store: Any, chat_id: str, helper_name: Optional[str]) -> Optional[dict[str, Any]]:
    """The newest message in a conversation, for the list: what was said, when, and who said it
    ("我", or a speaker's name in a group; nobody's name in a chat with one helper)."""
    for event in reversed(store.events(chat_id)):
        kind = event.get("event")
        if kind == "user_message":
            source = ((event.get("provenance") or {}).get("session_message") or {}).get("session") or {}
            who = source.get("name") or "我"
        elif (event.get("source") or {}).get("kind") == "notice":
            continue
        elif kind == "message" and (event.get("turn_phase") == "answer" or (event.get("source") or {}).get("kind")):
            label = str((event.get("source") or {}).get("label") or "")
            who = label.split(" ", 1)[-1] if (event.get("source") or {}).get("kind") == "helper" else ""
            if not who and helper_name is None:
                who = "主助理"
        else:
            continue
        # A preview line is plain text: no **bold**, `code` or # heading marks.
        plain = _MARKS.sub("", _CARD.sub("[卡片]", str(event.get("text") or "")))
        text = " ".join(plain.split())
        if not text:
            continue
        return {"text": text[:80], "at": int(event.get("created_at_ms") or 0), "who": who}
    return None


# ── what each turn carries ────────────────────────────────────────────────────────────────────────

def context_for(chat_id: Optional[str], user_text: str = "") -> Optional[str]:
    """In a helper's working chat: its job and notes. In the companion's chats: the helpers it can
    call, and the ones the user named (``@名字``), which it must call."""
    if not chat_id:
        return None
    if is_desk(chat_id):
        return ("【浏览器代办】下面是一个帮手请你（主助理）在浏览器里做的事。只做浏览器上的这件事，"
                "做完把看到的内容、做到了哪一步、有什么没做成完整地写出来（帮手看不到浏览器，只能看你的回答）。"
                "说网页上有什么、没有什么之前，先用 browser_snapshot 读页面的文字结构（标题、按钮、表格都在里面），"
                "拿不准时再用 browser_evaluate 取 document.body.innerText 核对，照页面原文转述，别凭印象下结论。"
                "需要用户登录、验证码这类时，照常请用户接手。不要再交给帮手。")
    helper = helper_for_chat(chat_id)
    if helper and (helper.get("setup") or not helper.get("instructions")):
        return ("【你现在的身份】你是用户刚建的一个新帮手，还没有设定。\n"
                "- 用户已经说了你叫什么或负责什么（比如名字是「邮件分拣」）：不要一条条问，直接推断你的职责，给出一份完整的建议："
                "名字（用户给的名字就用它，不要改；没给才自己起，2~6 个字）、一句话职责（16 个字以内）、做法（3~5 条：怎么做、交出什么、哪些事不能做）；适合定时做的（例行）或有事发生时自动做的"
                "（自动触发：来新邮件、日程快开始、网页变了）也一并建议。末尾放一个 choices 卡片，选项是「就这样」和「改一改」。\n"
                "- 什么都不知道：只问一个问题——想让你负责什么。\n"
                "用户说「就这样」（或说了要改的地方并改好后），用 helper_profile 设定自己（再配一个合适的 emoji），"
                "建议的例行和自动触发照着设好（用户已经同意，不用再确认），然后用一两句话告诉用户设好了，"
                "以后在主要聊天里写「@你的名字」就能叫你。")
    if helper:
        lines = [f"【你现在的身份】你是用户的帮手「{helper['name']}」" + (f"，负责{helper['title']}" if helper.get("title") else "") + "。",
                 "标着「XX交给你的事」的消息是主助理转来的，做完直接给出结果（它会转告用户），不用寒暄；"
                 "其余消息是用户直接在跟你说话，就正常回答用户。"]
        if helper.get("instructions"):
            lines.append("你的职责说明：" + helper["instructions"])
        lines.append("你不能直接用浏览器：要在网页上看或做什么，用 browser_request 交给主助理，它做完会把结果告诉你。")
        others = [h for h in _read() if h["id"] != helper["id"] and not h.get("setup")]
        if others:
            lines.append("其他帮手：" + "；".join(f"「{h['name']}」" + (f"（{h['title']}）" if h.get("title") else "") for h in others)
                         + "。一部分活明显是它们的专长时，可以用 helper_ask 交给它（说清要做什么、交回什么），"
                         "它的回答会以它自己的名义显示，你再接着做、汇总。自己能做好的就自己做。")
        lines.append("用户让你以后换一种做法时，用 helper_profile 改你的职责说明（保留原来仍然有效的条目）。"
                     "这份工作以后用得上的做法、结论，用 helper_note 记下。")
        if helper.get("notes"):
            lines.append("你之前记下的工作笔记：\n" + "\n".join(f"- {n}" for n in helper["notes"]))
        habits = habits_of(helper)
        if habits:
            lines.append("你摸清的用户习惯（默认照着来，不用再问；用户这次另有说法就按这次的）：\n"
                         + "\n".join(f"- {h['text']}" for h in habits))
        lines.append("用户表达了一个以后都适用的偏好（比如“别把时间排太紧”“每次都要附上价格”），用 helper_habit 记下，"
                     "跟已有的冲突就替换那一条；一次性的要求不用记。")
        return "\n".join(lines)
    group = group_for_chat(chat_id)
    if group:
        members = members_of(group)
        listing = "；".join(f"「{h['name']}」" + (f"（{h['title']}）" if h.get("title") else "") for h in members)
        named = [h for h in members if any(m in (h["name"], "everyone", "所有人") for m in _MENTION.findall(user_text or ""))]
        lines = [f"【这是群聊「{group['name']}」】成员是这几个帮手：{listing}。你是主持人：用户说的事交给最合适的那一个帮手"
                 "（一件事一个负责人，需要时可以分给几个人做不同部分），用 helper_ask 交给它。帮手的回答会以它自己的名义直接显示在群里，"
                 "你不要复述，最多补一句总结或下一步。闲聊、简单的问题你自己简短回答。"]
        if named:
            lines.append("用户这次点名要 " + "、".join(f"「{h['name']}」" for h in named) + " 来做：交给它们。")
        return "\n".join(lines)
    rows = _read()
    if not rows:
        return None
    named = [h for h in rows if any(m == h["name"] for m in _MENTION.findall(user_text or ""))]
    listing = "；".join(f"「{h['name']}」" + (f"（{h['title']}）" if h.get("title") else "") for h in rows)
    lines = [f"【你的帮手】{listing}。你是主助理，它们听你调遣：一件事明显属于某个帮手的职责、或者交给它做得更好时，"
             "用 helper_ask 交给它（把要做的事和背景说完整）；几个帮手可以分头做不同部分，最后由你汇总告诉用户。简单的事你自己做。"]
    learned = [f"「{h['name']}」：" + "；".join(x["text"] for x in habits_of(h)[-4:]) for h in rows if habits_of(h)]
    if learned:
        # What the helpers learned of the user is the companion's to use too, when it does such work itself.
        lines.append("帮手们摸清的用户习惯（你自己做这类事时也照着来）：\n" + "\n".join(f"- {line}" for line in learned))
    if named:
        lines.append("用户这次点名要 " + "、".join(f"「{h['name']}」" for h in named) + " 来做：用 helper_ask 交给它们。")
    return "\n".join(lines)


def _text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(str(p.get("text") or "") for p in message if isinstance(p, dict))
    return str(message or "")


def on_pre_llm_call(platform: str = "", session_id: str = "", user_message: Any = None, **kwargs: Any) -> Optional[dict[str, str]]:
    """Hermes hook: a helper's job and notes on each of its turns; elsewhere, the helpers there are."""
    if platform != "companion":
        return None
    from ..activity import _chat_for_session

    chat_id = _chat_for_session(str(session_id)) if session_id else None
    context = context_for(chat_id, _text(user_message))
    return {"context": context} if context else None


# ── the companion's tools ─────────────────────────────────────────────────────────────────────────

def _result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _current_chat(kwargs: dict[str, Any]) -> Optional[str]:
    from ..activity import _chat_for_session

    session = kwargs.get("session_id")
    return _chat_for_session(str(session)) if session else None


def _handle_list(args: dict[str, Any], **kwargs: Any) -> str:
    return _result(success=True, helpers=[{k: h.get(k) for k in ("name", "title", "instructions")} for h in _read()])


def _handle_create(args: dict[str, Any], **kwargs: Any) -> str:
    if _adapter is None:
        return _result(success=False, error="还没准备好")
    try:
        row = create(_adapter.store, {k: args.get(k) for k in ("name", "title", "instructions", "emoji") if args.get(k)})
    except ValueError as exc:
        return _result(success=False, error=str(exc))
    return _result(success=True, name=row["name"], note="建好了，用户在「帮手」页能看到和修改它")


def _handle_ask(args: dict[str, Any], **kwargs: Any) -> str:
    if _adapter is None or _loop is None:
        return _result(success=False, error="还没准备好")
    helper = find(str(args.get("helper") or ""))
    task = str(args.get("task") or "").strip()
    if not helper:
        return _result(success=False, error="没有这个帮手", helpers=[h["name"] for h in _read()])
    if not task:
        return _result(success=False, error="要说清楚交给它什么事")
    goal_id = str(args.get("goal_id") or "")
    origin = _current_chat(kwargs)
    if is_desk(origin):
        return _result(success=False, error="浏览器代办只做浏览器上的事，不交给帮手")
    if origin == helper["chat_id"]:
        return _result(success=False, error="帮手不能把事交给自己")
    asking = helper_for_chat(origin)
    if asking:
        group = group_for_chat(_adapter.helper_origin(origin))
        if group:
            # Working for a group: pass it to another member of that group, in the group.
            if helper["id"] not in (group.get("members") or []):
                return _result(success=False, error="在群聊里只能交给同群的其他帮手")
            origin = group["chat_id"]
        else:
            # From its own chat (as Grok Bot's chief of staff): the answer shows there. Waited
            # for, and never back up the chain (two helpers each waiting on the other).
            chain = _chain_of(origin)
            if helper["chat_id"] in chain:
                return _result(success=False, error=f"「{helper['name']}」正在等你的结果，不能再交回给它")
            if len(chain) >= _MAX_CHAIN:
                return _result(success=False, error="已经转了好几手了，这件事你自己做完吧")
            return _ask_from_helper(origin, helper, task, goal_id)
    from .plans import log_event

    if goal_id:  # work for a goal goes on its record (the 目标 page)
        log_event(goal_id, "helper", f"交给「{helper['name']}」：{task}", helper=helper["name"], emoji=helper.get("emoji"))
    if args.get("wait") is False and origin:
        # In the background: the chat goes on; the answer shows up there when it's ready.
        _loop.call_soon_threadsafe(_adapter.hand_off, origin, helper, task[:4000], _BACKGROUND_TIMEOUT_S, goal_id)
        return _result(success=True, helper=helper["name"], background=True,
                       note=f"已经交给「{helper['name']}」在后台做了，做完它会自己在这里说。告诉用户一声就行，不要等它。")
    future = asyncio.run_coroutine_threadsafe(
        _adapter.ask_helper(origin, helper["chat_id"], task[:4000], _ASK_TIMEOUT_S), _loop)
    try:
        answer = future.result(_ASK_TIMEOUT_S + 30)
    except TimeoutError:
        return _result(success=False, error=f"「{helper['name']}」还没做完，做完的结果在「帮手」页能看到")
    except Exception as exc:
        return _result(success=False, error=str(exc) or type(exc).__name__)
    if goal_id:
        log_event(goal_id, "helper_done", f"「{helper['name']}」做完了：{answer}", helper=helper["name"], emoji=helper.get("emoji"))
    if origin:
        # The helper speaks for itself in the chat that asked, as in a group: the user sees who did it.
        asyncio.run_coroutine_threadsafe(_adapter.post_as_helper(origin, helper, answer), _loop).result(30)
        return _result(success=True, helper=helper["name"], posted=True, answer=answer,
                       note="它的回答已经以它自己的名义显示在聊天里了，不要复述。需要的话补一句结论或下一步；"
                            "交给了几个帮手的，由你把它们的结果汇总成一个结论。")
    return _result(success=True, helper=helper["name"], answer=answer)


def _chain_of(chat_id: str) -> list[str]:
    """The chats waiting, one on the next, for this one's work (it first)."""
    chain = [chat_id]
    with _LOCK:
        while chain[-1] in _asking and len(chain) < 10:
            chain.append(_asking[chain[-1]])
    return chain


def _ask_from_helper(origin: str, helper: dict[str, Any], task: str, goal_id: str) -> str:
    from .plans import log_event

    with _LOCK:
        _asking[helper["chat_id"]] = origin
    try:
        future = asyncio.run_coroutine_threadsafe(
            _adapter.ask_helper(origin, helper["chat_id"], task[:4000], _ASK_TIMEOUT_S), _loop)
        answer = future.result(_ASK_TIMEOUT_S + 30)
    except TimeoutError:
        return _result(success=False, error=f"「{helper['name']}」还没做完，做完的结果在它的聊天里能看到")
    except Exception as exc:
        return _result(success=False, error=str(exc) or type(exc).__name__)
    finally:
        with _LOCK:
            _asking.pop(helper["chat_id"], None)
    if goal_id:
        log_event(goal_id, "helper_done", f"「{helper['name']}」做完了：{answer}", helper=helper["name"], emoji=helper.get("emoji"))
    asyncio.run_coroutine_threadsafe(_adapter.post_as_helper(origin, helper, answer), _loop).result(30)
    return _result(success=True, helper=helper["name"], posted=True, answer=answer,
                   note="它的回答已经以它自己的名义显示在聊天里了，不要复述；接着把你的活做完、汇总。")


def _handle_profile(args: dict[str, Any], **kwargs: Any) -> str:
    if _adapter is None:
        return _result(success=False, error="还没准备好")
    helper = helper_for_chat(_current_chat(kwargs))
    if not helper:
        return _result(success=False, error="只有帮手能改自己的设定")
    values = {k: args[k] for k in ("name", "title", "instructions", "emoji") if args.get(k)}
    if values.get("instructions"):
        values["setup"] = False
    try:
        row = update(_adapter.store, helper["id"], values)
    except ValueError as exc:
        return _result(success=False, error=str(exc))
    return _result(success=True, name=row["name"], title=row.get("title"), emoji=row.get("emoji"))


def _handle_browser(args: dict[str, Any], **kwargs: Any) -> str:
    """A helper asking the companion to do something in the browser for it."""
    if _adapter is None or _loop is None:
        return _result(success=False, error="还没准备好")
    helper = helper_for_chat(_current_chat(kwargs))
    if not helper:
        return _result(success=False, error="主助理自己直接用浏览器工具就行")
    task = str(args.get("task") or "").strip()
    if not task:
        return _result(success=False, error="要说清楚在网页上做什么、要什么结果")
    future = asyncio.run_coroutine_threadsafe(_adapter.browser_for(helper, task[:4000], _ASK_TIMEOUT_S), _loop)
    try:
        answer = future.result(_ASK_TIMEOUT_S + 30)
    except TimeoutError:
        return _result(success=False, error="浏览器上的事还没做完")
    except Exception as exc:
        return _result(success=False, error=str(exc) or type(exc).__name__)
    return _result(success=True, result=answer)


def _handle_habit(args: dict[str, Any], **kwargs: Any) -> str:
    helper = helper_for_chat(_current_chat(kwargs))
    if not helper:
        return _result(success=False, error="只有帮手能记用户的习惯")
    replaces = str(args.get("replaces") or "") or None
    if replaces is None:
        # Name the one it supersedes by its words, too.
        old = str(args.get("replaces_text") or "").strip()
        replaces = next((h["id"] for h in habits_of(helper) if old and old in h["text"]), None)
    habit = add_habit(helper["id"], str(args.get("habit") or ""), "chat", replaces)
    if habit is not None:
        notice(helper, f"记住了：{habit['text']}")
    if habit is None:
        return _result(success=False, error="习惯是空的")
    return _result(success=True, note="记住了，以后默认照这个来。可以用一句话告诉用户你记住了。")


def _handle_note(args: dict[str, Any], **kwargs: Any) -> str:
    helper = helper_for_chat(_current_chat(kwargs))
    if not helper:
        return _result(success=False, error="只有帮手能记工作笔记")
    note = _clean(args.get("note"), 300)
    if not note:
        return _result(success=False, error="笔记是空的")
    add_note(helper["id"], note)
    return _result(success=True)


TOOLS = (
    ("helper_list", "列出用户的帮手。", {}, [], _handle_list),
    ("helper_create", "按用户的要求新建一个帮手（有固定职责、听你调遣的助手，比如记账、研究员）。只在用户要求时用。",
     {"name": {"type": "string", "description": "帮手的名字，2~6 个字"},
      "title": {"type": "string", "description": "一句话职责（16 个字以内），比如「记录和统计花销」"},
      "instructions": {"type": "string", "description": "职责说明：负责什么、怎么做、要注意什么"},
      "emoji": {"type": "string", "description": "代表它的一个 emoji"}},
     ["name", "instructions"], _handle_create),
    ("helper_ask", "把一件事交给某个帮手去做，等它做完把结果带回来（可能要几分钟）。几个帮手可以分别交不同部分。"
                   "要花很久、用户不用等着看的事（比如长时间调研、整理一大堆资料），用 wait=false 放到后台，做完它会自己在聊天里说。",
     {"helper": {"type": "string", "description": "帮手的名字"},
      "wait": {"type": "boolean", "description": "默认 true：等它做完再接着说。false：放到后台，聊天继续，做完它自己来报"},
      "task": {"type": "string", "description": "要它做的事，说完整：背景、要什么结果、什么格式"},
      "goal_id": {"type": "string", "description": "这件事是为了推进哪个目标（goal_list 里的 id），不是就不填"}},
     ["helper", "task"], _handle_ask),
    ("helper_profile", "（帮手用）设定或修改你自己：名字、一句话职责、职责说明（完整的新版本）、emoji 头像。只填要改的。",
     {"name": {"type": "string", "description": "名字，2~6 个字"},
      "title": {"type": "string", "description": "一句话职责（16 个字以内）"},
      "instructions": {"type": "string", "description": "完整的职责说明，一行一条"},
      "emoji": {"type": "string", "description": "一个 emoji"}},
     [], _handle_profile),
    ("browser_request", "（帮手用）请主助理在浏览器里替你做一件事（打开网页查看、填写、操作），等它把结果告诉你。帮手不能自己用浏览器。",
     {"task": {"type": "string", "description": "在哪个网站、做什么、要拿回什么结果，说完整"}},
     ["task"], _handle_browser),
    ("helper_habit", "（帮手用）记下用户在你这方面的一个长期习惯或偏好（以后默认照着来），比如“行程别排太紧，每天最多三个点”。",
     {"habit": {"type": "string", "description": "一句话，具体"},
      "replaces_text": {"type": "string", "description": "它取代的旧习惯里的几个字（没有就不填）"}},
     ["habit"], _handle_habit),
    ("helper_note", "（帮手用）记下一条这份工作以后用得上的笔记：做法、结论。用户的偏好用 helper_habit 记。",
     {"note": {"type": "string", "description": "一句话"}}, ["note"], _handle_note),
)


def register_tools(ctx: Any) -> None:
    for name, description, properties, required, handler in TOOLS:
        schema = {"name": name, "description": description,
                  "parameters": {"type": "object", "properties": properties, "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description,
                          emoji="🧑‍💼")
