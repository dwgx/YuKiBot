"""WebUI 聊天消息格式化辅助函数。

从 core/webui.py 拆分。包含:
- OneBot 消息段解析和格式化
- 会话联系人信息提取
- 消息渲染和 CQ 码解析
- 媒体相关工具函数
"""
from __future__ import annotations

import base64
import contextlib
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from utils.text import clip_text, normalize_text

from fastapi import HTTPException
from core.napcat_compat import build_napcat_diagnostics, call_napcat_bot_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
)

# Module-level state — injected by webui.py at init time
_engine: Any = None
_GROUP_ROLE_CACHE: dict[str, tuple[float, str]] = {}
_GROUP_ROLE_CACHE_OK_TTL_SECONDS = 30
_GROUP_ROLE_CACHE_MISS_TTL_SECONDS = 300


def _unwrap_onebot_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "data" in payload and ("retcode" in payload or "status" in payload):
            return payload.get("data")
    return payload


def _normalize_chat_type(value: str) -> str:
    raw = normalize_text(str(value)).lower()
    if raw in {"group", "group_chat", "2", "grp"}:
        return "group"
    if raw in {"private", "friend", "dm", "1", "single"}:
        return "private"
    return ""


async def _get_onebot_runtime(bot_id: str = "") -> Any:
    try:
        import nonebot
    except Exception as exc:
        raise HTTPException(503, f"NoneBot 不可用: {exc}") from exc

    bots = nonebot.get_bots()
    if not isinstance(bots, dict) or not bots:
        raise HTTPException(503, "未检测到在线 OneBot 实例")

    prefer_id = normalize_text(str(bot_id))
    if prefer_id and prefer_id in bots:
        return bots[prefer_id]
    for _, bot in bots.items():
        return bot
    raise HTTPException(503, "未检测到在线 OneBot 实例")


async def _onebot_call(api: str, *, bot_id: str = "", **kwargs: Any) -> Any:
    bot = await _get_onebot_runtime(bot_id=bot_id)
    try:
        payload = await call_napcat_bot_api(bot, api, **kwargs)
    except Exception as exc:
        tail = clip_text(normalize_text(str(exc)), 220)
        raise HTTPException(502, f"调用 {api} 失败: {tail}") from exc
    return _unwrap_onebot_payload(payload)


def _count_registered_napcat_tools() -> int:
    reg = getattr(_engine, "agent_tool_registry", None)
    schemas = getattr(reg, "_schemas", {})
    if not isinstance(schemas, dict):
        return 0
    return sum(1 for schema in schemas.values() if getattr(schema, "category", "") == "napcat")


async def _collect_napcat_status(bot_id: str = "") -> dict[str, Any]:
    resolved_bot_id = normalize_text(bot_id)
    errors: dict[str, str] = {}

    try:
        bot = await _get_onebot_runtime(bot_id=resolved_bot_id)
    except HTTPException as exc:
        diagnostics = build_napcat_diagnostics(
            status_payload={},
            version_payload={},
            bot_id=resolved_bot_id,
        )
        diagnostics["availability"] = {
            "onebot_connected": False,
            "status_api_ok": False,
            "version_api_ok": False,
        }
        diagnostics["errors"] = {"runtime": normalize_text(str(exc.detail))}
        diagnostics["integration"] = {
            "registered_napcat_tools": _count_registered_napcat_tools(),
            "webui_diagnostics": True,
        }
        return diagnostics

    status_payload: Any = {}
    version_payload: Any = {}

    try:
        status_payload = _unwrap_onebot_payload(await call_napcat_bot_api(bot, "get_status"))
    except Exception as exc:
        errors["get_status"] = clip_text(normalize_text(str(exc)), 220)

    try:
        version_payload = _unwrap_onebot_payload(await call_napcat_bot_api(bot, "get_version_info"))
    except Exception as exc:
        errors["get_version_info"] = clip_text(normalize_text(str(exc)), 220)

    diagnostics = build_napcat_diagnostics(
        status_payload=status_payload,
        version_payload=version_payload,
        bot_self_id=normalize_text(str(getattr(bot, "self_id", ""))),
        bot_id=resolved_bot_id or normalize_text(str(getattr(bot, "self_id", ""))),
    )
    diagnostics["availability"] = {
        "onebot_connected": True,
        "status_api_ok": "get_status" not in errors,
        "version_api_ok": "get_version_info" not in errors,
    }
    diagnostics["errors"] = errors
    diagnostics["integration"] = {
        "registered_napcat_tools": _count_registered_napcat_tools(),
        "webui_diagnostics": True,
    }
    return diagnostics


def _render_message_text(raw_message: Any, segments: Any) -> str:
    seg_list = segments if isinstance(segments, list) else []

    # NapCat 某些接口会直接把消息段数组塞到 raw_message。
    if not seg_list and isinstance(raw_message, list):
        seg_list = raw_message

    text = ""
    if isinstance(raw_message, str):
        text = normalize_text(raw_message)
        if text and "[CQ:" in text and not seg_list:
            parsed_segments = _normalize_message_segments(text)
            if parsed_segments:
                parsed_text = _render_message_text("", parsed_segments)
                if parsed_text:
                    return parsed_text
    elif raw_message is not None and not isinstance(raw_message, (list, dict)):
        text = normalize_text(str(raw_message))
    elif isinstance(raw_message, dict):
        text = (
            normalize_text(str(raw_message.get("raw_message", "")))
            or normalize_text(str(raw_message.get("text", "")))
            or normalize_text(str(raw_message.get("content", "")))
            or normalize_text(str(raw_message.get("summary", "")))
            or normalize_text(str(raw_message.get("msg", "")))
        )
        if not seg_list:
            nested = (
                raw_message.get("message")
                or raw_message.get("segments")
                or raw_message.get("msgSegs")
                or raw_message.get("lastMsgSegs")
                or raw_message.get("lastestMsg")
            )
            if isinstance(nested, list):
                seg_list = nested
    if text:
        return text
    if not isinstance(seg_list, list):
        return ""
    parts: list[str] = []
    for seg in seg_list:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        data = seg.get("data", {}) or {}
        if seg_type == "text":
            part = normalize_text(str(data.get("text", "")))
            if part:
                parts.append(part)
        elif seg_type in {"image", "video", "record", "audio", "file"}:
            parts.append(f"[{seg_type}]")
        elif seg_type == "at":
            qq = normalize_text(str(data.get("qq", "")))
            parts.append(f"@{qq or 'someone'}")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    rendered = normalize_text(" ".join(parts))
    if rendered:
        return rendered
    if seg_list:
        return "[消息]"
    return ""


def _recent_contact_chat_type(item: dict[str, Any]) -> str:
    raw = (
        item.get("chatType")
        or item.get("chat_type")
        or item.get("conversationType")
        or item.get("type")
    )
    lowered = normalize_text(str(raw)).lower()
    if lowered in {"2", "group", "group_chat", "grp"}:
        return "group"
    with contextlib.suppress(Exception):
        if int(raw) == 2:
            return "group"
    return "private"


def _recent_contact_peer_id(item: dict[str, Any]) -> str:
    direct = (
        item.get("peerUin")
        or item.get("peerUid")
        or item.get("peer_id")
        or item.get("peerId")
        or item.get("peerID")
        or item.get("target_id")
        or item.get("targetId")
        or item.get("group_id")
        or item.get("user_id")
        or item.get("uin")
    )
    peer_id = normalize_text(str(direct))
    if peer_id:
        return peer_id
    peer_obj = item.get("peer", {})
    if isinstance(peer_obj, dict):
        nested = (
            peer_obj.get("uin")
            or peer_obj.get("uid")
            or peer_obj.get("id")
            or peer_obj.get("peerUin")
            or peer_obj.get("peerUid")
        )
        return normalize_text(str(nested))
    return ""


def _recent_contact_peer_name(item: dict[str, Any], peer_id: str) -> str:
    return (
        normalize_text(str(item.get("peerName", "")))
        or normalize_text(str(item.get("peer_name", "")))
        or normalize_text(str(item.get("remark", "")))
        or normalize_text(str(item.get("nickname", "")))
        or normalize_text(str(item.get("groupName", "")))
        or normalize_text(str(item.get("name", "")))
        or normalize_text(str(item.get("displayName", "")))
        or peer_id
    )


def _recent_contact_int(item: dict[str, Any], keys: list[str]) -> int:
    for key in keys:
        if key not in item:
            continue
        with contextlib.suppress(Exception):
            return int(item.get(key) or 0)
    return 0


def _recent_contact_last_message(item: dict[str, Any]) -> str:
    raw_candidate = (
        item.get("lastMsg")
        or item.get("lastestMsg")
        or item.get("last_message")
        or item.get("lastMessage")
        or item.get("msgPreview")
        or item.get("preview")
        or item.get("msg")
    )
    seg_candidate = (
        item.get("lastMsgSegs")
        or item.get("lastestMsgSegs")
        or item.get("last_message_segments")
        or item.get("lastMessageSegs")
        or item.get("msgSegs")
        or item.get("segments")
    )
    text = _render_message_text(raw_candidate, seg_candidate)
    if text:
        return text

    msg_record = item.get("msgRecord")
    if isinstance(msg_record, dict):
        text = _render_message_text(
            msg_record.get("raw_message", "") or msg_record.get("text", "") or msg_record.get("msg", ""),
            msg_record.get("message", []) or msg_record.get("segments", []),
        )
        if text:
            return text
    if raw_candidate is not None:
        if isinstance(raw_candidate, list) and raw_candidate:
            return "[消息]"
        if isinstance(raw_candidate, dict):
            guessed_type = normalize_text(str(raw_candidate.get("type", ""))).lower()
            if guessed_type:
                return f"[{guessed_type}]"
    return ""


def _format_chat_message_item(item: dict[str, Any], *, bot_self_id: str) -> dict[str, Any]:
    sender = item.get("sender", {}) if isinstance(item, dict) else {}
    if not isinstance(sender, dict):
        sender = {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    sender_name = (
        normalize_text(str(sender.get("card", "")))
        or normalize_text(str(sender.get("nickname", "")))
        or sender_id
    )
    role = normalize_text(str(sender.get("role", ""))).lower()
    segments = _normalize_message_segments(item.get("message"))
    if not segments:
        segments = _normalize_message_segments(item.get("segments"))
    if not segments:
        segments = _normalize_message_segments(item.get("raw_message"))
    text = _render_message_text(item.get("raw_message", ""), segments)
    ts = int(item.get("time", 0) or 0)
    return {
        "message_id": normalize_text(str(item.get("message_id", "") or item.get("real_id", "") or item.get("id", ""))),
        "seq": normalize_text(str(item.get("message_seq", "") or item.get("real_seq", ""))),
        "timestamp": ts,
        "time_iso": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts > 0 else ""),
        "sender_id": sender_id,
        "sender_name": sender_name or "未知用户",
        "sender_role": role,
        "is_self": bool(sender_id and sender_id == normalize_text(bot_self_id)),
        "is_essence": False,
        "is_recalled": False,
        "recalled_at": 0,
        "recalled_source": "",
        "text": text,
        "segments": segments,
    }


async def _resolve_group_bot_role(group_id: int, *, bot_id: str = "") -> str:
    """查询当前机器人在群内的角色（owner/admin/member）。"""
    if group_id <= 0:
        return ""

    cache_key = f"{normalize_text(bot_id) or '-'}:{int(group_id)}"
    cached = _GROUP_ROLE_CACHE.get(cache_key)
    if cached:
        expires_at, cached_role = cached
        if time.time() < float(expires_at):
            return cached_role
        _GROUP_ROLE_CACHE.pop(cache_key, None)

    bot = await _get_onebot_runtime(bot_id=bot_id)
    self_id = normalize_text(str(getattr(bot, "self_id", "")))
    if not self_id.isdigit():
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + 60, "")
        return ""
    try:
        info = await _onebot_call(
            "get_group_member_info",
            bot_id=bot_id,
            group_id=int(group_id),
            user_id=int(self_id),
            no_cache=True,
        )
    except Exception as exc:
        err_text = normalize_text(str(exc))
        err_lower = err_text.lower()
        miss_ttl = 60
        if ("成员" in err_text and "不存在" in err_text) or (
            "member" in err_lower and ("not exists" in err_lower or "not exist" in err_lower or "not found" in err_lower)
        ):
            # 机器人不在群里时，前端轮询会频繁命中该接口；加长负缓存避免刷屏日志。
            miss_ttl = _GROUP_ROLE_CACHE_MISS_TTL_SECONDS
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + miss_ttl, "")
        return ""
    if not isinstance(info, dict):
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + 60, "")
        return ""
    role = normalize_text(str(info.get("role", ""))).lower()
    ttl = _GROUP_ROLE_CACHE_OK_TTL_SECONDS if role else 60
    _GROUP_ROLE_CACHE[cache_key] = (time.time() + ttl, role)
    return role


async def _resolve_group_essence_message_ids(group_id: int, *, bot_id: str = "") -> set[str]:
    try:
        raw = await _onebot_call("get_essence_msg_list", bot_id=bot_id, group_id=int(group_id))
    except Exception:
        return set()
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return set()

    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("message_id", "msg_id", "id", "messageId"):
            value = normalize_text(str(item.get(key, "")))
            if value:
                ids.add(value)
        for key in ("message_seq", "msg_seq", "seq"):
            value = normalize_text(str(item.get(key, "")))
            if value:
                ids.add(f"seq:{value}")
    return ids


def _chat_message_item_key(item: dict[str, Any]) -> str:
    message_id = normalize_text(str(item.get("message_id", "")))
    if message_id:
        return message_id
    seq = normalize_text(str(item.get("seq", "")))
    return f"seq:{seq}" if seq else ""


def _unwrap_onebot_message_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and (
            data.get("message_id")
            or data.get("real_id")
            or data.get("message")
            or data.get("raw_message")
        ):
            return data
        return raw
    return {}


def _resolve_message_scope_from_raw(item: dict[str, Any]) -> tuple[str, str]:
    message_type = normalize_text(str(item.get("message_type", ""))).lower()
    group_id = normalize_text(str(item.get("group_id", "")))
    user_id = normalize_text(str(item.get("user_id", "")))
    sender = item.get("sender", {}) if isinstance(item.get("sender"), dict) else {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    if message_type == "group" or group_id:
        return "group", group_id
    if message_type in {"private", "friend"}:
        peer_id = user_id or sender_id
        return "private", peer_id
    return "", ""


def _build_recall_payload_from_message(
    item: dict[str, Any],
    *,
    bot_self_id: str,
    chat_type: str,
    peer_id: str,
    bot_id: str = "",
    operator_id: str = "",
    operator_name: str = "",
    source: str = "",
    note: str = "",
) -> dict[str, Any]:
    mapped = _format_chat_message_item(item, bot_self_id=bot_self_id)
    mapped.update(
        {
            "conversation_id": _build_recall_conversation_id(chat_type, peer_id),
            "chat_type": chat_type,
            "peer_id": peer_id,
            "bot_id": bot_id,
            "operator_id": operator_id,
            "operator_name": operator_name,
            "source": source,
            "note": note,
        }
    )
    return mapped


def _format_recalled_record_item(item: dict[str, Any]) -> dict[str, Any]:
    ts = int(item.get("timestamp", 0) or 0)
    recalled_at = int(item.get("recalled_at", 0) or 0)
    segments = item.get("segments", [])
    return {
        "message_id": normalize_text(str(item.get("message_id", ""))),
        "seq": normalize_text(str(item.get("seq", ""))),
        "timestamp": ts,
        "time_iso": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts > 0 else ""),
        "sender_id": normalize_text(str(item.get("sender_id", ""))),
        "sender_name": normalize_text(str(item.get("sender_name", ""))) or "未知用户",
        "sender_role": normalize_text(str(item.get("sender_role", ""))).lower(),
        "is_self": bool(item.get("is_self")),
        "is_essence": False,
        "is_recalled": True,
        "recalled_at": recalled_at,
        "recalled_source": normalize_text(str(item.get("source", ""))),
        "text": str(item.get("text", "") or _render_message_text("", segments)),
        "segments": segments if isinstance(segments, list) else [],
    }


def _normalize_message_segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        items: list[dict[str, Any]] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            raw_data = seg.get("data", {}) or {}
            seg_data = raw_data if isinstance(raw_data, dict) else {}
            if seg_type:
                items.append({"type": seg_type, "data": seg_data})
        return items

    if not isinstance(message, str) or not message:
        return []

    items: list[dict[str, Any]] = []
    for m in re.finditer(r"\[CQ:([a-zA-Z0-9_]+)(?:,([^\]]*))?\]", message):
        seg_type = normalize_text(m.group(1)).lower()
        raw_data = m.group(2) or ""
        seg_data: dict[str, Any] = {}
        if raw_data:
            for pair in raw_data.split(","):
                if "=" not in pair:
                    continue
                key, value = pair.split("=", 1)
                seg_data[normalize_text(key)] = normalize_text(value)
        if seg_type:
            items.append({"type": seg_type, "data": seg_data})
    return items


def _resolve_local_path_from_file_uri(raw: str) -> Path | None:
    value = normalize_text(raw)
    if not value:
        return None
    if value.lower().startswith("file://"):
        path_text = unquote(value[7:])
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        return Path(path_text)
    return Path(unquote(value))


def _decode_base64_payload(value: str) -> bytes | None:
    raw = normalize_text(value)
    if not raw:
        return None
    if raw.startswith("base64://"):
        raw = raw[len("base64://") :]
    if raw.startswith("data:"):
        _, _, tail = raw.partition(",")
        raw = tail
    if not raw:
        return None
    with contextlib.suppress(Exception):
        return base64.b64decode(raw)
    return None


def _guess_media_type_from_hint(hint: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(normalize_text(str(hint)))
    return guessed or fallback


def _is_private_ip(hostname: str) -> bool:
    """检查 hostname 是否指向私有/内网地址，防止 SSRF。"""
    import ipaddress
    import socket
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except (socket.gaierror, ValueError):
        return True  # 无法解析时拒绝
    return False
