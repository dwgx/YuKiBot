"""Auto-split from core/agent_tools.py — 搜索工具"""
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

def _register_search_tools(registry: AgentToolRegistry, search_engine: Any) -> None:
    """注册搜索相关工具。"""

    registry.register(
        ToolSchema(
            name="web_search",
            description="搜索互联网获取实时信息。用于回答需要最新数据、事实核查、新闻、技术文档等问题",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "mode": {"type": "string", "description": "搜索模式: text(默认)/image/video"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _make_search_handler(search_engine),
    )

    registry.register(
        ToolSchema(
            name="search_web_media",
            description=(
                "检索图片/视频/GIF候选，并返回可读的编号列表。\n"
                "适用于“给我找图/视频/GIF”场景，先给候选再让用户选。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "media_type": {"type": "string", "description": "类型: image/video/gif"},
                    "limit": {"type": "integer", "description": "返回数量，默认5，最大8"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _make_search_web_media_handler(search_engine),
    )

    registry.register(
        ToolSchema(
            name="search_media",
            description=(
                "搜索并推送最合适的图片/视频/GIF。\n"
                "适用于用户说想看某主题视频、图片、壁纸、头像或 GIF，但没有给具体链接的场景。\n"
                "视频会尽量解析成可发送 video_url；图片/GIF 会返回可发送 image_url。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "媒体主题或关键词"},
                    "media_type": {"type": "string", "description": "类型: image/video/gif"},
                    "limit": {"type": "integer", "description": "候选数量提示，默认5，最大8"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _make_search_media_handler(search_engine),
    )

    registry.register(
        ToolSchema(
            name="search_download_resources",
            description=(
                "检索可下载资源候选（压缩包/安装包/数据集/模组等），返回编号列表供用户选择。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "资源关键词"},
                    "file_type": {"type": "string", "description": "可选: zip/exe/msi/pdf/apk/mod 等"},
                    "limit": {"type": "integer", "description": "返回数量，默认6，最大10"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _make_search_download_resources_handler(search_engine),
    )


def _make_search_handler(search_engine: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        query = str(args.get("query", "")).strip()
        mode = str(args.get("mode", "text")).strip()
        if not query:
            return ToolCallResult(ok=False, error="empty query")
        try:
            # 根据 mode 路由到对应的搜索方法
            if mode == "image":
                results = await search_engine.search_images(query=query)
            elif mode == "video":
                results = await search_engine.search_bilibili_videos(query=query)
            else:
                results = await search_engine.search(query=query)
            if not results:
                return ToolCallResult(ok=True, data={"results": []}, display=f"搜索 '{query}' 无结果")
            items = []
            display_lines = []
            for i, r in enumerate(results[:8]):
                item = {
                    "title": getattr(r, "title", ""),
                    "url": getattr(r, "url", getattr(r, "source_url", "")),
                    "snippet": getattr(r, "snippet", ""),
                }
                # 图片模式额外带上 image_url
                if mode == "image":
                    item["image_url"] = getattr(r, "image_url", "")
                items.append(item)
                display_lines.append(f"{i+1}. {item['title']}: {clip_text(item['snippet'] or item['url'], 120)}")
            return ToolCallResult(
                ok=True,
                data={"results": items, "query": query, "mode": mode},
                display="\n".join(display_lines),
            )
        except Exception as exc:
            _log.warning("web_search failed | query=%s mode=%s err=%s", query, mode, exc)
            return ToolCallResult(ok=False, error=f"search_error: {exc}", display=f"搜索失败: {exc}")
    return handler
def _make_search_web_media_handler(search_engine: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        query = normalize_text(str(args.get("query", "")))
        media_type = normalize_text(str(args.get("media_type", "image"))).lower() or "image"
        limit = max(1, min(8, int(args.get("limit", 5) or 5)))
        if not query:
            return ToolCallResult(ok=False, error="missing query", display="缺少 query")

        try:
            if media_type == "video":
                rows = await search_engine.search_bilibili_videos(query=query, limit=limit)
                items = []
                lines = [f"【视频候选: {query}】"]
                for i, row in enumerate(rows[:limit], 1):
                    title = normalize_text(getattr(row, "title", "")) or f"候选{i}"
                    url = normalize_text(getattr(row, "url", "")) or normalize_text(getattr(row, "source_url", ""))
                    snippet = clip_text(normalize_text(getattr(row, "snippet", "")) or url, 120)
                    items.append({"index": i, "title": title, "url": url, "snippet": snippet, "media_type": "video"})
                    lines.append(f"{i}. {title} | {snippet}")
                return ToolCallResult(
                    ok=True,
                    data={"query": query, "media_type": media_type, "items": items},
                    display="\n".join(lines),
                )

            # image / gif 都走图片检索，gif 会补关键词
            image_query = query
            if media_type == "gif" and "gif" not in query.lower():
                image_query = f"{query} gif"
            rows = await search_engine.search_images(query=image_query, max_results=limit)
            items = []
            lines = [f"【{media_type.upper()}候选: {query}】"]
            for i, row in enumerate(rows[:limit], 1):
                title = normalize_text(getattr(row, "title", "")) or f"候选{i}"
                image_url = normalize_text(getattr(row, "image_url", ""))
                thumbnail_url = normalize_text(getattr(row, "thumbnail_url", ""))
                source_url = normalize_text(getattr(row, "source_url", "")) or image_url
                snippet = clip_text(normalize_text(getattr(row, "snippet", "")) or source_url, 120)
                items.append(
                    {
                        "index": i,
                        "title": title,
                        "image_url": image_url,
                        "thumbnail_url": thumbnail_url,
                        "url": source_url,
                        "snippet": snippet,
                        "media_type": media_type,
                    }
                )
                lines.append(f"{i}. {title} | {snippet}")
            return ToolCallResult(
                ok=True,
                data={"query": query, "media_type": media_type, "items": items},
                display="\n".join(lines),
            )
        except Exception as exc:
            _log.warning("search_web_media_error | query=%s | type=%s | err=%s", query, media_type, exc)
            return ToolCallResult(ok=False, error=f"search_web_media_error:{exc}", display=f"媒体检索失败: {exc}")

    return handler


def _infer_media_search_type(query: str, explicit: str = "") -> str:
    explicit_type = normalize_text(explicit).lower()
    if explicit_type in {"image", "img", "picture", "photo", "pic"}:
        return "image"
    if explicit_type in {"video", "movie", "clip", "vid"}:
        return "video"
    if explicit_type in {"gif", "动图"}:
        return "gif"

    text = normalize_text(query).lower()
    compact = re.sub(r"\s+", "", text)
    if any(cue in compact for cue in ("gif", "动图", "表情动图")):
        return "gif"
    video_cues = (
        "视频",
        "影片",
        "短片",
        "片段",
        "教程视频",
        "youtube",
        "b站",
        "bilibili",
        "抖音",
        "douyin",
        "快手",
        "kuaishou",
        "acfun",
        "爱奇艺",
        "iqiyi",
        "腾讯视频",
    )
    image_cues = (
        "图片",
        "图",
        "搜图",
        "找图",
        "来张",
        "壁纸",
        "头像",
        "配图",
        "照片",
        "梗图",
        "猫图",
        "表情包",
        "贴图",
    )
    video_compact = re.sub(
        r"(?:不要|别|別|不是|不用|无需|禁止)[^，。,.!?！？；;]{0,10}"
        r"(?:视频|影片|短片|片段|video|clip)",
        "",
        compact,
        flags=re.IGNORECASE,
    )
    image_compact = re.sub(
        r"(?:不要|别|別|不是|不用|无需|禁止)[^，。,.!?！？；;]{0,10}"
        r"(?:图片|图|壁纸|头像|配图|照片|image|photo|picture|wallpaper|avatar)",
        "",
        compact,
        flags=re.IGNORECASE,
    )
    video_hit = any(cue in video_compact for cue in video_cues) or bool(
        re.search(r"\b(?:video|movie|clip)\b", video_compact)
    )
    image_hit = any(cue in image_compact for cue in image_cues) or bool(
        re.search(r"\b(?:image|photo|picture|wallpaper|avatar)\b", image_compact)
    )
    if video_hit:
        return "video"
    if image_hit:
        return "image"
    return "image"


def _coerce_group_id(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _make_search_media_handler(search_engine: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        _ = search_engine
        query = normalize_text(str(args.get("query", "")))
        if not query:
            return ToolCallResult(ok=False, error="missing query", display="缺少 query")

        media_type = _infer_media_search_type(query, str(args.get("media_type", "")))
        tool_executor = context.get("tool_executor")
        if tool_executor is None:
            return ToolCallResult(
                ok=False,
                error="tool_executor unavailable",
                display="媒体搜索模块未初始化",
            )

        search_query = query
        if media_type == "gif" and "gif" not in search_query.lower():
            search_query = f"{search_query} gif"
        mode = "video" if media_type == "video" else "image"

        try:
            result = await tool_executor.execute(
                action="search",
                tool_name="search_media",
                tool_args={"query": search_query, "mode": mode},
                message_text=str(context.get("message_text", "")) or search_query,
                conversation_id=str(context.get("conversation_id", "")),
                user_id=str(context.get("user_id", "")),
                user_name=str(context.get("user_name", "")),
                group_id=_coerce_group_id(context.get("group_id", 0)),
                api_call=context.get("api_call"),
                raw_segments=context.get("raw_segments", []) or [],
                bot_id=str(context.get("bot_id", "")),
                trace_id=str(context.get("trace_id", "")),
            )
        except Exception as exc:
            _log.warning("search_media_error | query=%s | type=%s | err=%s", query, media_type, exc)
            return ToolCallResult(
                ok=False,
                error=f"search_media_error:{exc}",
                display=f"媒体搜索失败: {exc}",
            )

        payload = result.payload if isinstance(getattr(result, "payload", None), dict) else {}
        payload = dict(payload)
        payload.setdefault("query", query)
        payload.setdefault("media_type", media_type)
        payload.setdefault("mode", mode)
        display = normalize_text(str(payload.get("text", "")))
        if not display:
            if payload.get("video_url"):
                display = "找到可发送视频。"
            elif payload.get("image_url") or payload.get("image_urls"):
                display = "找到可发送图片。"
            else:
                display = f"媒体搜索完成: {query}"
        return ToolCallResult(
            ok=bool(result.ok),
            data=payload,
            error=result.error,
            display=display,
        )

    return handler


def _make_search_download_resources_handler(search_engine: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        query = normalize_text(str(args.get("query", "")))
        file_type = normalize_text(str(args.get("file_type", ""))).lower()
        limit = max(1, min(10, int(args.get("limit", 6) or 6)))
        if not query:
            return ToolCallResult(ok=False, error="missing query", display="缺少 query")

        search_query = query
        if file_type:
            search_query = f"{query} {file_type} 下载"
        try:
            rows = await search_engine.search(search_query)
            ranked_rows: list[tuple[int, str, str, str]] = []
            for row in rows:
                title = normalize_text(getattr(row, "title", "")) or ""
                url = normalize_text(getattr(row, "url", "")) or normalize_text(getattr(row, "source_url", ""))
                snippet = clip_text(normalize_text(getattr(row, "snippet", "")) or url, 140)
                score, reasons = _score_download_source_trust(
                    url=url,
                    title=title,
                    snippet=snippet,
                    query=query,
                )
                # Minor boost for explicit official words in title/snippet.
                if any(token in f"{title} {snippet}".lower() for token in ("官网", "官方", "official")):
                    score += 2
                ranked_rows.append((score, title, url, snippet + (f" | trust={','.join(reasons)}" if reasons else "")))
            ranked_rows.sort(key=lambda item: item[0], reverse=True)

            items = []
            lines = [f"【资源候选: {query}】"]
            for i, (score, title, url, snippet) in enumerate(ranked_rows[:limit], 1):
                title = title or f"候选{i}"
                items.append({"index": i, "title": title, "url": url, "snippet": snippet, "source_score": score})
                lines.append(f"{i}. [score={score}] {title} | {snippet}")
            return ToolCallResult(
                ok=True,
                data={"query": query, "file_type": file_type, "items": items},
                display="\n".join(lines),
            )
        except Exception as exc:
            _log.warning("search_download_resources_error | query=%s | err=%s", query, exc)
            return ToolCallResult(ok=False, error=f"search_download_resources_error:{exc}", display=f"资源检索失败: {exc}")

    return handler


# ── 媒体工具 ──
