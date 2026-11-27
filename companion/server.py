"""HTTP + WebSocket server for the companion web app (the routes it calls).

Shapes follow the web app's client (``webui/src/lib``), which was written
against nanobot's gateway; keeping them lets the app run unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import ipaddress
import re
import uuid
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .modules import chat_branch
from .modules import chat_export as chat_export_module
from .modules import chat_search as chat_search_module
from .modules import automations, avatars, chat_context, companion_state, computer, connections, creations, feed, followups, helpers, ideas, knowledge, memory, memory_learn, model_catalog, models, oauth_login, permissions, plans, push, routines, skills_page, stt_sensevoice, triggers, tts_api, vault, widgets
from .approvals import SCOPES as APPROVAL_SCOPES
from .media import child_file
from .store import chat_id_of

if TYPE_CHECKING:
    from muse.app import NativeCompanionAdapter as CompanionAdapter

logger = logging.getLogger(__name__)

WS_PATH = "/ws"
MAX_FRAME_BYTES = 36 * 1024 * 1024
NO_STORE = {"Cache-Control": "no-store"}

Mutation = Callable[["CompanionAdapter", dict[str, Any]], Awaitable[Any]]


def adapter_debug() -> bool:
    import os

    return bool(os.environ.get("COMPANION_DEBUG_FRAMES"))


_FORWARDED = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
    "cf-connecting-ip",
    "tailscale-user-login",
)
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")
_HOOK_STREAM_LIMIT = 1024 * 1024
_HOOK_BODY_CHARS = 65_536


def _is_loopback_ip(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return raw.strip().lower() == "localhost"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


_PRIVATE_PROXY_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy_peer(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return raw.strip().lower() == "localhost"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback or any(addr in net for net in _PRIVATE_PROXY_NETS)


def _is_loopback_netloc(netloc: str | None) -> bool:
    if not netloc:
        return False
    try:
        host = urlsplit(f"//{netloc.strip()}").hostname
    except ValueError:
        return False
    return _is_loopback_ip(host)


def _normalize_netloc(netloc: str | None, scheme: str) -> str:
    raw = (netloc or "").strip().lower()
    if not raw:
        return ""
    default_port = ":443" if scheme == "https" else ":80" if scheme == "http" else ""
    if default_port and raw.endswith(default_port):
        host_part = raw[: -len(default_port)]
        if host_part.startswith("[") or ":" not in host_part:
            return host_part
    return raw


def _origin_allowed(conn: HTTPConnection, *, local_only: bool = False) -> bool:
    """Validate browser ``Origin`` against ``Host`` (or loopback in local mode) to prevent CSRF and
    cross-site WebSocket hijacking. Non-browser clients (native iOS, CLI scripts) omit ``Origin``."""
    origin = (conn.headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin.lower() == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname or not parsed.netloc:
        return False
    if local_only:
        return _is_loopback_netloc(parsed.netloc)
    origin_netloc = _normalize_netloc(parsed.netloc, scheme)
    raw_host = (conn.headers.get("host") or "").strip().lower()
    host_netloc = _normalize_netloc(raw_host, scheme)
    if _is_loopback_netloc(origin_netloc) and _is_loopback_netloc(raw_host):
        return True
    if host_netloc and origin_netloc == host_netloc:
        return True
    peer = conn.client.host if conn.client else None
    if _is_trusted_proxy_peer(peer):
        raw_forwarded_host = (conn.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip().lower()
        forwarded_netloc = _normalize_netloc(raw_forwarded_host, scheme)
        if forwarded_netloc and origin_netloc == forwarded_netloc:
            return True
    return False


def _is_local(conn: HTTPConnection) -> bool:
    """A browser or CLI on this machine itself. Behind a reverse proxy every request comes from
    loopback, so forwarded headers disqualify it; ``Host`` and ``Origin`` must also be loopback so a
    DNS-rebinding or cross-origin page cannot mint tokens without a password."""
    peer = conn.client.host if conn.client else None
    if not _is_loopback_ip(peer) or any(conn.headers.get(h) for h in _FORWARDED):
        return False
    host_header = (conn.headers.get("host") or "").strip()
    if host_header and not _is_loopback_netloc(host_header):
        return False
    return _origin_allowed(conn, local_only=True)


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if _is_trusted_proxy_peer(peer):
        forwarded = (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return peer or ""


def _clean_base_url(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment or not _SAFE_HOST.fullmatch(parsed.netloc):
        return None
    path = parsed.path.rstrip("/")
    if any(ch.isspace() or ord(ch) < 32 for ch in path):
        return None
    return f"{scheme}://{parsed.netloc}{path}"


def _public_base(request: Request) -> str:
    """This server's address as the device sees it (the proxy's, only when behind a trusted proxy)."""
    peer = request.client.host if request.client else None
    trusted_proxy = _is_trusted_proxy_peer(peer)
    raw_scheme = (request.headers.get("x-forwarded-proto") if trusted_proxy else None) or request.url.scheme
    scheme = raw_scheme.split(",", 1)[0].strip().lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    raw_host = (
        (request.headers.get("x-forwarded-host") if trusted_proxy else None)
        or request.headers.get("host")
        or request.url.netloc
    )
    host = raw_host.split(",", 1)[0].strip()
    if not host or not _SAFE_HOST.fullmatch(host):
        host = request.url.netloc or "127.0.0.1:8766"
    return f"{scheme}://{host}"


async def _read_body_bounded(request: Request, max_bytes: int, *, keep_bytes: Optional[int] = None) -> Optional[bytes]:
    """Read request body via ``request.stream()``, returning ``None`` immediately if it exceeds ``max_bytes``."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return None
        except ValueError:
            return None
    limit = keep_bytes if keep_bytes is not None else max_bytes
    chunks: list[bytes] = []
    total = 0
    kept = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        if kept < limit:
            take = chunk[: limit - kept]
            chunks.append(take)
            kept += len(take)
    return b"".join(chunks)


def _device_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("device ") else None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# -- mutations (``webui_request`` frames) -----------------------------------------------------------


def _runtime_capabilities() -> dict[str, bool]:
    return {
        "stt": stt_sensevoice.stt_available(),
        "tts": tts_api.tts_available(),
        "push": bool(push.available()),
        "browser": bool(computer._executable()),
    }


def _companion_payload(state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    base = state if state is not None else companion_state.read_companion_state()
    return {**base, "capabilities": _runtime_capabilities()}


async def _companion_update(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        raise ApiError(400, "修改内容格式不正确")
    try:
        state = companion_state.update_companion_state(changes)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    adapter.sync_identity()
    return await asyncio.to_thread(_companion_payload, state)


async def _approval_decide(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    approval_id = payload.get("id")
    decision = payload.get("decision")
    if not isinstance(approval_id, str) or decision not in {"approve", "reject"}:
        raise ApiError(400, "缺少审核记录 ID 或决定选项")
    scope = payload.get("scope", "once")
    if scope not in APPROVAL_SCOPES:
        raise ApiError(400, "授权范围不正确")
    record = await adapter.approvals.decide(approval_id, decision == "approve", scope)
    if record is None:
        raise ApiError(409, "这条审核不存在或已经处理过了")
    return record.public()


def _changes(payload: dict[str, Any]) -> dict[str, Any]:
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        raise ApiError(400, "修改内容格式不正确")
    return changes


async def _models_update(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await asyncio.to_thread(models.update_models, _changes(payload))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _models_check(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """Check that models answer (``names``), or with ``vendor`` read that company's prices and ids."""
    if payload.get("vendor"):
        return await _models_vendor_check(adapter, payload)
    names = payload.get("names")
    if names is not None and not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
        raise ApiError(400, "模型列表格式不正确")
    try:
        return await models.check_models(names)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _routines_update(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await asyncio.to_thread(routines.update_routines, _changes(payload))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _grants_revoke(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    key = payload.get("key")
    if key is not None and not isinstance(key, str):
        raise ApiError(400, "权限标识格式不正确")
    adapter.approvals.grants.revoke(key)
    return _grants(adapter)


def _grants(adapter: "CompanionAdapter") -> dict[str, Any]:
    return {"grants": [grant.public() for grant in adapter.approvals.grants.list()]}


async def _goals_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    goal_id, action = payload.get("id"), payload.get("action")
    if action not in {"add", "update", "remove", "checkin"} or (action != "add" and not isinstance(goal_id, str)):
        raise ApiError(400, "操作类型或目标 ID 不正确")
    changes = payload.get("changes") or {}
    if not isinstance(changes, dict):
        raise ApiError(400, "修改内容格式不正确")
    try:
        if action == "add":
            await asyncio.to_thread(plans.add_goal, changes.get("title", ""), changes.get("steps"),
                                    changes.get("checkin"), changes.get("due"), changes.get("category"))
        elif action == "update":
            await asyncio.to_thread(plans.update_goal, goal_id, changes)
        elif action == "remove":
            await asyncio.to_thread(plans.remove_goal, goal_id)
        else:
            await asyncio.to_thread(plans.check_in_now, goal_id)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return await asyncio.to_thread(plans.list_goals)


async def _triggers_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    trigger_id, action = payload.get("id"), payload.get("action")
    if action not in {"remove", "pause", "resume"} or not isinstance(trigger_id, str):
        raise ApiError(400, "操作类型或触发器 ID 不正确")
    try:
        if action == "remove":
            await asyncio.to_thread(triggers.remove_trigger, trigger_id)
        else:
            await asyncio.to_thread(triggers.set_paused, trigger_id, action == "pause")
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return {"triggers": await asyncio.to_thread(triggers.list_triggers)}


async def _devices_remove(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    device_id = payload.get("id")
    if not isinstance(device_id, str) or not await asyncio.to_thread(adapter.devices.remove, device_id):
        raise ApiError(404, "找不到这台设备")
    # Its sessions end now: tokens stop working, open connections close (the app then asks to pair).
    adapter.tokens.revoke_device(device_id)
    for socket, owner in list(adapter.socket_devices.items()):
        if owner == device_id:
            with contextlib.suppress(Exception):
                await socket.close(code=4401)
    return {"devices": await asyncio.to_thread(adapter.devices.list)}


async def _soul_update(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "请填写设定内容")
    try:
        saved = await asyncio.to_thread(companion_state.write_user_soul, text)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    adapter.sync_identity()
    return {"text": saved}


async def _memory_doc_update(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "请填写记忆内容")
    try:
        return await asyncio.to_thread(memory.write_memory_doc, text)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None


async def _memory_tidy(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    return await asyncio.to_thread(memory.tidy_now)


async def _memory_learn(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """现在更新: read what was said since the last pass and update the memory now."""
    result = await asyncio.to_thread(memory_learn.learn, adapter.store, True)
    return {**await asyncio.to_thread(memory.read_memory), "learned": result}


async def _memory_undo(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    change_id = payload.get("id")
    if not isinstance(change_id, str) or not change_id:
        raise ApiError(400, "缺少变更记录 ID")
    try:
        return await asyncio.to_thread(memory.undo_change, change_id)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None


async def _memory_forget(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    file, line = payload.get("file"), payload.get("line")
    if not isinstance(file, str) or not isinstance(line, str):
        raise ApiError(400, "缺少记忆文件或条目内容")
    try:
        return await asyncio.to_thread(memory.forget_memory, file, line)
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None


async def _memory_remember(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "请填写要记住的内容")
    try:
        return await asyncio.to_thread(memory.remember, text)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _memory_pin(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    file, line = payload.get("file"), payload.get("line")
    pinned = payload.get("pinned")
    if not isinstance(file, str) or not isinstance(line, str) or not line.strip():
        raise ApiError(400, "缺少记忆文件或条目内容")
    if pinned is not None and not isinstance(pinned, bool):
        raise ApiError(400, "置顶状态格式不正确")
    try:
        return await asyncio.to_thread(memory.pin_memory, file, line, pinned)
    except ValueError as exc:
        msg = str(exc)
        status = 400 if ("不支持" in msg or "缺少" in msg) else 404 if "不在了" in msg else 409
        raise ApiError(status, msg) from None


def _companion_events(adapter: "CompanionAdapter") -> list[dict[str, Any]]:
    chat_id = companion_state.read_companion_state().get("chat_id")
    return adapter.store.events(chat_id) if chat_id and adapter.store.exists(chat_id) else []


async def _ideas_refresh(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await ideas.get_ideas(_companion_events(adapter), refresh=True)
    except Exception as exc:
        raise ApiError(503, str(exc) or "没有想出合适的建议，稍后再试") from None


CREATIONS_PREFIX = "/api/webui/companion/creations/file"


def _creations(adapter: "CompanionAdapter") -> Any:
    return creations.list_creations(lambda rel: adapter.media.sign(CREATIONS_PREFIX, rel))


async def _creations_hide(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    creation_id = payload.get("id")
    if not isinstance(creation_id, str):
        raise ApiError(400, "缺少作品 ID")
    try:
        await asyncio.to_thread(creations.hide_creation, creation_id)
    except ValueError as exc:
        raise ApiError(404, str(exc)) from None
    return await asyncio.to_thread(_creations, adapter)


async def _creations_save(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    creation_id = str(payload.get("id") or "").strip() or None
    title = str(payload.get("title") or "").strip()
    content = payload.get("content")
    fmt = str(payload.get("fmt") or payload.get("format") or "md").strip() or "md"
    prompt = payload.get("prompt")
    if not isinstance(content, str) or not content.strip():
        raise ApiError(400, "作品内容不能为空")
    if prompt is not None and not isinstance(prompt, str):
        raise ApiError(400, "提示词格式不正确")
    try:
        rel = await asyncio.to_thread(
            creations.save_artifact,
            title=title or ("" if creation_id else "画布作品"),
            content=content,
            fmt=fmt,
            prompt=prompt,
            creation_id=creation_id,
        )
    except ValueError as exc:
        msg = str(exc)
        raise ApiError(404 if "找不到" in msg else 400, msg) from None
    return {**(await asyncio.to_thread(_creations, adapter)), "saved_id": rel, "updated": bool(creation_id)}


async def _knowledge_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    action = payload.get("action")
    list_collection = str(payload.get("list_collection") or "").strip() or None
    if action == "read":
        doc_id = str(payload.get("id") or payload.get("doc_id") or "").strip()
        if not doc_id:
            raise ApiError(400, "缺少文档 ID")
        try:
            return await asyncio.to_thread(knowledge.get_document, doc_id)
        except ValueError as exc:
            msg = str(exc)
            raise ApiError(404 if "找不到" in msg else 400, msg) from None
    if action == "delete":
        doc_id = str(payload.get("id") or "").strip()
        if not doc_id:
            raise ApiError(400, "缺少文档 ID")
        try:
            await asyncio.to_thread(knowledge.delete_document, doc_id)
        except ValueError as exc:
            raise ApiError(404, str(exc)) from None
        return await asyncio.to_thread(knowledge.list_knowledge, collection=list_collection)
    if action == "update":
        doc_id = str(payload.get("id") or "").strip()
        if not doc_id:
            raise ApiError(400, "缺少文档 ID")
        title = payload.get("title") if "title" in payload else None
        collection = payload.get("collection") if "collection" in payload else None
        if title is not None and not isinstance(title, str):
            raise ApiError(400, "标题格式不正确")
        if collection is not None and not isinstance(collection, str):
            raise ApiError(400, "分组格式不正确")
        if title is None and collection is None:
            raise ApiError(400, "请提供要修改的标题或分组")
        try:
            updated = await asyncio.to_thread(knowledge.update_document, doc_id, title=title, collection=collection)
        except ValueError as exc:
            msg = str(exc)
            status = 404 if "找不到" in msg else 409 if "已存在" in msg else 400
            raise ApiError(status, msg) from None
        return {**(await asyncio.to_thread(knowledge.list_knowledge, collection=list_collection)), "document": updated}
    if action == "import_creation":
        creation_id = str(payload.get("creation_id") or payload.get("id") or "").strip()
        if not creation_id:
            raise ApiError(400, "缺少作品 ID")
        collection = str(payload.get("collection") or "").strip() or knowledge.DEFAULT_COLLECTION
        replace_existing = payload.get("replace_existing", True) is not False
        try:
            added = await asyncio.to_thread(
                knowledge.import_from_creation,
                creation_id,
                collection=collection,
                replace_existing=replace_existing,
            )
        except ValueError as exc:
            msg = str(exc)
            raise ApiError(404 if "找不到" in msg else 400, msg) from None
        return {**(await asyncio.to_thread(knowledge.list_knowledge, collection=list_collection)), "added": added}
    if action == "search":
        query = str(payload.get("query") or payload.get("q") or "").strip()
        collection = str(payload.get("collection") or "").strip() or None
        raw_limit = payload.get("limit")
        limit = (
            int(raw_limit)
            if isinstance(raw_limit, int) and not isinstance(raw_limit, bool)
            else knowledge.DEFAULT_SEARCH_LIMIT
        )
        hits = await asyncio.to_thread(knowledge.search_knowledge, query, collection=collection, limit=limit)
        return {"query": query, "collection": collection or "", "hits": hits}
    raise ApiError(400, "不支持的知识库操作")


def _soul_payload() -> dict[str, Any]:
    """SOUL.md as the user edits it, and when it last changed (ms; None while it is the first draft)."""
    try:
        updated = int(companion_state.user_soul_path().stat().st_mtime * 1000)
    except OSError:
        updated = None
    return {"text": companion_state.read_user_soul(), "updated_at": updated}


AVATARS_PREFIX = "/api/webui/companion/avatars/file"


def _avatars(adapter: "CompanionAdapter", created: Any = None) -> Any:
    root = avatars.avatars_root().resolve()

    def sign(path: Any) -> Any:
        return adapter.media.sign(AVATARS_PREFIX, path.resolve().relative_to(root).as_posix())

    listing = avatars.list_avatars(sign)
    if created is not None:
        listing["value"] = avatars.avatar_value(created)
        listing["avatar"] = next(item for item in listing["items"] if item["id"] == created["id"])
    return listing


def _avatar_action(action: str) -> Mutation:
    async def handler(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
        def run() -> Any:
            if action == "upload":
                return _avatars(adapter, avatars.upload(payload))
            if action == "from_creation":
                path = child_file(creations.creations_root(), str(payload.get("id") or ""))
                if path is None or creations.kind_of(path.name) != "image":
                    raise ApiError(404, "找不到这张图片")
                return _avatars(adapter, avatars.from_creation(path, name=path.stem))
            avatars.delete(str(payload.get("id") or ""))
            return _avatars(adapter)
        try:
            return await asyncio.to_thread(run)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        except OSError:
            logger.exception("companion avatar storage failed")
            raise ApiError(500, "保存图片失败，请稍后再试") from None
    return handler


async def _push_subscribe(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return {"devices": await asyncio.to_thread(push.add_subscription, payload.get("subscription"))}
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _push_unsubscribe(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str):
        raise ApiError(400, "缺少通知订阅地址")
    return {"devices": await asyncio.to_thread(push.remove_subscription, endpoint)}


async def _push_test(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    name = companion_state.read_companion_state().get("name") or "伙伴"
    return {"sent": await push.send_push(name, "通知已经开好了，有事我会在这里告诉你。", tag="test")}


def _connection_action(action: str) -> Mutation:
    async def handler(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
        name = str(payload.get("name") or "")
        try:
            if action == "test":
                last = await connections.test_server_async(name)
                return await asyncio.to_thread(connections.presets_payload, last)
            last = await asyncio.to_thread(connections.run_action, action, name, payload)
        except connections.ConnectionError_ as exc:
            raise ApiError(exc.status, str(exc)) from None
        # Live: reconnect MCP services on the active event loop so open chats see updated tools.
        try:
            reload = await connections.apply_live_async(
                engine=getattr(adapter, "engine", None),
                runner=getattr(adapter, "gateway_runner", None),
            )
        except Exception as exc:
            logger.exception("companion: MCP reload failed")
            reload = {"ok": False, "failed": [name], "message": str(exc)}
        result = await asyncio.to_thread(connections.presets_payload, last)
        if action in ("enable", "tools") and name in reload.get("failed", []):
            title = next((p["display_name"] for p in result["presets"] if p["name"] == name), name)
            reload = {**reload, "ok": False, "message": f"{title} 没连上，检查一下填的信息，或者稍后再试"}
        else:
            reload = {**reload, "ok": True}
        result["hot_reload"] = reload
        return result
    return handler


def _automation_action(action: str) -> Mutation:
    async def handler(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
        try:
            return await asyncio.to_thread(automations.run_action, action, payload.get("id"))
        except ValueError as exc:
            msg = str(exc)
            status = 409 if "不能删除" in msg else 400 if "不支持" in msg else 404
            raise ApiError(status, msg) from None
    return handler


async def _models_add(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        added = await asyncio.to_thread(models.add_preset, payload)
        if str(payload.get("kind") or "text") != "text":
            return added
        # Say right away whether the service accepts the key and model.
        return {**await models.check_models([added["added"]]), "added": added["added"]}
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _helpers_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """帮手: create one (from a template or from scratch), change it (name, emoji, job, how it works, model,
    notes), put a default one back as it came, or delete one."""
    action, helper_id = payload.get("action"), str(payload.get("id") or "")
    values = {k: payload[k] for k in ("name", "emoji", "avatar", "title", "instructions", "notes", "model") if k in payload}
    opened = None
    try:
        if action == "create":
            opened = (await asyncio.to_thread(helpers.create, adapter.store, values))["chat_id"]
        elif action == "blank":
            # 新建帮手: a blank one. Told what it is for (a name or a sentence), it proposes its whole
            # setup straight away (as Grok Bot infers a Bot's role from its name); else it asks.
            idea = " ".join(str(payload.get("idea") or "").split())[:200]
            opened = (await asyncio.to_thread(helpers.create_blank, adapter.store, idea))["chat_id"]
            notice = (f"【系统通知】用户刚把你建好，想让你负责：{idea}。用一句话打招呼，然后直接给出你的完整设定建议。"
                      if idea else "【系统通知】用户刚把你建好，打开了和你的对话。用一句话打招呼，然后问想让你负责什么。")
            asyncio.ensure_future(adapter.submit_internal(opened, notice))
        elif action == "update":
            await asyncio.to_thread(helpers.update, adapter.store, helper_id, values)
        elif action == "reset":
            await asyncio.to_thread(helpers.reset, adapter.store, helper_id)
        elif action == "habit_add":
            # 告诉它一个习惯, typed on its profile.
            if not await asyncio.to_thread(helpers.add_habit, helper_id, str(payload.get("text") or ""), "you"):
                raise ValueError("习惯是空的")
        elif action == "habit_remove":
            await asyncio.to_thread(helpers.remove_habit, helper_id, str(payload.get("habit") or ""))
        elif action == "avatar":
            # 画一个头像: the image model draws its face.
            await asyncio.to_thread(helpers.generate_avatar, adapter.store, helper_id, str(payload.get("idea") or ""))
        elif action == "delete":
            await asyncio.to_thread(helpers.delete, adapter.store, helper_id)
        else:
            raise ApiError(400, "不支持的帮手操作")
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return {**helpers.page(adapter.store, set(adapter._turns)), **({"opened": opened} if opened else {})}


async def _groups_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """群聊: make one (a name and 2~6 helpers), change its name or members, or delete it."""
    action, group_id = payload.get("action"), str(payload.get("id") or "")
    members = payload.get("members") if isinstance(payload.get("members"), list) else None
    opened = None
    try:
        if action == "create":
            opened = (await asyncio.to_thread(helpers.create_group, adapter.store, str(payload.get("name") or ""),
                                              members or []))["chat_id"]
        elif action == "update":
            await asyncio.to_thread(helpers.update_group, adapter.store, group_id,
                                    payload.get("name") if "name" in payload else None, members)
        elif action == "delete":
            await asyncio.to_thread(helpers.delete_group, adapter.store, group_id)
        else:
            raise ApiError(400, "不支持的群聊操作")
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return {**helpers.page(adapter.store, set(adapter._turns)), **({"opened": opened} if opened else {})}


async def _permissions_set(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """设置 → 权限: one kind of action set to 禁止 / 询问我 / 允许."""
    try:
        return await asyncio.to_thread(permissions.set_level, str(payload.get("kind") or ""), str(payload.get("level") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _vault_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """安全凭证: add a site login or an API key (what is typed goes straight here, never into a chat),
    or remove one."""
    action = payload.get("action")
    try:
        if action == "add_login":
            return await asyncio.to_thread(vault.add_login, str(payload.get("label") or ""), str(payload.get("origin") or ""),
                                           str(payload.get("identifier") or ""), str(payload.get("password") or ""))
        if action == "add_key":
            return await asyncio.to_thread(vault.add_key, str(payload.get("label") or ""), str(payload.get("env") or ""),
                                           str(payload.get("value") or ""))
        if action == "remove":
            return await asyncio.to_thread(vault.remove, str(payload.get("kind") or ""), str(payload.get("id") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    raise ApiError(400, "不支持的安全凭证操作")


async def _skills_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """技能: read one, save (or create) one, delete one, or draft one from steps the user recorded."""
    action, name = payload.get("action"), str(payload.get("name") or "").strip()
    try:
        if action == "read":
            return await asyncio.to_thread(skills_page.read_skill, name)
        if action == "save":
            return await asyncio.to_thread(skills_page.save_skill, name, str(payload.get("content") or ""),
                                           bool(payload.get("new")))
        if action == "delete":
            return await asyncio.to_thread(skills_page.delete_skill, name)
        if action == "draft":
            steps = payload.get("steps")
            return await asyncio.to_thread(skills_page.draft_from_steps, str(payload.get("goal") or ""),
                                           [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else [])
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    raise ApiError(400, "不支持的技能操作")


async def _feed_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """动态: its settings (instructions, daily, time), write now, delete a post, or ❤ / 讨论 one."""
    action = payload.get("action")
    try:
        if action == "update":
            return await asyncio.to_thread(feed.update, payload.get("changes") or {})
        if action == "write_now":
            return await asyncio.to_thread(feed.write_now)
        if action == "delete":
            return await asyncio.to_thread(feed.delete_post, str(payload.get("id") or ""))
        if action in ("like", "discussed"):
            how = "liked" if action == "like" else "discussed"
            return await asyncio.to_thread(feed.react, str(payload.get("id") or ""), how, payload.get("on", True) is not False)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    raise ApiError(400, "不支持的动态操作")


async def _chats_change(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """Manage chats: pin/archive/prune/rename/delete side chats, set per-chat model, fork, edit, regenerate, or delete messages."""
    chat_id, action = str(payload.get("id") or ""), payload.get("action")
    if action == "prune_empty":
        protected: set[str] = set(getattr(adapter, "_turns", {}).keys())
        main_id = companion_state.read_companion_state().get("chat_id")
        if main_id:
            protected.add(str(main_id))
        engine = getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)
        actors = getattr(engine, "_actors", None) if engine is not None else None
        if isinstance(actors, dict):
            for cid, actor in actors.items():
                if getattr(actor, "active_turn_id", None) is not None or getattr(actor, "_phase", "idle") != "idle":
                    protected.add(str(cid))
        with contextlib.suppress(Exception):
            for h in await asyncio.to_thread(helpers.all_helpers):
                if h.get("chat_id"):
                    protected.add(str(h["chat_id"]))
        removed = await asyncio.to_thread(adapter.store.prune_empty, protected)
        del_chat_fn = getattr(adapter, "delete_chat", None) or getattr(engine, "delete_chat", None)
        for cid in removed:
            followups.invalidate_followups(cid)
            if callable(del_chat_fn):
                await del_chat_fn(cid)
        return {"sessions": await asyncio.to_thread(adapter.store.list), "removed_count": len(removed)}

    if action == "delete" and chat_id and not await asyncio.to_thread(adapter.store.exists, chat_id):
        return {"sessions": await asyncio.to_thread(adapter.store.list)}  # already gone (deleted on another device, say)
    valid_actions = {
        "rename", "delete", "delete_message", "regenerate", "fork", "edit_message", "set_model", "pin", "archive",
    }
    if action not in valid_actions or not chat_id:
        raise ApiError(400, "找不到这个对话，或操作类型不正确")
    if not await asyncio.to_thread(adapter.store.exists, chat_id):
        status = 404 if action in {"fork", "edit_message", "delete_message", "set_model", "pin", "archive"} else 400
        raise ApiError(status, "找不到这个对话" if status == 404 else "找不到这个对话，或操作类型不正确")
    if action == "fork":
        turn_id = str(payload.get("turn_id") or payload.get("up_to_turn_id") or "").strip() or None
        title = str(payload.get("title") or "").strip() or None
        try:
            return await asyncio.to_thread(chat_branch.fork_chat, adapter, chat_id, turn_id=turn_id, title=title)
        except ValueError as exc:
            raise ApiError(getattr(exc, "status", 404), str(exc)) from None
    if action == "edit_message":
        raw_content = payload.get("content") if "content" in payload else payload.get("text")
        try:
            result = await chat_branch.edit_and_resubmit(
                adapter,
                chat_id,
                turn_id=str(payload.get("turn_id") or ""),
                content=str(raw_content or ""),
            )
            followups.invalidate_followups(chat_id)
            return result
        except chat_branch.ChatBusyError as exc:
            raise ApiError(409, str(exc)) from None
        except ValueError as exc:
            raise ApiError(getattr(exc, "status", 400), str(exc)) from None
    if action == "regenerate":  # 重新生成 the last answer
        try:
            await adapter.regenerate(chat_id, str(payload.get("turn_id") or ""))
        except ValueError as exc:
            raise ApiError(409, str(exc)) from None
        followups.invalidate_followups(chat_id)
        return {"sessions": await asyncio.to_thread(adapter.store.list)}
    if action == "delete_message":
        try:
            result = await chat_branch.delete_message(
                adapter,
                chat_id,
                turn_id=str(payload.get("turn_id") or ""),
                role=str(payload.get("role") or ""),
            )
        except chat_branch.ChatBusyError as exc:
            raise ApiError(409, str(exc)) from None
        except ValueError as exc:
            raise ApiError(getattr(exc, "status", 400), str(exc)) from None
        followups.invalidate_followups(chat_id)
        return result
    if action == "set_model":
        raw_preset = payload.get("model_preset")
        raw_effort = payload.get("reasoning_effort")
        if raw_preset is not None and not isinstance(raw_preset, str):
            raise ApiError(400, "模型预设格式不正确")
        if raw_effort is not None and not isinstance(raw_effort, str):
            raise ApiError(400, "思考强度格式不正确")
        preset_val = raw_preset.strip() or None if isinstance(raw_preset, str) else None
        effort_val = raw_effort.strip().lower() or None if isinstance(raw_effort, str) else None
        try:
            override = await asyncio.to_thread(
                models.resolve_session_override,
                {"model_preset": preset_val, "reasoning_effort": effort_val},
            )
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        await asyncio.to_thread(
            adapter.store.set_model_override,
            chat_id,
            model_preset=preset_val,
            reasoning_effort=effort_val,
        )
        engine = getattr(adapter, "engine", None) or getattr(adapter, "_engine", None)
        set_ov_fn = getattr(engine, "set_session_model_override", None)
        if callable(set_ov_fn):
            set_ov_fn(chat_id, override)
        return {
            "sessions": await asyncio.to_thread(adapter.store.list),
            "chat_id": chat_id,
            "model_preset": preset_val,
            "reasoning_effort": effort_val,
        }
    if action == "pin":
        pinned = payload.get("pinned")
        if pinned is not None and not isinstance(pinned, bool):
            raise ApiError(400, "置顶状态格式不正确")
        await asyncio.to_thread(adapter.store.set_pinned, chat_id, pinned)
        return {"sessions": await asyncio.to_thread(adapter.store.list, include_archived=True)}

    if chat_id == companion_state.read_companion_state().get("chat_id"):
        raise ApiError(409, "主要聊天不能改名、归档或删除")
    if action == "archive":
        archived = payload.get("archived")
        if archived is not None and not isinstance(archived, bool):
            raise ApiError(400, "归档状态格式不正确")
        await asyncio.to_thread(adapter.store.set_archived, chat_id, archived)
    elif action == "rename":
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ApiError(400, "名字不能为空")
        await asyncio.to_thread(adapter.store.set_title, chat_id, title)
    else:
        await asyncio.to_thread(adapter.store.delete, chat_id)
        followups.invalidate_followups(chat_id)
        del_chat_fn = getattr(adapter, "delete_chat", None) or getattr(getattr(adapter, "engine", None), "delete_chat", None)
        if callable(del_chat_fn):
            await del_chat_fn(chat_id)
    return {"sessions": await asyncio.to_thread(adapter.store.list)}


TTS_PREFIX = tts_api.TTS_PREFIX


async def _oauth_start(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await asyncio.to_thread(oauth_login.start, str(payload.get("login") or ""))
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None


async def _oauth_import(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        await asyncio.to_thread(oauth_login.import_local, str(payload.get("login") or ""))
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return {"ok": True}


async def _oauth_status(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return oauth_login.status(str(payload.get("id") or ""))
    except ValueError as exc:
        raise ApiError(404, str(exc)) from None


async def _models_vendor(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await asyncio.to_thread(model_catalog.vendor_models, str(payload.get("vendor") or ""),
                                       str(payload.get("kind") or "text"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _models_vendor_check(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    """检测: read the company's platform now, then its model list as 添加模型 shows it."""
    from .modules import model_check

    vendor_id, kind = str(payload.get("vendor") or ""), str(payload.get("kind") or "text")
    try:
        await asyncio.to_thread(model_check.check, vendor_id, kind)
        return await asyncio.to_thread(model_catalog.vendor_models, vendor_id, kind)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _models_remove(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        return await asyncio.to_thread(models.remove_preset, str(payload.get("name") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


async def _connection_custom(adapter: "CompanionAdapter", payload: dict[str, Any]) -> Any:
    try:
        last = await asyncio.to_thread(connections.add_custom, payload)
    except connections.ConnectionError_ as exc:
        raise ApiError(exc.status, str(exc)) from None
    name = str(payload.get("name") or "").strip().lower()
    try:
        reload = await connections.apply_live_async(
            engine=getattr(adapter, "engine", None),
            runner=getattr(adapter, "gateway_runner", None),
        )
    except Exception as exc:
        logger.exception("companion: MCP reload failed")
        reload = {"ok": False, "failed": [name], "message": str(exc)}
    result = await asyncio.to_thread(connections.presets_payload, last)
    failed = name in reload.get("failed", [])
    result["hot_reload"] = {**reload, "ok": not failed,
                            "message": f"{name} 没连上，检查一下地址或命令" if failed else ""}
    return result


MUTATIONS: dict[str, Mutation] = {
    "companion.models.add": _models_add,
    "companion.models.vendor": _models_vendor,
    "companion.models.check": _models_check,
    "companion.oauth.start": _oauth_start,
    "companion.chats.change": _chats_change,
    "companion.feed.change": _feed_change,
    "companion.skills.change": _skills_change,
    "companion.permissions.set": _permissions_set,
    "companion.vault.change": _vault_change,
    "companion.helpers.change": _helpers_change,
    "companion.groups.change": _groups_change,
    "companion.oauth.status": _oauth_status,
    "companion.oauth.import": _oauth_import,
    "companion.models.remove": _models_remove,
    "settings.mcp.custom": _connection_custom,
    "automation.enable": _automation_action("enable"),
    "automation.disable": _automation_action("disable"),
    "automation.delete": _automation_action("delete"),
    "automation.run": _automation_action("run"),
    "settings.mcp.enable": _connection_action("enable"),
    "settings.mcp.disable": _connection_action("disable"),
    "settings.mcp.remove": _connection_action("remove"),
    "settings.mcp.test": _connection_action("test"),
    "settings.mcp.tools": _connection_action("tools"),
    "push.subscribe": _push_subscribe,
    "push.unsubscribe": _push_unsubscribe,
    "push.test": _push_test,
    "companion.avatars.upload": _avatar_action("upload"),
    "companion.avatars.from_creation": _avatar_action("from_creation"),
    "companion.avatars.delete": _avatar_action("delete"),
    "companion.creations.hide": _creations_hide,
    "companion.creations.save": _creations_save,
    "companion.knowledge.change": _knowledge_change,
    "companion.ideas.refresh": _ideas_refresh,
    "companion.memory.forget": _memory_forget,
    "companion.memory.remember": _memory_remember,
    "companion.memory.pin": _memory_pin,
    "companion.memory.tidy": _memory_tidy,
    "companion.memory.learn": _memory_learn,
    "companion.soul.update": _soul_update,
    "companion.memory.doc": _memory_doc_update,
    "companion.goals.change": _goals_change,
    "companion.triggers.change": _triggers_change,
    "companion.devices.remove": _devices_remove,
    "companion.memory.undo": _memory_undo,
    "companion.routines.update": _routines_update,
    "companion.models.update": _models_update,
    "companion.update": _companion_update,
    "approval.decide": _approval_decide,
    "approval.grants.revoke": _grants_revoke,
}


class _WebApp(StaticFiles):
    """The built web app: pages are always revalidated (a new build must reach the phone),
    content-hashed ``/assets/`` files are cached for good."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        path = scope.get("path", "")
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def build_app(adapter: "CompanionAdapter") -> "_Compressed":
    def authorized(request: Request) -> bool:
        return adapter.tokens.valid(_bearer(request))

    async def bootstrap(request: Request) -> Response:
        if not _origin_allowed(request):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        offered = _device_token(request)
        device = await asyncio.to_thread(adapter.devices.check, offered) if offered else None
        if offered and device is None:
            # Removed elsewhere, or the server's devices were reset: pair again.
            return JSONResponse({"error": "device_unknown"}, status_code=401, headers=NO_STORE)
        if device is None and not adapter.tokens.password_ok(request.headers.get("x-nanobot-auth"),
                                                             local=_is_local(request)):
            return JSONResponse({"error": "pair_required"}, status_code=401, headers=NO_STORE)
        return JSONResponse(
            {
                "token": adapter.tokens.issue(device, reuse=True, purpose="ws"),
                "api_token": adapter.tokens.issue(device, reuse=True, purpose="api"),
                "device_id": device,
                "ws_path": WS_PATH,
                "expires_in": 24 * 60 * 60,
                "limits": {"transport": {"max_frame_bytes": MAX_FRAME_BYTES, "envelope_reserve_bytes": 64 * 1024}},
                "model_name": None,
                "runtime_surface": "browser",
                "capabilities": await asyncio.to_thread(_runtime_capabilities),
            },
            headers=NO_STORE,
        )

    async def pair(request: Request) -> Response:
        """A pairing code for a device token (see ``devices``)."""
        if not _origin_allowed(request):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        raw = await _read_body_bounded(request, 16 * 1024)
        if raw is None:
            return JSONResponse({"error": "too_large"}, status_code=413, headers=NO_STORE)
        try:
            import json as _json

            body = _json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        client_ip = _client_ip(request)
        result = await asyncio.to_thread(
            adapter.devices.pair, str(body.get("code") or ""), str(body.get("name") or ""), client_ip
        )
        if result is None:
            error = "locked" if adapter.devices.locked(client_ip) else "wrong_code"
            return JSONResponse({"error": error}, status_code=403, headers=NO_STORE)
        logger.info("Companion: paired a new device (%s)", result["device_id"])
        return JSONResponse(result, headers=NO_STORE)

    async def pair_code(request: Request) -> Response:
        """A new pairing code: for the server's own console, or a device that is already connected."""
        if not _origin_allowed(request):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        local = _is_local(request)
        if not (local or authorized(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)
        code = adapter.devices.new_code()
        # As nanoMuse's printed link: opening it pairs at once. The code rides in the fragment,
        # which browsers never send, so it stays out of every access log on the way.
        requested_base = _clean_base_url(request.query_params.get("base", "")) if local else None
        base = requested_base or _public_base(request)
        return JSONResponse({**code, "link": f"{base.rstrip('/')}/companion.html#pair={code['code']}"}, headers=NO_STORE)

    async def sessions(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw_inc = request.query_params.get("include_archived")
        include_archived = True if raw_inc is None else raw_inc.strip().lower() not in ("0", "false", "no")
        query = request.query_params.get("q") or ""
        rows = await asyncio.to_thread(adapter.store.list, include_archived=include_archived, query=query)
        return JSONResponse({"sessions": rows}, headers=NO_STORE)

    async def thread(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        chat_id = chat_id_of(request.path_params["key"])
        if not chat_id or not await asyncio.to_thread(adapter.store.exists, chat_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        # A phone keeping the chat itself asks only for what came after its copy (known/last).
        def number(name: str) -> Optional[int]:
            value = request.query_params.get(name, "")
            return int(value) if value.isdigit() else None
        payload = await asyncio.to_thread(
            adapter.store.thread_payload, chat_id, known=number("known"), last=number("last")
        )
        # Whether a turn is running right now, from the gateway itself: a turn cut off by a restart
        # never gets its turn_end in the log, and an app that missed the turn_end (asleep, offline)
        # must not keep showing it at work.
        payload["active_turn_id"] = adapter._turns.get(chat_id)
        payload["has_pending_tool_calls"] = chat_id in adapter._turns
        # The page asks again after every reply with the copy it has (If-None-Match): unchanged,
        # it keeps that one instead of the whole chat again. Whether a turn runs is part of it.
        payload["revision"] = f"{payload['revision']}.{payload['active_turn_id'] or '-'}"
        if request.headers.get("if-none-match", "").strip().removeprefix("W/").strip('"') == payload["revision"]:
            return Response(status_code=304, headers={**NO_STORE, "ETag": f'"{payload["revision"]}"'})
        return JSONResponse(payload, headers={**NO_STORE, "ETag": f'"{payload["revision"]}"'})

    async def session_export(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)
        chat_id = chat_id_of(request.path_params["key"])
        if not chat_id or not adapter.store.exists(chat_id):
            return JSONResponse({"error": "not found"}, status_code=404, headers=NO_STORE)
        fmt = (request.query_params.get("format") or "md").strip().lower()
        if fmt not in ("md", "json"):
            return JSONResponse({"error": "不支持的导出格式，仅支持 md 或 json"}, status_code=400, headers=NO_STORE)
        exported = await asyncio.to_thread(chat_export_module.export_chat, adapter.store, chat_id)
        if fmt == "json":
            return JSONResponse(exported, headers=NO_STORE)
        from urllib.parse import quote

        filename = chat_export_module.export_filename(exported["title"], chat_id, "md")
        ascii_name = f"chat-{chat_id[:8]}.md"
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
        return Response(
            exported["markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={**NO_STORE, "Content-Disposition": disposition},
        )

    async def session_followups(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)
        chat_id = chat_id_of(request.path_params["key"])
        if not chat_id or not await asyncio.to_thread(adapter.store.exists, chat_id):
            return JSONResponse({"error": "not found"}, status_code=404, headers=NO_STORE)
        turn_id = (request.query_params.get("turn_id") or "").strip() or None
        payload = await followups.get_followups(adapter.store, chat_id, turn_id=turn_id)
        return JSONResponse(payload, headers=NO_STORE)

    async def session_context(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)
        chat_id = chat_id_of(request.path_params["key"])
        if not chat_id or not await asyncio.to_thread(adapter.store.exists, chat_id):
            return JSONResponse({"error": "not found"}, status_code=404, headers=NO_STORE)
        payload = await asyncio.to_thread(chat_context.get_session_context, adapter, chat_id)
        return JSONResponse(payload, headers=NO_STORE)

    async def companion(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(await asyncio.to_thread(_companion_payload), headers=NO_STORE)

    async def approvals(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.query_params.get("all") == "1":
            # 审核记录 on the companion's page: every chat's, newest first.
            records = await asyncio.to_thread(adapter.approvals.store.list_all)
        else:
            records = adapter.approvals.store.list_for_session(request.query_params.get("session_key", ""))
        return JSONResponse({"approvals": [record.public() for record in records]}, headers=NO_STORE)

    async def chat_search(request: Request) -> Response:
        """查找聊天记录: messages in the user's chats that say the words (newest first)."""
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        qp = request.query_params
        query = qp.get("q", "")[:100]
        main = companion_state.read_companion_state().get("chat_id")
        target_chat = (qp.get("chat_id") or "").strip() or None
        role_filter = (qp.get("role") or "").strip() or None
        scope = (qp.get("scope") or "all").strip() or "all"
        raw_limit = (qp.get("limit") or "").strip()
        try:
            limit = int(raw_limit) if raw_limit else 30
        except ValueError:
            limit = 30
        payload = await asyncio.to_thread(
            chat_search_module.search,
            adapter.store,
            query,
            main,
            chat_id=target_chat,
            role=role_filter,
            scope=scope,
            limit=limit,
        )
        return JSONResponse(payload, headers=NO_STORE)

    async def tts_synthesize(request: Request) -> Response:
        """按需神经语音朗读：清洗文本、本地 SHA-256 缓存并通过安全签名媒体 URL 返回。"""
        if not _origin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        if not authorized(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401, headers=NO_STORE)
        raw = await _read_body_bounded(request, 64 * 1024)
        if raw is None:
            return JSONResponse({"ok": False, "error": "朗读文本过大"}, status_code=413, headers=NO_STORE)
        try:
            import json as _json

            body = _json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"ok": False, "error": "请求格式不正确"}, status_code=400, headers=NO_STORE)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "请求格式不正确"}, status_code=400, headers=NO_STORE)
        try:
            result = await asyncio.to_thread(tts_api.synthesize, adapter, body)
        except tts_api.TtsError as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status, headers=NO_STORE)
        except Exception:
            logger.exception("companion: tts synthesize failed")
            return JSONResponse({"ok": False, "error": "语音生成失败，请稍后再试"}, status_code=500, headers=NO_STORE)
        return JSONResponse(result, headers=NO_STORE)

    async def tts_file(request: Request) -> Response:
        rel = adapter.media.verify(TTS_PREFIX, request.path_params["sig"], request.path_params["payload"])
        path = child_file(tts_api.audio_cache_dir(), rel) if rel else None
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            path,
            media_type="audio/mpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    async def automation_run(request: Request) -> Response:
        """One scheduled run's report (运行记录 → a run)."""
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            report = await asyncio.to_thread(automations.run_report, request.path_params["job_id"], request.path_params["run_id"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse(report, headers=NO_STORE)

    async def media(request: Request) -> Response:
        # The signature is the permission: <img>/<video> tags can't send a bearer token.
        path = adapter.media.resolve(request.path_params["sig"], request.path_params["payload"])
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    def guarded(read: Callable[[], Any]) -> Callable[[Request], Awaitable[Response]]:
        """A signed-in GET whose body is ``read()`` (run off the event loop)."""
        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return JSONResponse(await asyncio.to_thread(read), headers=NO_STORE)
        return endpoint

    # 自动触发: other services call in with the trigger's own key (no sign-in), and Hermes's monitor
    # reads a trigger's state the same way (see modules/triggers).
    async def hook(request: Request) -> Response:
        raw = await _read_body_bounded(request, _HOOK_STREAM_LIMIT, keep_bytes=_HOOK_BODY_CHARS)
        if raw is None:
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:  # some Chinese services and Windows tools still send GBK
            body = raw.decode("gb18030", errors="replace")
        ok = await asyncio.to_thread(triggers.receive, request.path_params["trigger_id"],
                                     request.query_params.get("key"), body)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    async def trigger_state(request: Request) -> Response:
        try:
            text = await asyncio.to_thread(triggers.state, request.path_params["trigger_id"],
                                           request.query_params.get("key"))
        except Exception as exc:  # the monitor records a failed check, not a change
            return Response(f"check failed: {exc}", status_code=502)
        if text is None:
            return Response("not found", status_code=404)
        return Response(text, media_type="text/plain; charset=utf-8", headers=NO_STORE)

    def usage() -> Any:
        return models.usage_summary(companion_state.read_companion_state().get("timezone"))

    async def ideas_view(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return JSONResponse(await ideas.get_ideas(_companion_events(adapter), refresh=False), headers=NO_STORE)
        except Exception as exc:
            return JSONResponse({"error": str(exc) or "没有想出合适的建议，稍后再试"}, status_code=503)

    async def knowledge_view(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        query = (request.query_params.get("q") or "").strip() or None
        collection = (request.query_params.get("collection") or "").strip() or None
        payload = await asyncio.to_thread(knowledge.list_knowledge, collection=collection, query=query)
        return JSONResponse(payload, headers=NO_STORE)

    async def knowledge_upload(request: Request) -> Response:
        if not _origin_allowed(request):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)

        from urllib.parse import unquote

        ctype = (request.headers.get("content-type") or "").strip()
        is_multipart = ctype.lower().startswith("multipart/form-data")
        read_limit = knowledge.MAX_DOCUMENT_BYTES + (256 * 1024 if is_multipart else 0)
        raw = await _read_body_bounded(request, read_limit)
        if raw is None:
            return JSONResponse({"error": "文件大小超过 20 MB 上限"}, status_code=413, headers=NO_STORE)
        if not raw:
            return JSONResponse({"error": "上传内容为空"}, status_code=400, headers=NO_STORE)

        qp = request.query_params
        filename = unquote(qp.get("filename") or request.headers.get("x-filename") or "").strip()
        collection = (
            unquote(qp.get("collection") or request.headers.get("x-collection") or "").strip()
            or knowledge.DEFAULT_COLLECTION
        )
        title = unquote(qp.get("title") or request.headers.get("x-title") or "").strip() or None
        file_bytes = raw

        if is_multipart:
            from starlette.datastructures import UploadFile
            from starlette.formparsers import MultiPartParser

            async def _raw_stream():
                yield raw

            form = None
            try:
                parser = MultiPartParser(
                    request.headers,
                    _raw_stream(),
                    max_part_size=knowledge.MAX_DOCUMENT_BYTES,
                )
                form = await parser.parse()
                upload_item = form.get("file")
                if not isinstance(upload_item, UploadFile):
                    upload_item = next(
                        (val for _, val in form.multi_items() if isinstance(val, UploadFile)),
                        None,
                    )
                if not isinstance(upload_item, UploadFile):
                    return JSONResponse({"error": "缺少上传文件字段 file"}, status_code=400, headers=NO_STORE)
                file_bytes = await upload_item.read()
                if len(file_bytes) > knowledge.MAX_DOCUMENT_BYTES:
                    return JSONResponse({"error": "文件大小超过 20 MB 上限"}, status_code=413, headers=NO_STORE)
                form_filename = str(form.get("filename") or "").strip()
                filename = form_filename or str(upload_item.filename or "").strip() or filename
                form_collection = str(form.get("collection") or "").strip()
                if form_collection:
                    collection = form_collection
                form_title = str(form.get("title") or "").strip()
                if form_title:
                    title = form_title
            except Exception as exc:
                status = 413 if "exceeded" in str(exc).lower() else 400
                return JSONResponse({"error": f"上传表单格式不正确：{type(exc).__name__}"}, status_code=status, headers=NO_STORE)
            finally:
                if form is not None:
                    await form.close()

        if not filename:
            return JSONResponse({"error": "缺少文件名（filename）"}, status_code=400, headers=NO_STORE)
        try:
            added = await asyncio.to_thread(
                knowledge.ingest_bytes,
                filename,
                file_bytes,
                title=title,
                collection=collection,
                origin="upload",
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=NO_STORE)

        listing = await asyncio.to_thread(knowledge.list_knowledge)
        return JSONResponse({**listing, "added": added}, headers=NO_STORE)

    async def creation_file(request: Request) -> Response:
        # Signed links: <img>/<video> and the save button can't send a bearer token.
        rel = adapter.media.verify(CREATIONS_PREFIX, request.path_params["sig"], request.path_params["payload"])
        path = child_file(creations.creations_root(), rel) if rel else None
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        headers = {"Cache-Control": "private, max-age=3600"}
        if request.query_params.get("thumb") and creations.kind_of(path.name) == "image":
            # 资源库's grid: a small copy, not the original.
            return FileResponse(await asyncio.to_thread(creations.thumbnail, path), headers=headers)
        if request.query_params.get("download"):
            from urllib.parse import quote

            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"
        return FileResponse(path, headers=headers)

    async def avatar_file(request: Request) -> Response:
        rel = adapter.media.verify(AVATARS_PREFIX, request.path_params["sig"], request.path_params["payload"])
        path = child_file(avatars.avatars_root(), rel) if rel else None
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    async def push_config(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not push.available():
            return JSONResponse({"error": "通知功能还没装好"}, status_code=503)
        payload = await asyncio.to_thread(lambda: {"public_key": push.public_key(), "devices": len(push.list_subscriptions())})
        return JSONResponse(payload, headers=NO_STORE)

    async def widget_page(request: Request) -> Response:
        # Opened by the app's sandboxed frame: the signature is the permission.
        name = adapter.media.verify(widgets.PREFIX, request.path_params["sig"], request.path_params["payload"])
        body = await asyncio.to_thread(widgets.page, name) if name else None
        if body is None:
            return Response("not found", status_code=404)
        return Response(body, media_type="text/html; charset=utf-8", headers={
            "Content-Security-Policy": widgets.POLICY, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cache-Control": "private, max-age=3600"})

    async def root(_request: Request) -> Response:
        return RedirectResponse("/companion.html")

    async def attachment(request: Request) -> Response:
        """A file the app sends with a message (any kind): kept briefly until the message names it."""
        if not _origin_allowed(request):
            return JSONResponse({"error": "forbidden_origin"}, status_code=403, headers=NO_STORE)
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower() or "application/octet-stream"
        body = await _read_body_bounded(request, _ATTACHMENT_LIMIT)
        if not body:
            return JSONResponse({"error": "too_large" if body is None else "empty"}, status_code=413 if body is None else 400)
        from urllib.parse import unquote

        name = unquote(request.headers.get("x-attachment-name") or "")[:120]
        reference = await asyncio.to_thread(adapter.keep_attachment, body, mime, name)
        return JSONResponse({"reference": reference}, headers=NO_STORE)

    async def websocket(socket: WebSocket) -> None:
        if not _origin_allowed(socket):
            await socket.close(code=4403)
            return
        token = socket.query_params.get("token")
        if not adapter.tokens.valid(token):
            await socket.close(code=4401)
            return
        await socket.accept()
        adapter.socket_devices[socket] = adapter.tokens.device_of(token)
        # A fresh id for legacy single-chat clients; it only becomes a chat once used. ``upload``:
        # where the app posts a picture before sending it (signed with this connection's token).
        await socket.send_json({"event": "ready", "chat_id": str(uuid.uuid4()),
                                "upload": {"path": "/api/attachments", "token": token}})
        try:
            while True:
                frame = await socket.receive_json()
                if adapter_debug():
                    logger.warning("Companion frame <- %s", str(frame)[:160])
                if isinstance(frame, dict):
                    await _handle_frame(adapter, socket, frame)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            adapter.detach_all(socket)
            adapter.socket_devices.pop(socket, None)

    async def computer_socket(socket: WebSocket) -> None:
        if not _origin_allowed(socket):
            await socket.close(code=4403)
            return
        token = socket.query_params.get("token")
        if not adapter.tokens.valid(token):
            await socket.close(code=4401)
            return
        await socket.accept()
        adapter.socket_devices[socket] = adapter.tokens.device_of(token)
        try:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await computer.stream(socket, adapter.browser_done)
        finally:
            adapter.socket_devices.pop(socket, None)

    routes = [
        Route("/", root),
        WebSocketRoute("/ws/computer", computer_socket),
        Route("/api/webui/companion/computer", guarded(computer.status)),
        Route("/webui/bootstrap", bootstrap),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/api/pair/code", pair_code, methods=["POST"]),
        Route("/api/webui/devices", guarded(lambda: {"devices": adapter.devices.list()})),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{key}/webui-thread", thread),
        Route("/api/sessions/{key}/export", session_export),
        Route("/api/sessions/{key}/followups", session_followups),
        Route("/api/sessions/{key}/context", session_context),
        Route("/api/webui/companion", companion),
        Route("/api/webui/approvals", approvals),
        Route("/api/webui/companion/search", chat_search),
        Route("/api/webui/companion/tts", tts_synthesize, methods=["POST"]),
        Route(TTS_PREFIX + "/{sig}/{payload}", tts_file),
        Route("/api/webui/approvals/grants", guarded(lambda: _grants(adapter))),
        Route("/api/webui/companion/models", guarded(models.models_payload)),
        Route("/api/webui/companion/model-catalog", guarded(lambda: model_catalog.catalog_payload(models.saved_key_urls()))),
        Route("/api/webui/companion/usage", guarded(usage)),
        Route("/api/webui/companion/routines", guarded(routines.read_routines)),
        Route("/api/webui/companion/memory", guarded(memory.read_memory)),
        Route("/api/webui/companion/memory-doc", guarded(memory.read_memory_doc)),
        Route("/api/webui/companion/soul", guarded(_soul_payload)),
        Route("/api/webui/companion/goals", guarded(plans.list_goals)),
        Route("/api/webui/companion/triggers", guarded(lambda: {"triggers": triggers.list_triggers()})),
        Route("/api/webui/companion/feed", guarded(feed.payload)),
        Route("/api/webui/companion/skills", guarded(skills_page.list_skills)),
        Route("/api/webui/companion/permissions", guarded(permissions.payload)),
        Route("/api/webui/companion/vault", guarded(vault.payload)),
        Route("/api/webui/companion/helpers", guarded(lambda: helpers.page(adapter.store, set(adapter._turns)))),
        Route("/api/webui/companion/triggers/{trigger_id}/state", trigger_state),
        Route("/hooks/{trigger_id}", hook, methods=["POST"]),
        Route("/api/webui/companion/ideas", ideas_view),
        Route("/api/webui/companion/knowledge", knowledge_view),
        Route("/api/webui/companion/knowledge/upload", knowledge_upload, methods=["POST"]),
        Route("/api/webui/companion/creations", guarded(lambda: _creations(adapter))),
        Route(CREATIONS_PREFIX + "/{sig}/{payload}", creation_file),
        Route("/api/webui/companion/avatars", guarded(lambda: _avatars(adapter))),
        Route("/api/settings/mcp-presets", guarded(connections.presets_payload)),
        Route("/api/webui/automations", guarded(automations.list_automations)),
        Route("/api/webui/automations/{job_id}/runs/{run_id}", automation_run),
        Route("/api/webui/push", push_config),
        Route(AVATARS_PREFIX + "/{sig}/{payload}", avatar_file),
        Route(widgets.PREFIX + "/{sig}/{payload}", widget_page),
        Route("/api/media/{sig}/{payload}", media),
        Route("/api/attachments", attachment, methods=["POST"]),
        WebSocketRoute(WS_PATH, websocket),
    ]
    if adapter.web_dist.is_dir():
        routes.append(Mount("/", app=_WebApp(directory=str(adapter.web_dist), html=True), name="web"))
    else:
        logger.warning("Companion: web app folder not found: %s", adapter.web_dist)
    return _Compressed(Starlette(routes=routes))


class _Compressed:
    """Gzip for the app's code and data (scripts, styles, pages, JSON: about a third of the size over
    a phone's connection); pictures, videos, audio and sockets pass through untouched."""

    _TEXT = (".js", ".css", ".html", ".json", ".svg", ".webmanifest")

    def __init__(self, app: Any) -> None:
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        text = (
            path == "/"
            or path.endswith(self._TEXT)
            or (path.startswith("/api/") and not path.startswith(("/api/media/", TTS_PREFIX + "/")))
        )
        await (self.gzip if text else self.app)(scope, receive, send)


async def _handle_frame(adapter: "CompanionAdapter", socket: WebSocket, frame: dict[str, Any]) -> None:
    kind = frame.get("type")
    if kind == "new_chat":
        chat_id = adapter.store.create()
        adapter.attach(chat_id, socket)
        await socket.send_json({"event": "attached", "chat_id": chat_id})
    elif kind == "fork_chat":
        src_chat_id = str(frame.get("source_chat_id") or frame.get("chat_id") or "").strip()
        up_to_turn_id = str(frame.get("up_to_turn_id") or frame.get("turn_id") or "").strip() or None
        raw_before = frame.get("before_user_index")
        before_idx = int(raw_before) if isinstance(raw_before, int) and not isinstance(raw_before, bool) else None
        title = str(frame.get("title") or "").strip() or None
        try:
            forked = await asyncio.to_thread(
                chat_branch.fork_chat,
                adapter,
                src_chat_id,
                turn_id=up_to_turn_id,
                title=title,
                before_user_index=before_idx,
            )
        except ValueError as exc:
            await socket.send_json({"event": "error", "detail": "fork_failed", "reason": str(exc), "chat_id": src_chat_id})
            return
        adapter.attach(forked["chat_id"], socket)
        await socket.send_json({
            "event": "chat_forked",
            "chat_id": forked["chat_id"],
            "source_chat_id": src_chat_id,
            "key": forked["key"],
            "sessions": forked["sessions"],
        })
    elif kind == "attach":
        chat_id = str(frame.get("chat_id") or "")
        if chat_id:
            adapter.attach(chat_id, socket)
            await socket.send_json({"event": "attached", "chat_id": chat_id})
            await adapter.push_goal_state(chat_id, socket)
    elif kind == "message":
        chat_id = str(frame.get("chat_id") or "")
        text = str(frame.get("content") or "")
        # Pictures uploaded just before (/api/attachments), named by their reference.
        refs = [m.get("reference") for m in frame.get("media") or [] if isinstance(m, dict)]
        attachments = [a for a in (adapter.take_attachment(r) for r in refs if isinstance(r, str)) if a]
        if not chat_id or not (text.strip() or attachments):
            return
        adapter.attach(chat_id, socket)
        turn_id = frame.get("turn_id") if isinstance(frame.get("turn_id"), str) else None
        await socket.send_json({"event": "message_accepted", "chat_id": chat_id, "turn_id": turn_id})
        asyncio.create_task(adapter.submit(chat_id, text, turn_id, attachments))
    elif kind == "webui_request":
        await _handle_mutation(adapter, socket, frame)
    elif kind == "transcribe_audio":
        asyncio.create_task(_transcribe(socket, frame))


_ATTACHMENT_LIMIT = 20 * 1024 * 1024

_NOT_CONFIGURED = ("No STT provider", "STT is disabled", "not installed", "No installed local STT")
_AUDIO_SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
                 "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac"}


async def _transcribe(socket: WebSocket, frame: dict[str, Any]) -> None:
    """Voice input: the recorded clip (a data URL) to text with Hermes's speech recognition."""
    import base64
    import re as _re
    import tempfile

    request_id = frame.get("request_id")
    match = _re.match(r"^data:([\w/.+-]+)(?:;[^,]*)?;base64,(.*)$", str(frame.get("data_url") or ""), _re.S)

    async def reply(event: dict[str, Any]) -> None:
        try:
            await socket.send_json({**event, "request_id": request_id})
        except Exception:
            pass

    if not match:
        await reply({"event": "transcription_error", "detail": "failed"})
        return
    mime = match.group(1).split(";")[0].lower()
    try:
        audio = base64.b64decode(match.group(2))
    except ValueError:
        await reply({"event": "transcription_error", "detail": "failed"})
        return

    def run() -> dict[str, Any]:
        from muse.media.stt import transcribe_audio

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"voice{_AUDIO_SUFFIX.get(mime, '.webm')}"
            path.write_bytes(audio)
            return transcribe_audio(str(path), source="gateway")

    try:
        result = await asyncio.to_thread(run)
    except Exception:
        logger.exception("companion: transcription failed")
        result = {"success": False, "error": ""}
    if result.get("success") and str(result.get("transcript") or "").strip():
        await reply({"event": "transcription_result", "text": str(result["transcript"]).strip()})
        return
    error = str(result.get("error") or "")
    detail = "not_configured" if error.startswith(_NOT_CONFIGURED) or "not installed" in error else "failed"
    await reply({"event": "transcription_error", "detail": detail})


async def _handle_mutation(adapter: "CompanionAdapter", socket: WebSocket, frame: dict[str, Any]) -> None:
    request_id = frame.get("request_id")
    handler = MUTATIONS.get(str(frame.get("action") or ""))
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    try:
        if handler is None:
            raise ApiError(404, f"不支持的请求操作：{frame.get('action')}")
        result = await handler(adapter, payload)
        await socket.send_json({"event": "webui_response", "request_id": request_id, "ok": True, "result": result})
    except ApiError as exc:
        await socket.send_json({
            "event": "webui_response", "request_id": request_id, "ok": False,
            "error": {"status": exc.status, "message": exc.message},
        })
    except Exception as exc:
        logger.error("companion: mutation %s failed (%s)", frame.get("action"), type(exc).__name__, exc_info=True)
        await socket.send_json({
            "event": "webui_response", "request_id": request_id, "ok": False,
            "error": {"status": 500, "message": "服务器处理请求时出错了，请稍后再试"},
        })
