"""Auto-split from core/agent_tools.py — 社交功能工具 (日报/QZone)"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from core.agent_tools_types import PromptHint, ToolCallResult, ToolSchema
from core.agent_tools_registry import AgentToolRegistry
from core.napcat_compat import call_napcat_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning, looks_like_preferred_name_knowledge
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")


def _has_cross_user_portrait_access(context: dict[str, Any]) -> bool:
    level = normalize_text(str(context.get("permission_level", ""))).lower()
    return level == "super_admin"


def _resolve_portrait_target_user(
    args: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, ToolCallResult | None]:
    requested_user_id = normalize_text(str(args.get("user_id", "")))
    current_user_id = normalize_text(str(context.get("user_id", "")))
    if _has_cross_user_portrait_access(context):
        target_user_id = requested_user_id or current_user_id
        if not target_user_id:
            return "", ToolCallResult(ok=False, error="missing user_id")
        return target_user_id, None
    if not current_user_id:
        return "", ToolCallResult(ok=False, error="missing user_id")
    if requested_user_id and requested_user_id != current_user_id:
        return "", ToolCallResult(
            ok=False,
            error="permission_denied:user_scope",
            display="普通用户只能查看自己的画像信息。",
        )
    return current_user_id, None

def _register_daily_report_tools(registry: AgentToolRegistry) -> None:
    """Register daily report and user portrait tools."""

    async def _handle_daily_report(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        day_key = normalize_text(str(args.get("date", ""))).strip() or None
        try:
            report = memory.generate_daily_report(day_key)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc)[:200])
        if not report:
            return ToolCallResult(ok=True, data={}, display="今天还没有足够的聊天记录生成日报。")
        return ToolCallResult(ok=True, data={"report": report}, display=report)

    registry.register(
        ToolSchema(
            name="daily_report",
            description=(
                "生成群聊每日日报/总结。"
                "使用场景: 用户说'今日总结'、'群聊日报'、'今天聊了什么'时使用。"
                "可选参数 date 指定日期(YYYY-MM-DD)，默认今天。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD，默认今天"},
                },
                "required": [],
            },
            category="memory",
        ),
        _handle_daily_report,
    )

    async def _handle_user_portrait(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        user_id, denied = _resolve_portrait_target_user(args, context)
        if denied is not None:
            return denied
        try:
            portrait = memory.get_user_portrait(user_id)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc)[:200])
        if not portrait:
            return ToolCallResult(ok=True, data={}, display="暂无该用户的画像数据。")
        return ToolCallResult(ok=True, data={"portrait": portrait}, display=portrait)

    registry.register(
        ToolSchema(
            name="user_portrait",
            description=(
                "查看用户画像/档案。"
                "使用场景: 用户问'我的画像'、'XX是什么样的人'、'了解一下XX'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标用户QQ号，留空则查当前用户"},
                },
                "required": [],
            },
            category="memory",
        ),
        _handle_user_portrait,
    )


# ─────────────────────────────────────────────
# AI Method 桥接工具 — 将 tools.py 的 AI method 暴露给 Agent
# ─────────────────────────────────────────────


def _register_qzone_tools(registry: AgentToolRegistry, config: dict[str, Any]) -> None:
    """注册 QQ空间查询工具。"""

    registry.register(
        ToolSchema(
            name="get_qzone_profile",
            description=(
                "查询QQ用户的QQ空间详细资料: 昵称、性别、所在地、等级、VIP状态、个性签名等。\n"
                "比 get_user_info 返回更丰富的信息。\n"
                "使用场景: 用户说'查一下XX的空间'、'看看XX的QQ资料'、'XX的签名是什么'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq_number": {"type": "string", "description": "目标用户的QQ号"},
                },
                "required": ["qq_number"],
            },
            category="napcat",
        ),
        _make_qzone_handler("profile", config),
    )

    registry.register(
        ToolSchema(
            name="get_qzone_moods",
            description=(
                "获取QQ用户的QQ空间说说(动态)列表。\n"
                "返回最近的说说内容、发布时间、评论数等。\n"
                "使用场景: 用户说'看看XX的说说'、'XX最近发了什么'、'查一下XX的动态'时使用。\n"
                "需要对方空间对你可见。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq_number": {"type": "string", "description": "目标用户的QQ号"},
                    "count": {"type": "integer", "description": "获取条数，默认10，最多20"},
                },
                "required": ["qq_number"],
            },
            category="napcat",
        ),
        _make_qzone_handler("moods", config),
    )

    registry.register(
        ToolSchema(
            name="get_qzone_albums",
            description=(
                "获取QQ用户的QQ空间相册列表（仅列表，不含照片）。\n"
                "返回相册名称、描述、照片数量等。\n"
                "使用场景: 用户说'看看XX的相册'、'XX有哪些相册'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq_number": {"type": "string", "description": "目标用户的QQ号"},
                },
                "required": ["qq_number"],
            },
            category="napcat",
        ),
        _make_qzone_handler("albums", config),
    )

    registry.register(
        ToolSchema(
            name="analyze_qzone",
            description=(
                "分析QQ空间公开信息，聚合资料 + 最近说说 + 相册统计。\n"
                "优先用于'分析XX空间'、'看看XX空间什么风格'、'总结一下XX空间动态'这类请求。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq_number": {"type": "string", "description": "目标用户的QQ号"},
                    "mood_count": {"type": "integer", "description": "分析最近说说条数，默认8，最多20"},
                    "include_moods": {"type": "boolean", "description": "是否包含说说分析，默认true"},
                    "include_albums": {"type": "boolean", "description": "是否包含相册分析，默认true"},
                },
                "required": ["qq_number"],
            },
            category="napcat",
        ),
        _make_qzone_handler("analyze", config),
    )

    # ── QZone 相册照片列表 ──
    registry.register(
        ToolSchema(
            name="get_qzone_photos",
            description=(
                "获取QQ空间指定相册中的照片列表（含原图URL）。\n"
                "需要先用 get_qzone_albums 获取相册ID。\n"
                "使用场景: 用户说'看看XX相册里的照片'、'下载XX的照片'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq_number": {"type": "string", "description": "目标用户的QQ号"},
                    "album_id": {"type": "string", "description": "相册ID（从 get_qzone_albums 获取）"},
                    "count": {"type": "integer", "description": "获取照片数，默认30，最多100"},
                },
                "required": ["qq_number", "album_id"],
            },
            category="napcat",
        ),
        _make_qzone_handler("photos", config),
    )


def _make_qzone_handler(mode: str, config: dict[str, Any]) -> ToolHandler:
    """创建 QZone 工具 handler，优先使用 runtime config，兼容热重载。"""

    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        from core.qzone import QZoneClient, parse_cookie_string

        qz_cfg = _resolve_qzone_config(config, context)
        if not qz_cfg.get("enable", True):
            return ToolCallResult(ok=False, error="QZone 功能未启用")

        cookie_str = str(qz_cfg.get("cookie", "")).strip()
        if not cookie_str:
            return ToolCallResult(ok=False, error="未配置 QZone cookie，请在 config.yml 的 video_analysis.qzone.cookie 中配置")

        cookies = parse_cookie_string(cookie_str)
        if not (cookies.get("p_skey") or cookies.get("skey")):
            return ToolCallResult(ok=False, error="QZone cookie 缺少 p_skey/skey，请重新配置")

        qq_number = str(args.get("qq_number", "")).strip()
        if not qq_number or not qq_number.isdigit():
            return ToolCallResult(ok=False, error="无效的QQ号")

        try:
            client = QZoneClient(cookies)

            if mode == "profile":
                profile = await client.get_profile(qq_number)
                lines = [f"QQ空间资料 — {qq_number}"]
                if profile.nickname:
                    lines.append(f"昵称: {profile.nickname}")
                if profile.gender and profile.gender != "未知":
                    lines.append(f"性别: {profile.gender}")
                if profile.age:
                    lines.append(f"年龄: {profile.age}")
                if profile.location:
                    lines.append(f"所在地: {profile.location}")
                if profile.level:
                    lines.append(f"等级: {profile.level}")
                if profile.vip_info:
                    lines.append(f"会员: {profile.vip_info}")
                if profile.signature:
                    lines.append(f"个性签名: {profile.signature}")
                if profile.birthday:
                    lines.append(f"生日: {profile.birthday}")
                if profile.constellation:
                    lines.append(f"星座: {profile.constellation}")
                display = "\n".join(lines)
                return ToolCallResult(
                    ok=True,
                    data={"profile": _qzone_profile_payload(profile)},
                    display=display,
                )

            elif mode == "moods":
                count = _safe_int(args.get("count", 10), 10, min_value=1, max_value=20)
                moods = await client.get_moods(qq_number, count=count)
                if not moods:
                    return ToolCallResult(ok=True, data={"count": 0}, display=f"QQ {qq_number} 的说说为空或不可见")
                lines = [f"QQ {qq_number} 的最近说说 ({len(moods)}条):"]
                for i, m in enumerate(moods, 1):
                    content = m.content[:100] + ("..." if len(m.content) > 100 else "")
                    lines.append(f"\n[{i}] {m.create_time}")
                    lines.append(f"  {content}")
                    if m.pic_urls:
                        lines.append(f"  [含{len(m.pic_urls)}张图片]")
                    lines.append(f"  评论:{m.comment_count} 转发:{m.like_count}")
                display = "\n".join(lines)
                return ToolCallResult(
                    ok=True,
                    data={"count": len(moods), "moods": [_qzone_mood_payload(item) for item in moods]},
                    display=display,
                )

            elif mode == "albums":
                albums = await client.get_albums(qq_number)
                if not albums:
                    return ToolCallResult(ok=True, data={"count": 0}, display=f"QQ {qq_number} 的相册为空或不可见")
                lines = [f"QQ {qq_number} 的相册列表 ({len(albums)}个):"]
                for a in albums:
                    desc = f" — {a.desc}" if a.desc else ""
                    lines.append(f"  {a.name} ({a.photo_count}张){desc}")
                display = "\n".join(lines)
                return ToolCallResult(
                    ok=True,
                    data={"count": len(albums), "albums": [_qzone_album_payload(item) for item in albums]},
                    display=display,
                )

            elif mode == "analyze":
                mood_count = _safe_int(args.get("mood_count", 8), 8, min_value=1, max_value=20)
                include_moods = bool(args.get("include_moods", True))
                include_albums = bool(args.get("include_albums", True))
                analysis = await client.analyze_space(
                    qq_number,
                    mood_count=mood_count,
                    include_moods=include_moods,
                    include_albums=include_albums,
                )
                lines = [f"QQ空间分析 — {qq_number}"]
                if analysis.profile.nickname:
                    lines.append(f"昵称: {analysis.profile.nickname}")
                if analysis.profile.gender and analysis.profile.gender != "未知":
                    lines.append(f"性别: {analysis.profile.gender}")
                if analysis.profile.location:
                    lines.append(f"所在地: {analysis.profile.location}")
                if analysis.profile.signature:
                    lines.append(f"签名: {clip_text(analysis.profile.signature, 120)}")
                if include_moods:
                    lines.append(
                        f"最近说说: {len(analysis.moods)} 条"
                        + (f"（最近一条: {analysis.latest_mood_time}）" if analysis.latest_mood_time else "")
                    )
                    if analysis.avg_mood_length > 0:
                        lines.append(f"说说平均长度: {analysis.avg_mood_length} 字")
                    if analysis.moods:
                        lines.append(f"图文说说占比: {int(round(analysis.image_post_ratio * 100, 0))}%")
                    if analysis.mood_keywords:
                        lines.append(f"高频关键词: {', '.join(analysis.mood_keywords[:6])}")
                if include_albums:
                    lines.append(
                        f"相册: {len(analysis.albums)} 个 / 总照片约 {analysis.total_album_photos} 张"
                    )
                    if analysis.albums:
                        top_album_lines = []
                        for item in analysis.albums[:3]:
                            if item.name:
                                top_album_lines.append(f"{item.name}({item.photo_count})")
                        if top_album_lines:
                            lines.append(f"相册示例: {', '.join(top_album_lines)}")
                display = "\n".join(lines)
                return ToolCallResult(
                    ok=True,
                    data={
                        "qq_number": qq_number,
                        "profile": _qzone_profile_payload(analysis.profile),
                        "moods": [_qzone_mood_payload(item) for item in analysis.moods],
                        "albums": [_qzone_album_payload(item) for item in analysis.albums],
                        "summary": {
                            "mood_keywords": analysis.mood_keywords,
                            "image_post_ratio": analysis.image_post_ratio,
                            "avg_mood_length": analysis.avg_mood_length,
                            "total_album_photos": analysis.total_album_photos,
                            "latest_mood_time": analysis.latest_mood_time,
                        },
                    },
                    display=display,
                )

            elif mode == "photos":
                album_id = str(args.get("album_id", "")).strip()
                if not album_id:
                    return ToolCallResult(ok=False, error="缺少 album_id 参数，请先用 get_qzone_albums 获取相册ID")
                count = _safe_int(args.get("count", 30), 30, min_value=1, max_value=100)
                photos = await client.get_photos(qq_number, album_id, count=count)
                if not photos:
                    return ToolCallResult(ok=True, data={"count": 0}, display=f"相册 {album_id} 为空或不可见")
                lines = [f"QQ {qq_number} 相册照片 ({len(photos)}张):"]
                for i, p in enumerate(photos[:10], 1):
                    desc = f" — {p.desc}" if p.desc else ""
                    size = f" {p.width}x{p.height}" if p.width and p.height else ""
                    lines.append(f"  [{i}] {p.create_time}{size}{desc}")
                if len(photos) > 10:
                    lines.append(f"  ... 共 {len(photos)} 张")
                display = "\n".join(lines)
                photo_data = [
                    {
                        "photo_id": p.photo_id, "url": p.url, "thumb_url": p.thumb_url,
                        "name": p.name, "desc": p.desc, "create_time": p.create_time,
                        "width": p.width, "height": p.height,
                    }
                    for p in photos
                ]
                return ToolCallResult(
                    ok=True,
                    data={"count": len(photos), "album_id": album_id, "photos": photo_data},
                    display=display,
                )

            return ToolCallResult(ok=False, error=f"unknown_qzone_mode: {mode}")

        except PermissionError as exc:
            return ToolCallResult(ok=False, error=_normalize_qzone_tool_error(exc))
        except Exception as exc:
            _log.warning("qzone_tool_error | mode=%s | qq=%s | %s", mode, qq_number, exc)
            return ToolCallResult(ok=False, error=_normalize_qzone_tool_error(exc))

    return handler


# ── ScrapyLLM 智能网页抓取工具 ──

