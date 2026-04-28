"""Auto-split from core/agent_tools.py — 实用工具 (final_answer, think) + 表情系统"""
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
from core.agent_tools_napcat import _unwrap_onebot_message_result
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning, looks_like_preferred_name_knowledge
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")

def _register_utility_tools(registry: AgentToolRegistry) -> None:
    """注册实用工具。"""

    registry.register(
        ToolSchema(
            name="final_answer",
            description="当你已经收集到足够信息，调用此工具输出最终回复给用户。这是结束对话的唯一方式",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "回复给用户的文本内容"},
                    "image_url": {"type": "string", "description": "可选，附带的单张图片URL"},
                    "image_urls": {"type": "array", "items": {"type": "string"}, "description": "可选，多张图片URL列表（图文作品等需要发送多张图时使用）"},
                    "video_url": {"type": "string", "description": "可选，附带的视频URL"},
                    "audio_file": {"type": "string", "description": "可选，本地音频文件路径（用于发送语音）"},
                },
                "required": ["text"],
            },
            category="general",
        ),
        _handle_final_answer,
    )

    registry.register(
        ToolSchema(
            name="think",
            description="内部思考工具。当你需要分析复杂问题、规划多步操作、或整理信息时使用。不会产生任何外部效果",
            parameters={
                "type": "object",
                "properties": {
                    "thought": {"type": "string", "description": "你的思考内容"},
                },
                "required": ["thought"],
            },
            category="general",
        ),
        _handle_think,
    )

    registry.register(
        ToolSchema(
            name="navigate_section",
            description="内部 Prompt Navigator 分区跳转工具。当当前分区缺少工具或任务应由其他提示词分区处理时调用",
            parameters={
                "type": "object",
                "properties": {
                    "section_id": {"type": "string", "description": "目标分区 ID"},
                    "reason": {"type": "string", "description": "切换原因，简短说明当前分区为什么不够用"},
                },
                "required": ["section_id"],
            },
            category="general",
        ),
        _handle_navigate_section,
    )


async def _handle_final_answer(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    text = str(args.get("text", "")).strip()
    image_url = str(args.get("image_url", "")).strip()
    video_url = str(args.get("video_url", "")).strip()
    audio_file = str(args.get("audio_file", "")).strip()
    raw_image_urls = args.get("image_urls", [])
    image_urls: list[str] = []
    if isinstance(raw_image_urls, list):
        image_urls = [str(u).strip() for u in raw_image_urls if str(u).strip()]
    # 如果有 image_url 但不在 image_urls 中，合并
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    if image_urls and not image_url:
        image_url = image_urls[0]
    return ToolCallResult(
        ok=True,
        data={
            "text": text,
            "image_url": image_url,
            "image_urls": image_urls,
            "video_url": video_url,
            "audio_file": audio_file,
            "is_final": True,
        },
        display=text,
    )


async def _handle_think(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    thought = str(args.get("thought", "")).strip()
    return ToolCallResult(ok=True, data={"thought": thought}, display=f"[思考] {clip_text(thought, 200)}")


async def _handle_navigate_section(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    _ = (args, context)
    return ToolCallResult(
        ok=False,
        error="navigate_section_internal_only",
        display="navigate_section 只能由 AgentLoop 内部处理。",
    )


# ── Sticker / Face tools ──

_STICKER_SEND_CUES = (
    "发个表情",
    "发一个表情",
    "发表情",
    "发送表情",
    "来个表情",
    "来一个表情",
    "用表情",
    "回个表情",
    "发张表情包",
    "来张表情包",
    "发出来看看",
    "看看效果",
    "预览",
    "把刚学的发",
)

_STICKER_MANAGEMENT_CUES = (
    "学习表情",
    "学这个表情",
    "添加表情",
    "收录表情",
    "记住这个表情",
    "表情包库",
    "表情库",
    "meme更新",
    "更新了吗",
    "学会了吗",
    "学到了吗",
    "描述不对",
    "识别错",
    "纠正表情",
    "改这个表情",
    "最近学的",
    "刚学的是什么",
)


def _looks_like_explicit_sticker_send_message(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    if any(cue in content for cue in _STICKER_SEND_CUES):
        return True
    return bool(
        re.search(r"(发|来|回|用).{0,8}(表情|表情包|emoji|sticker)", content)
        or re.search(r"(表情|表情包).{0,8}(发|来|回|看)", content)
    )


def _looks_like_sticker_management_message(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    if any(cue in content for cue in _STICKER_MANAGEMENT_CUES):
        return True
    if "表情" not in content and "meme" not in content and "emoji" not in content:
        return False
    return bool(
        re.search(r"(学习|添加|收录|记住|纠正|修改|更新|识别|描述|标签|分类).{0,8}(表情|表情包)", content)
        or re.search(r"(表情|表情包).{0,8}(学习|添加|收录|纠正|更新|描述|标签|分类)", content)
    )


def _should_block_sticker_send_for_management_turn(context: dict[str, Any]) -> bool:
    message_text = normalize_text(
        str(context.get("original_message_text", "") or context.get("message_text", ""))
    )
    if not message_text:
        return False
    return (
        _looks_like_sticker_management_message(message_text)
        and not _looks_like_explicit_sticker_send_message(message_text)
    )


def _extract_message_segments_from_onebot_payload(raw: Any) -> list[dict[str, Any]]:
    payload = _unwrap_onebot_message_result(raw)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, list):
        return []
    return [dict(seg) for seg in message if isinstance(seg, dict)]


async def _load_reply_message_segments_for_sticker(context: dict[str, Any]) -> list[dict[str, Any]]:
    reply_to_message_id = normalize_text(str(context.get("reply_to_message_id", "")))
    api_call = context.get("api_call")
    if not reply_to_message_id or not callable(api_call):
        return []
    message_ids: list[Any] = [reply_to_message_id]
    try:
        message_ids.insert(0, int(reply_to_message_id))
    except (TypeError, ValueError):
        pass
    for message_id in message_ids:
        try:
            raw = await call_napcat_api(api_call, "get_msg", message_id=message_id)
            segments = _extract_message_segments_from_onebot_payload(raw)
            if segments:
                return segments
        except Exception as exc:
            _log.warning(
                "learn_sticker_get_msg_failed | reply_to_message_id=%s | tried=%s | error=%s",
                reply_to_message_id,
                message_id,
                exc,
            )
            continue
    return []


def _extract_first_native_sticker_segment(
    segments: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any]]:
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if seg_type not in {"mface", "face"}:
            continue
        data = seg.get("data", {}) or {}
        if isinstance(data, dict):
            return seg_type, dict(data)
    return "", {}


def _extract_first_sticker_media_payload(
    segments: list[dict[str, Any]] | None,
) -> tuple[str, str, str]:
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        data = seg.get("data", {}) or {}
        if seg_type != "image" or not isinstance(data, dict):
            continue
        image_url = normalize_text(str(data.get("url", "")))
        image_file = normalize_text(str(data.get("file", "")))
        image_sub_type = normalize_text(str(data.get("sub_type", "")))
        if image_url or image_file:
            return image_url, image_file, image_sub_type

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        data = seg.get("data", {}) or {}
        if not isinstance(data, dict):
            continue
        if seg_type == "mface":
            image_url = normalize_text(
                str(data.get("url", "") or data.get("download_url", "") or data.get("src", ""))
            )
            image_file = normalize_text(str(data.get("file", "") or data.get("file_id", "")))
            if image_url or image_file:
                return image_url, image_file, ""
    return "", "", ""


async def _handle_send_face(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """Send a classic QQ face by emotion/description query."""
    if _should_block_sticker_send_for_management_turn(context):
        return ToolCallResult(
            ok=False,
            data={},
            display="当前是在学习或查询表情包状态，不应直接发送表情",
        )
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolCallResult(ok=False, data={}, display="缺少 query 参数")

    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    faces = sticker_mgr.find_face(query)
    if not faces:
        return ToolCallResult(ok=False, data={}, display=f"没有找到匹配 '{query}' 的表情")

    face = faces[0]
    seg = sticker_mgr.get_face_segment(face.face_id)
    api_call = context.get("api_call")
    if not api_call:
        return ToolCallResult(ok=False, data={}, display="api_call 不可用")

    group_id = context.get("group_id", 0)
    user_id = context.get("user_id", "")

    try:
        if group_id:
            await call_napcat_api(api_call, "send_group_msg", group_id=group_id, message=[seg])
        elif user_id:
            await call_napcat_api(api_call, "send_private_msg", user_id=int(user_id), message=[seg])
        else:
            return ToolCallResult(ok=False, data={}, display="无法确定发送目标")
        return ToolCallResult(
            ok=True,
            data={"face_id": face.face_id, "desc": face.desc, "send_mode": "face"},
            display=f"已发送表情 [{face.desc}] (id={face.face_id})",
        )
    except Exception as e:
        return ToolCallResult(ok=False, data={}, display=f"发送失败: {e}")


async def _handle_send_emoji(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """Send a local emoji image. Supports keyword search or random."""
    if _should_block_sticker_send_for_management_turn(context):
        return ToolCallResult(
            ok=False,
            data={},
            display="当前是在学习或查询表情包状态，不应直接发送表情包",
        )
    query = normalize_text(str(args.get("query", ""))).strip()
    if not query:
        query = normalize_text(str(args.get("keyword", ""))).strip()
    if not query:
        query = normalize_text(str(args.get("name", ""))).strip()
    if not query:
        query = "随机"
    config = context.get("config", {})
    if not isinstance(config, dict):
        config = {}
    control = config.get("control", {})
    if not isinstance(control, dict):
        control = {}
    emoji_level = normalize_text(str(control.get("emoji_level", "medium"))).lower() or "medium"
    if emoji_level == "off":
        return ToolCallResult(ok=False, data={}, display="emoji_disabled_by_control")
    if query.lower() in {"随机", "random"}:
        prob_map = {"low": 0.2, "medium": 0.45, "high": 0.75}
        p = float(prob_map.get(emoji_level, 0.45))
        if random.random() > p:
            return ToolCallResult(ok=False, data={}, display="当前语境不适合发表情")

    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    if sticker_mgr.learned_count == 0:
        return ToolCallResult(ok=False, data={}, display="表情包库为空，还没有学习任何表情包")

    q = normalize_text(query).lower()
    q_compact = re.sub(r"\s+", "", q)
    latest_cues = (
        "最近",
        "最新",
        "刚学",
        "刚刚学",
        "刚才学",
        "刚学的",
        "刚刚学的",
        "刚刚学习",
        "刚学习",
        "刚学会",
        "刚刚学会",
        "刚才那张",
        "上一个",
    )
    latest_mode = any(cue in q or cue in q_compact for cue in latest_cues)
    # “刚刚学习的表情包”优先走最后学习结果，避免被“最近文件”误命中。
    prefer_last_learned = any(
        cue in q_compact
        for cue in ("刚刚学习", "刚学习", "刚学会", "刚刚学会", "刚学的", "刚刚学的", "刚才那张")
    )
    key = ""
    e = None
    if latest_mode:
        source_user = str(context.get("user_id", "")).strip()
        if prefer_last_learned and hasattr(sticker_mgr, "last_learned_emoji"):
            latest_exact = sticker_mgr.last_learned_emoji(source_user=source_user)
            if latest_exact is None:
                latest_exact = sticker_mgr.last_learned_emoji()
            if latest_exact:
                key, e = latest_exact
        latest = sticker_mgr.latest_emoji(source_user=source_user, count=1)
        if (not e or not key) and not latest:
            latest = sticker_mgr.latest_emoji(count=1)
        if not e and not latest:
            return ToolCallResult(ok=False, data={}, display="还没有可发送的最近表情包")
        if not e:
            key, e = latest[0]
    else:
        emojis = sticker_mgr.find_emoji(query, strict=True)
        if not emojis:
            return ToolCallResult(ok=False, data={}, display=f"没有找到匹配的表情包 (共{sticker_mgr.learned_count}张)")
        e = emojis[0]
        key = sticker_mgr.emoji_key(e) or ""
    if not e or not key:
        return ToolCallResult(ok=False, data={}, display="表情包 key 丢失")
    seg, send_mode, send_meta = sticker_mgr.get_preferred_emoji_segment(key)
    if not seg or not send_mode:
        return ToolCallResult(ok=False, data={}, display="表情包发送数据不存在")

    api_call = context.get("api_call")
    if not api_call:
        return ToolCallResult(ok=False, data={}, display="api_call 不可用")

    group_id = context.get("group_id", 0)
    user_id = context.get("user_id", "")

    try:
        if group_id:
            await call_napcat_api(api_call, "send_group_msg", group_id=group_id, message=[seg])
        elif user_id:
            await call_napcat_api(api_call, "send_private_msg", user_id=int(user_id), message=[seg])
        else:
            return ToolCallResult(ok=False, data={}, display="无法确定发送目标")
        desc = e.description or key.split("/")[-1]
        # 返回空 display，让 Agent 根据 ok=True 自己组织回复
        result_data = {"key": key, "desc": desc, "send_mode": send_mode}
        if isinstance(send_meta, dict):
            result_data.update(send_meta)
        return ToolCallResult(
            ok=True,
            data=result_data,
            display="",
        )
    except Exception as e_err:
        return ToolCallResult(ok=False, data={}, display=f"发送失败: {e_err}")


async def _handle_list_faces(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """List available QQ faces matching a query, without sending."""
    query = str(args.get("query", "")).strip()
    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    if query:
        faces = sticker_mgr.find_face(query)
    else:
        faces = [sticker_mgr._faces[fid] for fid in [13, 14, 5, 9, 11, 76, 179, 271, 264, 182]
                if fid in sticker_mgr._faces]

    lines = [f"id={f.face_id} {f.desc}" for f in faces]
    return ToolCallResult(
        ok=True,
        data={"faces": [{"id": f.face_id, "desc": f.desc} for f in faces]},
        display="\n".join(lines) if lines else "没有匹配的表情",
    )


async def _handle_list_emojis(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """List emoji stats and sample available emojis."""
    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    total = sticker_mgr.emoji_count
    registered = sticker_mgr.registered_count
    cat_stats = sticker_mgr.category_stats()

    # 随机取几张已注册的展示
    samples = sticker_mgr.random_emoji(5)
    sample_lines = [f"  {k} ({e.description or '无描述'})" for k, e in samples]

    cat_lines = [f"  {cat}: {cnt}张" for cat, cnt in sorted(cat_stats.items(), key=lambda x: -x[1])]
    cat_block = "\n".join(cat_lines) if cat_lines else "  暂无分类数据"

    display = (
        f"表情包库: 共{total}张, 已注册{registered}张\n"
        f"分类:\n{cat_block}\n"
        f"示例:\n" + "\n".join(sample_lines)
    )
    return ToolCallResult(
        ok=True,
        data={"total": total, "registered": registered, "categories": cat_stats},
        display=display,
    )


async def _handle_browse_categories(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """Browse sticker categories and their counts."""
    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    category = str(args.get("category", "")).strip()
    if category:
        emojis = sticker_mgr.find_emoji_by_category(category)
        if not emojis:
            return ToolCallResult(ok=True, data={}, display=f"分类'{category}'没有表情包")
        lines = [f"  {e.description} [{','.join(e.tags[:3])}]" for e in emojis[:10]]
        return ToolCallResult(
            ok=True,
            data={"category": category, "count": len(emojis)},
            display=f"分类'{category}' ({len(emojis)}张):\n" + "\n".join(lines),
        )

    cat_stats = sticker_mgr.category_stats()
    if not cat_stats:
        return ToolCallResult(ok=True, data={}, display="暂无分类数据，表情包正在自动注册中")
    lines = [f"  {cat}: {cnt}张" for cat, cnt in sorted(cat_stats.items(), key=lambda x: -x[1])]
    return ToolCallResult(
        ok=True,
        data={"categories": cat_stats},
        display="表情包分类:\n" + "\n".join(lines),
    )


def _parse_sticker_terms(value: Any, max_items: int = 16) -> list[str]:
    if value is None:
        return []
    raw_items: list[str] = []
    if isinstance(value, list):
        raw_items = [normalize_text(str(item)) for item in value]
    else:
        text = normalize_text(str(value))
        if text:
            raw_items = [
                normalize_text(part)
                for part in re.split(r"[,\n;，；、|/\s]+", text)
            ]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        token = normalize_text(item).lstrip("#")
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= max(1, int(max_items)):
            break
    return out


async def _handle_correct_sticker(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """纠正自动学习错误的表情包元数据。"""
    sticker_mgr = context.get("sticker_manager")
    if not sticker_mgr:
        return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

    key = normalize_text(str(args.get("key", "") or args.get("target", "")))
    source_user = normalize_text(str(args.get("source_user", "") or context.get("user_id", "")))
    description = normalize_text(str(args.get("description", "")))
    category = normalize_text(str(args.get("category", "")))

    has_tags_input = "tags" in args
    has_emotions_input = "emotions" in args
    tags = _parse_sticker_terms(args.get("tags"), max_items=16) if has_tags_input else None
    emotions = _parse_sticker_terms(args.get("emotions"), max_items=12) if has_emotions_input else None

    ok, message, payload = sticker_mgr.update_emoji_metadata(
        key=key,
        source_user=source_user,
        description=description,
        category=category,
        tags=tags,
        emotions=emotions,
    )
    if not ok:
        return ToolCallResult(ok=False, data=payload or {}, display=message)

    key_show = normalize_text(str(payload.get("key", "")))
    desc_show = normalize_text(str(payload.get("description", "")))
    cat_show = normalize_text(str(payload.get("category", "")))
    tag_show = ",".join(payload.get("tags", [])[:6]) if isinstance(payload.get("tags"), list) else ""
    em_show = ",".join(payload.get("emotions", [])[:6]) if isinstance(payload.get("emotions"), list) else ""

    lines = [message]
    if key_show:
        lines.append(f"key: {key_show}")
    if desc_show:
        lines.append(f"描述: {desc_show}")
    if cat_show:
        lines.append(f"分类: {cat_show}")
    if tag_show:
        lines.append(f"标签: {tag_show}")
    if em_show:
        lines.append(f"情绪: {em_show}")

    return ToolCallResult(ok=True, data=payload, display="\n".join(lines))


def _make_learn_sticker_handler(model_client: Any) -> ToolHandler:
    """创建 learn_sticker 工具的 handler (闭包持有 model_client)。"""

    async def _handle_learn_sticker(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """用户发图片+要求学习 → 下载+LLM审核+存入表情包库。"""
        sticker_mgr = context.get("sticker_manager")
        if not sticker_mgr:
            return ToolCallResult(ok=False, data={}, display="表情系统未初始化")

        # 从参数或 raw_segments / reply_media_segments 提取图片 URL / file 标识
        image_url = str(args.get("image_url", "")).strip()
        image_file = str(args.get("image_file", "")).strip()
        image_sub_type = str(args.get("image_sub_type", "")).strip()
        native_segment_type = normalize_text(str(args.get("native_segment_type", ""))).lower()
        native_segment_data = args.get("native_segment_data", {})
        if not isinstance(native_segment_data, dict):
            native_segment_data = {}

        # 优先从当前消息提取，然后从引用消息提取
        raw_segments = context.get("raw_segments") or []
        reply_media_segments = context.get("reply_media_segments") or []
        reply_message_segments = await _load_reply_message_segments_for_sticker(context)

        if not native_segment_type:
            for segs in (raw_segments, reply_message_segments, reply_media_segments):
                native_segment_type, native_segment_data = _extract_first_native_sticker_segment(segs)
                if native_segment_type:
                    break

        if not image_url or not image_file or not image_sub_type:
            for segs in (raw_segments, reply_message_segments, reply_media_segments):
                resolved_url, resolved_file, resolved_sub_type = _extract_first_sticker_media_payload(segs)
                if not image_url and resolved_url:
                    image_url = resolved_url
                if not image_file and resolved_file:
                    image_file = resolved_file
                if not image_sub_type and resolved_sub_type:
                    image_sub_type = resolved_sub_type
                if image_url or image_file:
                    break

        if not image_url and not image_file:
            return ToolCallResult(ok=False, data={}, display="没有找到图片，请用户发送图片并说'学习表情包'")

        user_id = str(context.get("user_id", ""))

        async def _llm_call(messages: list) -> str:
            return await model_client.chat_text(messages=messages, max_tokens=300)

        ok, msg = await sticker_mgr.learn_from_chat(
            image_url=image_url,
            image_file=image_file,
            image_sub_type=image_sub_type,
            user_id=user_id,
            llm_call=_llm_call,
            api_call=context.get("api_call"),
            native_segment_type=native_segment_type,
            native_segment_data=native_segment_data,
        )
        return ToolCallResult(ok=ok, data={"learned": ok}, display=msg)

    return _handle_learn_sticker


def register_sticker_tools(registry: "AgentToolRegistry", model_client: Any = None) -> None:
    """Register sticker/face tools into the agent tool registry."""
    registry.register_prompt_hint(
        PromptHint(
            source="sticker",
            section="tools_guidance",
            content="当用户说“学错了/描述不对/帮我改这个表情包”时，优先用 correct_sticker 修正元数据并写回知识库。",
            priority=35,
            tool_names=("correct_sticker",),
        )
    )
    registry.register_prompt_hint(
        PromptHint(
            source="sticker",
            section="tools_guidance",
            content=(
                "对某一条具体消息做表情回应时，优先使用 set_msg_emoji_like。"
                "send_emoji/send_sticker 只用于独立发送表情，发送顺序是原生 mface -> 原生/语义匹配 face -> image 回退。"
            ),
            priority=34,
            tool_names=("set_msg_emoji_like", "send_emoji", "send_sticker", "send_face"),
        )
    )

    registry.register(
        ToolSchema(
            name="send_face",
            description=(
                "独立发送QQ经典表情。使用场景: 当你想表达情绪时，发送一个QQ原生表情。"
                "参数query填写情绪关键词如'开心','哭','doge','吃瓜','赞'等。"
                "如果是要对某条具体消息点表情回应，不要用本工具，改用 set_msg_emoji_like。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "情绪或表情描述"},
                },
                "required": ["query"],
            },
            category="napcat",
        ),
        _handle_send_face,
    )

    registry.register(
        ToolSchema(
            name="send_emoji",
            description=(
                "独立发送表情包。库里有大量表情包可用，会优先发送原生 mface，其次 face，最后才回退 image。"
                "query填写情绪关键词匹配，或填'随机'/'random'随机发一张。"
                "支持按分类搜索(如'搞笑','可爱')，按标签搜索(如'#猫','#无语')。"
                "如果不确定有什么表情包，直接填'随机'即可。"
                "如果是要对某条具体消息点表情回应，不要用本工具，改用 set_msg_emoji_like。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "情绪/描述关键词，或'随机'随机发送",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "兼容字段：等价于 query",
                    },
                    "name": {
                        "type": "string",
                        "description": "兼容字段：等价于 query",
                    },
                },
                "required": [],
            },
            category="napcat",
        ),
        _handle_send_emoji,
    )

    # send_sticker — send_emoji 的兼容别名，避免旧提示词调用失败
    registry.register(
        ToolSchema(
            name="send_sticker",
            description=(
                "兼容别名：等价于 send_emoji。"
                "用于独立发送表情包，内部优先 mface，其次 face，最后 image。"
                "若要对某条具体消息点表情回应，改用 set_msg_emoji_like。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "情绪/描述关键词，或'随机'随机发送",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "兼容字段：等价于 query",
                    },
                    "name": {
                        "type": "string",
                        "description": "兼容字段：等价于 query",
                    },
                },
                "required": [],
            },
            category="napcat",
        ),
        _handle_send_emoji,
    )

    registry.register(
        ToolSchema(
            name="list_faces",
            description=(
                "查询可用的QQ经典表情列表。使用场景: 当你不确定有哪些表情可用时，"
                "先查询再决定发哪个。参数query可选，留空返回常用表情。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词(可选)"},
                },
                "required": [],
            },
            category="napcat",
        ),
        _handle_list_faces,
    )

    registry.register(
        ToolSchema(
            name="list_emojis",
            description=(
                "查看表情包库状态、分类统计和示例。了解有多少表情包可用。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            category="napcat",
        ),
        _handle_list_emojis,
    )

    registry.register(
        ToolSchema(
            name="browse_sticker_categories",
            description=(
                "浏览表情包分类。不传category返回所有分类及数量，"
                "传category返回该分类下的表情包列表。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "分类名(可选): 搞笑/可爱/嘲讽/日常/动漫/反应/文字/其他",
                    },
                },
                "required": [],
            },
            category="napcat",
        ),
        _handle_browse_categories,
    )

    # learn_sticker — 需要 model_client
    if model_client is not None:
        registry.register(
            ToolSchema(
                name="learn_sticker",
                description=(
                    "学习/添加用户发送的表情包到表情包库。"
                    "当用户发送图片并说'学习表情包'、'添加表情包'、'收录这张'等时使用。"
                    "会自动下载图片、用AI审核内容合法性、生成描述和分类，然后存入表情包库。"
                    "图片URL会自动从用户消息中提取，通常不需要手动填写image_url。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "image_url": {
                            "type": "string",
                            "description": "图片URL(可选，通常自动从消息中提取)",
                        },
                    },
                    "required": [],
                },
                category="napcat",
            ),
            _make_learn_sticker_handler(model_client),
        )

    registry.register(
        ToolSchema(
            name="correct_sticker",
            description=(
                "纠正表情包学习结果（用户指明AI识别偏差时使用）。"
                "默认修正最近学习的一张；也可传 key 指定目标。"
                "修正后会写回知识库并标记为手动覆盖。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "目标表情 key（可选，不填默认最近学习）",
                    },
                    "description": {
                        "type": "string",
                        "description": "纠正后的描述（可选）",
                    },
                    "category": {
                        "type": "string",
                        "description": "纠正后的分类（可选）：搞笑/可爱/嘲讽/日常/动漫/反应/文字/其他",
                    },
                    "tags": {
                        "type": "string",
                        "description": "标签，逗号或空格分隔（可选）",
                    },
                    "emotions": {
                        "type": "string",
                        "description": "情绪词，逗号或空格分隔（可选）",
                    },
                    "source_user": {
                        "type": "string",
                        "description": "按指定用户的最近学习记录定位目标（可选）",
                    },
                },
                "required": [],
            },
            category="napcat",
        ),
        _handle_correct_sticker,
    )

    # scan_stickers — 重新扫描表情包目录
    async def _handle_scan_stickers(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        sticker_mgr = context.get("sticker_manager")
        if not sticker_mgr:
            return ToolCallResult(ok=False, data={}, display="表情系统未初始化")
        result = sticker_mgr.scan()
        return ToolCallResult(
            ok=True, data=result,
            display=(
                f"扫描完成: {result.get('faces', 0)} 经典表情, "
                f"{result.get('emojis', 0)} 本地表情包, "
                f"{result.get('registry', 0)} 手动注册覆盖"
            ),
        )

    registry.register(
        ToolSchema(
            name="scan_stickers",
            description=(
                "重新扫描表情包目录，刷新表情包库。"
                "当用户说'扫描表情包'或你需要刷新表情包列表时使用。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="napcat",
        ),
        _handle_scan_stickers,
    )


# ── Memory tools ──
