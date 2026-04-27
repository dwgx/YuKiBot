"""app.py 辅助函数 — 从 app.py 拆分。

包含 OneBot 事件处理、消息构建、媒体段处理等辅助函数。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
from core.napcat_compat import call_napcat_bot_api
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageEvent, MessageSegment
from utils.text import clip_text, normalize_text

# ── 从 app.py 引用的常量 ──
_MEDIA_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
_MEDIA_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_VIDEO_PROBE_MAX_BYTES = 512 * 1024
_MEDIA_MIN_VIDEO_BYTES = 180 * 1024
_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_log = logging.getLogger("yukiko.app")
_BOT_ONLINE_STATE: dict[str, bool] = {}


def bind_runtime_dependencies(**deps: Any) -> None:
    """注入仍由 app.py 持有的运行时依赖，避免辅助模块反向导入 app.py。"""
    globals().update(deps)

def _event_timestamp(event: MessageEvent) -> datetime:
    ts = getattr(event, "time", None)
    if isinstance(ts, int):
        return datetime.fromtimestamp(ts, tz=UTC)
    return datetime.now(UTC)


def _log_qq_message_event(
    event: MessageEvent,
    raw_segments: list[dict[str, Any]],
    bot_id: str,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    conversation_id = _build_conversation_id(event)
    message_type = str(getattr(event, "message_type", "") or "")
    group_id = int(getattr(event, "group_id", 0) or 0)
    message_id = str(getattr(event, "message_id", "") or "")
    user_id = str(event.get_user_id())
    try:
        plain = normalize_text(event.get_plaintext())
    except Exception:
        plain = ""
    payload = raw_payload if isinstance(raw_payload, dict) else _event_to_dict(event)
    segment_summary = _summarize_segments(raw_segments)
    _log.info(
        "qq_recv | bot=%s | type=%s | conversation=%s | group=%s | user=%s | message_id=%s | plain=%s | segments=%s | raw=%s",
        bot_id,
        message_type or "-",
        conversation_id,
        group_id,
        user_id,
        message_id or "-",
        clip_text(plain, 180) if plain else "-",
        segment_summary,
        _safe_json_dumps(payload, max_chars=900),
    )


def _log_qq_generic_event(kind: str, event: Event, bot_id: str) -> None:
    payload = _event_to_dict(event)
    post_type = str(getattr(event, "post_type", "") or "")
    user_id = str(getattr(event, "user_id", "") or "")
    group_id = str(getattr(event, "group_id", "") or "")
    _log.info(
        "%s | bot=%s | post_type=%s | group=%s | user=%s | raw=%s",
        kind,
        bot_id,
        post_type or "-",
        group_id or "-",
        user_id or "-",
        _safe_json_dumps(payload, max_chars=1000),
    )
    if kind == "qq_meta":
        _update_bot_online_state(bot_id=bot_id, payload=payload)


def _update_bot_online_state(bot_id: str, payload: dict[str, Any]) -> None:
    bid = normalize_text(str(bot_id))
    if not bid or not isinstance(payload, dict):
        return
    meta_event_type = normalize_text(str(payload.get("meta_event_type", ""))).lower()
    if meta_event_type == "lifecycle":
        sub_type = normalize_text(str(payload.get("sub_type", ""))).lower()
        if sub_type == "connect":
            _BOT_ONLINE_STATE[bid] = True
            _resume_bot_send(bid, reason="meta_lifecycle_connect")
        return
    if meta_event_type != "heartbeat":
        return

    status = payload.get("status")
    online: bool | None = None
    if isinstance(status, dict) and "online" in status:
        try:
            online = bool(status.get("online"))
        except Exception:
            online = None
    if online is None:
        return
    prev = _BOT_ONLINE_STATE.get(bid)
    _BOT_ONLINE_STATE[bid] = online
    if prev is not None and prev == online:
        return
    if online:
        _resume_bot_send(bid, reason="meta_heartbeat_online")
    else:
        _suspend_bot_send(bid, seconds=120, reason="meta_heartbeat_offline")


def _event_to_dict(event: Event) -> dict[str, Any]:
    for attr in ("model_dump", "dict"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                data = fn()
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    json_fn = getattr(event, "json", None)
    if callable(json_fn):
        try:
            parsed = json.loads(json_fn())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    fallback = {
        "post_type": getattr(event, "post_type", ""),
        "self_id": getattr(event, "self_id", ""),
        "time": getattr(event, "time", ""),
    }
    if hasattr(event, "get_event_name"):
        try:
            fallback["event_name"] = event.get_event_name()
        except Exception:
            pass
    return fallback


def _summarize_segments(raw_segments: list[dict[str, Any]]) -> str:
    if not raw_segments:
        return "-"
    parts: list[str] = []
    for seg in raw_segments[:8]:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        data = seg.get("data", {}) or {}
        preview = ""
        if seg_type == "text":
            preview = clip_text(normalize_text(str(data.get("text", ""))), 30)
        elif seg_type in {"image", "video", "record", "audio"}:
            preview = clip_text(
                normalize_text(str(data.get("url", "") or data.get("file", "") or data.get("summary", ""))),
                50,
            )
        elif seg_type in {"at", "reply"}:
            preview = clip_text(
                normalize_text(str(data.get("qq", "") or data.get("user_id", "") or data.get("id", ""))),
                20,
            )
        part = seg_type or "unknown"
        if preview:
            part = f"{part}:{preview}"
        parts.append(part)
    if len(raw_segments) > 8:
        parts.append(f"+{len(raw_segments) - 8}")
    return " | ".join(parts) if parts else "-"


def _safe_json_dumps(payload: dict[str, Any], max_chars: int = 1000) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        text = str(payload)
    if max_chars > 0:
        return clip_text(text, max_chars)
    return text


def _build_conversation_id(event: MessageEvent) -> str:
    msg_type = getattr(event, "message_type", "")
    user_id = str(event.get_user_id())
    if msg_type == "group":
        group_id = getattr(event, "group_id", 0)
        return f"group:{group_id}"
    if msg_type == "private":
        return f"private:{user_id}"
    return f"{msg_type}:{user_id}"


def _build_trace_id(conversation_id: str, seq: int) -> str:
    conversation_token = re.sub(r"[^a-z0-9]", "", normalize_text(conversation_id).lower())
    conversation_token = conversation_token[-6:] if conversation_token else "conv"
    return f"{conversation_token}-{int(seq):x}-{uuid4().hex[:8]}"


_RE_WEB_URL = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_RE_BARE_WEB_HOST = re.compile(
    r"(?<![@A-Za-z0-9_.-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|dev|io|ai|app|site|xyz|me|co|cn|jp|tv|gg|cc|info|wiki|top)"
    r"(?::\d{2,5})?(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE,
)


def _has_segment_type(raw_segments: list[dict[str, Any]], segment_types: set[str]) -> bool:
    expected = {normalize_text(item).lower() for item in segment_types if normalize_text(item)}
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type in expected:
            return True
    return False


def _message_and_segment_blob(text: str, raw_segments: list[dict[str, Any]]) -> str:
    parts = [normalize_text(text)]
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", "")))
        if seg_type:
            parts.append(seg_type)
        data = seg.get("data", {}) or {}
        if not isinstance(data, dict):
            continue
        for key in ("text", "url", "file", "file_id", "summary", "title", "content"):
            value = normalize_text(str(data.get(key, "")))
            if value:
                parts.append(value)
    return normalize_text(" ".join(part for part in parts if part))


def _looks_like_video_heavy_request(text: str, raw_segments: list[dict[str, Any]]) -> bool:
    content = _message_and_segment_blob(text, raw_segments).lower()
    if _has_segment_type(raw_segments, {"video"}):
        return True
    if re.search(r"\bbv[0-9a-z]{6,}\b", content, flags=re.IGNORECASE):
        return True
    cues = (
        "视频",
        "解析",
        "bilibili",
        "b23.tv",
        "抖音",
        "douyin",
        "快手",
        "kuaishou",
        "acfun",
        "v.qq.com",
        "m.v.qq.com",
        "腾讯视频",
        "騰訊視頻",
        "youku",
        "iqiyi",
        "mgtv",
        "youtube",
        "youtu.be",
        "小红书",
        "xiaohongshu",
        "xhslink",
    )
    return any(cue in content for cue in cues)


def _looks_like_download_heavy_request(text: str, raw_segments: list[dict[str, Any]]) -> bool:
    content = _message_and_segment_blob(text, raw_segments).lower()
    cues = (
        "下载",
        "安装包",
        "网盘",
        "download",
        "pan.baidu",
        "lanzou",
        "123pan",
        "quark.cn",
        "aliyundrive",
        "alipan",
    )
    if any(cue in content for cue in cues):
        return True
    return bool(re.search(r"\.(?:exe|apk|zip|rar|7z|dmg|pkg|msi)(?:\b|[?#/])", content))


def _looks_like_web_heavy_request(text: str, raw_segments: list[dict[str, Any]]) -> bool:
    content = _message_and_segment_blob(text, raw_segments).lower()
    if not content:
        return False
    if not (_RE_WEB_URL.search(content) or _RE_BARE_WEB_HOST.search(content)):
        return False
    if _looks_like_video_heavy_request(text, raw_segments) or _looks_like_download_heavy_request(text, raw_segments):
        return False
    cues = (
        "网站",
        "网页",
        "页面",
        "官网",
        "打开",
        "看看",
        "看下",
        "帮我看",
        "分析",
        "介绍",
        "是什么",
        "安全吗",
        "website",
        "webpage",
        "site",
        "page",
    )
    return any(cue in content for cue in cues)


def _looks_like_sticker_learning_request(
    text: str,
    raw_segments: list[dict[str, Any]],
    reply_media_segments: list[dict[str, Any]] | None = None,
) -> bool:
    media_segments = list(raw_segments or []) + list(reply_media_segments or [])
    content = _message_and_segment_blob(text, media_segments).lower()
    if not content:
        return False
    sticker_cues = ("表情包", "表情", "贴纸", "貼紙", "emoji", "emote", "sticker")
    learn_cues = (
        "学习",
        "学一下",
        "学下",
        "添加",
        "收录",
        "记住",
        "纠正",
        "改一下",
        "改下",
        "描述",
        "标签",
        "分类",
    )
    if not any(cue in content for cue in sticker_cues):
        return False
    if not any(cue in content for cue in learn_cues):
        return False
    return _has_segment_type(media_segments, {"image", "mface", "face", "reply"})


def _looks_like_cancel_previous_request(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    explicit_cues = (
        "打断",
        "停止上一个",
        "取消上一个",
        "先别回刚才",
        "忽略上一条",
        "中断",
        "插一句",
        "更正",
        "纠正",
        "我说的是",
        "不是这个",
        "不是那个",
        "刚才说错",
        "重新回答",
        "按我这条",
    )
    if any(cue in content for cue in explicit_cues):
        return True
    return bool(re.search(r"^(?:不是|更正|纠正|重新说|重新答|我指的是)", content))


def _extract_at_targets(event: MessageEvent) -> list[str]:
    targets: list[str] = []
    try:
        for segment in event.get_message():
            if str(segment.type).lower() != "at":
                continue
            qq = str(
                segment.data.get("qq")
                or segment.data.get("user_id")
                or segment.data.get("uid")
                or ""
            ).strip()
            if qq:
                targets.append(qq)
    except Exception:
        targets = []

    if not targets:
        try:
            raw = str(event.get_message())
        except Exception:
            raw = ""
        if raw:
            targets.extend(
                re.findall(r"\[at:qq=([0-9]+|all)\]", raw, flags=re.IGNORECASE)
            )
            targets.extend(
                re.findall(r"CQ:at,qq=([0-9]+|all)", raw, flags=re.IGNORECASE)
            )

    uniq: list[str] = []
    seen: set[str] = set()
    for item in targets:
        key = str(item).strip()
        if key.lower() == "all":
            key = "all"
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    return uniq


def _extract_reply_message_id(event: MessageEvent) -> str:
    # NoneBot2 OneBot V11 adapter 会把 reply 段从 get_message() 中剥离，
    # 转而存放在 event.reply (Reply model, 含 message_id 字段)。
    if hasattr(event, "reply") and event.reply:
        mid = getattr(event.reply, "message_id", None)
        if mid is not None:
            return str(mid)

    # fallback: 遍历消息段（某些旧版本可能保留 reply 段）
    try:
        for segment in event.get_message():
            if str(segment.type).lower() != "reply":
                continue
            message_id = str(segment.data.get("id") or segment.data.get("message_id") or "").strip()
            if message_id:
                return message_id
    except Exception as exc:
        _log.warning("reply_extract_error | %s", exc)

    # fallback: 正则匹配 raw 字符串
    try:
        raw = str(event.get_message())
    except Exception:
        raw = ""
    if raw:
        match = re.search(r"\[reply:id=(\d+)\]", raw, flags=re.IGNORECASE)
        if match:
            return str(match.group(1))
        match = re.search(r"CQ:reply,id=(\d+)", raw, flags=re.IGNORECASE)
        if match:
            return str(match.group(1))
    return ""


def _resolve_other_user_targets(
    *,
    bot_id: str,
    at_targets: list[str],
    reply_to_user_id: str,
    mentioned: bool,
) -> tuple[list[str], bool]:
    """Separate real @targets from reply anchors.

    Replying to another user is a distinct signal from explicitly @-mentioning
    that user. Mixing the two makes downstream context think the sender is
    currently talking to the quoted user, which can skew multi-user reasoning.
    """

    safe_bot_id = normalize_text(str(bot_id))
    at_other_user_ids = [
        item
        for item in at_targets
        if item not in {"all", safe_bot_id}
    ]
    at_other_user_ids = list(dict.fromkeys(at_other_user_ids))
    at_other_user_only = (
        (bool(at_targets) and not mentioned)
        or (
            bool(reply_to_user_id)
            and normalize_text(str(reply_to_user_id)) != safe_bot_id
        )
    )
    return at_other_user_ids, at_other_user_only


def _parse_reply_context_payload(payload: Any) -> tuple[str, str, str, list[dict[str, Any]]]:
    """解析 get_msg 或 event.reply 负载，返回 (uid, name, text, media)。"""
    if payload is None:
        return "", "", "", []

    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        data = payload
    else:
        try:
            model_dump = getattr(payload, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump()
                if isinstance(dumped, dict):
                    data = dumped
        except Exception:
            data = {}
        if not data:
            raw_dict = getattr(payload, "__dict__", None)
            if isinstance(raw_dict, dict):
                data = dict(raw_dict)

    sender = data.get("sender", {})
    user_id = ""
    user_name = ""
    if isinstance(sender, dict):
        uid = sender.get("user_id")
        if uid is not None:
            user_id = str(uid)
        user_name = normalize_text(str(sender.get("card") or sender.get("nickname") or ""))

    if not user_id:
        uid = data.get("user_id")
        if uid is None:
            uid = getattr(payload, "user_id", None)
        user_id = str(uid) if uid is not None else ""
    if not user_name:
        user_name = normalize_text(str(data.get("nickname", "")))
    if not user_name:
        user_name = normalize_text(str(getattr(payload, "nickname", "")))

    message_content = data.get("message", None)
    if message_content is None:
        message_content = getattr(payload, "message", None)
    if message_content is None:
        message_content = data.get("raw_message", None)
    if message_content is None:
        message_content = getattr(payload, "raw_message", None)

    reply_text_parts: list[str] = []
    reply_media: list[dict[str, Any]] = []

    def _iter_segments_list(items: list[Any]) -> None:
        for seg in items:
            seg_type = ""
            seg_data: dict[str, Any] = {}
            if isinstance(seg, dict):
                seg_type = normalize_text(str(seg.get("type", ""))).lower()
                raw_data = seg.get("data", {}) or {}
                if isinstance(raw_data, dict):
                    seg_data = dict(raw_data)
            else:
                seg_type = normalize_text(str(getattr(seg, "type", ""))).lower()
                raw_data = getattr(seg, "data", {}) or {}
                if isinstance(raw_data, dict):
                    seg_data = dict(raw_data)
            if not seg_type:
                continue
            if seg_type == "text":
                text_piece = normalize_text(str(seg_data.get("text", "")))
                if text_piece:
                    reply_text_parts.append(text_piece)
            if seg_type in {"image", "video", "record", "audio"}:
                reply_media.append({"type": seg_type, "data": seg_data})

    if isinstance(message_content, list):
        _iter_segments_list(message_content)
    elif isinstance(message_content, str):
        import re as _re

        text_fallback = _re.sub(r"\[CQ:[^\]]+\]", " ", message_content)
        text_fallback = normalize_text(text_fallback)
        if text_fallback:
            reply_text_parts.append(text_fallback)
        for m in _re.finditer(r"\[CQ:(image|video|record|audio),([^\]]+)\]", message_content):
            seg_type = m.group(1)
            pairs = dict(kv.split("=", 1) for kv in m.group(2).split(",") if "=" in kv)
            reply_media.append({"type": seg_type, "data": pairs})

    reply_text = _normalize_reply_text("\n".join(reply_text_parts))
    return user_id, user_name, reply_text, reply_media


async def _resolve_reply_context(
    bot: Bot,
    reply_message_id: str,
    event: MessageEvent | None = None,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """解析被引用消息，返回 (发送者user_id, 发送者昵称, 被引用文本, 被引用消息的媒体segments列表)。"""
    mid = str(reply_message_id or "").strip()
    event_reply = getattr(event, "reply", None) if event is not None else None
    event_ctx = _parse_reply_context_payload(event_reply) if event_reply else ("", "", "", [])

    if not mid:
        return event_ctx

    data: Any = None
    try:
        data = await call_napcat_bot_api(bot, "get_msg", message_id=int(mid))
    except Exception:
        try:
            data = await call_napcat_bot_api(bot, "get_msg", message_id=mid)
        except Exception:
            return event_ctx
    if not isinstance(data, dict):
        return event_ctx

    user_id, user_name, reply_text, reply_media = _parse_reply_context_payload(data)
    if not (user_id or user_name or reply_text or reply_media):
        return event_ctx
    if not reply_text and event_ctx[2]:
        reply_text = event_ctx[2]
    if not reply_media and event_ctx[3]:
        reply_media = event_ctx[3]

    if reply_media:
        _log.debug(
            "reply_media_found | mid=%s | count=%d | types=%s",
            mid,
            len(reply_media),
            [s["type"] for s in reply_media],
        )
    else:
        _log.debug("reply_media_empty | mid=%s | user=%s", mid, user_id)
    _log.debug(
        "reply_text_found | mid=%s | user=%s | user_name=%s | text=%s",
        mid,
        user_id,
        user_name or "-",
        clip_text(reply_text, 120),
    )
    return user_id, user_name, reply_text, reply_media


def _is_mentioned(bot: Bot, event: MessageEvent, at_targets: list[str] | None = None) -> bool:
    if bool(getattr(event, "to_me", False)):
        return True
    targets = at_targets if at_targets is not None else _extract_at_targets(event)
    self_id = str(bot.self_id)
    return any(target in {"all", self_id} for target in targets)


def _build_reply_prefix(
    event: MessageEvent,
    quote_message_id: str,
    sender_user_id: str,
    enable_quote: bool,
    enable_at: bool,
) -> Message:
    msg = Message()
    msg_type = str(getattr(event, "message_type", ""))
    if msg_type != "group":
        return msg

    if enable_quote and quote_message_id:
        reply_segment = _build_reply_segment(quote_message_id)
        if reply_segment is not None:
            msg += reply_segment
    if enable_at and sender_user_id:
        msg += MessageSegment.at(sender_user_id)
        msg += Message(" ")
    return msg


def _build_reply_segment(message_id: str) -> MessageSegment | None:
    raw = str(message_id or "").strip()
    if not raw:
        return None
    try:
        return MessageSegment.reply(int(raw))
    except Exception:
        try:
            return MessageSegment.reply(raw)
        except Exception:
            return None


def _strip_reply_segments(message: Message) -> Message:
    clean = Message()
    try:
        for seg in message:
            if str(getattr(seg, "type", "")).lower() == "reply":
                continue
            clean += seg
    except Exception:
        return message
    return clean


async def _safe_send(bot: Bot, event: MessageEvent, message: Message) -> bool:
    group_id = int(getattr(event, "group_id", 0) or 0)
    bot_id = str(getattr(bot, "self_id", "") or "")
    suspended, suspend_reason = _check_bot_send_suspended(bot_id)
    if suspended:
        _log.warning(
            "safe_send_skipped_bot_suspended | bot=%s | group=%s | reason=%s",
            bot_id or "-",
            group_id,
            suspend_reason,
        )
        return False
    blocked, block_reason = _check_group_send_block(group_id)
    if blocked:
        _log.warning("safe_send_skipped_blocked | group=%s | reason=%s", group_id, block_reason)
        return False

    try:
        await bot.send(event=event, message=message)
        return True
    except Exception as e:
        _log.debug("safe_send_primary_fail | %s", e)
        await _maybe_block_group_send_on_error(bot=bot, event=event, exc=e)
        if _is_hard_send_channel_error(e):
            _suspend_bot_send(bot_id=bot_id, seconds=120, reason=f"hard_send_error:{clip_text(str(e), 80)}")
            _log.warning("safe_send_abort_hard_error | bot=%s | group=%s", bot_id or "-", group_id)
            return False
        if _is_transient_send_error(e):
            retry_delays = (0.5, 1.2)
            for idx, delay in enumerate(retry_delays, start=1):
                await asyncio.sleep(delay)
                try:
                    await bot.send(event=event, message=message)
                    _log.info("safe_send_retry_ok | attempt=%d | delay=%.1fs", idx, delay)
                    return True
                except Exception as retry_exc:
                    _log.warning("safe_send_retry_fail | attempt=%d | %s", idx, retry_exc)
                    await _maybe_block_group_send_on_error(bot=bot, event=event, exc=retry_exc)
                    if _is_hard_send_channel_error(retry_exc):
                        _suspend_bot_send(
                            bot_id=bot_id,
                            seconds=120,
                            reason=f"hard_send_error:{clip_text(str(retry_exc), 80)}",
                        )
                        _log.warning("safe_send_abort_hard_error_retry | bot=%s | group=%s", bot_id or "-", group_id)
                        return False
            # 网络/链路类错误重试后仍失败时，不做 payload 回退，避免重复刷同一条。
            _log.warning("safe_send_abort_after_transient_retries | bot=%s | group=%s", bot_id or "-", group_id)
            return False
        # 只有“消息格式不兼容”才值得尝试 fallback；其余错误直接止损。
        if not _is_payload_send_error(e):
            _log.warning("safe_send_abort_non_payload_error | bot=%s | group=%s", bot_id or "-", group_id)
            return False

    # Fallback 1: strip reply segments
    fallback = _strip_reply_segments(message)
    if fallback and str(fallback) != str(message):
        try:
            await bot.send(event=event, message=fallback)
            return True
        except Exception as e:
            _log.debug("safe_send_fallback1_fail | %s", e)
            await _maybe_block_group_send_on_error(bot=bot, event=event, exc=e)
            if _is_hard_send_channel_error(e):
                _suspend_bot_send(bot_id=bot_id, seconds=120, reason=f"hard_send_error:{clip_text(str(e), 80)}")
                return False
            if _is_transient_send_error(e) or not _is_payload_send_error(e):
                return False

    # Fallback 2: plain text only (strip all non-text segments, replace special chars)
    plain = message.extract_plain_text().strip()
    if plain:
        try:
            await bot.send(event=event, message=Message(plain))
            return True
        except Exception as e:
            _log.debug("safe_send_fallback2_fail | %s", e)
            await _maybe_block_group_send_on_error(bot=bot, event=event, exc=e)
            if _is_hard_send_channel_error(e):
                _suspend_bot_send(bot_id=bot_id, seconds=120, reason=f"hard_send_error:{clip_text(str(e), 80)}")
            return False

    _log.warning("safe_send_all_fallbacks_failed | msg=%s", str(message)[:200])
    return False


def _extract_user_name(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender is None:
        return str(event.get_user_id())

    for field in ("card", "nickname"):
        value = getattr(sender, field, None)
        if value:
            return str(value)

    if isinstance(sender, dict):
        for field in ("card", "nickname"):
            value = sender.get(field)
            if value:
                return str(value)

    return str(event.get_user_id())


def _extract_sender_role(event: MessageEvent) -> str:
    """从 OneBot 事件中提取发送者的群角色: owner / admin / member。"""
    sender = getattr(event, "sender", None)
    if sender is None:
        return ""
    role = getattr(sender, "role", None)
    if role:
        return str(role).strip().lower()
    if isinstance(sender, dict):
        role = sender.get("role", "")
        if role:
            return str(role).strip().lower()
    return ""


def _extract_raw_segments(event: MessageEvent) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    try:
        for seg in event.get_message():
            seg_type = str(getattr(seg, "type", ""))
            seg_data = dict(getattr(seg, "data", {}) or {})
            segments.append({"type": seg_type, "data": seg_data})
    except Exception:
        return []
    return segments


def _has_media_segments(raw_segments: list[dict[str, Any]]) -> bool:
    for seg in raw_segments or []:
        seg_type = normalize_text(str((seg or {}).get("type", ""))).lower()
        if seg_type in {"image", "video", "record", "audio", "forward"}:
            return True
    return False


def _extract_text_segments(raw_segments: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type != "text":
            continue
        data = seg.get("data", {}) or {}
        text = normalize_text(str(data.get("text", "")))
        if text:
            chunks.append(text)
    return normalize_text(" ".join(chunks))


def _strip_media_placeholder_text(text: str) -> str:
    content = normalize_text(text)
    if not content:
        return ""
    content = re.sub(
        r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]",
        " ",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(r"\s+", " ", content).strip()
    return content


async def _try_extract_voice_text(bot: Bot, raw_segments: list[dict[str, Any]]) -> str:
    """尝试从语音消息中提取文字内容。

    使用 NapCat 的 get_record API 获取语音文件，
    然后尝试通过 QQ 内置的语音转文字功能获取文本。
    """
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type not in ("record", "audio"):
            continue
        data = seg.get("data", {}) or {}
        file_id = str(data.get("file", "") or data.get("file_id", "")).strip()
        if not file_id:
            continue
        try:
            # 尝试使用 NapCat 的 get_record API 获取语音文件信息
            result = await call_napcat_bot_api(bot, "get_record", file=file_id, out_format="mp3")
            if isinstance(result, dict):
                # 某些实现会返回 text 字段（语音转文字结果）
                text = str(result.get("text", "")).strip()
                if text:
                    return text
        except Exception:
            pass
        try:
            # 尝试使用 NapCat 扩展的 translate_en2zh 或其他 STT 接口
            # 如果 NapCat 支持 get_msg 获取消息详情中的语音文字
            msg_id = str(data.get("message_id", "") or data.get("id", "")).strip()
            if msg_id:
                msg_detail = await call_napcat_bot_api(bot, "get_msg", message_id=int(msg_id))
                if isinstance(msg_detail, dict):
                    # 检查消息详情中是否有语音转文字结果
                    for seg_detail in msg_detail.get("message", []):
                        if isinstance(seg_detail, dict) and seg_detail.get("type") == "text":
                            t = str(seg_detail.get("data", {}).get("text", "")).strip()
                            if t:
                                return t
        except Exception:
            pass
    return ""


def _build_multimodal_text(raw_segments: list[dict[str, Any]], mentioned: bool = False) -> str:
    """从 raw_segments 中提取媒体类型标记，生成多模态事件描述文本。"""
    media_tokens: list[str] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type in {"image", "video", "record", "audio", "forward"}:
            data = seg.get("data", {}) or {}
            summary = normalize_text(str(data.get("summary", "")))
            url = normalize_text(str(data.get("url", "")))
            if seg_type == "image" and summary:
                media_tokens.append(f"image:{summary}")
            elif seg_type == "image" and url:
                # 图片 URL（尤其 QQ CDN）可能很长且带临时参数，截断后会变成无效链接；这里仅保留图片事件标记。
                media_tokens.append("image:[image]")
            elif url:
                media_tokens.append(f"{seg_type}:{clip_text(url, 120)}")
            else:
                media_tokens.append(seg_type)

    if not media_tokens:
        return ""

    prefix = "MULTIMODAL_EVENT_AT" if mentioned else "MULTIMODAL_EVENT"
    human = "user mentioned bot and sent multimodal message:" if mentioned else "user sent multimodal message:"
    return f"{prefix} {human} {' | '.join(media_tokens)}"


def _normalize_reply_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""

    normalized: list[str] = []
    previous_blank = False
    for line in raw.split("\n"):
        clean = line.strip()
        if clean:
            normalized.append(clean)
            previous_blank = False
            continue
        if normalized and not previous_blank:
            normalized.append("")
            previous_blank = True

    while normalized and normalized[0] == "":
        normalized.pop(0)
    while normalized and normalized[-1] == "":
        normalized.pop()
    return "\n".join(normalized)


def _split_reply_chunks(
    text: str,
    max_lines: int = 3,
    max_chars: int = 220,
    max_chunks: int = 4,
) -> list[str]:
    if max_lines <= 0 or max_chars <= 0 or max_chunks <= 0:
        return []

    normalized = _normalize_reply_text(text)
    if not normalized:
        return []

    # 先按段落切，再按句子切，尽量保留 AI 连续说几句话的自然节奏。
    tokens: list[str | None] = []
    paragraphs = [seg.strip() for seg in re.split(r"\n\s*\n+", normalized) if seg.strip()]
    for idx, paragraph in enumerate(paragraphs):
        if idx > 0:
            tokens.append(None)
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        for line in lines:
            tokens.extend(_split_line_by_sentence(line, max_chars=max_chars))

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for token in tokens:
        if token is None:
            if not current_lines:
                continue
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
            continue

        line_len = len(token)
        projected_lines = len(current_lines) + 1
        projected_len = current_len + (1 if current_lines else 0) + line_len
        if current_lines and (projected_lines > max_lines or projected_len > max_chars):
            chunks.append("\n".join(current_lines))
            current_lines = [token]
            current_len = line_len
        else:
            current_lines.append(token)
            current_len = projected_len

    if current_lines:
        chunks.append("\n".join(current_lines))
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks == 1:
        merged = normalize_text("\n".join(chunks))
        return [merged] if merged else []
    head = chunks[: max_chunks - 1]
    tail = normalize_text("\n".join(chunks[max_chunks - 1 :]))
    if tail:
        head.append(tail)
    return [chunk for chunk in head if chunk.strip()]


def _rebalance_text_chunks_for_send(
    chunks: list[str],
    max_chars: int = 520,
) -> list[str]:
    safe_max = max(120, int(max_chars or 520))
    out: list[str] = []
    for raw in chunks or []:
        piece = _normalize_reply_text(raw)
        if not piece:
            continue
        if len(piece) <= safe_max:
            out.append(piece)
            continue
        split_rows = _split_line_by_sentence(piece, max_chars=safe_max)
        if not split_rows:
            split_rows = _hard_wrap_text(piece, max_chars=safe_max)
        for row in split_rows:
            clean = _normalize_reply_text(row)
            if clean:
                out.append(clean)
    return out


def _split_line_by_sentence(line: str, max_chars: int) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    url_tokens: dict[str, str] = {}

    def _mask_url(match: re.Match[str]) -> str:
        key = f"URLTOKEN{len(url_tokens)}PLACEHOLDER"
        url_tokens[key] = match.group(0)
        return key

    masked_text = url_pattern.sub(_mask_url, text)
    segments = [seg.strip() for seg in re.split(r"(?<=[。！？、!?；;])", masked_text) if seg.strip()]
    if url_tokens:
        restored_segments: list[str] = []
        for seg in segments:
            restored = seg
            for key, value in url_tokens.items():
                restored = restored.replace(key, value)
            restored_segments.append(restored)
        segments = restored_segments
    if len(segments) <= 1:
        if len(text) <= max_chars:
            return [text]
        return _hard_wrap_text(text, max_chars=max_chars)

    out: list[str] = []
    current = ""
    for seg in segments:
        if not current:
            if len(seg) <= max_chars:
                current = seg
            else:
                out.extend(_hard_wrap_text(seg, max_chars=max_chars))
            continue

        candidate = f"{current}{seg}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        out.append(current)
        if len(seg) <= max_chars:
            current = seg
        else:
            current = ""
            out.extend(_hard_wrap_text(seg, max_chars=max_chars))

    if current:
        out.append(current)
    return out


def _hard_wrap_text(text: str, max_chars: int) -> list[str]:
    content = str(text or "").strip()
    if not content:
        return []
    token_pattern = re.compile(r"https?://[^\s]+|[^\s]+", re.IGNORECASE)
    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    tokens = token_pattern.findall(content)
    if not tokens:
        return [content]
    parts: list[str] = []
    current = ""
    for token in tokens:
        if url_pattern.fullmatch(token):
            if current:
                parts.append(current)
                current = ""
            parts.append(token)
            continue
        if not current:
            current = token
            continue
        candidate = f"{current} {token}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        parts.append(current)
        current = token
    if current:
        parts.append(current)
    return [piece for piece in parts if piece.strip()]


async def _build_image_segment(url: str) -> MessageSegment | None:
    target = str(url or "").strip()
    if not target:
        return None

    if target.startswith("base64://"):
        return MessageSegment.image(target)

    if target.startswith("data:image") and ";base64," in target:
        _, b64 = target.split(";base64,", 1)
        if b64:
            return MessageSegment.image(f"base64://{b64}")

    if target.startswith("file://"):
        parsed = urlparse(target)
        local_raw = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", local_raw):
            local_raw = local_raw[1:]
        return _build_image_segment_from_local_path(Path(local_raw))

    local_path = Path(target)
    if local_path.exists() and local_path.is_file():
        return _build_image_segment_from_local_path(local_path)

    return await _build_image_segment_from_remote_url(target)


def _build_image_segment_from_local_path(path: Path) -> MessageSegment | None:
    """构建本地图片段：优先 file:// URI（零内存开销），超大文件 fallback 到 base64。"""
    try:
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return None
        file_size = resolved.stat().st_size
        if file_size == 0 or file_size > _MEDIA_MAX_IMAGE_BYTES:
            return None
    except Exception:
        return None
    # NapCat 原生支持 file:// URI，直接读取本地文件，零内存开销。
    file_uri = resolved.as_uri()  # file:///C:/xxx/pic.jpg
    return MessageSegment.image(file=file_uri)


async def _build_image_segment_from_remote_url(url: str) -> MessageSegment | None:
    """构建远程图片段：优先直传 URL 让 NapCat 自行下载，无法确认是图片时 fallback 到 base64。"""
    # 先用 HEAD 请求确认是图片（低开销）
    looks_like_image = url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))
    if not looks_like_image:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=5.0),
                follow_redirects=True,
                headers={"User-Agent": _MEDIA_USER_AGENT},
            ) as client:
                head = await client.head(url)
                if head.status_code < 400:
                    content_type = str(head.headers.get("content-type", "")).lower()
                    looks_like_image = "image/" in content_type
                    if not looks_like_image:
                        final_url = str(head.url).lower()
                        looks_like_image = final_url.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))
        except Exception:
            pass
    if looks_like_image:
        # 直传 URL 给 NapCat，由 NapCat 自行下载，零内存开销。
        return MessageSegment.image(file=url)
    # 无法从 URL/HEAD 判断是图片 → 下载后 base64 确认
    try:
        async with httpx.AsyncClient(
            timeout=_MEDIA_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _MEDIA_USER_AGENT},
        ) as client:
            response = await client.get(url)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    data = response.content
    if not data or len(data) > _MEDIA_MAX_IMAGE_BYTES:
        return None
    content_type = str(response.headers.get("content-type", "")).lower()
    if "image/" not in content_type:
        final_url = str(response.url).lower()
        if not final_url.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
            return None
    b64 = base64.b64encode(data).decode("ascii")
    return MessageSegment.image(f"base64://{b64}")


async def _video_seg_with_thumb(local_path: Path) -> MessageSegment | None:
    """构建本地视频 MessageSegment，优先附带本地缩略图。"""
    # 大视频先压缩，避免 NapCat WebSocket 超时
    actual_path = (await _compress_video_if_needed(local_path)).expanduser().resolve()
    # 再做 QQ 兼容转码，避免 AV1/HEVC 等格式在客户端无法内联预览。
    actual_path = (await _ensure_qq_preview_video(actual_path)).expanduser().resolve()
    ok, reason = await _probe_local_video_health(actual_path)
    if not ok:
        _log.warning("video_seg_reject_unhealthy | file=%s | reason=%s", actual_path.name, reason)
        return None
    thumb_path = await _generate_video_thumbnail(actual_path)

    data: dict[str, Any] = {"file": str(actual_path)}
    if thumb_path is not None and thumb_path.exists():
        data["thumb"] = str(thumb_path.expanduser().resolve())
        _log.info("video_seg_with_thumb | video=%s | thumb=%s", actual_path.name, thumb_path.name)
    else:
        _log.info("video_seg_no_thumb | video=%s", actual_path.name)
    return MessageSegment("video", data)


async def _build_video_segment(url: str) -> MessageSegment | None:
    target = str(url or "").strip()
    if not target:
        return None

    if target.startswith("file://"):
        parsed = urlparse(target)
        local_raw = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", local_raw):
            local_raw = local_raw[1:]
        local_path = Path(local_raw)
        if local_path.exists() and local_path.is_file():
            ok, _ = await _probe_local_video_health(local_path)
            if not ok:
                return None
            return await _video_seg_with_thumb(local_path)
        return None

    local_path = Path(target)
    if local_path.exists() and local_path.is_file():
        ok, _ = await _probe_local_video_health(local_path)
        if not ok:
            return None
        return await _video_seg_with_thumb(local_path)

    if not re.match(r"^https?://", target, flags=re.IGNORECASE):
        return None
    if not re.search(r"\.(mp4|webm|mov|m4v)(\?|$)", target, flags=re.IGNORECASE):
        if not await _is_remote_video_url(target):
            return None

    # NapCat 对远程视频 URL 经常无法生成缩略图导致发送失败，
    # 先下载到本地临时文件再发送。
    local_tmp = await _download_remote_video_to_tmp(target)
    if local_tmp is not None:
        ok, _ = await _probe_local_video_health(local_tmp)
        if not ok:
            return None
        return await _video_seg_with_thumb(local_tmp)
    # 下载失败则回退，让调用方走文本链接兜底
    return None


async def _is_remote_video_url(url: str) -> bool:
    headers = {"User-Agent": _MEDIA_USER_AGENT}
    head_content_type = ""
    try:
        async with httpx.AsyncClient(
            timeout=_MEDIA_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            head = None
            try:
                head = await client.head(url)
            except Exception:
                head = None
            if head is not None and head.status_code < 400:
                head_content_type = str(head.headers.get("content-type", "")).lower()
                if head_content_type.startswith("image/"):
                    return False
            probe = await client.get(url, headers={"Range": f"bytes=0-{_MEDIA_VIDEO_PROBE_MAX_BYTES - 1}"})
    except Exception:
        return False

    if probe.status_code >= 400:
        return False
    content_type = str(probe.headers.get("content-type", "")).lower()
    if content_type.startswith("image/"):
        return False
    probe_bytes = probe.content or b""
    if _looks_like_image_header(probe_bytes):
        return False
    if _looks_like_video_header(probe_bytes):
        return True
    if content_type.startswith("video/") and not _looks_like_image_header(probe_bytes):
        return True
    if not content_type and head_content_type.startswith("video/"):
        return True
    if content_type.startswith("text/") or "json" in content_type or "html" in content_type:
        return False
    final_url = str(probe.url).lower()
    return bool(re.search(r"\.(mp4|webm|mov|m4v|flv|mkv)(\?|$)", final_url))


_VIDEO_TMP_DIR = Path("storage/cache/video_send")
_VIDEO_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024  # 64MB
_VIDEO_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_VIDEO_SEND_COMPRESS_THRESHOLD = 8 * 1024 * 1024  # 超过 8MB 自动压缩
_VIDEO_SEND_MAX_BYTES = 25 * 1024 * 1024  # 压缩后仍超 25MB 走文件上传


def _read_media_stream_info_sync(path: Path) -> dict[str, str]:
    """尽量读取视频/音频编码信息（优先 ffprobe，缺失时回退 ffmpeg -i 文本解析）。"""
    info = {"video_codec": "", "audio_codec": "", "pix_fmt": ""}
    target = str(path)
    if _FFPROBE_BIN:
        cmd = [
            _FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt",
            "-of",
            "json",
            target,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or "{}")
                streams = payload.get("streams", [])
                if isinstance(streams, list):
                    for stream in streams:
                        if not isinstance(stream, dict):
                            continue
                        codec_type = str(stream.get("codec_type", "")).lower()
                        codec_name = normalize_text(str(stream.get("codec_name", ""))).lower()
                        if codec_type == "video" and codec_name and not info["video_codec"]:
                            info["video_codec"] = codec_name
                            info["pix_fmt"] = normalize_text(str(stream.get("pix_fmt", ""))).lower()
                        elif codec_type == "audio" and codec_name and not info["audio_codec"]:
                            info["audio_codec"] = codec_name
        except Exception:
            pass
        if info["video_codec"]:
            return info

    # 回退：ffmpeg -i stderr 解析（兼容没有 ffprobe 的环境）
    if not _FFMPEG_BIN:
        return info
    cmd = [_FFMPEG_BIN, "-hide_banner", "-i", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
        for line in text.splitlines():
            line_low = line.lower()
            if "video:" in line_low and not info["video_codec"]:
                m = re.search(r"Video:\s*([a-z0-9_]+)", line, flags=re.IGNORECASE)
                if m:
                    info["video_codec"] = normalize_text(m.group(1)).lower()
                m_pix = re.search(r"Video:\s*[^,]+,\s*([a-z0-9_]+)", line, flags=re.IGNORECASE)
                if m_pix:
                    info["pix_fmt"] = normalize_text(m_pix.group(1)).lower()
            elif "audio:" in line_low and not info["audio_codec"]:
                m = re.search(r"Audio:\s*([a-z0-9_]+)", line, flags=re.IGNORECASE)
                if m:
                    info["audio_codec"] = normalize_text(m.group(1)).lower()
    except Exception:
        pass
    return info


def _needs_qq_video_compat(path: Path) -> tuple[bool, str, dict[str, str]]:
    """判断视频是否需要转为 QQ 兼容格式。"""
    info = _read_media_stream_info_sync(path)
    vcodec = normalize_text(info.get("video_codec", "")).lower()
    acodec = normalize_text(info.get("audio_codec", "")).lower()
    pix_fmt = normalize_text(info.get("pix_fmt", "")).lower()

    if not vcodec:
        return True, "video_codec_unknown", info
    # QQ 预览对 AV1/HEVC/VP9 兼容较差，统一落到 H264。
    if vcodec not in {"h264", "avc1"}:
        return True, f"video_codec_{vcodec}", info
    if pix_fmt and not (pix_fmt.startswith("yuv420") or pix_fmt in {"nv12", "yuvj420p"}):
        return True, f"pix_fmt_{pix_fmt}", info
    # 无音轨或音轨非 AAC 时统一转 AAC，避免客户端兼容问题。
    if not acodec:
        return True, "audio_missing", info
    if acodec not in {"aac", "mp3", "mp2"}:
        return True, f"audio_codec_{acodec}", info
    return False, "", info


def _ensure_qq_preview_video_sync(src: Path) -> Path:
    """确保视频为 QQ 预览友好格式（H264 + AAC + yuv420p + faststart）。"""
    if not _FFMPEG_BIN:
        return src
    try:
        size = int(src.stat().st_size)
    except Exception:
        return src
    if size <= _MEDIA_MIN_VIDEO_BYTES:
        return src

    need, reason, info = _needs_qq_video_compat(src)
    _log.info(
        "video_qq_compat_check | file=%s | need=%s | reason=%s | v=%s | a=%s | pix=%s",
        src.name,
        need,
        reason or "-",
        info.get("video_codec", "") or "-",
        info.get("audio_codec", "") or "-",
        info.get("pix_fmt", "") or "-",
    )
    if not need:
        return src

    out = src.with_suffix(".qq.mp4")
    try:
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime and out.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            return out
    except Exception:
        pass

    src_has_audio = bool(normalize_text(info.get("audio_codec", "")))
    # 始终保留原始音轨映射；不再注入静音轨，避免误判后无声。
    cmd: list[str] = [
        _FFMPEG_BIN,
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-vf",
        "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(out),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            out_info = _read_media_stream_info_sync(out)
            out_has_audio = bool(normalize_text(out_info.get("audio_codec", "")))
            if src_has_audio and not out_has_audio:
                _log.warning(
                    "video_qq_compat_drop_audio | src=%s | out=%s | fallback=source",
                    src.name,
                    out.name,
                )
                out.unlink(missing_ok=True)
                return src
            _log.info(
                "video_qq_compat_ok | src=%s | out=%s | size=%d",
                src.name,
                out.name,
                out.stat().st_size,
            )
            return out
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="ignore")[:500]
        _log.warning(
            "video_qq_compat_fail | src=%s | rc=%d | stderr=%s",
            src.name,
            proc.returncode,
            stderr_text,
        )
    except Exception as exc:
        _log.warning("video_qq_compat_error | src=%s | %s", src.name, exc)
    out.unlink(missing_ok=True)
    return src


async def _ensure_qq_preview_video(path: Path) -> Path:
    return await asyncio.to_thread(_ensure_qq_preview_video_sync, path)


async def _download_remote_video_to_tmp(url: str) -> Path | None:
    """下载远程视频到本地临时文件，供 NapCat 发送。返回本地路径或 None。"""
    part_path: Path | None = None
    try:
        _VIDEO_TMP_DIR.mkdir(parents=True, exist_ok=True)
        # 用 URL hash 做文件名避免冲突
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        tmp_path = _VIDEO_TMP_DIR / f"{url_hash}.mp4"
        part_path = _VIDEO_TMP_DIR / f"{url_hash}.part"
        if tmp_path.exists() and tmp_path.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            ok, reason = await _probe_local_video_health(tmp_path)
            if ok:
                return tmp_path
            _log.warning("video_tmp_cache_invalid | %s | %s", tmp_path.name, reason)
            tmp_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)
        async with httpx.AsyncClient(
            timeout=_VIDEO_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _MEDIA_USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                total = 0
                with part_path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > _VIDEO_DOWNLOAD_MAX_BYTES:
                            part_path.unlink(missing_ok=True)
                            return None
                        fp.write(chunk)
        if not part_path.exists() or part_path.stat().st_size <= _MEDIA_MIN_VIDEO_BYTES:
            part_path.unlink(missing_ok=True)
            return None
        part_path.replace(tmp_path)
        ok, reason = await _probe_local_video_health(tmp_path)
        if ok:
            return tmp_path
        _log.warning("video_tmp_download_invalid | %s | %s", tmp_path.name, reason)
        tmp_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)
        return None
    except Exception as exc:
        _log.debug("video_tmp_download_error | %s", exc)
        if part_path is not None:
            part_path.unlink(missing_ok=True)
        return None


def _compress_video_sync(src: Path, max_bytes: int = _VIDEO_SEND_COMPRESS_THRESHOLD) -> Path:
    """用 ffmpeg 压缩视频到目标大小以内，返回压缩后路径（可能是原路径）。"""
    if not _FFMPEG_BIN:
        return src
    try:
        size = src.stat().st_size
        src_mtime = src.stat().st_mtime
    except Exception:
        return src
    if size <= max_bytes:
        return src

    compressed = src.with_suffix(".compressed.mp4")
    src_info = _read_media_stream_info_sync(src)
    src_has_audio = bool(normalize_text(src_info.get("audio_codec", "")))
    if compressed.exists() and compressed.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
        try:
            if compressed.stat().st_mtime >= src_mtime:
                cached_info = _read_media_stream_info_sync(compressed)
                cached_has_audio = bool(normalize_text(cached_info.get("audio_codec", "")))
                if src_has_audio and not cached_has_audio:
                    _log.warning(
                        "video_compress_cache_drop_audio | src=%s | cached=%s | recalc=true",
                        src.name,
                        compressed.name,
                    )
                    compressed.unlink(missing_ok=True)
                else:
                    return compressed
            else:
                _log.info(
                    "video_compress_cache_stale | src=%s | cached=%s | recalc=true",
                    src.name,
                    compressed.name,
                )
                compressed.unlink(missing_ok=True)
        except Exception:
            compressed.unlink(missing_ok=True)

    _log.info("video_compress | src=%s | size=%.1fMB | threshold=%.1fMB",
                src.name, size / 1024 / 1024, max_bytes / 1024 / 1024)

    # 先探测时长，用于计算目标码率
    target_bitrate = "1500k"  # 默认 1.5Mbps
    try:
        probe_cmd = [
            _FFPROBE_BIN or _FFMPEG_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(src),
        ]
        if _FFPROBE_BIN:
            probe_cmd[0] = _FFPROBE_BIN
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        if probe_result.returncode == 0:
            probe_data = json.loads(probe_result.stdout or "{}")
            duration = float(probe_data.get("format", {}).get("duration", 0) or 0)
            if duration > 0:
                # 目标: max_bytes 的 85%，留余量给容器开销
                target_total_bits = int(max_bytes * 0.85 * 8)
                calc_bitrate = max(400_000, int(target_total_bits / duration))
                target_bitrate = f"{calc_bitrate // 1000}k"
    except Exception:
        pass

    cmd = [
        _FFMPEG_BIN, "-y",
        "-i", str(src),
        "-c:v", "libx264",
        "-preset", "fast",
        "-b:v", target_bitrate,
        "-maxrate", target_bitrate,
        "-bufsize", f"{int(target_bitrate.rstrip('k')) * 2}k",
        "-vf", "scale='min(720,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(compressed),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if proc.returncode == 0 and compressed.exists() and compressed.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            compressed_info = _read_media_stream_info_sync(compressed)
            compressed_has_audio = bool(normalize_text(compressed_info.get("audio_codec", "")))
            if src_has_audio and not compressed_has_audio:
                _log.warning(
                    "video_compress_drop_audio | src=%s | out=%s | fallback=source",
                    src.name,
                    compressed.name,
                )
                compressed.unlink(missing_ok=True)
                return src
            new_size = compressed.stat().st_size
            _log.info("video_compress_ok | %s | %.1fMB -> %.1fMB",
                        src.name, size / 1024 / 1024, new_size / 1024 / 1024)
            return compressed
        _log.warning("video_compress_fail | rc=%d | stderr=%s",
                    proc.returncode, (proc.stderr or b"")[:300])
    except subprocess.TimeoutExpired:
        _log.warning("video_compress_timeout | %s", src.name)
    except Exception as exc:
        _log.warning("video_compress_error | %s", exc)

    # 压缩失败，清理并返回原文件
    compressed.unlink(missing_ok=True)
    return src


async def _try_upload_group_file(bot: Bot, event: MessageEvent, video_url: str) -> bool:
    """尝试用 upload_group_file API 上传视频文件（大文件兜底）。"""
    group_id = getattr(event, "group_id", 0)
    if not group_id:
        return False

    local_path = _as_local_video_path(video_url)
    if local_path is None:
        return False
    ok, reason = await _probe_local_video_health(local_path)
    if not ok:
        _log.warning("upload_group_file_skip_unhealthy | file=%s | reason=%s", local_path.name, reason)
        return False

    abs_path = str(local_path.resolve())
    file_name = local_path.name
    try:
        await call_napcat_bot_api(
            bot,
            "upload_group_file",
            group_id=int(group_id),
            file=abs_path,
            name=file_name,
        )
        _log.info("upload_group_file_ok | group=%s | file=%s", group_id, file_name)
        return True
    except Exception as exc:
        _log.warning("upload_group_file_fail | %s", exc)
        return False


async def _compress_video_if_needed(src: Path) -> Path:
    """异步包装：大视频自动压缩。"""
    return await asyncio.to_thread(_compress_video_sync, src, _VIDEO_SEND_COMPRESS_THRESHOLD)


async def _inspect_video_issue(url: str) -> str:
    target = str(url or "").strip()
    if not target:
        return "视频链接为空"
    path = _as_local_video_path(target)
    if path is None:
        return ""
    ok, reason = await _probe_local_video_health(path)
    return "" if ok else reason


def _as_local_video_path(value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        local_raw = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", local_raw):
            local_raw = local_raw[1:]
        path = Path(local_raw)
    else:
        path = Path(raw)
    if path.exists() and path.is_file():
        return path
    return None


async def _probe_local_video_health(path: Path) -> tuple[bool, str]:
    return await asyncio.to_thread(_probe_local_video_health_sync, path)


def _probe_local_video_health_sync(path: Path) -> tuple[bool, str]:
    try:
        size = int(path.stat().st_size)
    except Exception:
        return False, "读取本地视频文件失败"

    if size <= 0:
        return False, "视频文件为空"
    if size < _MEDIA_MIN_VIDEO_BYTES:
        kb = max(1, int(size / 1024))
        return False, f"视频文件过小（{kb}KB），疑似损坏"

    try:
        with path.open("rb") as fp:
            head = fp.read(32)
    except Exception:
        return False, "读取本地视频头失败"
    if _looks_like_image_header(head):
        return False, "文件内容是图片，不是视频"
    if not _looks_like_video_header(head):
        return False, "视频容器签名异常，疑似损坏"

    stream_info = _read_media_stream_info_sync(path)
    has_audio = bool(normalize_text(stream_info.get("audio_codec", "")))
    can_detect_audio = bool(_FFPROBE_BIN or _FFMPEG_BIN)
    if not _FFPROBE_BIN:
        if can_detect_audio and not has_audio:
            return False, "视频无音轨（发送会没声音）"
        return True, ""

    cmd = [
        _FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height,duration,codec_name",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        if can_detect_audio and not has_audio:
            return False, "视频无音轨（发送会没声音）"
        return True, ""
    if proc.returncode != 0:
        return False, "ffprobe 无法解析该视频（可能损坏）"

    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        return False, "ffprobe 输出异常，视频结构不可用"

    duration = 0.0
    width = 0
    height = 0
    fmt = payload.get("format", {})
    if isinstance(fmt, dict):
        try:
            duration = max(duration, float(fmt.get("duration", 0.0) or 0.0))
        except Exception:
            pass

    streams = payload.get("streams", [])
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            codec_type = normalize_text(str(stream.get("codec_type", ""))).lower()
            if codec_type == "audio":
                has_audio = True
            codec = str(stream.get("codec_name", "")).strip().lower()
            if codec and codec in {"png", "mjpeg"} and duration <= 0:
                # 明显是封面流/图片流
                continue
            try:
                duration = max(duration, float(stream.get("duration", 0.0) or 0.0))
            except Exception:
                pass
            try:
                width = max(width, int(stream.get("width", 0) or 0))
                height = max(height, int(stream.get("height", 0) or 0))
            except Exception:
                pass

    if duration <= 0.8:
        return False, f"视频时长异常（{duration:.2f}s）"
    if width > 0 and height > 0 and (width < 64 or height < 64):
        return False, f"视频分辨率异常（{width}x{height}）"
    if can_detect_audio and not has_audio:
        return False, "视频无音轨（发送会没声音）"
    return True, ""


def _looks_like_image_header(head: bytes) -> bool:
    if not head:
        return False
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xFF\xD8\xFF")
        or head.startswith(b"GIF87a")
        or head.startswith(b"GIF89a")
        or head.startswith(b"BM")
        or (head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP")
    )


def _looks_like_video_header(head: bytes) -> bool:
    if len(head) < 4:
        return False
    if len(head) >= 12 and (head[4:8] == b"ftyp" or head[8:12] == b"ftyp"):
        return True
    if head.startswith(b"\x1A\x45\xDF\xA3"):  # WebM/MKV EBML
        return True
    if head.startswith(b"FLV"):
        return True
    if head.startswith(b"OggS"):
        return True
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    return False


__all__ = ["bind_runtime_dependencies"] + [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__")
]
