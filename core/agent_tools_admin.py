"""Auto-split from core/agent_tools.py — 管理员工具"""
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

def _register_admin_tools(registry: AgentToolRegistry) -> None:
    """注册管理指令工具，让 Agent 可以执行 /yuki 系列命令。"""

    registry.register(
        ToolSchema(
            name="admin_command",
            description=(
                "执行YuKiKo管理指令。支持的命令:\n"
                "- reload: 热重载配置\n"
                "- ping: 检测存活\n"
                "- status: 查看运行状态\n"
                "- high_risk_confirm [on|off|default] [group|global]: 调整高风险确认策略\n"
                "- ignore_user <QQ> [group|global]: 忽略某个用户\n"
                "- unignore_user <QQ> [group|global]: 恢复某个用户\n"
                "- white_add: 加白本群\n"
                "- white_rm: 拉黑本群\n"
                "- white_list: 查看白名单\n"
                "- scale <0-3>: 设置安全尺度\n"
                "- sensitive [添加|删除] <词>: 管理敏感词\n"
                "- poke <QQ>: 戳一戳\n"
                "- dice: 骰子\n"
                "- rps: 猜拳\n"
                "- music_card <歌名>: 音乐卡片（仅发送QQ音乐卡片，不是语音；如需语音播放请用 music_play 工具）\n"
                "- json <JSON>: 发送JSON卡片\n"
                "- 定海神针 [行数] [段数] [延迟秒]: 刷屏定海神针\n"
                "- behavior [冷漠|安静|活跃|默认]: 切换行为模式\n"
                "当用户想执行管理操作但命令不准确时，推断正确命令并调用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "命令名（如 reload, ping, scale, poke 等）"},
                    "arg": {"type": "string", "description": "命令参数（可选）"},
                },
                "required": ["command"],
            },
            category="admin",
        ),
        _handle_admin_command,
    )

    registry.register(
        ToolSchema(
            name="config_update",
            description=(
                "仅超级管理员可用：修改机器人配置并立即生效。\n"
                "参数 patch 是对 config.yml 的最小补丁对象，示例:\n"
                "{\"patch\":{\"bot\":{\"allow_non_to_me\":false}}}\n"
                "或 {\"patch\":{\"output\":{\"verbosity\":\"short\"}}}\n"
                "规则：\n"
                "- 只改用户明确要求的字段\n"
                "- 不要传整份配置\n"
                "- 改完后再用 final_answer 告知变更结果"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {"type": "object", "description": "config.yml 的增量补丁对象"},
                    "reason": {"type": "string", "description": "变更原因摘要（可选）"},
                    "dry_run": {"type": "boolean", "description": "是否仅预检不写入（可选）"},
                },
                "required": ["patch"],
            },
            category="admin",
        ),
        _handle_config_update,
    )

    # 音乐搜索工具（返回搜索结果列表）
    registry.register(
        ToolSchema(
            name="music_search",
            description=(
                "搜索歌曲，返回搜索结果列表供选择。\n"
                "使用场景: 用户说'点歌 XXX'、'放歌 XXX'、'来首 XXX' 时先调用此工具搜索。\n"
                "返回结果包含歌曲 ID、歌名、歌手、专辑等信息。\n"
                "重要：不要依赖本地固定词表或主观印象猜歌，必须先做结果自检：\n"
                "- 先拆出 title / artist，再验证搜索结果是否真的命中标题与歌手\n"
                "- 除非用户明确要求，否则不要擅自改成翻唱版、DJ 版、伴奏版、Live、Remix 或片段\n"
                "- 搜索结果里只要存在标题/歌手不一致，就不能当成同一首歌直接播\n"
                "- 多个候选都像时，优先保留给后续 music_play_by_id 精确播放，不要拍脑袋猜\n"
                "选择后使用 music_play_by_id 工具播放。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "歌曲检索词，直接使用用户提供的关键词，不要自行修改或添加额外限定词"},
                    "title": {"type": "string", "description": "歌曲名（可选，建议与 artist 分开传）"},
                    "artist": {"type": "string", "description": "歌手名（可选）"},
                },
                "required": [],
            },
            category="utility",
        ),
        _handle_music_search,
    )

    # 音乐播放工具（按关键词自动选可播版本）
    registry.register(
        ToolSchema(
            name="music_play",
            description=(
                "按关键词直接点歌并播放（优先 Alger API，自动下载可播音频并发送语音）。\n"
                "使用场景: 用户说“点歌 XXX”“来首 XXX”“放歌 XXX”时优先使用本工具。\n"
                "注意：如果用户明确指定歌手或版本，请优先分开传 title / artist，再把补充限定词放进 keyword。\n"
                "内部音源顺序应理解为：Alger/官方优先，其次站内正规替代音源，再其次 SoundCloud，最后才是 B 站。\n"
                "如果标题或歌手对不上，不要为了“能播”就换歌。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "歌曲关键词（建议包含歌手+歌名）"},
                    "title": {"type": "string", "description": "歌曲名（可选）"},
                    "artist": {"type": "string", "description": "歌手名（可选）"},
                },
                "required": [],
            },
            category="utility",
        ),
        _handle_music_play,
    )

    # 音乐播放工具（根据 ID 播放）
    registry.register(
        ToolSchema(
            name="music_play_by_id",
            description=(
                "根据歌曲 ID 播放歌曲（发送 SILK 语音消息）。\n"
                "使用场景: 在 music_search 返回结果后，选择合适的歌曲 ID 调用此工具播放。\n"
                "只有在你已经确认标题、歌手、版本都匹配用户要求时才调用；不要靠本地词表主观猜测。\n"
                "如果 music_search 结果里带有 source / source_url，调用本工具时也要原样带上，避免把跨平台结果误当成网易云 ID。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "song_id": {"type": "integer", "description": "歌曲 ID（从 music_search 结果中获取）"},
                    "song_name": {"type": "string", "description": "歌曲名称（用于显示）"},
                    "artist": {"type": "string", "description": "歌手名称（用于显示）"},
                    "source": {"type": "string", "description": "可选，music_search 返回的音源类型，例如 soundcloud"},
                    "source_url": {"type": "string", "description": "可选，music_search 返回的原始页面地址，跨平台音源时必须原样透传"},
                },
                "required": ["song_id"],
            },
            category="utility",
        ),
        _handle_music_play_by_id,
    )

    # Bilibili 音频提取工具（音乐回退方案）
    registry.register(
        ToolSchema(
            name="bilibili_audio_extract",
            description=(
                "从 Bilibili 视频中提取音频作为音乐播放的回退方案。\n"
                "使用场景: 仅在 music_play / music_play_by_id 明确失败后，再尝试从 B 站搜索并提取音频。\n"
                "适用于用户点歌但网易云音乐版权受限的情况。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词（歌曲名+歌手）"},
                },
                "required": ["keyword"],
            },
            category="utility",
        ),
        _handle_bilibili_audio_extract,
    )


async def _handle_admin_command(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """执行管理指令。实际调用由 engine 的 admin 模块完成。"""
    command = str(args.get("command", "")).strip()
    arg = str(args.get("arg", "")).strip()
    if not command:
        return ToolCallResult(ok=False, error="missing command")

    # 构造 /yuki 命令文本，交给 admin 系统处理
    cmd_text = f"/yuki {command}" + (f" {arg}" if arg else "")
    # 通过 context 传递的 admin_handler 执行
    admin_handler = context.get("admin_handler")
    if admin_handler:
        try:
            result = await admin_handler(
                text=cmd_text,
                user_id=str(context.get("user_id", "")),
                group_id=int(context.get("group_id", 0)),
            )
            if result is None:
                return ToolCallResult(ok=True, display=f"命令 {command} 执行成功（无返回）")
            return ToolCallResult(ok=True, data={"reply": result}, display=str(result))
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"admin_command_error: {exc}")

    # 没有 admin_handler，通过 api_call 模拟
    return ToolCallResult(
        ok=True,
        data={"command": cmd_text, "needs_dispatch": True},
        display=f"已生成管理命令: {cmd_text}",
    )


async def _handle_config_update(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    patch = args.get("patch", {})
    reason = normalize_text(str(args.get("reason", "")))
    dry_run = bool(args.get("dry_run", False))
    if not isinstance(patch, dict) or not patch:
        return ToolCallResult(ok=False, error="invalid_patch")

    # 轻量安全护栏：限制体积，防止模型误把整份上下文塞进配置。
    try:
        patch_size = len(json.dumps(patch, ensure_ascii=False))
    except Exception:
        return ToolCallResult(ok=False, error="patch_serialize_failed")
    if patch_size > 20000:
        return ToolCallResult(ok=False, error="patch_too_large")

    config_patch_handler = context.get("config_patch_handler")
    if not config_patch_handler:
        return ToolCallResult(ok=False, error="config_patch_handler_unavailable")

    actor_user_id = str(context.get("user_id", "")).strip()
    try:
        result = config_patch_handler(
            patch=patch,
            actor_user_id=actor_user_id,
            reason=reason,
            dry_run=dry_run,
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"config_update_error:{exc}")

    ok = False
    message = ""
    merged_preview: dict[str, Any] = {}
    if isinstance(result, tuple) and len(result) >= 2:
        ok = bool(result[0])
        message = str(result[1] or "")
        if len(result) >= 3 and isinstance(result[2], dict):
            merged_preview = result[2]
    elif isinstance(result, dict):
        ok = bool(result.get("ok", False))
        message = str(result.get("message", ""))
        if isinstance(result.get("config"), dict):
            merged_preview = result.get("config", {})
    else:
        return ToolCallResult(ok=False, error="config_update_handler_invalid_result")

    top_keys = sorted(str(k) for k in patch.keys())
    mode = "预检" if dry_run else "更新"
    if not ok:
        return ToolCallResult(
            ok=False,
            error=f"config_update_failed:{message or 'unknown'}",
            display=f"配置{mode}失败: {message or 'unknown'}",
        )
    return ToolCallResult(
        ok=True,
        data={
            "updated_keys": top_keys,
            "dry_run": dry_run,
            "message": message,
            "config_preview": merged_preview if dry_run else {},
        },
        display=f"配置{mode}成功: {', '.join(top_keys) or '(empty)'}",
    )




async def _handle_music_search(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """搜索歌曲，返回结果列表。"""
    raw_keyword = str(args.get("keyword", "")).strip()
    is_url = bool(re.search(r"https?://", raw_keyword))
    keyword = raw_keyword if is_url else normalize_matching_text(raw_keyword)
    title = normalize_matching_text(str(args.get("title", "")))
    artist = normalize_matching_text(str(args.get("artist", "")))
    if not keyword and title:
        keyword = f"{title} {artist}".strip()
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    tool_executor = context.get("tool_executor")
    if tool_executor is None:
        return ToolCallResult(ok=False, error="tool_executor unavailable")

    try:
        result = await tool_executor.execute(
            action="music_search",
            tool_name="music_search",
            tool_args={"keyword": keyword, "title": title, "artist": artist, "limit": 8},
            message_text=keyword,
            conversation_id=str(context.get("conversation_id", "")),
            user_id=str(context.get("user_id", "")),
            user_name=str(context.get("user_name", "")),
            group_id=int(context.get("group_id", 0) or 0),
            api_call=context.get("api_call"),
        )
        if not result.ok:
            payload = result.payload if isinstance(getattr(result, "payload", None), dict) else {}
            return ToolCallResult(
                ok=False,
                error=result.error or "search_failed",
                display=str(payload.get("text", "")),
            )

        # 返回搜索结果列表
        payload = result.payload if isinstance(getattr(result, "payload", None), dict) else {}
        results = payload.get("results", [])
        if not results:
            return ToolCallResult(
                ok=False,
                error="no_results",
                display=str(payload.get("text", "")) or "没找到相关歌曲",
            )

        # 格式化结果供 Agent 选择
        lines = [f"找到 {len(results)} 首歌曲："]
        for i, r in enumerate(results, 1):
            source = normalize_text(str(r.get("source", ""))).lower()
            source_tag = f" [{source}]" if source and source != "netease" else ""
            lines.append(f"{i}. {r['name']} - {r['artist']} (ID: {r['id']}){source_tag}")

        return ToolCallResult(
            ok=True,
            data={"results": results},
            display="\n".join(lines),
        )
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"music_search_error: {exc}")


async def _handle_music_play_by_id(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """根据歌曲 ID 播放音乐。"""
    song_id = int(args.get("song_id", 0) or 0)
    if song_id <= 0:
        return ToolCallResult(ok=False, error="invalid song_id")

    song_name = normalize_matching_text(str(args.get("song_name", "")))
    artist = normalize_matching_text(str(args.get("artist", "")))
    keyword = normalize_matching_text(str(args.get("keyword", "")))
    source = normalize_matching_text(str(args.get("source", "")))
    source_url = normalize_text(str(args.get("source_url", "")))

    tool_executor = context.get("tool_executor")
    group_id = int(context.get("group_id", 0) or 0)
    api_call = context.get("api_call")

    if tool_executor is None:
        return ToolCallResult(ok=False, error="tool_executor unavailable")

    try:
        result = await tool_executor.execute(
            action="music_play_by_id",
            tool_name="music_play_by_id",
            tool_args={
                "song_id": song_id,
                "song_name": song_name,
                "artist": artist,
                "keyword": keyword,
                "source": source,
                "source_url": source_url,
            },
            message_text="",
            conversation_id=str(context.get("conversation_id", "")),
            user_id=str(context.get("user_id", "")),
            user_name=str(context.get("user_name", "")),
            group_id=group_id,
            api_call=api_call,
        )
        if not result.ok:
            return ToolCallResult(ok=False, error=result.error or "play_failed")

        payload = result.payload if isinstance(getattr(result, "payload", None), dict) else {}
        audio_file = payload.get("audio_file")
        audio_file_silk = payload.get("audio_file_silk")
        record_b64 = payload.get("record_b64")
        if not any((audio_file, audio_file_silk, record_b64)):
            return ToolCallResult(
                ok=False,
                error="voice_prepare_failed",
                display=str(payload.get("text", "")) or (song_name or "语音准备失败"),
            )

        display_name = song_name if song_name else str(payload.get("text", ""))
        return ToolCallResult(
            ok=True,
            data={
                "audio_file": audio_file,
                "audio_file_silk": audio_file_silk,
                "record_b64": record_b64,
            },
            display=display_name,
        )
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"music_play_error: {exc}")


async def _handle_bilibili_audio_extract(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """从 Bilibili 提取音频作为音乐回退方案。"""
    keyword = normalize_text(str(args.get("keyword", "")))
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    tool_executor = context.get("tool_executor")
    group_id = int(context.get("group_id", 0) or 0)
    api_call = context.get("api_call")

    if tool_executor is None:
        return ToolCallResult(ok=False, error="tool_executor unavailable")

    try:
        result = await tool_executor.execute(
            action="bilibili_audio_extract",
            tool_name="bilibili_audio_extract",
            tool_args={"keyword": keyword},
            message_text=keyword,
            conversation_id=str(context.get("conversation_id", "")),
            user_id=str(context.get("user_id", "")),
            user_name=str(context.get("user_name", "")),
            group_id=group_id,
            api_call=api_call,
        )
        if not result.ok:
            return ToolCallResult(ok=False, error=result.error or "extract_failed")

        # 返回音频信息
        return ToolCallResult(
            ok=True,
            data={
                "audio_file": result.payload.get("audio_file"),
                "audio_file_silk": result.payload.get("audio_file_silk"),
                "record_b64": result.payload.get("record_b64"),
                "text": result.payload.get("text", ""),
            },
            display=result.payload.get("text", "已从 B 站提取音频"),
        )
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"bilibili_audio_extract_error: {exc}")


async def _handle_music_play(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """通过 tool_executor 播放音乐，返回音频信息让 app.py 统一发送。

    不在此处直接发送语音，避免与 app.py 发送层重复发送。
    音频文件路径通过 data 字段传递，由 engine → app.py 的 send_response 统一处理。
    """
    raw_keyword = str(args.get("keyword", "")).strip()
    # URL 不能被 normalize（会破坏协议和路径），检测到 URL 时保留原始值
    is_url = bool(re.search(r"https?://", raw_keyword))
    keyword = raw_keyword if is_url else normalize_matching_text(raw_keyword)
    title = normalize_matching_text(str(args.get("title", "")))
    artist = normalize_matching_text(str(args.get("artist", "")))
    if not keyword and title:
        keyword = f"{title} {artist}".strip()
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    tool_executor = context.get("tool_executor")
    group_id = int(context.get("group_id", 0) or 0)
    api_call = context.get("api_call")

    if tool_executor is None:
        return ToolCallResult(ok=False, error="tool_executor unavailable")

    try:
        result = await tool_executor.execute(
            action="music_play",
            tool_name="music_play",
            tool_args={"keyword": keyword, "title": title, "artist": artist},
            message_text=keyword,
            conversation_id=str(context.get("conversation_id", "")),
            user_id=str(context.get("user_id", "")),
            user_name=str(context.get("user_name", "")),
            group_id=group_id,
            api_call=api_call,
            trace_id=str(context.get("trace_id", "")),
        )
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"music_play_error: {exc}")

    if result is None or not result.ok:
        error_msg = getattr(result, "error", "unknown") if result else "no_result"
        text = ""
        if result and hasattr(result, "payload"):
            text = str(result.payload.get("text", ""))
        # 提供更详细的错误信息
        if not text:
            if error_msg == "no_results":
                text = f"没找到 {keyword} 相关的歌曲"
            elif error_msg == "play_failed":
                text = f"{keyword} 暂时无法播放，可能是版权限制"
            else:
                text = f"播放失败: {error_msg}"
        return ToolCallResult(ok=False, error=error_msg, display=text)

    payload = result.payload if result and isinstance(result.payload, dict) else {}
    text = str(payload.get("text", ""))
    audio_file = str(payload.get("audio_file", ""))
    audio_file_silk = str(payload.get("audio_file_silk", ""))
    record_b64 = str(payload.get("record_b64", ""))
    data: dict[str, Any] = {}
    if audio_file:
        data["audio_file"] = audio_file
    if audio_file_silk:
        data["audio_file_silk"] = audio_file_silk
    if record_b64:
        data["record_b64"] = record_b64
    if text:
        data["text"] = text
    if data.get("audio_file"):
        data["media_prepared"] = "audio_file"
    elif data.get("record_b64"):
        data["media_prepared"] = "record_b64"

    if text:
        return ToolCallResult(ok=True, data=data, display=text)
    if audio_file or record_b64:
        return ToolCallResult(ok=True, data=data, display="语音已准备好")
    return ToolCallResult(ok=False, error="voice_prepare_failed", display="语音准备失败")

