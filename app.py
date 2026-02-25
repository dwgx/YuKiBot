from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from core.engine import EngineMessage, YukikoEngine
from core.queue import GroupQueueDispatcher
from utils.text import clip_text, normalize_text

_MEDIA_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
_MEDIA_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_VIDEO_PROBE_MAX_BYTES = 512 * 1024
_MEDIA_MIN_VIDEO_BYTES = 180 * 1024
_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
_FFPROBE_BIN = shutil.which("ffprobe")
if not _FFPROBE_BIN:
    _local_app = os.environ.get("LOCALAPPDATA", "")
    if _local_app:
        _probe_candidate = os.path.join(_local_app, "Microsoft", "WinGet", "Links", "ffprobe.exe") if os.name == "nt" else ""
        if _probe_candidate and os.path.isfile(_probe_candidate):
            _FFPROBE_BIN = _probe_candidate

_log = logging.getLogger("yukiko.app")


def _find_ffmpeg_bin() -> str:
    """查找 ffmpeg，兼容 winget 安装路径。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        winget_dir = os.path.join(local_app, "Microsoft", "WinGet", "Links")
        candidate = os.path.join(winget_dir, "ffmpeg.exe") if os.name == "nt" else os.path.join(winget_dir, "ffmpeg")
        if os.path.isfile(candidate):
            os.environ["PATH"] = winget_dir + os.pathsep + os.environ.get("PATH", "")
            return candidate
    return ""


_FFMPEG_BIN = _find_ffmpeg_bin()


def _generate_video_thumbnail_sync(video_path: Path) -> Path | None:
    """用 ffmpeg 为视频生成缩略图，返回 jpg 路径或 None。"""
    if not _FFMPEG_BIN:
        return None
    thumb_path = video_path.with_suffix(".thumb.jpg")
    if thumb_path.exists() and thumb_path.stat().st_size > 1000:
        return thumb_path
    cmd = [
        _FFMPEG_BIN, "-y",
        "-ss", "1",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "5",
        str(thumb_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        if proc.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 500:
            _log.info("thumbnail_ok | %s | %d bytes", thumb_path.name, thumb_path.stat().st_size)
            return thumb_path
        _log.warning("thumbnail_fail | rc=%d | stderr=%s", proc.returncode, (proc.stderr or b"")[:200])
    except Exception as exc:
        _log.warning("thumbnail_error | %s", exc)
    return None


async def _generate_video_thumbnail(video_path: Path) -> Path | None:
    return await asyncio.to_thread(_generate_video_thumbnail_sync, video_path)


def create_engine() -> YukikoEngine:
    root = Path(__file__).resolve().parent
    return YukikoEngine.from_default_paths(project_root=root)


def register_handlers(engine: YukikoEngine) -> None:
    dispatcher = GroupQueueDispatcher(engine.config.get("queue", {}))
    router = on_message(priority=90, block=False)
    bot_cfg = engine.config.get("bot", {}) if isinstance(engine.config, dict) else {}
    reply_with_quote = bool(bot_cfg.get("reply_with_quote", True))
    reply_with_at = bool(bot_cfg.get("reply_with_at", False))
    multi_reply_enable = bool(bot_cfg.get("multi_reply_enable", True))
    multi_reply_max_chunks = max(1, int(bot_cfg.get("multi_reply_max_chunks", 4)))
    multi_reply_max_lines = max(1, int(bot_cfg.get("multi_reply_max_lines", 1)))
    multi_reply_max_chars = max(80, int(bot_cfg.get("multi_reply_max_chars", 220)))
    multi_image_max_count = max(1, int(bot_cfg.get("multi_image_max_count", 9)))
    multi_image_interval_ms = max(0, int(bot_cfg.get("multi_image_interval_ms", 150)))

    # 启动时验证各平台 cookie 有效性
    @nonebot.get_driver().on_startup
    async def _check_cookies_on_startup():
        try:
            from core.cookie_auth import check_all_cookies
            results = await check_all_cookies(engine.config)
            for platform, valid in results.items():
                if not valid:
                    _log.warning("cookie_expired | %s | 建议重新登录: /yuki cookie %s", platform, platform)
        except Exception as e:
            _log.debug("cookie_check_skip | %s", e)

    @router.handle()
    async def handle_message(bot: Bot, event: MessageEvent) -> None:
        if str(event.get_user_id()) == str(bot.self_id):
            return

        conversation_id = _build_conversation_id(event)
        at_targets = _extract_at_targets(event)
        reply_to_message_id = _extract_reply_message_id(event)
        reply_to_user_id = ""
        if reply_to_message_id:
            reply_to_user_id = await _resolve_reply_to_user_id(bot, reply_to_message_id)

        mentioned = _is_mentioned(bot, event, at_targets=at_targets) or (
            reply_to_user_id and str(reply_to_user_id) == str(bot.self_id)
        )
        at_other_user_ids = [item for item in at_targets if item not in {"all", str(bot.self_id)}]
        if reply_to_user_id and str(reply_to_user_id) != str(bot.self_id):
            at_other_user_ids.append(str(reply_to_user_id))
        at_other_user_ids = list(dict.fromkeys(at_other_user_ids))
        at_other_user_only = (
            (bool(at_targets) and not mentioned)
            or (bool(reply_to_user_id) and str(reply_to_user_id) != str(bot.self_id))
        )
        raw_segments = _extract_raw_segments(event)
        has_media = _has_media_segments(raw_segments)
        text = _extract_text_segments(raw_segments) or event.get_plaintext().strip()
        # nickname 前缀也视为 mentioned（如 "yuki点歌"、"雪 帮我搜"）
        if not mentioned and text:
            _nick_lower = text.lower()
            _bot_nicks = {str(n).lower() for n in bot_cfg.get("nicknames", []) if n}
            _bot_nicks.add(str(bot_cfg.get("name", "")).lower())
            _bot_nicks.discard("")
            for _nick in _bot_nicks:
                if _nick_lower.startswith(_nick):
                    mentioned = True
                    text = text[len(_nick):].lstrip()
                    break
        if not text and mentioned and not has_media:
            # Mention-only prompt fallback.
            text = "__mention_only__"
        if has_media:
            # Normalize media placeholders produced by adapter text rendering.
            clean_text = _strip_media_placeholder_text(text)
            media_event = _build_multimodal_text(raw_segments, mentioned=mentioned)
            text = f"{media_event}\n{clean_text}" if clean_text else media_event
        if not text:
            return

        async def api_call(api: str, **kwargs: Any) -> Any:
            return await bot.call_api(api, **kwargs)

        # ── 管理员指令拦截 ──
        if engine.admin.is_admin_command(text):
            admin_reply = await engine.admin.handle_command(
                text=text,
                user_id=str(event.get_user_id()),
                group_id=int(getattr(event, "group_id", 0) or 0),
                engine=engine,
                api_call=api_call,
            )
            if admin_reply:
                await bot.send(event=event, message=Message(admin_reply))
            return

        seq = dispatcher.next_seq(conversation_id)
        trace_id = _build_trace_id(conversation_id=conversation_id, seq=seq)

        payload = EngineMessage(
            conversation_id=conversation_id,
            user_id=str(event.get_user_id()),
            user_name=_extract_user_name(event),
            text=text,
            message_id=str(getattr(event, "message_id", "")),
            seq=seq,
            raw_segments=raw_segments,
            queue_depth=dispatcher.pending_count(conversation_id),
            mentioned=mentioned,
            is_private=getattr(event, "message_type", "") == "private",
            timestamp=_event_timestamp(event),
            group_id=int(getattr(event, "group_id", 0) or 0),
            bot_id=str(bot.self_id),
            at_other_user_only=at_other_user_only,
            at_other_user_ids=at_other_user_ids,
            reply_to_message_id=reply_to_message_id,
            reply_to_user_id=reply_to_user_id,
            api_call=api_call,
            trace_id=trace_id,
        )
        video_pre_ack_sent = False
        queue_cfg = engine.config.get("queue", {}) if isinstance(engine.config, dict) else {}
        video_heavy_request = _looks_like_video_heavy_request(text=text, raw_segments=raw_segments)
        default_video_timeout = max(dispatcher.process_timeout_seconds + 45, 190)
        video_process_timeout = max(
            dispatcher.process_timeout_seconds,
            int(queue_cfg.get("video_process_timeout_seconds", default_video_timeout)),
        )
        process_timeout_override = video_process_timeout if video_heavy_request else None
        allow_late_emit_on_timeout = bool(video_heavy_request and queue_cfg.get("video_late_emit_on_timeout", True))

        async def process() -> Any:
            return await engine.handle_message(payload)

        async def send_response(result: Any) -> None:
            if getattr(result, "action", "") == "ignore":
                return

            async def send_msg(msg: Message) -> None:
                await _safe_send(bot=bot, event=event, message=msg)

            action = str(getattr(result, "action", "") or "")
            reply_text = _normalize_reply_text(str(getattr(result, "reply_text", "") or ""))
            image_url = str(getattr(result, "image_url", "") or "")
            raw_image_urls = getattr(result, "image_urls", []) or []
            image_urls: list[str] = []
            if isinstance(raw_image_urls, list):
                image_urls = [
                    normalize_text(str(item))
                    for item in raw_image_urls
                    if normalize_text(str(item))
                ]
            if image_url and image_url not in image_urls:
                image_urls.insert(0, image_url)
            if image_urls and not image_url:
                image_url = image_urls[0]
            video_url = str(getattr(result, "video_url", "") or "")
            cover_url = str(getattr(result, "cover_url", "") or "")
            record_b64 = str(getattr(result, "record_b64", "") or "")
            audio_file = str(getattr(result, "audio_file", "") or "")
            delivered = False
            video_issue = ""
            video_analysis_requested = bool(getattr(result, "pre_ack", ""))
            prefix = _build_reply_prefix(
                event=event,
                quote_message_id=str(getattr(event, "message_id", "") or ""),
                sender_user_id=str(event.get_user_id()),
                enable_quote=reply_with_quote,
                enable_at=reply_with_at,
            )
            prefixed_sent = False

            if video_url and video_analysis_requested and not video_pre_ack_sent:
                progress = Message()
                if not prefixed_sent:
                    progress += prefix
                    prefixed_sent = True
                pre_ack_text = str(getattr(result, "pre_ack", "") or "OK，我现在去深度分析这个视频，稍等。")
                progress += Message(pre_ack_text)
                await send_msg(progress)
                delivered = True

            # ── 语音/音频消息（点歌功能）──
            if record_b64 or audio_file:
                voice_msg = Message()
                if reply_text:
                    if not prefixed_sent:
                        voice_msg += prefix
                        prefixed_sent = True
                    voice_msg += Message(reply_text)
                    await send_msg(voice_msg)
                    delivered = True
                    # 语音前置文案已发送，避免后续文本分段重复发送一遍。
                    reply_text = ""
                # 发送语音条：优先 file://（NapCat 对本地文件时长识别最准确）
                sent_voice = False
                if audio_file:
                    try:
                        audio_uri = ""
                        try:
                            p = Path(audio_file).expanduser().resolve()
                            if p.exists():
                                audio_uri = p.as_uri()
                        except Exception:
                            audio_uri = ""
                        if not audio_uri:
                            normalized = audio_file.replace("\\", "/")
                            if not normalized.startswith("/"):
                                normalized = f"/{normalized}"
                            audio_uri = f"file://{normalized}"
                        await send_msg(Message(MessageSegment.record(file=audio_uri)))
                        sent_voice = True
                    except Exception as _voice_file_err:
                        _log.warning("voice_send_file_fail | %s", _voice_file_err)
                if not sent_voice and record_b64:
                    try:
                        await send_msg(Message(MessageSegment.record(file=f"base64://{record_b64}")))
                        sent_voice = True
                    except Exception as _voice_b64_err:
                        _log.warning("voice_send_b64_fail | %s", _voice_b64_err)
                if sent_voice:
                    delivered = True
                else:
                    fallback = Message()
                    if not prefixed_sent:
                        fallback += prefix
                        prefixed_sent = True
                    fallback += Message(reply_text or "语音发送失败了。")
                    await send_msg(fallback)
                    delivered = True

            if video_url:
                video_issue = await _inspect_video_issue(video_url)
                if video_issue:
                    warn = Message()
                    if not prefixed_sent:
                        warn += prefix
                        prefixed_sent = True
                    warn += Message(f"意外日志：{video_issue}")
                    warn += Message("\n意外发现：视频资源疑似损坏，我先走降级方案给你来源链接。")
                    await send_msg(warn)
                    delivered = True

            text_chunks: list[str] = []
            if reply_text:
                if multi_reply_enable:
                    chunk_max_lines = multi_reply_max_lines
                    chunk_max_chars = multi_reply_max_chars
                    chunk_max_count = multi_reply_max_chunks
                    if action == "search":
                        chunk_max_lines = max(chunk_max_lines, 4)
                        chunk_max_chars = max(chunk_max_chars, 260)
                        chunk_max_count = max(chunk_max_count, 6)
                    if video_analysis_requested:
                        chunk_max_lines = max(chunk_max_lines, 6)
                        chunk_max_chars = max(chunk_max_chars, 360)
                        chunk_max_count = max(chunk_max_count, 8)
                    text_chunks = _split_reply_chunks(
                        reply_text,
                        max_lines=chunk_max_lines,
                        max_chars=chunk_max_chars,
                        max_chunks=chunk_max_count,
                    )
                if not text_chunks:
                    text_chunks = [reply_text]

            # ── 有视频时：文本+封面图 → 视频分开发 ──
            if video_url:
                # 1) 发文本（第一条带 prefix + 封面图）
                cover_seg = None
                if cover_url:
                    cover_seg = await _build_image_segment(cover_url)
                if not cover_seg and not video_issue:
                    # 没有远程封面，用本地生成的缩略图
                    local_vp = Path(video_url) if not video_url.startswith(("http://", "https://", "file://")) else None
                    if local_vp and local_vp.exists():
                        thumb = await _generate_video_thumbnail(local_vp)
                        if thumb:
                            cover_seg = await _build_image_segment(str(thumb.resolve()))

                first_chunk = True
                for chunk in text_chunks:
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += Message(chunk)
                    if first_chunk and cover_seg is not None:
                        msg += cover_seg
                        cover_seg = None
                    first_chunk = False
                    await send_msg(msg)
                    delivered = True

                # 如果没有文本但有封面，单独发封面
                if not text_chunks and cover_seg is not None:
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += cover_seg
                    await send_msg(msg)
                    delivered = True

                # 2) 单独发视频
                seg = None if video_issue else await _build_video_segment(video_url)
                if seg is not None:
                    try:
                        await send_msg(Message(seg))
                    except Exception as send_exc:
                        _log.warning("video_send_fail | %s | trying upload_group_file fallback", send_exc)
                        # 尝试用 upload_group_file 上传（适合大文件）
                        uploaded = await _try_upload_group_file(
                            bot=bot, event=event, video_url=video_url,
                        )
                        if not uploaded:
                            direct_url = str(video_url or "").strip()
                            fallback = Message()
                            if re.match(r"^https?://", direct_url, flags=re.IGNORECASE):
                                fallback += Message(f"视频发送失败了，来源链接：{direct_url}")
                            else:
                                fallback += Message("视频发送失败了，你换一个分享链接我再试。")
                            await send_msg(fallback)
                else:
                    direct_url = str(video_url or "").strip()
                    fallback_msg = Message()
                    if re.match(r"^https?://", direct_url, flags=re.IGNORECASE):
                        fallback_msg += Message(f"视频暂时不能直发，来源链接：{direct_url}")
                    else:
                        fallback_msg += Message("视频暂时不能直发，你换一个分享链接我再试。")
                    await send_msg(fallback_msg)
                delivered = True

            else:
                # ── 无视频：正常发文本+图片 ──
                for chunk in text_chunks:
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += Message(chunk)
                    await send_msg(msg)
                    delivered = True

                if image_urls:
                    send_count = min(len(image_urls), multi_image_max_count)
                    if send_count > 1:
                        merged_text = normalize_text(" ".join(text_chunks))
                        if "图文" not in merged_text and "共" not in merged_text:
                            tip = Message()
                            if not prefixed_sent:
                                tip += prefix
                                prefixed_sent = True
                            tip += Message(f"识别到这是图文作品，共 {len(image_urls)} 张，先发你 {send_count} 张。")
                            await send_msg(tip)
                            delivered = True

                    for idx, item_url in enumerate(image_urls[:send_count], 1):
                        seg = await _build_image_segment(item_url)
                        msg = Message()
                        if not prefixed_sent:
                            msg += prefix
                            prefixed_sent = True
                        if seg is not None:
                            msg += seg
                        else:
                            msg += Message(f"第 {idx} 张图片发送失败，链接受限。")
                        await send_msg(msg)
                        delivered = True
                        if idx < send_count and multi_image_interval_ms > 0:
                            await asyncio.sleep(multi_image_interval_ms / 1000)

                    if len(image_urls) > send_count:
                        tip_more = Message()
                        if not prefixed_sent:
                            tip_more += prefix
                            prefixed_sent = True
                        tip_more += Message(f"其余 {len(image_urls) - send_count} 张先省略。")
                        await send_msg(tip_more)
                        delivered = True

            if delivered:
                engine.on_delivery_success(
                    conversation_id=payload.conversation_id,
                    user_id=payload.user_id,
                    action=action,
                )
            _log.info(
                "send_final | trace=%s | conversation=%s | seq=%s | action=%s | delivered=%s | has_video=%s | has_image=%s",
                payload.trace_id,
                payload.conversation_id,
                payload.seq,
                action,
                delivered,
                bool(video_url),
                bool(image_url or image_urls),
            )

        async def send_overload_notice(text_notice: str) -> None:
            if not text_notice:
                return
            await _safe_send(bot=bot, event=event, message=Message(text_notice))

        async def on_dispatch_complete(dispatch: Any) -> None:
            status = str(getattr(dispatch, "status", ""))
            dispatch_trace = str(getattr(dispatch, "trace_id", payload.trace_id))
            engine.logger.info(
                "queue_final | trace=%s | conversation=%s | seq=%s | status=%s | reason=%s | pending=%s",
                dispatch_trace,
                str(getattr(dispatch, "conversation_id", conversation_id)),
                str(getattr(dispatch, "seq", seq)),
                status,
                str(getattr(dispatch, "reason", "")),
                str(getattr(dispatch, "pending_count", 0)),
            )
            if status == "process_timeout_deferred":
                msg = Message()
                msg += _build_reply_prefix(
                    event=event,
                    quote_message_id=str(getattr(event, "message_id", "") or ""),
                    sender_user_id=str(event.get_user_id()),
                    enable_quote=reply_with_quote,
                    enable_at=reply_with_at,
                )
                msg += Message("这条请求还在后台继续处理，拿到稳定结果后我会自动发出来。")
                await _safe_send(bot=bot, event=event, message=msg)
                return
            if status in {"process_timeout", "process_error", "late_timeout", "late_process_error"}:
                msg = Message()
                msg += _build_reply_prefix(
                    event=event,
                    quote_message_id=str(getattr(event, "message_id", "") or ""),
                    sender_user_id=str(event.get_user_id()),
                    enable_quote=reply_with_quote,
                    enable_at=reply_with_at,
                )
                if status in {"process_timeout", "late_timeout"}:
                    msg += Message("意外日志：这次处理超时了。")
                    msg += Message("\n意外发现：视频链路超时，没拿到稳定结果。你重发同一条链接我继续试。")
                else:
                    msg += Message("意外日志：这次处理失败了。")
                    msg += Message("\n意外发现：解析阶段出现异常，我先停在安全状态。你重发链接我会走静默重试。")
                await _safe_send(bot=bot, event=event, message=msg)

        high_priority = bool(payload.mentioned or payload.is_private or text.startswith("/"))
        dispatch_result = await dispatcher.submit(
            conversation_id=conversation_id,
            seq=seq,
            created_at=payload.timestamp,
            process=process,
            send=send_response,
            high_priority=high_priority,
            process_timeout_seconds=process_timeout_override,
            allow_late_emit_on_timeout=allow_late_emit_on_timeout,
            trace_id=payload.trace_id,
            send_overload_notice=send_overload_notice,
            on_complete=on_dispatch_complete,
        )
        if dispatch_result.status in {"dropped", "expired"}:
            engine.logger.info(
                "queue_drop | trace=%s | conversation=%s | seq=%d | reason=%s",
                dispatch_result.trace_id,
                dispatch_result.conversation_id,
                dispatch_result.seq,
                dispatch_result.reason,
            )


def _event_timestamp(event: MessageEvent) -> datetime:
    ts = getattr(event, "time", None)
    if isinstance(ts, int):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.now(timezone.utc)


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


def _looks_like_video_heavy_request(text: str, raw_segments: list[dict[str, Any]]) -> bool:
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        if normalize_text(str(seg.get("type", ""))).lower() == "video":
            return True

    content = normalize_text(text).lower()
    if not content:
        return False
    if re.search(r"(?:\bbv[0-9a-z]{8,}\b|\bav\d{5,}\b)", content, flags=re.IGNORECASE):
        return True
    if re.search(r"https?://[^\s]+", content, flags=re.IGNORECASE):
        platform_cues = ("bilibili.com", "b23.tv", "douyin.com", "kuaishou.com", "acfun")
        if any(cue in content for cue in platform_cues):
            return True

    cues = (
        "视频",
        "发出来",
        "发视频",
        "转发视频",
        "解析这个视频",
        "解析视频",
        "下载这个视频",
        "找视频",
        "搜视频",
        "b站",
        "哔哩",
        "抖音",
        "快手",
        "acfun",
        "video",
    )
    return any(cue in content for cue in cues)


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
            targets.extend(re.findall(r"\[at:qq=(\d+)\]", raw, flags=re.IGNORECASE))
            targets.extend(re.findall(r"CQ:at,qq=(\d+)", raw, flags=re.IGNORECASE))

    uniq: list[str] = []
    seen: set[str] = set()
    for item in targets:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    return uniq


def _extract_reply_message_id(event: MessageEvent) -> str:
    try:
        for segment in event.get_message():
            if str(segment.type).lower() != "reply":
                continue
            message_id = str(segment.data.get("id") or segment.data.get("message_id") or "").strip()
            if message_id:
                return message_id
    except Exception:
        pass

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


async def _resolve_reply_to_user_id(bot: Bot, reply_message_id: str) -> str:
    mid = str(reply_message_id or "").strip()
    if not mid:
        return ""
    try:
        data = await bot.call_api("get_msg", message_id=int(mid))
    except Exception:
        try:
            data = await bot.call_api("get_msg", message_id=mid)
        except Exception:
            return ""
    if not isinstance(data, dict):
        return ""
    sender = data.get("sender", {})
    if isinstance(sender, dict):
        uid = sender.get("user_id")
        if uid is not None:
            return str(uid)
    uid = data.get("user_id")
    return str(uid) if uid is not None else ""


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


async def _safe_send(bot: Bot, event: MessageEvent, message: Message) -> None:
    try:
        await bot.send(event=event, message=message)
        return
    except Exception:
        # NapCat may fail on reply segment; resend without reply segment as fallback.
        fallback = _strip_reply_segments(message)
        if not fallback:
            raise
        if str(fallback) == str(message):
            raise
        await bot.send(event=event, message=fallback)


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


def _build_multimodal_text(raw_segments: list[dict[str, Any]], mentioned: bool) -> str:
    media_tokens: list[str] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type in {"image", "video", "record", "audio", "forward"}:
            data = seg.get("data", {}) or {}
            url = normalize_text(str(data.get("url", "")))
            if url:
                media_tokens.append(f"{seg_type}:{clip_text(url, 120)}")
            else:
                media_tokens.append(seg_type)

    if not media_tokens:
        return ""

    prefix = "MULTIMODAL_EVENT_AT" if mentioned else "MULTIMODAL_EVENT"
    human = "user mentioned bot and sent multimodal message:" if mentioned else "user sent multimodal message:"
    return f"{prefix} {human}{' | '.join(media_tokens)}"


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
    truncated = False

    for token in tokens:
        if token is None:
            if not current_lines:
                continue
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
            if len(chunks) >= max_chunks:
                truncated = True
                break
            continue

        line_len = len(token)
        projected_lines = len(current_lines) + 1
        projected_len = current_len + (1 if current_lines else 0) + line_len
        if current_lines and (projected_lines > max_lines or projected_len > max_chars):
            chunks.append("\n".join(current_lines))
            if len(chunks) >= max_chunks:
                truncated = True
                break
            current_lines = [token]
            current_len = line_len
        else:
            current_lines.append(token)
            current_len = projected_len

    if not truncated and current_lines and len(chunks) < max_chunks:
        chunks.append("\n".join(current_lines))
    elif current_lines and len(chunks) >= max_chunks:
        truncated = True

    if truncated and chunks:
        tail = chunks[-1].rstrip()
        if not tail.endswith("..."):
            tail = tail.rstrip("。！？…")
            chunks[-1] = f"{tail}..."

    return [chunk for chunk in chunks if chunk.strip()]


def _split_line_by_sentence(line: str, max_chars: int) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    segments = [seg.strip() for seg in re.split(r"(?<=[。！？、])", text) if seg.strip()]
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
    parts: list[str] = []
    for idx in range(0, len(content), max_chars):
        piece = content[idx : idx + max_chars].strip()
        if piece:
            parts.append(piece)
    return parts


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
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if not data or len(data) > _MEDIA_MAX_IMAGE_BYTES:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return MessageSegment.image(f"base64://{b64}")


async def _build_image_segment_from_remote_url(url: str) -> MessageSegment | None:
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
    looks_like_image = "image/" in content_type
    if not looks_like_image:
        final_url = str(response.url)
        looks_like_image = final_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))
    if not looks_like_image:
        return None

    b64 = base64.b64encode(data).decode("ascii")
    return MessageSegment.image(f"base64://{b64}")


async def _video_seg_with_thumb(local_path: Path) -> MessageSegment:
    """构建带缩略图的视频 MessageSegment，大文件自动压缩。"""
    # 大视频先压缩，避免 NapCat WebSocket 超时
    actual_path = await _compress_video_if_needed(local_path)
    abs_path = str(actual_path.resolve())
    thumb = await _generate_video_thumbnail(actual_path)
    if thumb is not None:
        abs_thumb = str(thumb.resolve())
        _log.info("video_seg_with_thumb | video=%s | thumb=%s", actual_path.name, thumb.name)
        return MessageSegment("video", {"file": abs_path, "thumb": abs_thumb})
    return MessageSegment.video(abs_path)


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


async def _download_remote_video_to_tmp(url: str) -> Path | None:
    """下载远程视频到本地临时文件，供 NapCat 发送。返回本地路径或 None。"""
    try:
        _VIDEO_TMP_DIR.mkdir(parents=True, exist_ok=True)
        # 用 URL hash 做文件名避免冲突
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        tmp_path = _VIDEO_TMP_DIR / f"{url_hash}.mp4"
        if tmp_path.exists() and tmp_path.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            return tmp_path
        async with httpx.AsyncClient(
            timeout=_VIDEO_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _MEDIA_USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                total = 0
                with tmp_path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > _VIDEO_DOWNLOAD_MAX_BYTES:
                            tmp_path.unlink(missing_ok=True)
                            return None
                        fp.write(chunk)
        if tmp_path.exists() and tmp_path.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
            return tmp_path
        tmp_path.unlink(missing_ok=True)
        return None
    except Exception:
        return None


def _compress_video_sync(src: Path, max_bytes: int = _VIDEO_SEND_COMPRESS_THRESHOLD) -> Path:
    """用 ffmpeg 压缩视频到目标大小以内，返回压缩后路径（可能是原路径）。"""
    if not _FFMPEG_BIN:
        return src
    try:
        size = src.stat().st_size
    except Exception:
        return src
    if size <= max_bytes:
        return src

    compressed = src.with_suffix(".compressed.mp4")
    if compressed.exists() and compressed.stat().st_size > _MEDIA_MIN_VIDEO_BYTES:
        return compressed

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

    abs_path = str(local_path.resolve())
    file_name = local_path.name
    try:
        await bot.call_api(
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

    if not _FFPROBE_BIN:
        return True, ""

    cmd = [
        _FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "stream=width,height,duration,codec_name",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
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
