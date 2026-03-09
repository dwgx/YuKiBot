"""注册新增的 Agent 工具 — 好感度、卡片消息、增强图片生成等。

在 engine.py 初始化时调用 register_enhanced_tools(registry, engine) 即可。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.affinity import AffinityEngine
from core.agent_tools import AgentToolRegistry, PromptHint, ToolCallResult, ToolSchema
from core.card_builder import CardBuilder
from core.image_gen import ImageGenEngine

_log = logging.getLogger("yukiko.enhanced_tools")


def register_enhanced_tools(
    registry: AgentToolRegistry,
    affinity: AffinityEngine,
    image_gen: ImageGenEngine,
    config: dict[str, Any] | None = None,
) -> None:
    """注册所有增强工具到 Agent 工具注册表。"""
    _register_affinity_tools(registry, affinity)
    _register_card_tools(registry)
    _register_image_gen_tools(registry, image_gen)
    _register_enhanced_napcat_tools(registry)

    # 注入好感度/心情上下文到 Agent prompt
    registry.register_context_provider(
        "affinity_context",
        lambda info: _build_affinity_context(affinity, info),
        priority=30,
    )

    # 注入提示词
    registry.register_prompt_hint(PromptHint(
        source="enhanced_tools",
        section="tools_guidance",
        content=(
            "好感度系统: 每次互动自动积累好感度。用户说'打卡'时调用 checkin 工具。"
            "用户问好感度/等级时调用 get_affinity。排行榜用 affinity_leaderboard。\n"
            "卡片消息: 需要精美展示时用 send_json_card 发送卡片。"
            "音乐分享用 send_music_card（支持自定义封面和音频链接）。\n"
            "图片生成: 用 generate_image_enhanced 生成图片，自动 NSFW 过滤。"
        ),
        priority=20,
    ))


def _build_affinity_context(affinity: AffinityEngine, info: dict[str, Any]) -> str:
    """构建好感度上下文注入到 Agent prompt。"""
    parts = [affinity.mood_prompt_hint()]
    user_id = info.get("user_id", "")
    if user_id:
        parts.append(affinity.affinity_prompt_hint(user_id))
    return " | ".join(parts)


# ── 好感度工具 ──

def _register_affinity_tools(registry: AgentToolRegistry, affinity: AffinityEngine) -> None:

    async def _handle_checkin(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        user_id = str(args.get("user_id", context.get("user_id", "")))
        if not user_id:
            return ToolCallResult(ok=False, error="缺少 user_id")
        user, msg = affinity.checkin(user_id)
        return ToolCallResult(ok=True, data={"message": msg, "affinity": user.affinity, "level": user.level}, display=msg)

    async def _handle_get_affinity(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        user_id = str(args.get("user_id", context.get("user_id", "")))
        if not user_id:
            return ToolCallResult(ok=False, error="缺少 user_id")
        user = affinity.get_user(user_id)
        return ToolCallResult(ok=True, data={
            "affinity": user.affinity, "level": user.level,
            "level_name": user.level_name, "streak": user.daily_checkin_streak,
            "total_interactions": user.total_interactions,
        }, display=f"好感度: {user.affinity:.1f}, Lv.{user.level} {user.level_name}")

    async def _handle_leaderboard(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        top_n = int(args.get("top_n", 10))
        users = affinity.get_leaderboard(top_n)
        items = [{"user_id": u.user_id, "nickname": u.nickname, "affinity": u.affinity, "level": u.level} for u in users]
        return ToolCallResult(ok=True, data={"leaderboard": items}, display=f"好感度排行榜 Top {len(items)}")

    async def _handle_update_mood(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        mood = str(args.get("mood", "neutral"))
        reason = str(args.get("reason", ""))
        intensity = args.get("intensity")
        result = affinity.update_mood(mood, reason, float(intensity) if intensity is not None else None)
        return ToolCallResult(ok=True, data={"mood": result.current, "intensity": result.intensity}, display=result.to_prompt_hint())

    registry.register(ToolSchema(
        name="checkin",
        description="每日打卡，增加好感度。连续打卡有额外奖励。\n使用场景: 用户说'打卡'、'签到'时调用",
        parameters={"type": "object", "properties": {"user_id": {"type": "string", "description": "用户QQ号(可选，默认当前用户)"}}, "required": []},
        category="general", group="social",
    ), _handle_checkin)

    registry.register(ToolSchema(
        name="get_affinity",
        description="查询用户好感度、等级、打卡天数等信息。\n使用场景: 用户问'我的好感度'、'我什么等级'时调用",
        parameters={"type": "object", "properties": {"user_id": {"type": "string", "description": "用户QQ号(可选，默认当前用户)"}}, "required": []},
        category="general", group="social",
    ), _handle_get_affinity)

    registry.register(ToolSchema(
        name="affinity_leaderboard",
        description="查看好感度排行榜。\n使用场景: 用户问'排行榜'、'谁好感度最高'时调用",
        parameters={"type": "object", "properties": {"top_n": {"type": "integer", "description": "显示前N名，默认10"}}, "required": []},
        category="general", group="social",
    ), _handle_leaderboard)

    registry.register(ToolSchema(
        name="update_bot_mood",
        description="更新bot心情状态。可选: happy/neutral/tired/annoyed/excited/melancholy\n使用场景: bot根据对话氛围自主调整心情",
        parameters={"type": "object", "properties": {
            "mood": {"type": "string", "description": "心情: happy/neutral/tired/annoyed/excited/melancholy"},
            "reason": {"type": "string", "description": "原因"},
            "intensity": {"type": "number", "description": "强度 0-1"},
        }, "required": ["mood"]},
        category="general", group="social",
    ), _handle_update_mood)


# ── 卡片消息工具 ──

def _register_card_tools(registry: AgentToolRegistry) -> None:

    async def _handle_send_json_card(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        title = str(args.get("title", ""))
        desc = str(args.get("desc", ""))
        url = str(args.get("url", ""))
        image = str(args.get("image", ""))
        if not title:
            return ToolCallResult(ok=False, error="缺少 title")
        card = CardBuilder.json_card(title=title, desc=desc, url=url, image=image)
        return ToolCallResult(ok=True, data={"segments": card.segments, "preview": card.preview_text},
                              display=f"已构建卡片: {title}")

    async def _handle_send_music_card(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        title = str(args.get("title", ""))
        singer = str(args.get("singer", ""))
        audio_url = str(args.get("audio_url", ""))
        jump_url = str(args.get("jump_url", ""))
        image_url = str(args.get("image_url", ""))
        platform = str(args.get("platform", ""))
        song_id = args.get("song_id", "")

        if platform and song_id:
            card = CardBuilder.platform_music_card(platform, song_id)
        elif title and audio_url:
            card = CardBuilder.custom_music_card(title=title, singer=singer, audio_url=audio_url, jump_url=jump_url, image_url=image_url)
        else:
            return ToolCallResult(ok=False, error="需要 (platform+song_id) 或 (title+audio_url)")
        return ToolCallResult(ok=True, data={"segments": card.segments, "preview": card.preview_text},
                              display=card.preview_text)

    async def _handle_send_forward(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        nodes = args.get("nodes", [])
        if not nodes or not isinstance(nodes, list):
            return ToolCallResult(ok=False, error="缺少 nodes 列表")
        card = CardBuilder.forward_message(nodes)
        return ToolCallResult(ok=True, data={"segments": card.segments}, display="已构建合并转发消息")

    registry.register(ToolSchema(
        name="send_json_card",
        description="发送JSON卡片消息（精美展示链接/信息）。\n使用场景: 需要精美展示搜索结果、信息卡片时使用",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "description": "卡片标题"},
            "desc": {"type": "string", "description": "卡片描述"},
            "url": {"type": "string", "description": "点击跳转链接(可选)"},
            "image": {"type": "string", "description": "卡片图片URL(可选)"},
        }, "required": ["title"]},
        category="napcat", group="messaging",
    ), _handle_send_json_card)

    registry.register(ToolSchema(
        name="send_music_card",
        description="发送音乐分享卡片。支持QQ音乐/网易云/酷狗/自定义。\n使用场景: 点歌成功后发送音乐卡片",
        parameters={"type": "object", "properties": {
            "platform": {"type": "string", "description": "平台: qq/163/kugou/migu/kuwo(可选)"},
            "song_id": {"type": "string", "description": "歌曲ID(配合platform使用)"},
            "title": {"type": "string", "description": "歌曲标题(自定义卡片)"},
            "singer": {"type": "string", "description": "歌手名(自定义卡片)"},
            "audio_url": {"type": "string", "description": "音频播放链接(自定义卡片)"},
            "jump_url": {"type": "string", "description": "点击跳转链接(可选)"},
            "image_url": {"type": "string", "description": "封面图片URL(可选)"},
        }, "required": []},
        category="napcat", group="messaging",
    ), _handle_send_music_card)

    registry.register(ToolSchema(
        name="send_forward_message",
        description="发送合并转发消息（多条消息合并为一条）。\n使用场景: 需要发送长内容、多段信息时使用",
        parameters={"type": "object", "properties": {
            "nodes": {"type": "array", "description": "消息节点列表，每个节点: {nickname, user_id, content}",
                      "items": {"type": "object"}},
        }, "required": ["nodes"]},
        category="napcat", group="messaging",
    ), _handle_send_forward)


# ── 增强图片生成工具 ──

def _register_image_gen_tools(registry: AgentToolRegistry, image_gen: ImageGenEngine) -> None:

    async def _handle_generate(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        prompt = str(args.get("prompt", ""))
        model = args.get("model")
        size = args.get("size")
        style = args.get("style")
        result = await image_gen.generate(prompt, model=model, size=size, style=style)
        if result.ok:
            return ToolCallResult(ok=True, data={
                "url": result.url, "image_url": result.url, "model": result.model_used,
                "revised_prompt": result.revised_prompt,
            }, display=f"图片已生成 (模型: {result.model_used})")
        return ToolCallResult(ok=False, error=result.message)

    async def _handle_list_models(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        models = image_gen.list_models()
        return ToolCallResult(ok=True, data={"models": models}, display=f"可用模型: {len(models)} 个")

    registry.register(ToolSchema(
        name="generate_image_enhanced",
        description="生成图片（增强版，支持多模型、NSFW过滤）。\n使用场景: 用户要求画图、生成图片时使用。自动过滤不当内容。",
        parameters={"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图片描述（英文效果更好）"},
            "model": {"type": "string", "description": "模型名(可选，默认使用 image_gen.default_model)"},
            "size": {"type": "string", "description": "尺寸(可选，如1024x1024)"},
            "style": {"type": "string", "description": "风格(可选，如vivid/natural)"},
        }, "required": ["prompt"]},
        category="media", group="media",
    ), _handle_generate)

    registry.register(ToolSchema(
        name="list_image_models",
        description="列出所有可用的图片生成模型。\n使用场景: 用户问'有哪些画图模型'时使用",
        parameters={"type": "object", "properties": {}, "required": []},
        category="media", group="media",
    ), _handle_list_models)


# ── 增强 NapCat 工具 ──

def _register_enhanced_napcat_tools(registry: AgentToolRegistry) -> None:
    """注册利用 NapCat 高级 API 的工具。"""

    async def _handle_send_ai_voice(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """发送 AI 语音合成消息。"""
        api_call = context.get("api_call")
        if not api_call:
            return ToolCallResult(ok=False, error="no_api_call_available")
        group_id = int(args.get("group_id", context.get("group_id", 0)))
        character = str(args.get("character", ""))
        text = str(args.get("text", ""))
        if not group_id or not text:
            return ToolCallResult(ok=False, error="缺少 group_id 或 text")
        try:
            # 如果没指定角色，先获取可用角色列表
            if not character:
                chars = await api_call("get_ai_characters", group_id=group_id)
                if isinstance(chars, list) and chars:
                    character = chars[0].get("character_id", chars[0].get("id", ""))
            if not character:
                return ToolCallResult(ok=False, error="无可用 AI 语音角色")
            result = await api_call("send_group_ai_record", group_id=group_id, character=character, text=text)
            return ToolCallResult(ok=True, data=result if isinstance(result, dict) else {}, display=f"已发送AI语音: {text[:30]}...")
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))

    async def _handle_set_input_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """设置输入状态（正在输入...）。"""
        api_call = context.get("api_call")
        if not api_call:
            return ToolCallResult(ok=False, error="no_api_call_available")
        event_type = int(args.get("event_type", 1))  # 1=正在输入
        user_id = int(args.get("user_id", context.get("user_id", 0)))
        try:
            await api_call("set_input_status", event_type=event_type, user_id=user_id)
            return ToolCallResult(ok=True, display="已设置输入状态")
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))

    async def _handle_ocr_image(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """OCR 识别图片中的文字。"""
        api_call = context.get("api_call")
        if not api_call:
            return ToolCallResult(ok=False, error="no_api_call_available")
        image = str(args.get("image", ""))
        if not image:
            return ToolCallResult(ok=False, error="缺少 image 参数")
        try:
            result = await api_call("ocr_image", image=image)
            if isinstance(result, dict):
                texts = result.get("texts", [])
                text_content = "\n".join(t.get("text", "") for t in texts if isinstance(t, dict))
                return ToolCallResult(ok=True, data={"text": text_content, "raw": result}, display=f"OCR结果: {text_content[:100]}...")
            return ToolCallResult(ok=True, data=result if isinstance(result, dict) else {}, display="OCR完成")
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))

    registry.register(ToolSchema(
        name="send_ai_voice",
        description="发送AI语音合成消息到群聊。\n使用场景: 用户要求bot用语音说话、朗读文字时使用",
        parameters={"type": "object", "properties": {
            "group_id": {"type": "integer", "description": "群号(可选，默认当前群)"},
            "character": {"type": "string", "description": "AI语音角色ID(可选，自动选择)"},
            "text": {"type": "string", "description": "要转为语音的文本"},
        }, "required": ["text"]},
        category="napcat", group="messaging",
    ), _handle_send_ai_voice)

    registry.register(ToolSchema(
        name="set_input_status",
        description="设置'正在输入'状态提示。\n使用场景: 处理耗时任务前显示输入状态",
        parameters={"type": "object", "properties": {
            "event_type": {"type": "integer", "description": "1=正在输入(默认)"},
            "user_id": {"type": "integer", "description": "目标用户(可选)"},
        }, "required": []},
        category="napcat", group="messaging",
    ), _handle_set_input_status)

    registry.register(ToolSchema(
        name="ocr_image",
        description="OCR识别图片中的文字。\n使用场景: 用户发图片问'上面写了什么'、需要提取图片文字时使用",
        parameters={"type": "object", "properties": {
            "image": {"type": "string", "description": "图片URL或file标识"},
        }, "required": ["image"]},
        category="napcat", group="media",
    ), _handle_ocr_image)
