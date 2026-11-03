"""动态: short posts the companion writes about what the user cares about (as Muse's feed).

Once a day (by default at 08:00), a scheduled run takes its topics from the companion's memory of
the user (MEMORY.md: what they care about, freshest first — what they stopped talking about is
fading and comes last, see memory_fade), searches the web for what is new on those topics, and writes a few posts with the ``feed_post`` tool: a title, a short
body (markdown, the key words linked to their sources), a topic, and optionally a picture and a
question to ask about it. The user steers it with one sentence (the instructions, editable in the
app) and can have it write now. The run answers [SILENT]: posts go to the feed, not the chat.

What the user does with the posts steers the next ones: a ❤ or a 讨论 marks a post as wanted, a
deleted post as not; ``feed_recent`` hands the run both lists, with the topics behind them.

Stored in ``<companion dir>/feed.json``; the newest ``_KEEP`` posts are kept.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .companion_state import read_companion_state
from .paths import get_webui_dir

TOOLSET = "companion"
JOB_NAME = "动态更新"
DEFAULT_INSTRUCTIONS = "为我打造一个关于我兴趣的动态版块。保持内容简洁直接。确保可快速浏览。尽量避免点击诱饵。"
AREAS = ("tech", "ai", "finance", "news", "health", "food", "travel", "learning", "entertainment", "sports",
         "home", "people", "goals", "other")
_KEEP = 80
_KEEP_FEEDBACK = 40
_LOCK = threading.Lock()
# Only what writing the feed needs: fewer tools, fewer detours before it runs out of steps.
_TOOLSETS = ["companion", "web", "memory", "session_search"]
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_adapter: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def bind(adapter: Any, loop: asyncio.AbstractEventLoop) -> None:
    """The running adapter: a change to the feed tells the apps at once (posts are written on worker threads)."""
    global _adapter, _loop
    _adapter, _loop = adapter, loop


def _path() -> Path:
    return get_webui_dir() / "feed.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("instructions", DEFAULT_INSTRUCTIONS)
    data.setdefault("daily", True)
    data.setdefault("time", "08:00")
    data.setdefault("posts", [])
    data.setdefault("generated_at", None)
    data.setdefault("job_id", None)
    data.setdefault("liked", [])      # post ids with a ❤
    data.setdefault("discussed", [])  # post ids the user asked about
    data.setdefault("removed", [])    # {title, area} of posts the user deleted: not wanted
    return data


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    if _adapter is not None and _loop is not None and not _loop.is_closed():
        # A new post shows up on the phone now, not at its next look.
        asyncio.run_coroutine_threadsafe(_adapter.broadcast({"event": "feed_updated"}), _loop)


def _prompt(instructions: str) -> str:
    return (
        "[这条消息不会直接给用户看。现在是给用户写「动态」的时间。]\n\n"
        f"用户对动态的要求：{instructions}\n\n"
        "1. 先用 feed_recent：它给出你的记忆里用户关心的事（interests，越靠前越是用户最近还在谈的），"
        "最近已经发过哪些（别重复），以及用户的反应。话题只从 interests 里选，优先前面的；标了「淡忘中」的只在别的都没新东西时才写。"
        "interests 不到 3 条时，才用 session_search 翻最近一周的聊天、goal_list 看目标来补充。\n"
        "2. 用户喜欢、讨论过的方向多写、写深一点；用户删掉的那类别再写。\n"
        "3. 针对用户关心的 3~5 个话题，用 web_search / web_extract 找最近一两天真正新的进展。没有新东西的话题就跳过。"
        "每个话题搜一两次就够，别反复深挖。\n"
        "4. 一个话题查清楚了就马上写一条 feed_post，再去查下一个（不要全部查完再一起写，也不要把内容写在回复里）：标题一句话说清是什么事；正文是你读完原文后用自己的话写的总结，2~3 句，说清发生了什么、为什么跟用户有关，"
        "其中的重点文字用 [重点文字](原文链接) 链到原文，原文链接也填进 link；原文的主图网址填 image（没找到就不填，会自动取原文的封面图）；"
        "prompt 写一个用户可能想接着问你的问题。\n"
        "5. 写 3~5 条就够，宁缺毋滥；不编造，不确定的不写。每次回复只调用一个工具，一条一条来（一次塞太多会被截断，整期作废）。\n"
        "全部写完后只回复 [SILENT]。"
    )


def _sync_job(data: dict[str, Any]) -> None:
    from cron.jobs import create_job, remove_job

    if data.get("job_id"):
        remove_job(data["job_id"])
        data["job_id"] = None
    if not data["daily"] or not read_companion_state().get("chat_id"):
        return
    hour, minute = (int(x) for x in data["time"].split(":"))
    job = create_job(prompt=_prompt(data["instructions"]), schedule=f"{minute} {hour} * * *", name=JOB_NAME,
                     deliver="local", failure_deliver="local", enabled_toolsets=_TOOLSETS)
    data["job_id"] = job.get("id") if isinstance(job, dict) else None


def ensure_job() -> None:
    """Start-up: the daily run exists as set (made the first time, kept after a reset of cron)."""
    from cron.jobs import get_job, update_job

    with _LOCK:
        data = _read()
        job = get_job(data["job_id"]) if data.get("job_id") else None
        current = job and job.get("prompt") == _prompt(data["instructions"]) and job.get("enabled_toolsets") == _TOOLSETS
        if data["daily"] and job and not current:
            # New wording for the run (an update of this code): the same job, so its runs stay listed.
            update_job(job["id"], {"prompt": _prompt(data["instructions"]), "enabled_toolsets": _TOOLSETS})
        elif data["daily"] and not current:
            _sync_job(data)
            _write(data)


# ── the app ───────────────────────────────────────────────────────────────────────────────────────

def payload() -> dict[str, Any]:
    data = _read()
    return {k: data[k] for k in ("instructions", "daily", "time", "generated_at", "posts", "liked")}


def update(changes: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        if "instructions" in changes:
            text = " ".join(str(changes["instructions"] or "").split())[:500]
            data["instructions"] = text or DEFAULT_INSTRUCTIONS
        if "daily" in changes:
            data["daily"] = bool(changes["daily"])
        if "time" in changes:
            if not _TIME.match(str(changes["time"])):
                raise ValueError("时间要像 08:00")
            data["time"] = str(changes["time"])
        _sync_job(data)
        _write(data)
    return payload()


def write_now() -> dict[str, Any]:
    """Run it now (the daily run, or a one-off when the daily one is off)."""
    from cron.jobs import create_job, trigger_job

    with _LOCK:
        data = _read()
        if data.get("job_id"):
            trigger_job(data["job_id"])
        else:
            from datetime import datetime, timedelta

            at = (datetime.now().astimezone() + timedelta(seconds=15)).isoformat(timespec="seconds")
            create_job(prompt=_prompt(data["instructions"]), schedule=at, name=JOB_NAME,
                       deliver="local", failure_deliver="local", enabled_toolsets=_TOOLSETS)
    return payload()


def delete_post(post_id: str) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        gone = next((p for p in data["posts"] if p.get("id") == post_id), None)
        data["posts"] = [p for p in data["posts"] if p.get("id") != post_id]
        if gone:
            data["removed"] = [{"title": gone["title"], "area": gone["area"]}, *data["removed"]][:_KEEP_FEEDBACK]
        for key in ("liked", "discussed"):
            data[key] = [i for i in data[key] if i != post_id]
        _write(data)
    return payload()


def react(post_id: str, how: str, on: bool = True) -> dict[str, Any]:
    """The user's ❤ (``liked``, on or off) or 讨论 (``discussed``) on a post."""
    if how not in ("liked", "discussed"):
        raise ValueError("how 只能是 liked 或 discussed")
    with _LOCK:
        data = _read()
        if any(p.get("id") == post_id for p in data["posts"]):
            rest = [i for i in data[how] if i != post_id]
            data[how] = ([post_id, *rest] if on else rest)[:_KEEP_FEEDBACK]
            _write(data)
    return payload()


# ── the companion's tools ─────────────────────────────────────────────────────────────────────────

def _clean_url(value: Any) -> Optional[str]:
    url = str(value or "").strip()
    return url if re.match(r"^https://\S+$", url) and len(url) < 1000 else None


_LINK = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
_COVER = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]*content=[\"']([^\"']+)"
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"'](?:og:image|twitter:image)[\"']",
    re.I)


def _cover_image(page: str) -> Optional[str]:
    """The picture the article shows when shared (its og:image), for a post written without one."""
    import html
    import urllib.request
    from urllib.parse import urljoin

    try:
        request = urllib.request.Request(page, headers={"User-Agent": "Mozilla/5.0 (compatible; companion-feed)"})
        with urllib.request.urlopen(request, timeout=6) as response:
            head = response.read(300_000).decode("utf-8", "ignore")
    except Exception:
        return None
    match = _COVER.search(head)
    return _clean_url(urljoin(page, html.unescape(match.group(1) or match.group(2)))) if match else None


def _post(args: dict[str, Any], **_: Any) -> str:
    title = " ".join(str(args.get("title") or "").split())[:60]
    body = str(args.get("body") or "").strip()[:800]
    if not title or not body:
        return json.dumps({"success": False, "error": "要有标题和正文"}, ensure_ascii=False)
    links = _LINK.findall(body)
    source = str(args.get("link") or "").strip()
    if not links and re.match(r"^https?://\S+$", source):
        # No key words linked in the body: the post still leads to its source, at the end.
        body = f"{body} [阅读原文]({source})"
        links = [source]
    if not links:
        return json.dumps({"success": False, "error": "要给 link（原文链接）"}, ensure_ascii=False)
    image = _clean_url(args.get("image"))
    if not image:
        image = next(filter(None, (_cover_image(link) for link in links[:2])), None)
    area = str(args.get("area") or "other")
    with _LOCK:
        data = _read()
        if any(p["title"] == title for p in data["posts"][:30]):
            return json.dumps({"success": False, "error": "最近已经发过这条了"}, ensure_ascii=False)
        post = {"id": uuid.uuid4().hex[:10], "ts": int(time.time() * 1000), "title": title, "body": body,
                "area": area if area in AREAS else "other", "image": image,
                "prompt": " ".join(str(args.get("prompt") or "").split())[:120]}
        data["posts"] = [post, *data["posts"]][:_KEEP]
        data["generated_at"] = post["ts"]
        _write(data)
    return json.dumps({"success": True, "id": post["id"]}, ensure_ascii=False)


def _interests() -> list[str]:
    """What the user cares about, from memory, freshest first: each line with how long ago it last
    came up, the fading ones marked (``memory_fade``). How to talk to the user is not a topic."""
    from .memory import _store
    from .memory_fade import freshness
    from .memory_tidy import _read_meta

    meta, now, store = _read_meta(), time.time(), _store()
    rows = []
    for target in ("user", "memory"):
        for entry in store._entries_for(target):
            entry = " ".join(entry.split())
            if not entry or entry.startswith("【相处方式】"):
                continue
            days, fading = freshness(meta, entry, now)
            rows.append((fading, days, f"{entry}（{'今天' if days == 0 else f'{days} 天前'}提起{'，淡忘中' if fading else ''}）"))
    return [line for _, _, line in sorted(rows)[:30]]


def _recent(args: dict[str, Any], **_: Any) -> str:
    data = _read()
    by_id = {p["id"]: p for p in data["posts"]}
    line = lambda p: f"[{p['area']}] {p['title']}"  # noqa: E731
    wanted = [by_id[i] for i in dict.fromkeys([*data["discussed"], *data["liked"]]) if i in by_id]
    try:
        interests = _interests()
    except Exception:  # memory unreadable right now: the run falls back on past chats
        interests = []
    return json.dumps({
        "success": True,
        "interests": interests,
        "recent": [f"{time.strftime('%m-%d', time.localtime(p['ts'] / 1000))} {p['title']}" for p in data["posts"][:30]],
        "user_liked_or_discussed": [line(p) for p in wanted[:20]],
        "user_deleted": [line(p) for p in data["removed"][:20]],
    }, ensure_ascii=False)


TOOLS = (
    ("feed_post", "在用户的「动态」里发一条帖子（只在写动态的时候用）。",
     {"title": {"type": "string", "description": "一句话标题，说清是什么事，不要标题党"},
      "body": {"type": "string", "description": "2~3 句正文，markdown；关键词用 [文字](链接) 链到来源"},
      "area": {"type": "string", "enum": list(AREAS), "description": "话题领域"},
      "link": {"type": "string", "description": "原文链接（https）"},
      "image": {"type": "string", "description": "这件事的配图网址（https），没有就不填"},
      "prompt": {"type": "string", "description": "用户可能想接着问的一个问题"}},
     ["title", "body", "area", "link"], _post),
    ("feed_recent", "写动态前先看：记忆里用户关心的事（interests，最近还在谈的在前），最近已经发过的帖子（避免重复），以及用户喜欢/讨论过、删掉了哪些。", {}, [], _recent),
)


def register_tools(ctx: Any) -> None:
    for name, description, properties, required, handler in TOOLS:
        schema = {"name": name, "description": description,
                  "parameters": {"type": "object", "properties": properties, "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description,
                          emoji="📰")
