"""Auto-split from core/agent_tools.py — AI Method / 爬虫 LLM 工具"""
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

def _register_ai_method_tools(registry: AgentToolRegistry) -> None:
    """注册 tools.py AI method 的 Agent 桥接工具。"""

    registry.register(
        ToolSchema(
            name="fetch_webpage",
            description=(
                "访问指定URL网页，返回页面标题、状态码和内容摘要。\n"
                "使用场景: 用户给了一个链接让你看看内容、总结网页、查看文章时使用。\n"
                "注意: 不能访问内网地址，有超时限制"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要访问的网页URL"},
                },
                "required": ["url"],
            },
            category="search",
        ),
        _handle_fetch_webpage,
    )

    registry.register(
        ToolSchema(
            name="github_search",
            description=(
                "在GitHub搜索开源仓库，返回仓库名、星标数、简介和链接。\n"
                "使用场景: 用户问'有什么好用的XX库'、'搜一下GitHub上的XX'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "language": {"type": "string", "description": "编程语言过滤(可选，如python/rust/go)"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _handle_github_search,
    )
    registry.register(
        ToolSchema(
            name="github_readme",
            description=(
                "读取指定GitHub仓库的README摘要。\n"
                "使用场景: 用户说'看看这个仓库'、'这个项目是干什么的'时使用。\n"
                "支持 owner/repo 格式或完整GitHub URL"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "仓库，格式: owner/repo 或 GitHub URL"},
                },
                "required": ["repo"],
            },
            category="search",
        ),
        _handle_github_readme,
    )

    registry.register(
        ToolSchema(
            name="douyin_search",
            description=(
                "在抖音搜索视频，返回视频标题、作者和链接。\n"
                "使用场景: 用户说'搜一下抖音上的XX'、'找个抖音视频'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
            category="media",
        ),
        _handle_douyin_search,
    )

    registry.register(
        ToolSchema(
            name="get_qq_avatar",
            description=(
                "获取QQ用户的头像图片URL。\n"
                "使用场景: 用户说'看看XX的头像'、'发一下我的头像'时使用。\n"
                "不传qq参数时获取当前用户头像"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "qq": {"type": "string", "description": "QQ号(可选，不传则取当前用户)"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_get_qq_avatar,
    )


# ── AI Method 桥接 handlers ──

async def _handle_fetch_webpage(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="tool_executor unavailable")
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolCallResult(ok=False, error="missing url")
    try:
        result = await tool_executor._method_browser_fetch_url(
            "browser.fetch_url", {"url": url}, url,
        )
        text = str((result.payload or {}).get("text", ""))
        return ToolCallResult(ok=result.ok, display=clip_text(text, 800), error=result.error)
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"fetch_error: {exc}")


async def _handle_github_search(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="tool_executor unavailable")
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolCallResult(ok=False, error="missing query")
    method_args: dict[str, Any] = {"query": query}
    lang = str(args.get("language", "")).strip()
    if lang:
        method_args["language"] = lang
    try:
        result = await tool_executor._method_browser_github_search(
            "browser.github_search", method_args, query,
        )
        text = str((result.payload or {}).get("text", ""))
        return ToolCallResult(ok=result.ok, display=clip_text(text, 800), error=result.error)
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"github_search_error: {exc}")


async def _handle_github_readme(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="tool_executor unavailable")
    repo = str(args.get("repo", "")).strip()
    if not repo:
        return ToolCallResult(ok=False, error="missing repo")
    method_args: dict[str, Any] = {}
    if "/" in repo and not repo.startswith("http"):
        method_args["repo"] = repo
    else:
        method_args["url"] = repo
    try:
        result = await tool_executor._method_browser_github_readme(
            "browser.github_readme", method_args, repo,
        )
        text = str((result.payload or {}).get("text", ""))
        return ToolCallResult(ok=result.ok, display=clip_text(text, 800), error=result.error)
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"github_readme_error: {exc}")


async def _handle_douyin_search(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="tool_executor unavailable")
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolCallResult(ok=False, error="missing query")
    try:
        result = await tool_executor._method_douyin_search_video(
            "douyin.search_video", {"query": query}, query,
        )
        text = str((result.payload or {}).get("text", ""))
        return ToolCallResult(ok=result.ok, display=clip_text(text, 800), error=result.error)
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"douyin_search_error: {exc}")


async def _handle_get_qq_avatar(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    qq = str(args.get("qq", "")).strip()
    if not qq:
        qq = str(context.get("user_id", ""))
    if not qq:
        return ToolCallResult(ok=False, error="missing qq")
    # QQ 头像直链
    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    return ToolCallResult(
        ok=True,
        data={"image_url": avatar_url, "qq": qq},
        display=f"QQ {qq} 的头像",
    )


# ── QQ空间 (QZone) 工具 ──

def _safe_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _resolve_qzone_config(base_config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    runtime_cfg = context.get("config")
    source_cfg = runtime_cfg if isinstance(runtime_cfg, dict) else base_config
    if not isinstance(source_cfg, dict):
        source_cfg = {}
    va = source_cfg.get("video_analysis", {}) if isinstance(source_cfg.get("video_analysis", {}), dict) else {}
    qz_cfg = va.get("qzone", {}) if isinstance(va.get("qzone", {}), dict) else {}
    return qz_cfg


def _normalize_qzone_tool_error(exc: Exception) -> str:
    msg = normalize_text(str(exc))
    if "cookie 已过期" in msg:
        return "qzone_cookie_expired:QZone cookie 已过期，请重新配置"
    if "访问权限" in msg:
        return f"qzone_permission_denied:{msg}"
    if not msg:
        return f"qzone_request_failed:{type(exc).__name__}"
    return f"qzone_request_failed:{msg}"


def _qzone_profile_payload(profile: Any) -> dict[str, Any]:
    return {
        "uin": str(getattr(profile, "uin", "")),
        "nickname": str(getattr(profile, "nickname", "")),
        "gender": str(getattr(profile, "gender", "")),
        "location": str(getattr(profile, "location", "")),
        "level": int(getattr(profile, "level", 0) or 0),
        "vip_info": str(getattr(profile, "vip_info", "")),
        "signature": str(getattr(profile, "signature", "")),
        "avatar_url": str(getattr(profile, "avatar_url", "")),
        "birthday": str(getattr(profile, "birthday", "")),
        "age": int(getattr(profile, "age", 0) or 0),
        "constellation": str(getattr(profile, "constellation", "")),
    }


def _qzone_mood_payload(mood: Any) -> dict[str, Any]:
    return {
        "tid": str(getattr(mood, "tid", "")),
        "content": str(getattr(mood, "content", "")),
        "create_time": str(getattr(mood, "create_time", "")),
        "comment_count": int(getattr(mood, "comment_count", 0) or 0),
        "like_count": int(getattr(mood, "like_count", 0) or 0),
        "pic_urls": list(getattr(mood, "pic_urls", []) or []),
        "video_url": str(getattr(mood, "video_url", "")),
        "video_cover_url": str(getattr(mood, "video_cover_url", "")),
    }


def _qzone_album_payload(album: Any) -> dict[str, Any]:
    return {
        "album_id": str(getattr(album, "album_id", "")),
        "name": str(getattr(album, "name", "")),
        "desc": str(getattr(album, "desc", "")),
        "photo_count": int(getattr(album, "photo_count", 0) or 0),
        "create_time": str(getattr(album, "create_time", "")),
    }



def _register_scrapy_llm_tools(registry: AgentToolRegistry, model_client: Any) -> None:
    """注册 ScrapyLLM 智能网页抓取 + LLM 提取工具。"""

    registry.register(
        ToolSchema(
            name="scrape_extract",
            description=(
                "智能网页抓取+LLM提取: 抓取指定URL网页，用AI按你的指令提取结构化信息。\n"
                "使用场景: 用户说'帮我看看这个网页里的XX信息'、'提取这个页面的价格/标题/列表'时使用。\n"
                "比 fetch_webpage 更强: 不只是返回原文，而是按指令智能提取关键信息。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的网页URL"},
                    "instruction": {"type": "string", "description": "提取指令，描述你想从网页中提取什么信息"},
                },
                "required": ["url", "instruction"],
            },
            category="search",
        ),
        _make_scrape_extract_handler(model_client),
    )

    registry.register(
        ToolSchema(
            name="scrape_summarize",
            description=(
                "智能网页摘要: 抓取网页并用AI生成中文摘要。\n"
                "使用场景: 用户说'总结一下这个链接'、'这篇文章讲了什么'时使用。\n"
                "可指定关注重点，如'重点关注技术细节'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要摘要的网页URL"},
                    "focus": {"type": "string", "description": "关注重点(可选)，如'技术细节'、'价格信息'"},
                },
                "required": ["url"],
            },
            category="search",
        ),
        _make_scrape_summarize_handler(model_client),
    )

    registry.register(
        ToolSchema(
            name="scrape_structured",
            description=(
                "结构化数据提取: 抓取网页并按指定格式提取JSON数据。\n"
                "使用场景: 需要从网页提取表格、列表、商品信息等结构化数据时使用。\n"
                "schema_desc 描述你想要的JSON格式，如 '{\"title\": \"文章标题\", \"price\": \"价格\"}'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要提取的网页URL"},
                    "schema_desc": {"type": "string", "description": "期望的JSON结构描述"},
                },
                "required": ["url", "schema_desc"],
            },
            category="search",
        ),
        _make_scrape_structured_handler(model_client),
    )

    registry.register(
        ToolSchema(
            name="scrape_follow_links",
            description=(
                "智能链接跟踪: 抓取页面，AI选择相关链接，跟进抓取并提取信息。\n"
                "使用场景: 用户说'帮我从这个页面找到XX的详情'、'看看这个列表页里哪些符合XX条件'时使用。\n"
                "两步操作: 先从首页选链接，再从子页面提取信息。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "起始页面URL"},
                    "link_instruction": {"type": "string", "description": "选择链接的指令，如'选择与Python相关的链接'"},
                    "extract_instruction": {"type": "string", "description": "从子页面提取信息的指令"},
                },
                "required": ["url", "link_instruction", "extract_instruction"],
            },
            category="search",
        ),
        _make_scrape_follow_links_handler(model_client),
    )


def _make_scrape_extract_handler(model_client: Any) -> ToolHandler:
    def _build_scrapy_engine(context: dict[str, Any]) -> Any:
        from utils.scrapy_llm import ScrapyLLM
        cfg = context.get("config", {}) if isinstance(context, dict) else {}
        search_cfg = cfg.get("search", {}) if isinstance(cfg, dict) else {}
        if not isinstance(search_cfg, dict):
            search_cfg = {}
        scrape_cfg = search_cfg.get("scrape", {})
        if not isinstance(scrape_cfg, dict):
            scrape_cfg = {}
        timeout_seconds = float(scrape_cfg.get("timeout_seconds", 14.0))
        max_text_len = int(scrape_cfg.get("max_text_len", 7000))
        llm_max_tokens = int(scrape_cfg.get("llm_max_tokens", 1200))
        return ScrapyLLM(
            model_client=model_client,
            timeout=max(6.0, min(45.0, timeout_seconds)),
            max_text_len=max(2000, min(20000, max_text_len)),
            llm_max_tokens=max(300, min(2400, llm_max_tokens)),
        )

    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        url = str(args.get("url", "")).strip()
        instruction = str(args.get("instruction", "")).strip()
        if not url or not instruction:
            return ToolCallResult(ok=False, error="missing url or instruction")
        engine = _build_scrapy_engine(context)
        try:
            result = await engine.scrape_and_extract(
                url,
                instruction,
                system_hint=(
                    "你是网页信息提取助手。"
                    "必须使用简体中文输出，不要英文套话。"
                    "只给与提取指令直接相关的结论，控制在 220 字以内。"
                ),
            )
            if not result.ok:
                return ToolCallResult(ok=False, error=result.error, display=f"抓取失败: {result.error}")
            return ToolCallResult(
                ok=True,
                data={"url": url, "raw_len": result.raw_text_len},
                display=clip_text(result.extracted, 260),
            )
        finally:
            await engine.close()
    return handler


def _make_scrape_summarize_handler(model_client: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        url = str(args.get("url", "")).strip()
        focus = str(args.get("focus", "")).strip()
        if not url:
            return ToolCallResult(ok=False, error="missing url")
        cfg = context.get("config", {}) if isinstance(context, dict) else {}
        search_cfg = cfg.get("search", {}) if isinstance(cfg, dict) else {}
        scrape_cfg = search_cfg.get("scrape", {}) if isinstance(search_cfg, dict) else {}
        timeout_seconds = float(scrape_cfg.get("timeout_seconds", 14.0)) if isinstance(scrape_cfg, dict) else 14.0
        max_text_len = int(scrape_cfg.get("max_text_len", 7000)) if isinstance(scrape_cfg, dict) else 7000
        llm_max_tokens = int(scrape_cfg.get("llm_max_tokens", 1200)) if isinstance(scrape_cfg, dict) else 1200
        from utils.scrapy_llm import ScrapyLLM
        engine = ScrapyLLM(
            model_client=model_client,
            timeout=max(6.0, min(45.0, timeout_seconds)),
            max_text_len=max(2000, min(20000, max_text_len)),
            llm_max_tokens=max(300, min(2400, llm_max_tokens)),
        )
        try:
            result = await engine.smart_summarize(url, focus)
            if not result.ok:
                return ToolCallResult(ok=False, error=result.error, display=f"摘要失败: {result.error}")
            return ToolCallResult(
                ok=True,
                data={"url": url, "raw_len": result.raw_text_len},
                display=clip_text(result.extracted, 1200),
            )
        finally:
            await engine.close()
    return handler


def _make_scrape_structured_handler(model_client: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        url = str(args.get("url", "")).strip()
        schema_desc = str(args.get("schema_desc", "")).strip()
        if not url or not schema_desc:
            return ToolCallResult(ok=False, error="missing url or schema_desc")
        cfg = context.get("config", {}) if isinstance(context, dict) else {}
        search_cfg = cfg.get("search", {}) if isinstance(cfg, dict) else {}
        scrape_cfg = search_cfg.get("scrape", {}) if isinstance(search_cfg, dict) else {}
        timeout_seconds = float(scrape_cfg.get("timeout_seconds", 14.0)) if isinstance(scrape_cfg, dict) else 14.0
        max_text_len = int(scrape_cfg.get("max_text_len", 7000)) if isinstance(scrape_cfg, dict) else 7000
        llm_max_tokens = int(scrape_cfg.get("llm_max_tokens", 1200)) if isinstance(scrape_cfg, dict) else 1200
        from utils.scrapy_llm import ScrapyLLM
        engine = ScrapyLLM(
            model_client=model_client,
            timeout=max(6.0, min(45.0, timeout_seconds)),
            max_text_len=max(2000, min(20000, max_text_len)),
            llm_max_tokens=max(300, min(2400, llm_max_tokens)),
        )
        try:
            result = await engine.extract_structured(url, schema_desc)
            if not result.ok:
                return ToolCallResult(ok=False, error=result.error)
            return ToolCallResult(
                ok=True,
                data={"url": url, "raw_len": result.raw_text_len},
                display=clip_text(result.extracted, 1500),
            )
        finally:
            await engine.close()
    return handler


def _make_scrape_follow_links_handler(model_client: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        url = str(args.get("url", "")).strip()
        link_inst = str(args.get("link_instruction", "")).strip()
        extract_inst = str(args.get("extract_instruction", "")).strip()
        if not url or not link_inst or not extract_inst:
            return ToolCallResult(ok=False, error="missing url, link_instruction or extract_instruction")
        cfg = context.get("config", {}) if isinstance(context, dict) else {}
        search_cfg = cfg.get("search", {}) if isinstance(cfg, dict) else {}
        scrape_cfg = search_cfg.get("scrape", {}) if isinstance(search_cfg, dict) else {}
        timeout_seconds = float(scrape_cfg.get("timeout_seconds", 14.0)) if isinstance(scrape_cfg, dict) else 14.0
        max_text_len = int(scrape_cfg.get("max_text_len", 7000)) if isinstance(scrape_cfg, dict) else 7000
        llm_max_tokens = int(scrape_cfg.get("llm_max_tokens", 1200)) if isinstance(scrape_cfg, dict) else 1200
        from utils.scrapy_llm import ScrapyLLM
        engine = ScrapyLLM(
            model_client=model_client,
            timeout=max(6.0, min(45.0, timeout_seconds)),
            max_text_len=max(2000, min(20000, max_text_len)),
            llm_max_tokens=max(300, min(2400, llm_max_tokens)),
        )
        try:
            results = await engine.find_and_follow(url, link_inst, extract_inst)
            parts = []
            for r in results:
                if r.ok:
                    parts.append(f"[{r.url}]\n{clip_text(r.extracted, 600)}")
                else:
                    parts.append(f"[{r.url}] 失败: {r.error}")
            display = "\n---\n".join(parts) if parts else "未提取到信息"
            return ToolCallResult(
                ok=True,
                data={"url": url, "sub_pages": len(results)},
                display=clip_text(display, 2000),
            )
        finally:
            await engine.close()
    return handler
