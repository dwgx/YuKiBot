"""Auto-split from core/agent_tools.py — 媒体工具（图片/视频/音乐/语音）"""
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
from utils.process_compat import macos_subprocess_kwargs, resolve_executable_for_spawn
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")

from core.image_gen import (
    IMAGE_PROMPT_BLOCKED_MESSAGE,
    assess_prompt_qq_ban_risk,
    detect_custom_prompt_risk_reason,
    detect_qq_ban_risk_reason,
)

def _register_media_tools(registry: AgentToolRegistry, model_client: Any, config: dict[str, Any]) -> None:
    """注册媒体分析相关工具。"""

    registry.register(
        ToolSchema(
            name="generate_image",
            description="根据文字描述生成图片（AI绘图）",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述（英文效果更好）"},
                    "size": {"type": "string", "description": "图片尺寸，默认 1024x1024"},
                    "style": {"type": "string", "description": "风格描述（可选，用于生成前风险检测）"},
                },
                "required": ["prompt"],
            },
            category="media",
        ),
        _make_image_gen_handler(model_client, config),
    )

    # ── 视频解析工具 ──
    registry.register(
        ToolSchema(
            name="parse_video",
            description=(
                "解析短视频链接，返回可发送的视频URL。\n"
                "支持平台: 抖音(douyin/v.douyin.com)、快手(kuaishou)、B站(bilibili/b23.tv)、AcFun、腾讯视频、爱奇艺、YouTube、优酷、直链视频(.mp4等)。\n"
                "使用场景: 用户发了视频链接让你解析/下载/发送时使用。\n"
                "返回 video_url 可直接通过 final_answer 的 video_url 参数发送。\n"
                "同时返回 qq_safety 安全度评估(safe/risky/blocked)。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频链接(支持短链接如 v.douyin.com/xxx)"},
                },
                "required": ["url"],
            },
            category="media",
        ),
        _handle_parse_video,
    )

    registry.register(
        ToolSchema(
            name="analyze_video",
            description=(
                "深度分析视频内容: 提取标题、作者、时长、标签、弹幕热词、热评、字幕等。\n"
                "B站视频可获取弹幕热词和热评，抖音可获取详情数据。\n"
                "有本地视频+ffmpeg时还能提取关键帧用AI识别画面内容。\n"
                "使用场景: 用户要求分析、评价、解说、总结视频内容时使用。\n"
                "同时返回 qq_safety 安全度评估。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频链接"},
                },
                "required": ["url"],
            },
            category="media",
        ),
        _handle_analyze_video,
    )

    # ── 图片识别工具 ──
    registry.register(
        ToolSchema(
            name="analyze_image",
            description=(
                "识别/分析图片内容（AI视觉识别）。\n"
                "可指定 url 参数传入图片链接，未指定时自动从当前消息中提取图片。\n"
                "使用场景: 用户发图片问'这是什么'、'看看这张图'、'识别一下'、'图里写了什么'时使用。\n"
                "也可用于识别表情包内容、截图文字、商品图片等。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "图片URL（可选，不传则从消息中自动提取）"},
                    "question": {"type": "string", "description": "针对图片的具体问题（可选，如'图里的文字是什么'）"},
                    "analyze_all": {"type": "boolean", "description": "是否批量识别可见范围内的所有图片（可选）"},
                    "max_images": {"type": "integer", "description": "批量识别时最多识别多少张（可选，默认 8）"},
                    "target_message_id": {"type": "string", "description": "强制指定要分析的消息ID（可选）"},
                    "allow_recent_fallback": {"type": "boolean", "description": "无图片时是否允许回退到最近一张图片（可选）"},
                    "recent_only_when_unique": {"type": "boolean", "description": "回退时仅在最近图片唯一时才自动命中（可选）"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_analyze_image,
    )

    registry.register(
        ToolSchema(
            name="resolve_image",
            description=(
                "验证并返回可直接发送的图片 URL。\n"
                "使用场景: 用户给了图片直链或 Markdown 图片，要求直接发图、预览或转发时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "图片 URL；不传则从当前消息/引用文本中提取"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_resolve_image,
    )

    # ── 语音分析工具 ──
    registry.register(
        ToolSchema(
            name="analyze_voice",
            description=(
                "转录语音/音频消息为文字（本地 Whisper 模型）。\n"
                "自动从当前消息或引用消息中提取语音文件进行转录。\n"
                "使用场景: 用户发了语音消息、或引用了一条语音消息并问内容时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "语音文件URL（可选，不传则从消息中自动提取）"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_analyze_voice,
    )

    # ── 本地视频内容分析工具 ──
    registry.register(
        ToolSchema(
            name="analyze_local_video",
            description=(
                "深度分析用户直接发送或引用的视频内容：关键帧、字幕/转写、画面摘要、结构化结论。\n"
                "自动从当前消息或引用消息中提取视频文件，优先用于视频内容理解/总结/解说。\n"
                "与 analyze_video 的区别：此工具处理用户直接发送的视频文件或引用视频，analyze_video 处理视频链接。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频文件URL（可选，不传则从消息中自动提取）"},
                    "question": {"type": "string", "description": "用户想重点了解的内容（可选）"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_analyze_local_video,
    )

    # ── 视频处理工具（切片 / 音频 / 封面 / 关键帧）──
    registry.register(
        ToolSchema(
            name="split_video",
            description=(
                "对视频做媒体处理：切片、导出音频、提取封面或关键帧。\n"
                "可处理用户直接发送的视频，或传入视频链接 URL。\n"
                "mode=clip: 输出视频片段（返回 video_url）\n"
                "mode=audio: 导出音频（返回 audio_file，可直接发语音）\n"
                "mode=cover: 提取单张封面（返回 image_url）\n"
                "mode=frames: 提取多张关键帧（返回 image_urls）"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频URL（可选，不传则从消息/引用中自动提取）"},
                    "mode": {
                        "type": "string",
                        "description": "处理模式：clip / audio / cover / frames（默认 clip）",
                    },
                    "start_seconds": {"type": "number", "description": "起始秒（clip/audio 可选）"},
                    "end_seconds": {"type": "number", "description": "结束秒（clip/audio 可选）"},
                    "duration_seconds": {"type": "number", "description": "持续秒数（与 start_seconds 搭配，可替代 end_seconds）"},
                    "max_audio_seconds": {
                        "type": "integer",
                        "description": "audio 模式的默认导出上限秒数（仅未指定 end/duration 时生效，0 表示不限制）",
                    },
                    "frame_time_seconds": {"type": "number", "description": "封面时间点秒数（cover 模式，默认 1）"},
                    "max_frames": {"type": "integer", "description": "关键帧数量上限（frames 模式，默认 6）"},
                    "interval_seconds": {"type": "number", "description": "关键帧提取间隔秒（frames 模式，可选）"},
                },
                "required": [],
            },
            category="media",
        ),
        _handle_split_video,
    )


def _make_image_gen_handler(
    model_client: Any,
    config: dict[str, Any] | None = None,
) -> ToolHandler:
    """创建图片生成工具的 handler。"""
    raw_cfg = config if isinstance(config, dict) else {}
    image_cfg = raw_cfg.get("image_gen", raw_cfg)
    if not isinstance(image_cfg, dict):
        image_cfg = {}
    prompt_review_enable = bool(image_cfg.get("prompt_review_enable", True))
    prompt_review_fail_closed = bool(
        image_cfg.get("prompt_review_fail_closed", False)
    )
    prompt_review_model = str(image_cfg.get("prompt_review_model", "")).strip()
    prompt_review_max_tokens = max(
        80, min(600, int(image_cfg.get("prompt_review_max_tokens", 180)))
    )
    custom_block_terms = list(image_cfg.get("custom_block_terms", []) or [])
    custom_allow_terms = list(image_cfg.get("custom_allow_terms", []) or [])

    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        prompt = str(args.get("prompt", "")).strip()
        size = str(args.get("size", "1024x1024")).strip()
        style = str(args.get("style", "")).strip()
        if not prompt:
            return ToolCallResult(ok=False, error="empty prompt")
        risk_check_text = prompt if not style else f"{prompt}\nstyle: {style}"
        if prompt_review_enable:
            safe, _reason = await assess_prompt_qq_ban_risk(
                prompt,
                style=style,
                model_client=model_client,
                review_model=prompt_review_model,
                max_tokens=prompt_review_max_tokens,
                fail_closed=prompt_review_fail_closed,
                custom_block_terms=custom_block_terms,
                custom_allow_terms=custom_allow_terms,
            )
            if not safe:
                return ToolCallResult(
                    ok=False,
                    error="image_prompt_blocked_nsfw",
                    display=IMAGE_PROMPT_BLOCKED_MESSAGE,
                )
        elif detect_custom_prompt_risk_reason(
            risk_check_text,
            custom_block_terms=custom_block_terms,
            custom_allow_terms=custom_allow_terms,
        ) or detect_qq_ban_risk_reason(risk_check_text):
            return ToolCallResult(
                ok=False,
                error="image_prompt_blocked_nsfw",
                display=IMAGE_PROMPT_BLOCKED_MESSAGE,
            )
        try:
            url = await model_client.generate_image(prompt=prompt, size=size, style=style or None)
            if url:
                return ToolCallResult(ok=True, data={"image_url": url}, display=f"图片已生成")
            return ToolCallResult(ok=False, error="image generation returned empty")
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"image_gen_error: {exc}")
    return handler


# ── QQ 视频安全度预估 ──

_QQ_VIDEO_SIZE_LIMIT_MB = 100
_QQ_VIDEO_DURATION_LIMIT_SEC = 300  # 5分钟以内较安全

_RISKY_DOMAINS = {
    "xvideos", "pornhub", "xhamster", "xnxx", "redtube",
    "youporn", "tube8", "spankbang", "hentai",
}

_RISKY_KEYWORDS = {
    "porn", "nsfw", "r18", "18禁", "成人", "无码", "av",
    "hentai", "里番", "黄色", "色情",
}


def _estimate_qq_safety(
    url: str,
    title: str = "",
    duration: int = 0,
    file_size_mb: float = 0,
    platform: str = "",
) -> dict[str, Any]:
    """预估视频能否安全发到QQ。

    返回:
        level: "safe" | "risky" | "blocked"
        reasons: list[str]
        suggestion: str
    """
    reasons: list[str] = []
    lower_url = url.lower()
    lower_title = title.lower()

    # 1. 域名黑名单
    for domain in _RISKY_DOMAINS:
        if domain in lower_url:
            return {
                "level": "blocked",
                "reasons": [f"域名命中黑名单: {domain}"],
                "suggestion": "该视频来源不适合在QQ发送",
            }

    # 2. 标题关键词
    for kw in _RISKY_KEYWORDS:
        if kw in lower_title or kw in lower_url:
            return {
                "level": "blocked",
                "reasons": [f"内容关键词命中: {kw}"],
                "suggestion": "该视频内容可能违规，不建议在QQ发送",
            }

    # 3. 文件大小
    if file_size_mb > _QQ_VIDEO_SIZE_LIMIT_MB:
        reasons.append(f"文件过大({file_size_mb:.0f}MB > {_QQ_VIDEO_SIZE_LIMIT_MB}MB)")

    # 4. 时长
    if duration > _QQ_VIDEO_DURATION_LIMIT_SEC:
        reasons.append(f"时长较长({duration}s > {_QQ_VIDEO_DURATION_LIMIT_SEC}s)")

    # 5. 平台可信度
    trusted_platforms = {"bilibili", "douyin", "kuaishou", "acfun", "tencent", "youku", "iqiyi", "mgtv"}
    if platform and platform not in trusted_platforms:
        reasons.append(f"非主流平台({platform})，QQ可能拦截外链")

    # 6. 直链检查
    if re.search(r"\.(?:mp4|flv|mkv|avi|mov|webm)(?:\?|$)", lower_url, re.IGNORECASE):
        if not any(trusted in lower_url for trusted in (
            "bilibili", "douyin", "kuaishou", "acfun", "douyinvod", "bilivideo",
            "v.qq.com", "youku", "iqiyi", "mgtv",
        )):
            reasons.append("非平台CDN直链，QQ可能无法预览")

    if not reasons:
        return {
            "level": "safe",
            "reasons": [],
            "suggestion": "可以安全发送到QQ",
        }

    level = "risky" if len(reasons) <= 1 else "blocked"
    suggestion = "建议" + ("谨慎发送" if level == "risky" else "不要发送") + "，" + "；".join(reasons)
    return {"level": level, "reasons": reasons, "suggestion": suggestion}


# ── 视频解析 handler ──

async def _handle_parse_video(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """解析短视频链接，返回可发送的 video_url + 安全度评估。"""
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolCallResult(ok=False, error="missing url")

    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="video_parser_unavailable", display="视频解析模块未初始化")

    # URL 类型硬校验：避免把 pixiv/artwork 等非视频链接误送入 parse_video。
    is_supported_video = False
    try:
        is_supported_video = bool(
            callable(getattr(tool_executor, "_is_supported_platform_video_url", None))
            and tool_executor._is_supported_platform_video_url(url)
        ) or bool(
            callable(getattr(tool_executor, "_is_direct_video_url", None))
            and tool_executor._is_direct_video_url(url)
        )
    except Exception:
        is_supported_video = False
    if not is_supported_video:
        return ToolCallResult(
            ok=False,
            error="invalid_args:not_supported_video_url",
            display="parse_video 只支持抖音/快手/B站/AcFun/腾讯视频/爱奇艺/YouTube/优酷/直链视频，不支持该链接类型。",
        )

    query = url  # 用 URL 作为 query
    try:
        result = await tool_executor._method_browser_resolve_video(
            method_name="parse_video",
            method_args={"url": url},
            query=query,
        )
    except Exception as exc:
        _log.warning("parse_video_error | url=%s | %s", url[:80], exc)
        return ToolCallResult(ok=False, error=f"parse_video_error: {exc}", display=f"视频解析失败: {exc}")

    video_url = str((result.payload or {}).get("video_url", ""))
    image_url = str((result.payload or {}).get("image_url", ""))
    image_urls = (result.payload or {}).get("image_urls", [])
    post_type = str((result.payload or {}).get("post_type", ""))
    text = str((result.payload or {}).get("text", ""))
    platform = ""
    try:
        from core.video_analyzer import VideoAnalyzer
        platform = VideoAnalyzer.detect_platform(url)
    except Exception:
        pass

    # 安全度预估
    safety = _estimate_qq_safety(url=video_url or url, title=text, platform=platform)

    if result.ok and video_url:
        display_parts = [f"解析成功: {text}"]
        display_parts.append(f"安全度: {safety['level']}")
        if safety["reasons"]:
            display_parts.append(f"注意: {'; '.join(safety['reasons'])}")
        return ToolCallResult(
            ok=True,
            data={
                "video_url": video_url,
                "source_url": url,
                "text": text,
                "platform": platform,
                "qq_safety": safety,
            },
            display="\n".join(display_parts),
        )

    # 图文作品：有图片但没有视频
    if result.ok and post_type == "image_text" and (image_url or image_urls):
        display = text or "识别到图文作品"
        return ToolCallResult(
            ok=True,
            data={
                "image_url": image_url,
                "image_urls": image_urls if isinstance(image_urls, list) else [],
                "source_url": url,
                "text": text,
                "platform": platform,
                "post_type": "image_text",
                "qq_safety": safety,
            },
            display=display,
        )

    error_text = result.error or "解析失败"
    display = text or f"视频解析失败: {error_text}"
    return ToolCallResult(ok=False, error=error_text, display=display)


async def _handle_resolve_image(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """Validate a direct image URL and expose it to final_answer delivery."""
    url = normalize_text(str(args.get("url", "") or ""))
    if not url:
        merged = "\n".join(
            normalize_text(str(context.get(key, "") or ""))
            for key in ("message_text", "original_message_text", "reply_to_text")
        )
        match = re.search(r"https?://[^\s<>\"]+", merged, flags=re.IGNORECASE)
        if match:
            url = normalize_text(match.group(0)).rstrip(").,，。!?！？】》」』")
    if not url:
        return ToolCallResult(ok=False, error="missing url", display="没有拿到图片链接。")
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return ToolCallResult(ok=False, error="invalid_url", display="请给完整 http/https 图片链接。")

    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="tool_executor unavailable", display="图片发送模块未初始化。")

    try:
        if (
            callable(getattr(tool_executor, "_is_blocked_image_url", None))
            and tool_executor._is_blocked_image_url(url)
        ):
            return ToolCallResult(ok=False, error="blocked_image_request", display="这类图片我不能发。")
        if (
            callable(getattr(tool_executor, "_is_safe_public_http_url", None))
            and not tool_executor._is_safe_public_http_url(url)
        ):
            return ToolCallResult(ok=False, error="unsafe_url", display="这个图片链接命中了安全限制。")
        if not (
            callable(getattr(tool_executor, "_is_sendable_image_url", None))
            and await tool_executor._is_sendable_image_url(url)
        ):
            return ToolCallResult(ok=False, error="not_sendable_image_url", display="这个链接不是可直发图片。")
    except Exception as exc:
        _log.warning("resolve_image_error | url=%s | %s", clip_text(url, 100), exc)
        return ToolCallResult(ok=False, error=f"resolve_image_error: {exc}", display="图片链接验证失败。")

    return ToolCallResult(
        ok=True,
        data={"image_url": url, "image_urls": [url], "source_url": url, "text": "图片可发送。"},
        display=f"图片可发送，来源：{url}",
    )


async def _handle_analyze_video(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """深度分析视频内容，返回结构化分析结果 + 安全度评估。"""
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolCallResult(ok=False, error="missing url")

    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="video_analyzer_unavailable", display="视频分析模块未初始化")

    message_text = str(context.get("message_text", url))
    raw_segments = context.get("raw_segments", [])
    conversation_id = str(context.get("conversation_id", ""))

    try:
        result = await tool_executor._method_video_analyze(
            method_name="analyze_video",
            method_args={"url": url},
            query=url,
            message_text=message_text,
            raw_segments=raw_segments if isinstance(raw_segments, list) else [],
            conversation_id=conversation_id,
        )
    except Exception as exc:
        _log.warning("analyze_video_error | url=%s | %s", url[:80], exc)
        return ToolCallResult(ok=False, error=f"analyze_video_error: {exc}", display=f"视频分析失败: {exc}")

    payload = result.payload or {}
    video_url = str(payload.get("video_url", ""))
    text = str(payload.get("text", ""))
    platform = ""
    duration = 0
    title = ""

    try:
        from core.video_analyzer import VideoAnalyzer
        platform = VideoAnalyzer.detect_platform(url)
    except Exception:
        pass

    # 从 analysis context 提取元数据
    analysis_context = str(payload.get("analysis_context", text))
    for line in analysis_context.split("\n"):
        if line.startswith("时长:"):
            try:
                parts = line.split(":")[1].strip().split(":")
                if len(parts) == 3:
                    duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    duration = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("标题:"):
            title = line.split(":", 1)[1].strip()

    # 安全度预估
    safety = _estimate_qq_safety(
        url=video_url or url,
        title=title or text,
        duration=duration,
        platform=platform,
    )

    if result.ok:
        display_parts = [clip_text(text, 500)]
        display_parts.append(f"安全度: {safety['level']} - {safety.get('suggestion', '')}")
        data = {
            "text": text,
            "platform": platform,
            "qq_safety": safety,
        }
        if video_url:
            data["video_url"] = video_url
        return ToolCallResult(ok=True, data=data, display="\n".join(display_parts))

    error_text = result.error or "分析失败"
    return ToolCallResult(ok=False, error=error_text, display=text or f"视频分析失败: {error_text}")


async def _handle_analyze_image(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """识别/分析图片内容，委托给 ToolExecutor 的视觉识别能力。"""
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(ok=False, error="vision_unavailable", display="图片识别模块未初始化")

    url = str(args.get("url", "")).strip()
    question = str(args.get("question", "")).strip()
    message_text = str(context.get("message_text", ""))
    raw_segments = context.get("raw_segments", [])
    reply_media_segments = context.get("reply_media_segments", [])
    conversation_id = str(context.get("conversation_id", ""))
    api_call = context.get("api_call")

    def _image_count(segments: Any) -> int:
        if not isinstance(segments, list):
            return 0
        count = 0
        for seg in segments:
            if isinstance(seg, dict) and normalize_text(str(seg.get("type", ""))).lower() == "image":
                count += 1
        return count

    current_image_count = _image_count(raw_segments)
    reply_image_count = _image_count(reply_media_segments)

    # 构建 query: 优先用用户的具体问题，否则用消息文本
    query = question or message_text or "请描述这张图片的内容"
    query_norm = normalize_text(query).lower()

    def _to_flag(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = normalize_text(str(value)).lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n"}:
            return False
        return default

    def _to_positive_int(value: Any, default: int = 0) -> int:
        text = normalize_text(str(value))
        if not text:
            return default
        try:
            parsed = int(text)
        except ValueError:
            return default
        if parsed <= 0:
            return default
        return parsed

    all_scope_cues = (
        "所有图片",
        "全部图片",
        "所有图",
        "全部图",
        "群里图片",
        "群里的图片",
        "群里所有图",
        "每张图",
        "每个图",
        "逐张",
        "一张张",
        "批量",
        "all images",
        "every image",
    )
    all_action_cues = ("识别", "分析", "看看", "描述", "提取", "总结", "识图", "analyze", "describe", "ocr", "read")
    inferred_analyze_all = any(cue in query_norm for cue in all_scope_cues) and any(cue in query_norm for cue in all_action_cues)
    analyze_all = _to_flag(args.get("analyze_all", False), False) or inferred_analyze_all
    max_images = _to_positive_int(args.get("max_images"), 8 if analyze_all else 0)

    method_args: dict[str, Any] = {}
    if analyze_all:
        method_args["analyze_all"] = True
        method_args["max_images"] = max_images
    selected_source = "none"
    selected_segments: list[dict[str, Any]] = []
    if url:
        method_args["url"] = url
        selected_source = "explicit_url"
    else:
        current_segments = list(raw_segments) if isinstance(raw_segments, list) else []
        reply_segments = list(reply_media_segments) if isinstance(reply_media_segments, list) else []
        message_id = normalize_text(str(context.get("message_id", "")))
        reply_to_message_id = normalize_text(str(context.get("reply_to_message_id", "")))
        forced_target_message_id = normalize_text(str(args.get("target_message_id", "")))

        # 没有显式图片时，不默认“盲猜最近一张图”。
        # 仅用户明确指向“这张/引用那张/刚才那张”时才允许最近图兜底，并且仅在最近唯一时使用。
        reference_cues = (
            "这张",
            "这个图",
            "这图",
            "引用那张",
            "回复那张",
            "刚才那张",
            "刚刚那张",
            "前面那张",
            "刚发的图",
        )
        prefer_reply_cues = ("引用那张", "回复那张", "刚才那张", "刚刚那张", "前面那张")
        prefer_current_cues = ("这张", "这个图", "这图", "当前这张", "这张图")
        explicit_reply_ref = any(cue in query_norm for cue in prefer_reply_cues)
        explicit_current_ref = any(cue in query_norm for cue in prefer_current_cues)
        explicit_reference = any(cue in query_norm for cue in reference_cues)
        has_direct_image_context = current_image_count > 0 or reply_image_count > 0
        if forced_target_message_id:
            if message_id and forced_target_message_id == message_id and current_image_count > 0:
                selected_source = "current_message"
                selected_segments = current_segments
                method_args["target_message_id"] = message_id
            elif reply_to_message_id and forced_target_message_id == reply_to_message_id and reply_image_count > 0:
                selected_source = "reply_message"
                selected_segments = reply_segments
                method_args["target_message_id"] = reply_to_message_id
            else:
                return ToolCallResult(
                    ok=False,
                    error="target_message_not_found",
                    display="你指定的目标消息里没有图片，请直接回复那张图再问我。",
                )
        elif current_image_count > 0 and reply_image_count > 0:
            if analyze_all:
                selected_source = "current_and_reply"
                selected_segments = current_segments + reply_segments
            elif explicit_reply_ref:
                selected_source = "reply_message"
                selected_segments = reply_segments
                if reply_to_message_id:
                    method_args["target_message_id"] = reply_to_message_id
            elif explicit_current_ref:
                selected_source = "current_message"
                selected_segments = current_segments
                if message_id:
                    method_args["target_message_id"] = message_id
            else:
                return ToolCallResult(
                    ok=False,
                    error="image_context_ambiguous",
                    display="你这条消息和引用里都有图片。请说“分析这张图”或“分析引用的图”，或者直接回复目标图片。",
                )
        elif current_image_count > 0:
            selected_source = "current_message"
            selected_segments = current_segments
            if message_id:
                method_args["target_message_id"] = message_id
        elif reply_image_count > 0:
            selected_source = "reply_message"
            selected_segments = reply_segments
            if reply_to_message_id:
                method_args["target_message_id"] = reply_to_message_id
        else:
            # 在“这条消息没有图片 / 也没引用图片”时，优先尝试会话最近图片兜底：
            # - recent_only_when_unique=True：仅当最近唯一时自动命中，避免误判错图。
            # - 用户显式说“这张/刚刚那张”时同样走该路径。
            selected_source = "recent_cache_fallback"
            method_args["allow_recent_fallback"] = True
            method_args["recent_only_when_unique"] = not analyze_all
            selected_segments = current_segments + reply_segments

        method_args.setdefault("allow_recent_fallback", False)
        method_args.setdefault("recent_only_when_unique", not analyze_all)
        if selected_source:
            method_args["target_source"] = selected_source

        _log.info(
            "analyze_image_target | source=%s | current_images=%d | reply_images=%d | message_id=%s | reply_to=%s | analyze_all=%s | max_images=%s",
            selected_source,
            current_image_count,
            reply_image_count,
            message_id or "-",
            reply_to_message_id or "-",
            analyze_all,
            max_images if analyze_all else "-",
        )

    try:
        result = await tool_executor._method_media_analyze_image(
            method_name="analyze_image",
            method_args=method_args,
            query=query,
            message_text=message_text,
            raw_segments=selected_segments if selected_segments else (list(raw_segments) if isinstance(raw_segments, list) else []),
            conversation_id=conversation_id,
            api_call=api_call,
        )
    except Exception as exc:
        _log.warning("analyze_image_error | %s", exc)
        return ToolCallResult(ok=False, error=f"analyze_image_error: {exc}", display=f"图片识别失败: {exc}")

    payload = result.payload or {}
    text = str(payload.get("text", ""))
    analysis = str(payload.get("analysis", text))

    if result.ok and (text or analysis):
        display = clip_text(analysis or text, 600)
        data: dict[str, Any] = {
            "analysis": analysis,
            "source": str(payload.get("source", "")),
        }
        analyses = payload.get("analyses")
        if isinstance(analyses, list) and analyses:
            data["analyses"] = analyses
            count_val = payload.get("count")
            if isinstance(count_val, int) and count_val > 0:
                data["count"] = count_val
            else:
                data["count"] = len(analyses)
        return ToolCallResult(
            ok=True,
            data=data,
            display=display,
        )

    error_text = result.error or "识别失败"
    return ToolCallResult(ok=False, error=error_text, display=text or f"图片识别失败: {error_text}")


# ── 语音分析 handler ──

async def _handle_analyze_voice(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """处理语音转文字请求。"""
    from pathlib import Path as _Path
    import hashlib

    explicit_url = normalize_text(str(args.get("url", "")))
    raw_segments = context.get("raw_segments", [])
    reply_media_segments = context.get("reply_media_segments", [])
    api_call = context.get("api_call")

    url_candidates: list[str] = []
    file_id_candidates: list[str] = []
    seen_urls: set[str] = set()
    seen_file_ids: set[str] = set()

    def _append_voice_url(value: Any) -> None:
        url = normalize_text(str(value))
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        url_candidates.append(url)

    def _append_voice_file_id(value: Any) -> None:
        file_id = normalize_text(str(value))
        if not file_id or file_id in seen_file_ids:
            return
        seen_file_ids.add(file_id)
        file_id_candidates.append(file_id)

    if explicit_url:
        _append_voice_url(explicit_url)

    for segs in (raw_segments, reply_media_segments):
        for seg in (segs or []):
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            if seg_type not in ("record", "audio"):
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            _append_voice_url(data.get("url", ""))
            _append_voice_file_id(data.get("file", "") or data.get("file_id", ""))

    if not url_candidates and not file_id_candidates:
        return ToolCallResult(
            ok=False,
            error="voice_not_found",
            display="没有找到语音消息。请发送语音或回复一条语音消息再试。",
        )

    # 尝试通过 NapCat API 用 file_id 反查完整可下载地址。
    if callable(api_call):
        for voice_file_id in file_id_candidates:
            try:
                result = await call_napcat_api(
                    api_call,
                    "get_record",
                    file=voice_file_id,
                    out_format="mp3",
                )
            except Exception as exc:
                _log.warning("voice_get_record_error | file=%s | %s", voice_file_id, exc)
                continue
            if not isinstance(result, dict):
                continue

            # 某些实现直接返回转录文本
            text = str(result.get("text", "")).strip()
            if text:
                return ToolCallResult(
                    ok=True,
                    data={"text": text, "source": "napcat_stt"},
                    display=f"语音内容: {text}",
                )

            _append_voice_url(result.get("url", "") or result.get("file", ""))

    if not url_candidates:
        return ToolCallResult(ok=False, error="voice_url_unavailable", display="无法获取语音文件地址")

    try:
        from utils.media import download_file, extract_audio, transcribe_audio_enhanced

        cache_dir = _Path("storage/cache/voice")
        cache_dir.mkdir(parents=True, exist_ok=True)

        had_download = False
        had_empty_transcript = False

        for voice_url in url_candidates:
            fname = hashlib.md5(voice_url.encode()).hexdigest()
            voice_path = cache_dir / f"{fname}.mp3"

            if not (voice_path.is_file() and voice_path.stat().st_size > 0):
                downloaded = False
                if re.match(r"^https?://", voice_url, flags=re.IGNORECASE):
                    downloaded = await download_file(voice_url, voice_path, timeout=20.0)
                else:
                    local_path = _Path(voice_url).expanduser()
                    if local_path.is_file():
                        try:
                            shutil.copyfile(local_path, voice_path)
                            downloaded = voice_path.is_file() and voice_path.stat().st_size > 0
                        except Exception as exc:
                            _log.warning(
                                "voice_local_copy_error | src=%s | %s", local_path, exc
                            )
                if not downloaded:
                    continue

            had_download = True

            wav_path = await extract_audio(voice_path, voice_path.with_suffix(".wav"))
            if not wav_path:
                wav_path = str(voice_path)  # 尝试直接用 mp3

            res = await transcribe_audio_enhanced(wav_path, language="zh")
            text = res.get("text", "")
            formatted_text = res.get("formatted_text", text)
            
            if text:
                _score = res.get("score")
                _score = float(_score) if _score is not None else -999.0
                _pass = res.get("pass", "unknown")
                return ToolCallResult(
                    ok=True,
                    data={"text": text, "formatted": formatted_text, "score": _score, "pass": _pass, "source": "whisper_enhanced"},
                    display=f"【语音内容识别】(置信度分: {_score:.2f}, 策略: {_pass})\n{formatted_text}",
                )
            had_empty_transcript = True

        if had_empty_transcript:
            return ToolCallResult(
                ok=False,
                error="whisper_transcribe_empty",
                display="语音转录结果为空，可能是静音或无法识别",
            )
        if not had_download:
            return ToolCallResult(
                ok=False, error="voice_download_failed", display="语音文件下载失败"
            )
        return ToolCallResult(
            ok=False,
            error="voice_prepare_failed",
            display="语音准备失败",
        )
    except ImportError:
        return ToolCallResult(ok=False, error="whisper_not_installed", display="语音转录功能需要安装 openai-whisper: pip install openai-whisper")
    except Exception as exc:
        _log.warning("analyze_voice_error | %s", exc)
        return ToolCallResult(ok=False, error=f"voice_error: {exc}", display=f"语音分析失败: {exc}")


def _resolve_video_url_from_args_or_context(args: dict[str, Any], context: dict[str, Any]) -> str:
    """优先显式 url，否则从当前消息/引用消息里提取视频 url。"""
    explicit_url = normalize_text(str(args.get("url", "")))
    if explicit_url:
        return explicit_url

    raw_segments = context.get("raw_segments", []) or []
    reply_media_segments = context.get("reply_media_segments", []) or []
    for segs in (raw_segments, reply_media_segments):
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            if seg_type != "video":
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            url = normalize_text(str(data.get("url", "")))
            if url:
                return url
    return ""


def _to_non_negative_float(value: Any, default: float = 0.0) -> float:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return default
    if val < 0:
        return 0.0
    return val


def _resolve_local_path_from_uri(value: str) -> Path | None:
    text = normalize_text(value)
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlparse(text)
        local_raw = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:/", local_raw):
            local_raw = local_raw[1:]
        path = Path(local_raw)
    else:
        path = Path(text)
    if path.exists() and path.is_file():
        return path
    return None


def _probe_stream_flags_fallback(video_path: Path) -> tuple[bool, bool]:
    """ffprobe 不可用时，尽力从 ffmpeg -i 输出判断是否有音视频流。"""
    try:
        from utils.media import get_ffmpeg
        ffmpeg = get_ffmpeg()
    except Exception:
        ffmpeg = None
    if not ffmpeg:
        # 最保守：默认有视频、音频未知（按无音频处理）
        return True, False
    try:
        import subprocess

        ffmpeg = resolve_executable_for_spawn(ffmpeg)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video_path)],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
            **macos_subprocess_kwargs(),
        )
        text = normalize_text((proc.stdout or "") + "\n" + (proc.stderr or ""))
        has_video = bool(re.search(r"\bvideo\b", text, flags=re.IGNORECASE))
        has_audio = bool(re.search(r"\baudio\b", text, flags=re.IGNORECASE))
        if has_video:
            return has_video, has_audio
    except Exception:
        pass
    suffix = video_path.suffix.lower()
    return suffix in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv", ".avi"}, False


async def _handle_split_video(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """视频处理：切片、提取音频、封面、关键帧。"""
    import hashlib
    from pathlib import Path as _Path

    video_url = _resolve_video_url_from_args_or_context(args, context)
    if not video_url:
        return ToolCallResult(
            ok=False,
            error="video_not_found",
            display="没有找到视频。请发送视频或回复一条视频消息，或者传入视频链接 URL。",
        )

    mode_raw = normalize_text(str(args.get("mode", "clip"))).lower()
    mode_alias = {
        "clip": "clip",
        "split": "clip",
        "segment": "clip",
        "切片": "clip",
        "分割": "clip",
        "片段": "clip",
        "audio": "audio",
        "extract_audio": "audio",
        "音频": "audio",
        "导出音频": "audio",
        "cover": "cover",
        "thumbnail": "cover",
        "封面": "cover",
        "截图": "cover",
        "画面": "cover",
        "frames": "frames",
        "keyframes": "frames",
        "关键帧": "frames",
    }
    mode = mode_alias.get(mode_raw, "")
    if not mode:
        return ToolCallResult(
            ok=False,
            error="invalid_mode",
            display="mode 只支持 clip / audio / cover / frames。",
        )

    try:
        from utils.media import (
            download_file,
            extract_audio,
            extract_keyframes,
            get_media_info,
            run_ffmpeg,
            run_ffprobe_json,
        )
    except ImportError as exc:
        return ToolCallResult(ok=False, error=f"missing_dependency: {exc}", display=f"缺少依赖: {exc}")

    cache_root = _Path("storage/cache/videos/split")
    cache_root.mkdir(parents=True, exist_ok=True)

    source_path: _Path | None = _resolve_local_path_from_uri(video_url)
    source_tag = hashlib.md5(video_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
    if source_path is None:
        parsed = urlparse(video_url)
        suffix = _Path(parsed.path or "").suffix.lower()
        if suffix not in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv", ".avi"}:
            suffix = ".mp4"
        source_path = cache_root / f"{source_tag}.source{suffix}"
        if not source_path.exists():
            ok = await download_file(video_url, source_path, timeout=80.0, max_size_mb=128.0)
            if not ok:
                return ToolCallResult(ok=False, error="video_download_failed", display="视频下载失败（可能链接失效或体积过大）")

    probe = await run_ffprobe_json(source_path)
    info = get_media_info(probe) if isinstance(probe, dict) and probe else {}
    if not info:
        has_video, has_audio = _probe_stream_flags_fallback(source_path)
        info = {"duration": 0.0, "has_video": has_video, "has_audio": has_audio}
    if not bool(info.get("has_video")):
        return ToolCallResult(ok=False, error="not_video", display="目标文件不是有效视频。")

    duration = float(info.get("duration", 0.0) or 0.0)
    source_has_audio = bool(info.get("has_audio"))

    start_seconds = _to_non_negative_float(args.get("start_seconds", 0.0), 0.0)
    end_input = args.get("end_seconds", None)
    duration_input = args.get("duration_seconds", None)
    end_seconds = 0.0
    if end_input not in (None, ""):
        end_seconds = _to_non_negative_float(end_input, 0.0)
    elif duration_input not in (None, ""):
        span = _to_non_negative_float(duration_input, 0.0)
        if span > 0:
            end_seconds = start_seconds + span

    if duration > 0:
        start_seconds = min(start_seconds, max(duration - 0.1, 0.0))
        if end_seconds > 0:
            end_seconds = min(end_seconds, duration)
            if end_seconds <= start_seconds + 0.02:
                end_seconds = 0.0

    if mode == "clip":
        if end_seconds <= 0:
            if duration > 0:
                end_seconds = min(duration, start_seconds + 30.0)
            else:
                end_seconds = start_seconds + 30.0
        clip_duration = max(0.5, end_seconds - start_seconds)
        out_name = f"{source_tag}_{int(start_seconds * 1000)}_{int(end_seconds * 1000)}.clip.mp4"
        out_path = cache_root / out_name
        cmd = [
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{clip_duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        ok, err = await run_ffmpeg(cmd, timeout=180.0)
        if not ok or not out_path.exists() or out_path.stat().st_size <= 1024:
            out_path.unlink(missing_ok=True)
            return ToolCallResult(
                ok=False,
                error=f"clip_failed:{err}",
                display=f"视频切片失败: {clip_text(err, 180) if err else 'unknown error'}",
            )

        out_probe = await run_ffprobe_json(out_path)
        out_info = get_media_info(out_probe) if isinstance(out_probe, dict) and out_probe else {}
        if not out_info:
            has_video_out, has_audio_out = _probe_stream_flags_fallback(out_path)
            out_info = {"has_video": has_video_out, "has_audio": has_audio_out}
        if source_has_audio and not bool(out_info.get("has_audio")):
            out_path.unlink(missing_ok=True)
            return ToolCallResult(
                ok=False,
                error="clip_audio_missing",
                display="切片结果丢失音轨，已中止发送。建议改短一点再试。",
            )
        return ToolCallResult(
            ok=True,
            data={
                "mode": "clip",
                "video_url": str(out_path.resolve()),
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(end_seconds, 3),
                "duration_seconds": round(clip_duration, 3),
            },
            display=f"切片完成: {start_seconds:.1f}s ~ {end_seconds:.1f}s",
        )

    if mode == "audio":
        if not source_has_audio:
            return ToolCallResult(ok=False, error="video_no_audio", display="原视频没有音轨，无法导出音频。")

        # 默认不在工具层裁剪，交给发送层按平台能力做“整段/分段/兜底”。
        default_max_audio_seconds = 0

        max_audio_seconds_raw = args.get("max_audio_seconds", default_max_audio_seconds)
        try:
            max_audio_seconds = max(0, int(max_audio_seconds_raw or 0))
        except Exception:
            max_audio_seconds = default_max_audio_seconds

        # 若未显式指定 end/duration，默认导出语音友好长度，提升 QQ 发送成功率。
        auto_trimmed = False
        if end_seconds <= start_seconds + 0.02 and max_audio_seconds > 0:
            if duration > 0:
                end_seconds = min(duration, start_seconds + float(max_audio_seconds))
            else:
                end_seconds = start_seconds + float(max_audio_seconds)
            auto_trimmed = end_seconds > start_seconds + 0.02

        out_name = f"{source_tag}_{int(start_seconds * 1000)}_{int(end_seconds * 1000)}.audio.mp3"
        out_path = cache_root / out_name
        cmd = []
        if start_seconds > 0:
            cmd.extend(["-ss", f"{start_seconds:.3f}"])
        cmd.extend(["-i", str(source_path)])
        if end_seconds > start_seconds + 0.02:
            cmd.extend(["-t", f"{(end_seconds - start_seconds):.3f}"])
        cmd.extend(
            [
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(out_path),
            ]
        )
        ok, err = await run_ffmpeg(cmd, timeout=120.0)
        if not ok or not out_path.exists() or out_path.stat().st_size <= 512:
            out_path.unlink(missing_ok=True)
            # 兜底输出 wav，至少保证“可导出音频”
            wav_name = f"{source_tag}_{int(start_seconds * 1000)}_{int(end_seconds * 1000)}.audio.wav"
            wav_path = cache_root / wav_name
            wav_result = await extract_audio(source_path, wav_path)
            if not wav_result:
                return ToolCallResult(
                    ok=False,
                    error=f"audio_extract_failed:{err}",
                    display=f"音频导出失败: {clip_text(err, 180) if err else 'unknown error'}",
                )
            out_path = _Path(wav_result)

        out_duration = 0.0
        out_probe = await run_ffprobe_json(out_path)
        out_info = get_media_info(out_probe) if isinstance(out_probe, dict) and out_probe else {}
        if isinstance(out_info, dict):
            out_duration = float(out_info.get("duration", 0.0) or 0.0)
        if out_duration <= 0 and end_seconds > start_seconds + 0.02:
            out_duration = max(0.0, end_seconds - start_seconds)

        display = "音频导出完成"
        if auto_trimmed and out_duration > 0:
            display = f"音频导出完成（已裁剪至 {out_duration:.1f}s）"

        return ToolCallResult(
            ok=True,
            data={
                "mode": "audio",
                "audio_file": str(out_path.resolve()),
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(end_seconds, 3) if end_seconds > 0 else 0,
                "duration_seconds": round(out_duration, 3) if out_duration > 0 else 0,
                "auto_trimmed": auto_trimmed,
            },
            display=display,
        )

    if mode == "cover":
        frame_time = _to_non_negative_float(args.get("frame_time_seconds", 1.0), 1.0)
        if duration > 0:
            frame_time = min(frame_time, max(duration - 0.05, 0.0))
        out_name = f"{source_tag}_{int(frame_time * 1000)}.cover.jpg"
        out_path = cache_root / out_name
        ok, err = await run_ffmpeg(
            [
                "-ss",
                f"{frame_time:.3f}",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ],
            timeout=80.0,
        )
        if not ok or not out_path.exists() or out_path.stat().st_size <= 512:
            out_path.unlink(missing_ok=True)
            return ToolCallResult(
                ok=False,
                error=f"cover_extract_failed:{err}",
                display=f"封面提取失败: {clip_text(err, 180) if err else 'unknown error'}",
            )
        return ToolCallResult(
            ok=True,
            data={"mode": "cover", "image_url": str(out_path.resolve()), "image_urls": [str(out_path.resolve())]},
            display=f"封面提取完成（{frame_time:.1f}s）",
        )

    # mode == "frames"
    max_frames = int(_to_non_negative_float(args.get("max_frames", 6), 6))
    max_frames = max(1, min(max_frames, 12))
    interval_seconds = _to_non_negative_float(args.get("interval_seconds", 0), 0)
    frame_dir = cache_root / f"{source_tag}_frames_{max_frames}"
    if frame_dir.exists():
        for stale in frame_dir.glob("frame_*.jpg"):
            stale.unlink(missing_ok=True)
    frames = await extract_keyframes(
        source_path,
        frame_dir,
        max_frames=max_frames,
        interval_seconds=interval_seconds if interval_seconds > 0 else 0,
        timeout=120.0,
    )
    if not frames:
        return ToolCallResult(ok=False, error="frames_extract_failed", display="关键帧提取失败。")

    abs_frames = [str(_Path(item).resolve()) for item in frames if _Path(item).is_file()]
    if not abs_frames:
        return ToolCallResult(ok=False, error="frames_extract_empty", display="关键帧提取结果为空。")
    return ToolCallResult(
        ok=True,
        data={
            "mode": "frames",
            "image_url": abs_frames[0],
            "image_urls": abs_frames,
            "frame_count": len(abs_frames),
        },
        display=f"关键帧提取完成，共 {len(abs_frames)} 张",
    )


# ── 本地视频内容分析 handler ──

async def _handle_analyze_local_video(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """处理本地/引用视频的深度内容分析，统一复用共享 VideoAnalyzer 链路。"""
    tool_executor = context.get("tool_executor")
    if not tool_executor:
        return ToolCallResult(
            ok=False,
            error="video_analyzer_unavailable",
            display="视频分析模块未初始化",
        )

    explicit_url = normalize_text(str(args.get("url", "")))
    message_text = normalize_text(str(context.get("message_text", "")))
    conversation_id = str(context.get("conversation_id", ""))
    raw_segments = context.get("raw_segments", [])
    reply_media_segments = context.get("reply_media_segments", [])

    merged_segments: list[dict[str, Any]] = []
    for segs in (raw_segments, reply_media_segments):
        if isinstance(segs, list):
            merged_segments.extend(item for item in segs if isinstance(item, dict))

    method_args: dict[str, Any] = {}
    if explicit_url:
        method_args["url"] = explicit_url

    try:
        result = await tool_executor._method_video_analyze(
            method_name="analyze_local_video",
            method_args=method_args,
            query=message_text or explicit_url,
            message_text=message_text,
            raw_segments=merged_segments,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        _log.warning("analyze_local_video_error | %s", exc)
        return ToolCallResult(
            ok=False,
            error=f"video_analysis_error: {exc}",
            display=f"视频分析失败: {exc}",
        )

    payload = result.payload or {}
    text = str(payload.get("text", ""))
    video_url = str(payload.get("video_url", "")) or explicit_url
    analysis_context = str(payload.get("analysis_context", text))
    image_url = str(payload.get("image_url", ""))
    image_urls = [
        normalize_text(str(item))
        for item in (payload.get("image_urls", []) or [])
        if normalize_text(str(item))
    ]
    duration = 0
    title = ""

    for line in analysis_context.split("\n"):
        if line.startswith("时长:"):
            try:
                parts = line.split(":", 1)[1].strip().split(":")
                if len(parts) == 3:
                    duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    duration = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("标题:"):
            title = line.split(":", 1)[1].strip()

    safety = _estimate_qq_safety(
        url=video_url or explicit_url or "local-video",
        title=title or text,
        duration=duration,
        platform="local",
    )

    if result.ok:
        display_parts = [clip_text(text or analysis_context or "视频分析完成", 500)]
        display_parts.append(f"安全度: {safety['level']} - {safety.get('suggestion', '')}")
        data: dict[str, Any] = {
            "text": text or analysis_context,
            "qq_safety": safety,
            "platform": "local",
        }
        if video_url:
            data["video_url"] = video_url
        if image_url:
            data["image_url"] = image_url
        if image_urls:
            data["image_urls"] = image_urls
        return ToolCallResult(ok=True, data=data, display="\n".join(display_parts))

    error_text = result.error or "分析失败"
    return ToolCallResult(
        ok=False,
        error=error_text,
        display=text or f"视频分析失败: {error_text}",
    )
