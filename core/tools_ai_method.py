"""ToolExecutor AI 方法 mixin — 动态方法执行与 schema 构建。

从 core/tools.py 拆分。"""
from __future__ import annotations

import asyncio
from typing import Any
import logging as _logging

from core.tools_types import ToolResult, _tool_trace_tag
from utils.text import clip_text, normalize_text

_tool_log = _logging.getLogger("yukiko.tools")


class ToolAiMethodMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    def get_ai_method_schemas(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._ai_method_schemas]

    def _build_ai_method_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "browser.fetch_url",
                "scope": "browser",
                "description": "访问网页并返回状态、最终链接、内容类型和文本摘要",
                "args_schema": {"url": "string"},
            },
            {
                "name": "browser.resolve_video",
                "scope": "browser",
                "description": "解析抖音/快手/B站/AcFun 或直链视频，返回可发送 video_url",
                "args_schema": {"url": "string"},
            },
            {
                "name": "browser.resolve_image",
                "scope": "browser",
                "description": "校验图片链接是否可直发，返回可发送 image_url",
                "args_schema": {"url": "string"},
            },
            {
                "name": "douyin.search_video",
                "scope": "douyin",
                "description": "在抖音搜索视频/图文详情链接，优先返回可解析或可发送结果",
                "args_schema": {
                    "query": "string",
                    "limit": "int(optional, default=5)",
                },
            },
            {
                "name": "browser.github_search",
                "scope": "browser",
                "description": "在 GitHub 搜索开源仓库，返回仓库名、星标、简介和链接",
                "args_schema": {
                    "query": "string",
                    "language": "string(optional)",
                    "stars_min": "int(optional)",
                    "sort": "stars|updated(optional)",
                },
            },
            {
                "name": "browser.github_readme",
                "scope": "browser",
                "description": '读取指定 GitHub 仓库 README 摘要，便于"学习这个仓库"',
                "args_schema": {
                    "repo": "owner/repo(optional)",
                    "url": "string(optional)",
                    "max_chars": "int(optional)",
                },
            },
            {
                "name": "local.read_text",
                "scope": "local",
                "description": "读取本地文本文件（受 allowlist 限制）",
                "args_schema": {"path": "string", "max_chars": "int(optional)"},
            },
            {
                "name": "local.media_from_path",
                "scope": "local",
                "description": "从本地路径发送图片或视频（受 allowlist 限制）",
                "args_schema": {"path": "string"},
            },
            {
                "name": "media.qq_avatar",
                "scope": "media",
                "description": "按 QQ 号或消息上下文获取 QQ 头像",
                "args_schema": {"qq": "string(optional)"},
            },
            {
                "name": "media.pick_image_from_message",
                "scope": "media",
                "description": "从当前消息里提取图片并返回可发送 image_url",
                "args_schema": {},
            },
            {
                "name": "media.pick_video_from_message",
                "scope": "media",
                "description": "从当前消息里提取视频并返回可发送 video_url",
                "args_schema": {},
            },
            {
                "name": "media.pick_audio_from_message",
                "scope": "media",
                "description": "从当前消息里提取语音/音频链接并返回文本说明",
                "args_schema": {},
            },
            {
                "name": "media.analyze_image",
                "scope": "media",
                "description": "识别图片内容；可指定 url，未指定时从当前消息里取图",
                "args_schema": {"url": "string(optional)"},
            },
            {
                "name": "video.analyze",
                "scope": "media",
                "description": (
                    "深度分析视频内容：提取关键信息并用视觉 AI 识别画面内容；"
                    "可抓取 B 站标签、弹幕热词、热评或抖音详情数据；"
                    "用户要求分析、评价、解说、总结视频时可使用此方法；"
                    "需要提供视频 URL，返回结构化分析结果。"
                ),
                "args_schema": {
                    "url": "string",
                    "depth": "metadata|rich_metadata|multimodal(optional, default=auto)",
                },
            },
        ]

    async def _execute_ai_method(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
        conversation_id: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        raw_segments: list[dict[str, Any]],
        bot_id: str,
    ) -> ToolResult | None:
        name = normalize_text(method_name).lower()
        if not name:
            return None
        _tool_log.info(
            "tool_method_call%s | name=%s | args=%s",
            _tool_trace_tag(),
            name,
            clip_text(normalize_text(repr(method_args)), 220),
        )

        if name.startswith("browser.") and not self._tool_interface_browser_enable:
            return ToolResult(
                ok=False,
                tool_name=name,
                payload={"text": "浏览器方法已关闭"},
                error="browser_method_disabled",
            )
        if name.startswith("local.") and not self._tool_interface_local_enable:
            return ToolResult(
                ok=False,
                tool_name=name,
                payload={"text": "本地方法已关闭"},
                error="local_method_disabled",
            )

        if name == "browser.fetch_url":
            return await self._method_browser_fetch_url(name, method_args, query)
        if name == "browser.resolve_video":
            return await self._method_browser_resolve_video(name, method_args, query)
        if name == "browser.resolve_image":
            return await self._method_browser_resolve_image(
                name, method_args, query, message_text
            )
        if name == "douyin.search_video":
            return await self._method_douyin_search_video(name, method_args, query)
        if name == "browser.github_search":
            return await self._method_browser_github_search(
                name,
                method_args,
                query,
                message_text=message_text,
                group_id=group_id,
                api_call=api_call,
            )
        if name == "browser.github_readme":
            return await self._method_browser_github_readme(name, method_args, query)
        if name == "local.read_text":
            return await self._method_local_read_text(name, method_args)
        if name == "local.media_from_path":
            return await self._method_local_media_from_path(name, method_args)
        if name == "media.qq_avatar":
            qq = normalize_text(str(method_args.get("qq", "")))
            avatar_query = f"qq头像 {qq}" if qq else query
            return await self._search_qq_avatar(
                query=avatar_query,
                message_text=message_text,
                user_id=user_id,
                user_name=user_name,
                group_id=group_id,
                api_call=api_call,
                raw_segments=raw_segments,
                bot_id=bot_id,
            )
        if name == "media.pick_image_from_message":
            return await self._method_media_pick_image(name, raw_segments)
        if name == "media.pick_video_from_message":
            return await self._method_media_pick_video(name, raw_segments)
        if name == "media.pick_audio_from_message":
            return await self._method_media_pick_audio(name, raw_segments)
        if name == "media.analyze_image":
            return await self._method_media_analyze_image(
                method_name=name,
                method_args=method_args,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                conversation_id=conversation_id,
                api_call=api_call,
            )
        if name == "video.analyze":
            return await self._method_video_analyze(
                method_name=name,
                method_args=method_args,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                conversation_id=conversation_id,
            )

        return ToolResult(
            ok=False,
            tool_name=name,
            payload={"text": f"方法 {name} 不存在。"},
            error=f"unsupported_ai_method:{name}",
        )
