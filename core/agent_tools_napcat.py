"""Auto-split from core/agent_tools.py — NapCat OneBot V11 API 工具"""
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

def _register_napcat_tools(registry: AgentToolRegistry) -> None:
    """注册 NapCat OneBot V11 API 工具，让 Agent 可以直接操作 QQ。"""

    # 发送群消息（多模态）
    registry.register(
        ToolSchema(
            name="send_group_message",
            description=(
                "向QQ群发送消息，支持文本、图片、@某人、引用回复的任意组合。\n"
                "使用场景: 用户让你在群里说话、发通知、发图片、回复群消息时使用。\n"
                "示例: 配合 image_url 可在一条消息内同时发文字和图片。\n"
                "注意: image_url 支持 http/https 链接 或 file:// 本地路径"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "目标群号"},
                    "message": {"type": "string", "description": "要发送的文本内容"},
                    "image_url": {"type": "string", "description": "（可选）图片URL或本地file://路径，会和文本合成一条消息发送"},
                    "at_user_id": {"type": "integer", "description": "（可选）要@的用户QQ号，填 0 表示@全体"},
                    "reply_id": {"type": "integer", "description": "（可选）要引用回复的消息ID"},
                },
                "required": ["group_id", "message"],
            },
            category="napcat",
        ),
        _handle_send_group_message,
    )

    # 发送私聊消息（多模态）
    registry.register(
        ToolSchema(
            name="send_private_message",
            description=(
                "向QQ用户发送私聊消息，支持文本和图片组合。\n"
                "使用场景: 用户让你私聊某人、悄悄告诉某人消息时使用。\n"
                "注意: 对方必须是机器人好友才能发送"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "目标用户QQ号"},
                    "message": {"type": "string", "description": "要发送的文本内容"},
                    "image_url": {"type": "string", "description": "（可选）图片URL或本地file://路径"},
                    "reply_id": {"type": "integer", "description": "（可选）要引用回复的消息ID"},
                },
                "required": ["user_id", "message"],
            },
            category="napcat",
        ),
        _handle_send_private_message,
    )

    # 获取群成员列表
    registry.register(
        ToolSchema(
            name="get_group_member_list",
            description=(
                "获取群的全部成员列表。\n"
                "返回每个成员的: QQ号、昵称、群名片、角色(owner/admin/member)。\n"
                "使用场景: 用户问群里有谁、查某人在不在群里、统计群人数时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "目标群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_member_list,
    )

    # 获取群信息
    registry.register(
        ToolSchema(
            name="get_group_info",
            description="获取群的基本信息（群名、成员数、群主等）。\n使用场景: 用户问'这个群有多少人'、'群主是谁'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_info,
    )

    # 获取用户信息
    registry.register(
        ToolSchema(
            name="get_user_info",
            description="获取QQ用户的详细信息（昵称、性别、年龄等）。\n使用场景: 用户问'XX是谁'、'查一下这个QQ号'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                },
                "required": ["user_id"],
            },
            category="napcat",
        ),
        _handle_get_user_info,
    )

    # 获取消息详情
    registry.register(
        ToolSchema(
            name="get_message",
            description="根据消息ID获取消息详情(发送者、内容、时间等)。\n使用场景: 需要查看某条消息的具体内容时使用",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer", "description": "消息ID"},
                },
                "required": ["message_id"],
            },
            category="napcat",
        ),
        _handle_get_message,
    )

    # 撤回消息
    registry.register(
        ToolSchema(
            name="delete_message",
            description="撤回一条消息（需要管理员权限或是自己发的消息）。\n使用场景: 用户说'撤回那条消息'、'删掉刚才的消息'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer", "description": "消息ID"},
                },
                "required": ["message_id"],
            },
            category="napcat",
        ),
        _handle_delete_message,
    )

    registry.register(
        ToolSchema(
            name="recall_recent_messages",
            description=(
                "按用户+时间窗批量撤回群消息（需要管理员权限）。\n"
                "使用场景: 用户说'撤回这个人10分钟内说的话'、'把他刚刚刷的内容都撤回'时使用。\n"
                "优先用于管理员明确要求的批量撤回，不需要本地关键词硬编码判断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "目标群号"},
                    "user_id": {"type": "integer", "description": "要撤回的用户QQ号"},
                    "within_minutes": {"type": "integer", "description": "时间窗，单位分钟，例如 10"},
                    "limit": {"type": "integer", "description": "最多撤回多少条，默认20，最大50"},
                    "max_pages": {"type": "integer", "description": "最多翻多少页历史，默认6，最大12"},
                },
                "required": ["group_id", "user_id", "within_minutes"],
            },
            category="napcat",
        ),
        _handle_recall_recent_messages,
    )

    registry.register_prompt_hint(
        PromptHint(
            source="message_recall",
            section="tools_guidance",
            content=(
                "当用户指出机器人上一条/某条消息说错了，且你能定位到具体消息ID时，优先调用 delete_message 撤回，再给更正版本。"
                "当管理员要求“撤回某人最近N分钟的话”时，优先调用 recall_recent_messages。"
                "能执行撤回就直接执行，不要只口头说“我帮你撤回”。"
            ),
            priority=28,
            tool_names=("delete_message", "recall_recent_messages"),
        )
    )

    # 群禁言
    registry.register(
        ToolSchema(
            name="set_group_ban",
            description="对群成员禁言。\n使用场景: 用户说'禁言XX'、'把XX禁言10分钟'、'解除禁言'时使用。\n普通用户只能对自己执行自助禁言/解除禁言；管理员可对其他成员操作。\nduration=0 为解除禁言，单位是秒(如600=10分钟, 3600=1小时)。若 user_id 留空，必须能从 @、回复或群聊共享上下文唯一解析。",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "目标用户QQ号；可选，留空时仅在目标能唯一解析时允许"},
                    "duration": {"type": "integer", "description": "禁言时长（秒），0=解除禁言"},
                },
                "required": ["duration"],
            },
            category="napcat",
        ),
        _handle_set_group_ban,
    )

    registry.register_prompt_hint(
        PromptHint(
            source="group_ban_context",
            section="tools_guidance",
            content=(
                "当用户说'禁言我XX秒/分钟'、'让我闭嘴'、'把我禁言了'时，使用 set_group_ban 对该用户自己执行禁言。"
                "当用户说'让bot闭嘴'、'你闭嘴'、'别说话了'时，理解为用户希望你少说话，调整回复频率即可，不需要禁言。"
                "当管理员说'禁言XX'并@了某人时，对被@的人执行禁言。"
                "duration 单位是秒：60=1分钟, 600=10分钟, 3600=1小时。"
                "duration=0 表示解除禁言。"
            ),
            priority=25,
            tool_names=("set_group_ban",),
        )
    )

    # 设置群名片
    registry.register(
        ToolSchema(
            name="set_group_card",
            description="设置群成员的群名片(群昵称)。\n使用场景: 用户说'把XX的名片改成YY'、'改群昵称'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "card": {"type": "string", "description": "新群名片，空字符串=删除名片"},
                },
                "required": ["group_id", "user_id", "card"],
            },
            category="napcat",
        ),
        _handle_set_group_card,
    )

    # 群踢人
    registry.register(
        ToolSchema(
            name="set_group_kick",
            description="将成员踢出群（需要管理员权限）。\n使用场景: 用户说'踢了XX'、'把XX踢出去'时使用。\n注意: 这是不可逆操作",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "reject_add_request": {"type": "boolean", "description": "是否拒绝再次加群，默认false"},
                },
                "required": ["group_id", "user_id"],
            },
            category="napcat",
        ),
        _handle_set_group_kick,
    )

    # 设置群头衔
    registry.register(
        ToolSchema(
            name="set_group_special_title",
            description="设置群成员专属头衔（需要群主权限）。\n使用场景: 用户说'给XX加个头衔'、'设置头衔'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "special_title": {"type": "string", "description": "头衔内容，空字符串=删除"},
                },
                "required": ["group_id", "user_id", "special_title"],
            },
            category="napcat",
        ),
        _handle_set_group_special_title,
    )

    # 获取群荣誉信息
    registry.register(
        ToolSchema(
            name="get_group_honor_info",
            description="获取群荣誉信息（龙王、群聊之火、快乐源泉等）。\n使用场景: 用户问'谁是龙王'、'群荣誉'时使用。\ntype可选: talkative(龙王)/performer(群聊之火)/legend(群聊炽焰)/strong_newbie(冒尖小春笋)/emotion(快乐源泉)/all(全部)",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "type": {"type": "string", "description": "荣誉类型: talkative/performer/legend/strong_newbie/emotion/all"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_honor_info,
    )

    # 发送群文件
    registry.register(
        ToolSchema(
            name="upload_group_file",
            description="上传文件到群文件。\n使用场景: 用户说'上传文件到群里'时使用。\nfile 是本地文件绝对路径",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "file": {"type": "string", "description": "本地文件路径"},
                    "name": {"type": "string", "description": "文件名"},
                    "folder": {"type": "string", "description": "可选群文件夹 ID"},
                },
                "required": ["group_id", "file", "name"],
            },
            category="napcat",
        ),
        _handle_upload_group_file,
    )

    # 获取群公告
    registry.register(
        ToolSchema(
            name="get_group_notice",
            description="获取群公告列表。\n使用场景: 用户问'群公告是什么'、'看看公告'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_notice,
    )

    # 发送群公告
    registry.register(
        ToolSchema(
            name="send_group_notice",
            description="发送群公告（需要管理员权限）。\n使用场景: 用户说'发个公告'、'通知全群'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "content": {"type": "string", "description": "公告内容"},
                },
                "required": ["group_id", "content"],
            },
            category="napcat",
        ),
        _handle_send_group_notice,
    )

    # 获取好友列表
    registry.register(
        ToolSchema(
            name="get_friend_list",
            description="获取机器人的好友列表。\n使用场景: 用户问'机器人有哪些好友'时使用",
            parameters={"type": "object", "properties": {}},
            category="napcat",
        ),
        _handle_get_friend_list,
    )

    # 获取群列表
    registry.register(
        ToolSchema(
            name="get_group_list",
            description="获取机器人加入的群列表。\n使用场景: 用户问'机器人在哪些群'时使用",
            parameters={"type": "object", "properties": {}},
            category="napcat",
        ),
        _handle_get_group_list,
    )

    # 点赞
    registry.register(
        ToolSchema(
            name="send_like",
            description="给用户点赞(QQ名片赞)。\n使用场景: 用户说'给XX点赞'、'赞一下'时使用。\n每天最多给同一人点10次",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "times": {"type": "integer", "description": "点赞次数（1-10）"},
                },
                "required": ["user_id"],
            },
            category="napcat",
        ),
        _handle_send_like,
    )

    # 群全员禁言
    registry.register(
        ToolSchema(
            name="set_group_whole_ban",
            description="开启/关闭群全员禁言（需要管理员权限）。\n使用场景: 用户说'全员禁言'、'开启禁言'、'关闭禁言'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "enable": {"type": "boolean", "description": "true=开启全员禁言, false=关闭"},
                },
                "required": ["group_id", "enable"],
            },
            category="napcat",
        ),
        _handle_set_group_whole_ban,
    )

    # 设置群管理员
    registry.register(
        ToolSchema(
            name="set_group_admin",
            description="设置/取消群管理员（需要群主权限）。\n使用场景: 用户说'把XX设为管理员'、'取消XX管理员'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "enable": {"type": "boolean", "description": "true=设为管理员, false=取消管理员"},
                },
                "required": ["group_id", "user_id", "enable"],
            },
            category="napcat",
        ),
        _handle_set_group_admin,
    )

    # 群打卡
    registry.register(
        ToolSchema(
            name="set_group_sign",
            description="群打卡签到。\n使用场景: 用户说'打卡'、'签到'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_set_group_sign,
    )

    # 设置群名
    registry.register(
        ToolSchema(
            name="set_group_name",
            description="修改群名称（需要管理员权限）。\n使用场景: 用户说'改群名'、'把群名改成XX'时使用",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "group_name": {"type": "string", "description": "新群名"},
                },
                "required": ["group_id", "group_name"],
            },
            category="napcat",
        ),
        _handle_set_group_name,
    )

    # 获取登录号信息
    registry.register(
        ToolSchema(
            name="get_login_info",
            description="获取机器人自身的QQ号和昵称。\n使用场景: 需要知道机器人自己的信息时使用",
            parameters={"type": "object", "properties": {}},
            category="napcat",
        ),
        _handle_get_login_info,
    )

    # ── 以下为新增的 NapCat 扩展 API 工具 ──

    # 转发消息
    registry.register(
        ToolSchema(
            name="forward_message",
            description=(
                "转发一条消息到群或私聊。\n"
                "使用场景: 用户说'把这条消息转到XXX群'、'把这条消息转给某人'时使用。\n"
                "target_type: 'group' 或 'private'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer", "description": "要转发的消息ID"},
                    "target_type": {"type": "string", "description": "'group' 或 'private'"},
                    "target_id": {"type": "integer", "description": "目标群号(group)或用户QQ号(private)"},
                },
                "required": ["message_id", "target_type", "target_id"],
            },
            category="napcat",
        ),
        _handle_forward_message,
    )

    # 获取群历史消息
    registry.register(
        ToolSchema(
            name="get_group_history",
            description=(
                "获取群的历史消息记录。\n"
                "使用场景: 用户说'帮我看看群里之前说了什么'、'群聊天记录'时使用。\n"
                "返回最近的消息列表，包含发送者、内容、时间"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "count": {"type": "integer", "description": "获取条数，默认20，最多50"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_history,
    )

    # 获取私聊历史消息
    registry.register(
        ToolSchema(
            name="get_chat_history",
            description=(
                "获取与某用户的私聊历史消息。\n"
                "使用场景: 用户说'我之前跟你说过什么'、'帮我翻翻聊天记录'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方QQ号"},
                    "count": {"type": "integer", "description": "获取条数，默认20，最多50"},
                },
                "required": ["user_id"],
            },
            category="napcat",
        ),
        _handle_get_chat_history,
    )

    # 获取群文件列表
    registry.register(
        ToolSchema(
            name="get_group_files",
            description=(
                "获取群文件列表（根目录）。\n"
                "使用场景: 用户说'群文件里有什么'、'看看群文件'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_group_files,
    )

    # 获取群文件下载链接
    registry.register(
        ToolSchema(
            name="get_group_file_url",
            description=(
                "获取群文件的下载URL。\n"
                "使用场景: 用户说'帮我下载群文件'、'给我这个文件的链接'时使用。\n"
                "需要先通过 get_group_files 获取 file_id 和 busid"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "file_id": {"type": "string", "description": "文件ID"},
                    "busid": {"type": "integer", "description": "文件busid"},
                },
                "required": ["group_id", "file_id", "busid"],
            },
            category="napcat",
        ),
        _handle_get_group_file_url,
    )

    # 获取禁言列表
    registry.register(
        ToolSchema(
            name="get_muted_list",
            description=(
                "获取群里被禁言的成员列表。\n"
                "使用场景: 用户问'谁被禁言了'、'禁言列表'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                },
                "required": ["group_id"],
            },
            category="napcat",
        ),
        _handle_get_muted_list,
    )

    # 查看用户在线状态
    registry.register(
        ToolSchema(
            name="check_user_status",
            description=(
                "查看某用户是否在线及当前状态。\n"
                "使用场景: 用户问'XX在线吗'、'他现在在干嘛'时使用"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                },
                "required": ["user_id"],
            },
            category="napcat",
        ),
        _handle_check_user_status,
    )

    # 统一戳一戳（替代分离的 group_poke / friend_poke）
    registry.register(
        ToolSchema(
            name="send_poke",
            description=(
                "戳一戳某人（群聊或私聊均可）。\n"
                "使用场景: 用户说'戳他一下'、'拍一拍'、'poke某人'时使用。\n"
                "群聊场景需同时提供 group_id 和 user_id，私聊只需 user_id"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "目标用户QQ号"},
                    "group_id": {"type": "integer", "description": "（群聊时必填）群号"},
                },
                "required": ["user_id"],
            },
            category="napcat",
        ),
        _handle_send_poke,
    )


# ── NapCat tool handlers ──


def _build_onebot_message_segments(
    text: str,
    *,
    image_url: str = "",
    at_user_id: int | None = None,
    reply_id: int | None = None,
) -> list[dict[str, Any]]:
    """构建 OneBot V11 消息段数组，支持 text + image + at + reply 的任意组合。

    NapCat 原生支持传入消息段数组：
    [{"type": "text", "data": {"text": "..."}}, {"type": "image", "data": {"file": "..."}}]
    这样可以在 **一条消息** 内同时发送 文字 + 图片 + @某人 + 引用回复。
    """
    segments: list[dict[str, Any]] = []
    # 引用回复（必须在最前面）
    if reply_id is not None and reply_id != 0:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})
    # @某人
    if at_user_id is not None:
        qq_val = "all" if at_user_id == 0 else str(at_user_id)
        segments.append({"type": "at", "data": {"qq": qq_val}})
    # 文本
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    # 图片
    if image_url:
        segments.append({"type": "image", "data": {"file": image_url}})
    return segments


async def _handle_send_group_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    group_id = int(args.get("group_id", 0))
    message = str(args.get("message", ""))
    if not group_id or not message:
        return ToolCallResult(ok=False, error="missing group_id or message")
    if len(message) > 4000:
        message = message[:3997] + "..."
    image_url = str(args.get("image_url", "") or "").strip()
    at_user_id = args.get("at_user_id")
    if at_user_id is not None:
        try:
            at_user_id = int(at_user_id)
        except (ValueError, TypeError):
            at_user_id = None
    reply_id = args.get("reply_id")
    if reply_id is not None:
        try:
            reply_id = int(reply_id)
        except (ValueError, TypeError):
            reply_id = None
    # 有多模态参数时使用消息段数组，否则保持纯文本兼容
    has_multimodal = bool(image_url or at_user_id is not None or reply_id)
    try:
        if has_multimodal:
            segments = _build_onebot_message_segments(
                message, image_url=image_url, at_user_id=at_user_id, reply_id=reply_id,
            )
            await call_napcat_api(api_call, "send_group_msg", group_id=group_id, message=segments)
        else:
            await call_napcat_api(api_call, "send_group_msg", group_id=group_id, message=message)
        parts = [f"已发送群消息到 {group_id}"]
        if image_url:
            parts.append("(含图片)")
        if at_user_id is not None:
            parts.append(f"(@{at_user_id})")
        if reply_id:
            parts.append(f"(引用{reply_id})")
        return ToolCallResult(ok=True, display=" ".join(parts))
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


async def _handle_send_private_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    user_id = int(args.get("user_id", 0))
    message = str(args.get("message", ""))
    if not user_id or not message:
        return ToolCallResult(ok=False, error="missing user_id or message")
    if len(message) > 4000:
        message = message[:3997] + "..."
    image_url = str(args.get("image_url", "") or "").strip()
    reply_id = args.get("reply_id")
    if reply_id is not None:
        try:
            reply_id = int(reply_id)
        except (ValueError, TypeError):
            reply_id = None
    has_multimodal = bool(image_url or reply_id)
    try:
        if has_multimodal:
            segments = _build_onebot_message_segments(
                message, image_url=image_url, reply_id=reply_id,
            )
            await call_napcat_api(api_call, "send_private_msg", user_id=user_id, message=segments)
        else:
            await call_napcat_api(api_call, "send_private_msg", user_id=user_id, message=message)
        parts = [f"已发送私聊消息到 {user_id}"]
        if image_url:
            parts.append("(含图片)")
        if reply_id:
            parts.append(f"(引用{reply_id})")
        return ToolCallResult(ok=True, display=" ".join(parts))
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


# ── 通用 NapCat API 调用封装 ──

async def _napcat_api_call(
    context: dict[str, Any], api: str, display_ok: str, **kwargs: Any
) -> ToolCallResult:
    """通用 NapCat API 调用封装，减少重复代码。"""
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    try:
        result = await call_napcat_api(api_call, api, **kwargs)
        data = {}
        if isinstance(result, dict):
            data = result
        elif isinstance(result, list):
            data = {"items": result[:50], "total": len(result)}
        return ToolCallResult(ok=True, data=data, display=display_ok)
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


async def _handle_generic_napcat_api(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """Generic handler for NapCat APIs that just pass args through.

    The tool name is inferred from the schema registration context.
    Uses the tool_name stored in context by the agent loop.
    """
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    # The tool name is passed via context["_tool_name"] by the agent loop,
    # or we can infer from the registered schema name
    tool_name = context.get("_tool_name", "")
    if not tool_name:
        return ToolCallResult(ok=False, error="cannot_determine_api_name")
    # Filter out empty/None args
    clean_args = {k: v for k, v in args.items() if v is not None and v != ""}
    try:
        result = await call_napcat_api(api_call, tool_name, **clean_args)
        data = {}
        if isinstance(result, dict):
            data = result
        elif isinstance(result, list):
            data = {"items": result[:50], "total": len(result)}
        return ToolCallResult(ok=True, data=data, display=f"{tool_name} 执行成功")
    except Exception as exc:
        return ToolCallResult(ok=False, error=f"{tool_name}: {exc}")


_QQ_ID_SAFE_PATTERN = re.compile(r"^[1-9]\d{5,11}$")


def _safe_user_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = normalize_text(value)
    else:
        return None
    if not _QQ_ID_SAFE_PATTERN.fullmatch(raw):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _render_onebot_message_text(raw_message: Any, segments: Any) -> str:
    text = normalize_text(str(raw_message))
    if text:
        return text
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        data = seg.get("data", {}) or {}
        if seg_type == "text":
            content = normalize_text(str(data.get("text", "")))
            if content:
                parts.append(content)
        elif seg_type in {"image", "video", "record", "audio", "file"}:
            parts.append(f"[{seg_type}]")
        elif seg_type == "at":
            qq = normalize_text(str(data.get("qq", "")))
            parts.append(f"@{qq or 'someone'}")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return normalize_text(" ".join(parts))


def _unwrap_onebot_message_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    if isinstance(data, dict) and (
        data.get("message_id")
        or data.get("real_id")
        or data.get("message")
        or data.get("raw_message")
    ):
        return data
    return raw


def _extract_history_messages(raw: Any) -> list[dict[str, Any]]:
    payload = _unwrap_onebot_message_result(raw)
    if isinstance(payload, dict):
        items = payload.get("messages")
        if not isinstance(items, list):
            items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _resolve_recall_scope_from_message(item: dict[str, Any], context: dict[str, Any]) -> tuple[str, str]:
    message_type = normalize_text(str(item.get("message_type", ""))).lower()
    group_id = normalize_text(str(item.get("group_id", "")))
    user_id = normalize_text(str(item.get("user_id", "")))
    sender = item.get("sender", {}) if isinstance(item.get("sender"), dict) else {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    bot_id = normalize_text(str(context.get("bot_id", "")))
    if message_type == "group" or group_id:
        return "group", group_id or normalize_text(str(context.get("group_id", "")))
    if message_type in {"private", "friend"}:
        peer_id = user_id
        if peer_id and bot_id and peer_id == bot_id and sender_id and sender_id != bot_id:
            peer_id = sender_id
        if not peer_id:
            peer_id = normalize_text(str(context.get("user_id", ""))) or sender_id
        return "private", peer_id
    if bool(context.get("is_private")):
        return "private", normalize_text(str(context.get("user_id", "")))
    fallback_group_id = normalize_text(str(context.get("group_id", "")))
    if fallback_group_id:
        return "group", fallback_group_id
    return "", ""


def _build_recall_payload_from_message(
    item: dict[str, Any],
    context: dict[str, Any],
    *,
    source: str,
    note: str,
) -> dict[str, Any] | None:
    chat_type, peer_id = _resolve_recall_scope_from_message(item, context)
    if chat_type not in {"group", "private"} or not peer_id:
        return None
    sender = item.get("sender", {}) if isinstance(item.get("sender"), dict) else {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    sender_name = (
        normalize_text(str(sender.get("card", "")))
        or normalize_text(str(sender.get("nickname", "")))
        or sender_id
        or "未知用户"
    )
    sender_role = normalize_text(str(sender.get("role", ""))).lower()
    segments = item.get("message", [])
    if not isinstance(segments, list):
        segments = []
    bot_id = normalize_text(str(context.get("bot_id", "")))
    return {
        "conversation_id": _build_recall_conversation_id(chat_type, peer_id),
        "chat_type": chat_type,
        "peer_id": peer_id,
        "bot_id": bot_id,
        "message_id": normalize_text(str(item.get("message_id", "") or item.get("real_id", "") or item.get("id", ""))),
        "seq": normalize_text(str(item.get("message_seq", "") or item.get("real_seq", ""))),
        "timestamp": int(item.get("time", 0) or 0),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_role": sender_role,
        "is_self": bool(sender_id and bot_id and sender_id == bot_id),
        "text": _render_onebot_message_text(item.get("raw_message", ""), segments),
        "segments": segments,
        "operator_id": normalize_text(str(context.get("user_id", ""))),
        "operator_name": normalize_text(str(context.get("user_name", ""))),
        "source": source,
        "note": note,
        "recalled_at": int(time.time()),
    }


async def _record_recalled_message_from_message_id(
    message_id: int,
    context: dict[str, Any],
    *,
    source: str,
    note: str,
) -> None:
    api_call = context.get("api_call")
    if not callable(api_call):
        return
    try:
        raw = await call_napcat_api(api_call, "get_msg", message_id=message_id)
    except Exception:
        return
    item = _unwrap_onebot_message_result(raw)
    if not item:
        return
    payload = _build_recall_payload_from_message(item, context, source=source, note=note)
    if not payload:
        return
    try:
        _record_recalled_message(payload)
    except Exception:
        _log.warning("record_recalled_message_failed | message_id=%s", message_id, exc_info=True)


async def _handle_get_group_member_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_member_list", f"获取群 {group_id} 成员列表成功", group_id=group_id)
    if result.ok and result.data.get("items"):
        members = result.data["items"]
        summary_lines = []
        for m in members[:30]:
            if isinstance(m, dict):
                uid = m.get("user_id", "")
                nick = m.get("nickname", "")
                card = m.get("card", "")
                role = m.get("role", "")
                display_name = card or nick
                summary_lines.append(f"{uid}({display_name})[{role}]")
        result.display = f"群 {group_id} 共 {result.data.get('total', len(members))} 人: " + ", ".join(summary_lines)
    return result


async def _handle_get_group_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_info", f"获取群 {group_id} 信息成功", group_id=group_id)
    if result.ok and result.data:
        d = result.data
        result.display = f"群名: {d.get('group_name','?')}, 成员数: {d.get('member_count','?')}, 最大成员: {d.get('max_member_count','?')}"
    return result


async def _handle_get_user_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = _safe_user_id(args.get("user_id", 0))
    if user_id is None:
        return ToolCallResult(ok=False, error="invalid user_id")
    result = await _napcat_api_call(context, "get_stranger_info", f"获取用户 {user_id} 信息成功", user_id=user_id)
    if result.ok and result.data:
        d = result.data
        result.display = f"昵称: {d.get('nickname','?')}, 性别: {d.get('sex','?')}, 年龄: {d.get('age','?')}"
    return result


async def _handle_get_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    return await _napcat_api_call(context, "get_msg", f"获取消息 {message_id} 成功", message_id=message_id)


async def _handle_delete_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    await _record_recalled_message_from_message_id(
        message_id,
        context,
        source="agent.delete_message",
        note="agent single recall",
    )
    return await _napcat_api_call(context, "delete_msg", f"已撤回消息 {message_id}", message_id=message_id)


async def _handle_recall_recent_messages(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    group_id = int(args.get("group_id", 0))
    user_id = _safe_user_id(args.get("user_id", 0))
    within_minutes = int(args.get("within_minutes", 0) or 0)
    limit = max(1, min(50, int(args.get("limit", 20) or 20)))
    max_pages = max(1, min(12, int(args.get("max_pages", 6) or 6)))
    if not group_id or user_id is None or within_minutes <= 0:
        return ToolCallResult(ok=False, error="missing group_id, user_id or within_minutes")

    cutoff_ts = int(time.time()) - (within_minutes * 60)
    seen_keys: set[str] = set()
    matched: list[dict[str, Any]] = []
    next_seq: int | None = None

    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"group_id": group_id}
        if next_seq is not None and next_seq > 0:
            kwargs["message_seq"] = next_seq
        try:
            raw_page = await call_napcat_api(api_call, "get_group_msg_history", **kwargs)
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"history_fetch_failed:{exc}")
        page_items = _extract_history_messages(raw_page)
        if not page_items:
            break

        oldest_ts_on_page: int | None = None
        lowest_seq_on_page: int | None = None
        for item in page_items:
            sender = item.get("sender", {}) if isinstance(item.get("sender"), dict) else {}
            sender_id = normalize_text(str(sender.get("user_id", "")))
            message_id = normalize_text(str(item.get("message_id", "") or item.get("real_id", "") or item.get("id", "")))
            seq = int(item.get("message_seq", 0) or item.get("real_seq", 0) or 0)
            ts = int(item.get("time", 0) or 0)
            key = message_id or (f"seq:{seq}" if seq else "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            if ts > 0 and (oldest_ts_on_page is None or ts < oldest_ts_on_page):
                oldest_ts_on_page = ts
            if seq > 0 and (lowest_seq_on_page is None or seq < lowest_seq_on_page):
                lowest_seq_on_page = seq
            if sender_id != str(user_id):
                continue
            if ts <= 0 or ts < cutoff_ts:
                continue
            matched.append(item)
            if len(matched) >= limit:
                break

        if len(matched) >= limit:
            break
        if oldest_ts_on_page is not None and oldest_ts_on_page < cutoff_ts:
            break
        if lowest_seq_on_page is None or lowest_seq_on_page <= 1:
            break
        next_seq = lowest_seq_on_page - 1

    if not matched:
        return ToolCallResult(
            ok=True,
            data={"group_id": group_id, "user_id": user_id, "matched": 0, "recalled": 0, "failed": 0},
            display=f"未找到 {user_id} 在最近 {within_minutes} 分钟内可撤回的消息",
        )

    matched.sort(
        key=lambda item: (
            int(item.get("time", 0) or 0),
            int(item.get("message_seq", 0) or item.get("real_seq", 0) or 0),
        )
    )
    recalled_ids: list[str] = []
    failed_ids: list[str] = []
    preview_lines: list[str] = []
    for item in matched[:limit]:
        message_id_text = normalize_text(str(item.get("message_id", "") or item.get("real_id", "") or item.get("id", "")))
        if not message_id_text:
            failed_ids.append("unknown")
            continue
        payload = _build_recall_payload_from_message(
            item,
            context,
            source="agent.recall_recent_messages",
            note=f"agent batch recall {within_minutes}m",
        )
        if payload:
            try:
                _record_recalled_message(payload)
            except Exception:
                _log.warning("record_recalled_message_failed | tool=recall_recent_messages | message_id=%s", message_id_text, exc_info=True)
        try:
            message_id_arg: Any = int(message_id_text) if message_id_text.isdigit() else message_id_text
            await call_napcat_api(api_call, "delete_msg", message_id=message_id_arg)
            recalled_ids.append(message_id_text)
            preview_lines.append(
                f"[{time.strftime('%H:%M:%S', time.localtime(int(item.get('time', 0) or 0)))}] "
                f"{clip_text(_render_onebot_message_text(item.get('raw_message', ''), item.get('message', [])), 50)}"
            )
        except Exception:
            failed_ids.append(message_id_text)

    summary = f"已撤回 {len(recalled_ids)} 条，目标={user_id}，时间窗={within_minutes}分钟"
    if preview_lines:
        summary += "\n" + "\n".join(preview_lines[:6])
    if failed_ids:
        summary += f"\n失败 {len(failed_ids)} 条"
    return ToolCallResult(
        ok=bool(recalled_ids),
        data={
            "group_id": group_id,
            "user_id": user_id,
            "within_minutes": within_minutes,
            "matched": len(matched),
            "recalled": len(recalled_ids),
            "failed": len(failed_ids),
            "message_ids": recalled_ids,
            "failed_message_ids": failed_ids,
        },
        error="" if recalled_ids else "no_messages_recalled",
        display=summary,
    )


def _normalize_target_from_context(value: Any) -> str:
    text = normalize_text(str(value))
    return text if _QQ_ID_SAFE_PATTERN.fullmatch(text) else ""


def _message_contains_explicit_user_id(message_text: str, user_id: str) -> bool:
    content = normalize_text(message_text)
    uid = normalize_text(user_id)
    if not content or not uid:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(uid)}(?!\d)", content))


_BAN_REFERENCE_GENERIC_TOKENS = frozenset(
    {
        "禁言",
        "解除禁言",
        "取消禁言",
        "解禁",
        "30天",
        "30",
        "分钟",
        "小时",
        "天",
        "秒",
        "那个",
        "这个",
        "那位",
        "这位",
        "那个人",
        "这个人",
        "刚刚",
        "刚才",
        "之前",
        "前面",
        "一直",
        "老是",
        "总是",
        "的人",
        "那个人",
    }
)
_BAN_REFERENCE_DEMONSTRATIVE_CUES = ("那个", "这位", "这个", "刚刚", "刚才", "前面", "一直", "老是", "总是")


def _collect_shared_ban_candidates(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    def _touch(uid: str, *, name: str = "", text: str = "") -> None:
        key = _normalize_target_from_context(uid)
        if not key:
            return
        bucket = candidates.setdefault(key, {"uid": key, "names": set(), "texts": []})
        clean_name = normalize_text(name)
        clean_text = normalize_text(text)
        if clean_name:
            bucket["names"].add(clean_name)
        if clean_text:
            texts = bucket["texts"]
            if clean_text not in texts:
                texts.append(clean_text)

    recent_speakers = context.get("recent_speakers", [])
    if isinstance(recent_speakers, list):
        for row in recent_speakers:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            uid = _normalize_target_from_context(row[0])
            if not uid:
                continue
            _touch(uid, name=str(row[1]), text=str(row[2]))

    runtime_rows = context.get("runtime_group_context", [])
    if isinstance(runtime_rows, list):
        for raw_row in runtime_rows:
            row = normalize_text(str(raw_row))
            if not row:
                continue
            matched = re.match(r"^(?P<name>.+?)\(QQ:(?P<uid>[1-9]\d{5,11})\):\s*(?P<text>.+)$", row)
            if not matched:
                continue
            _touch(
                matched.group("uid"),
                name=matched.group("name"),
                text=matched.group("text"),
            )
    return candidates


def _score_shared_ban_candidate(message_text: str, candidate: dict[str, Any]) -> int:
    content = normalize_text(message_text)
    if not content:
        return 0
    content_lower = content.lower()
    has_demonstrative = any(cue in content for cue in _BAN_REFERENCE_DEMONSTRATIVE_CUES)
    ascii_tokens = [token.lower() for token in re.findall(r"[a-zA-Z0-9_]{1,16}", content)]
    multi_tokens = [
        token.lower()
        for token in tokenize(content)
        if token and token.lower() not in _BAN_REFERENCE_GENERIC_TOKENS
    ]
    one_char_ascii_tokens = [token for token in ascii_tokens if len(token) == 1]
    long_ascii_tokens = [token for token in ascii_tokens if len(token) >= 2]

    score = 0
    for raw_name in sorted(candidate.get("names", set()), key=len, reverse=True):
        name = normalize_text(str(raw_name))
        if not name:
            continue
        name_lower = name.lower()
        if len(name_lower) >= 2 and name_lower in content_lower:
            score = max(score, 100)
            continue
        if len(name_lower) >= 2 and any(
            token == name_lower or token in name_lower or name_lower in token
            for token in long_ascii_tokens + multi_tokens
        ):
            score = max(score, 72)
            continue
        if has_demonstrative and re.fullmatch(r"[a-z0-9_]{2,8}", name_lower):
            if any(token and token in name_lower for token in one_char_ascii_tokens):
                score = max(score, 28)

    preview_tokens: set[str] = set()
    for raw_text in candidate.get("texts", []):
        preview_tokens.update(
            token.lower()
            for token in tokenize(normalize_text(str(raw_text)))
            if token and token.lower() not in _BAN_REFERENCE_GENERIC_TOKENS
        )
    overlap = [token for token in multi_tokens if token in preview_tokens]
    if overlap:
        score += min(24, 8 * len(set(overlap)))
    return score


def _infer_unique_ban_target_from_shared_context(context: dict[str, Any]) -> tuple[str, str]:
    message_text = normalize_text(
        str(context.get("original_message_text", "") or context.get("message_text", ""))
    )
    if not message_text:
        return "", "shared_context_message_empty"
    candidates = _collect_shared_ban_candidates(context)
    if not candidates:
        return "", "shared_context_empty"

    scored: list[tuple[int, str]] = []
    for uid, candidate in candidates.items():
        score = _score_shared_ban_candidate(message_text, candidate)
        if score > 0:
            scored.append((score, uid))
    if not scored:
        return "", "shared_context_no_match"
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_uid = scored[0]
    if top_score < 20:
        return "", "shared_context_match_too_weak"
    if len(scored) > 1:
        second_score = scored[1][0]
        if second_score == top_score or top_score - second_score < 8:
            return "", "shared_context_ambiguous"
    return top_uid, ""


def _resolve_group_ban_target(args: dict[str, Any], context: dict[str, Any]) -> tuple[int | None, str]:
    raw_target = _safe_user_id(args.get("user_id", 0))
    target_uid = str(raw_target) if raw_target else ""
    actor_uid = _normalize_target_from_context(context.get("user_id", ""))
    bot_id = normalize_text(str(context.get("bot_id", "")))
    reply_uid = _normalize_target_from_context(context.get("reply_to_user_id", ""))
    if reply_uid and reply_uid == bot_id:
        reply_uid = ""

    at_targets: list[str] = []
    raw_at_targets = context.get("at_other_user_ids", [])
    if isinstance(raw_at_targets, list):
        for item in raw_at_targets:
            uid = _normalize_target_from_context(item)
            if uid:
                at_targets.append(uid)
    explicit_signals = {uid for uid in at_targets if uid}
    if reply_uid:
        explicit_signals.add(reply_uid)

    msg_text = normalize_text(
        str(context.get("original_message_text", "") or context.get("message_text", ""))
    )
    text_mentions_target = _message_contains_explicit_user_id(msg_text, target_uid) if target_uid else False
    shared_context_uid, shared_context_err = _infer_unique_ban_target_from_shared_context(context)

    if explicit_signals:
        if target_uid:
            if target_uid not in explicit_signals and not text_mentions_target:
                if len(explicit_signals) == 1:
                    target_uid = next(iter(explicit_signals))
                else:
                    return None, "target_user_mismatch_with_explicit_context"
        else:
            if len(explicit_signals) == 1:
                target_uid = next(iter(explicit_signals))
            else:
                return None, "target_user_ambiguous_explicit_context"
    elif target_uid and text_mentions_target:
        pass
    elif shared_context_uid:
        target_uid = shared_context_uid
    elif target_uid and actor_uid and target_uid == actor_uid:
        pass
    elif target_uid:
        return None, "target_user_not_explicit"
    else:
        return None, shared_context_err or "missing_or_invalid_user_id"

    try:
        return int(target_uid), ""
    except Exception:
        return None, "target_user_invalid_after_resolve"


def _extract_onebot_data(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            merged = dict(result)
            merged.update(data)
            return merged
        return result
    return {}


async def _verify_group_ban_applied(
    api_call: Any,
    *,
    group_id: int,
    user_id: int,
    duration: int,
) -> tuple[bool, dict[str, Any]]:
    if not callable(api_call):
        return False, {}
    now = int(time.time())
    latest_payload: dict[str, Any] = {}
    for _ in range(5):
        try:
            payload_raw = await call_napcat_api(
                api_call,
                "get_group_member_info",
                group_id=group_id,
                user_id=user_id,
                no_cache=True,
            )
            payload = _extract_onebot_data(payload_raw)
            latest_payload = payload
            shut_up_until = int(
                payload.get("shut_up_timestamp")
                or payload.get("shut_up_timestamp_sec")
                or payload.get("ban_until")
                or 0
            )
            now = int(time.time())
            if duration <= 0:
                if shut_up_until <= now + 2:
                    return True, {"shut_up_timestamp": shut_up_until}
            else:
                if shut_up_until > now + 5:
                    return True, {"shut_up_timestamp": shut_up_until}
        except Exception:
            pass
        await asyncio.sleep(0.7)
    return False, latest_payload


async def _handle_set_group_ban(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")

    group_id = int(args.get("group_id", 0) or context.get("group_id", 0) or 0)
    duration = int(args.get("duration", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    duration = max(0, min(duration, 30 * 24 * 3600))

    resolved_user_id, resolve_err = _resolve_group_ban_target(args, context)
    if not resolved_user_id:
        return ToolCallResult(
            ok=False,
            error=f"target_resolve_failed:{resolve_err}",
            display="禁言目标不明确：需要 @、回复、明确QQ号，或能从群聊共享上下文唯一解析。",
        )

    permission_level = normalize_text(str(context.get("permission_level", ""))).lower() or "user"
    actor_uid = _normalize_target_from_context(context.get("user_id", ""))
    if permission_level not in {"super_admin", "group_admin"}:
        if not actor_uid or str(resolved_user_id) != actor_uid:
            return ToolCallResult(
                ok=False,
                error="permission_denied:self_ban_only",
                data={"group_id": group_id, "user_id": resolved_user_id, "duration": duration},
                display="普通成员只能请求禁言自己或解除自己的禁言。",
            )

    try:
        await call_napcat_api(
            api_call,
            "set_group_ban",
            group_id=group_id,
            user_id=resolved_user_id,
            duration=duration,
        )
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc), display=f"禁言操作失败: {exc}")

    verified, verify_payload = await _verify_group_ban_applied(
        api_call,
        group_id=group_id,
        user_id=resolved_user_id,
        duration=duration,
    )
    action = "解除禁言" if duration == 0 else f"禁言 {duration}秒"
    if not verified:
        return ToolCallResult(
            ok=False,
            error="ban_unverified",
            data={
                "group_id": group_id,
                "user_id": resolved_user_id,
                "duration": duration,
                "verify_payload": verify_payload,
            },
            display=f"已提交{action}请求，但未拿到可验证回执，请稍后复查。",
        )
    return ToolCallResult(
        ok=True,
        data={
            "group_id": group_id,
            "user_id": resolved_user_id,
            "duration": duration,
            "verify_payload": verify_payload,
        },
        display=f"已对 {resolved_user_id} {action}（已校验）",
    )


async def _handle_set_group_card(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    card = str(args.get("card", ""))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    return await _napcat_api_call(
        context, "set_group_card", f"已设置 {user_id} 群名片为 '{card}'",
        group_id=group_id, user_id=user_id, card=card,
    )


async def _handle_set_group_kick(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    reject = bool(args.get("reject_add_request", False))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    return await _napcat_api_call(
        context, "set_group_kick", f"已将 {user_id} 踢出群 {group_id}",
        group_id=group_id, user_id=user_id, reject_add_request=reject,
    )


async def _handle_set_group_special_title(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    title = str(args.get("special_title", ""))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    return await _napcat_api_call(
        context, "set_group_special_title", f"已设置 {user_id} 头衔为 '{title}'",
        group_id=group_id, user_id=user_id, special_title=title,
    )


async def _handle_get_group_honor_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    honor_type = str(args.get("type", "all"))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    return await _napcat_api_call(
        context, "get_group_honor_info", f"获取群 {group_id} 荣誉信息成功",
        group_id=group_id, type=honor_type,
    )


async def _handle_upload_group_file(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    file_path = str(args.get("file", ""))
    name = str(args.get("name", ""))
    folder = str(args.get("folder", "") or "").strip()
    if not group_id or not file_path or not name:
        return ToolCallResult(ok=False, error="missing group_id, file or name")
    # 安全: 限制文件路径，防止 LLM 上传任意系统文件
    resolved = Path(file_path).resolve()
    # 只允许 storage/ 和 tmp/ 目录下的文件
    project_root = Path(__file__).resolve().parents[1]
    allowed_dirs = [
        project_root / "storage",
        project_root / "tmp",
        Path("/tmp"),
        Path(tempfile.gettempdir()),
    ]
    # 兼容 NapCat 常见下载目录
    home = Path.home()
    for extra in (
        home / "OneDrive" / "文档" / "Tencent Files" / "NapCat" / "temp",
        home / "Documents" / "Tencent Files" / "NapCat" / "temp",
        home / "Tencent Files" / "NapCat" / "temp",
        Path(tempfile.gettempdir()) / "NapCat" / "temp",
    ):
        allowed_dirs.append(extra)
    if not any(str(resolved).lower().startswith(str(d.resolve()).lower()) for d in allowed_dirs):
        return ToolCallResult(ok=False, error="安全限制: 只能上传 storage/tmp 或 NapCat temp 目录下的文件")
    if not resolved.is_file():
        _log.warning("upload_group_file_missing | file=%s", clip_text(str(resolved), 180))
        return ToolCallResult(ok=False, error="文件不存在或当前进程不可读")
    kwargs: dict[str, Any] = {"group_id": group_id, "file": str(resolved), "name": name}
    if folder:
        kwargs["folder"] = folder
    return await _napcat_api_call(
        context, "upload_group_file", f"已上传文件 {name} 到群 {group_id}", **kwargs,
    )


async def _handle_get_group_notice(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    return await _napcat_api_call(context, "_get_group_notice", f"获取群 {group_id} 公告成功", group_id=group_id)


async def _handle_send_group_notice(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    content = str(args.get("content", ""))
    if not group_id or not content:
        return ToolCallResult(ok=False, error="missing group_id or content")
    return await _napcat_api_call(
        context, "_send_group_notice", f"已发送群 {group_id} 公告",
        group_id=group_id, content=content,
    )


async def _handle_get_friend_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_friend_list", "获取好友列表成功")
    if result.ok and result.data.get("items"):
        friends = result.data["items"]
        summary = [f"{f.get('user_id','')}({f.get('nickname','')})" for f in friends[:20] if isinstance(f, dict)]
        result.display = f"共 {result.data.get('total', len(friends))} 好友: " + ", ".join(summary)
    return result


async def _handle_get_group_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_group_list", "获取群列表成功")
    if result.ok and result.data.get("items"):
        groups = result.data["items"]
        summary = [f"{g.get('group_id','')}({g.get('group_name','')})" for g in groups[:20] if isinstance(g, dict)]
        result.display = f"共 {result.data.get('total', len(groups))} 个群: " + ", ".join(summary)
    return result


async def _handle_send_like(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    times = min(10, max(1, int(args.get("times", 1))))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    return await _napcat_api_call(context, "send_like", f"已给 {user_id} 点赞 {times} 次", user_id=user_id, times=times)


async def _handle_set_group_whole_ban(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    enable = bool(args.get("enable", True))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    action = "开启" if enable else "关闭"
    return await _napcat_api_call(
        context, "set_group_whole_ban", f"已{action}群 {group_id} 全员禁言",
        group_id=group_id, enable=enable,
    )


async def _handle_set_group_admin(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    enable = bool(args.get("enable", True))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    action = "设为管理员" if enable else "取消管理员"
    return await _napcat_api_call(
        context, "set_group_admin", f"已将 {user_id} {action}",
        group_id=group_id, user_id=user_id, enable=enable,
    )


async def _handle_set_group_sign(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    return await _napcat_api_call(context, "send_group_sign", f"已在群 {group_id} 打卡", group_id=group_id)


async def _handle_set_group_name(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    group_name = str(args.get("group_name", ""))
    if not group_id or not group_name:
        return ToolCallResult(ok=False, error="missing group_id or group_name")
    return await _napcat_api_call(
        context, "set_group_name", f"已将群 {group_id} 改名为 '{group_name}'",
        group_id=group_id, group_name=group_name,
    )


async def _handle_get_login_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_login_info", "获取登录信息成功")
    if result.ok and result.data:
        d = result.data
        result.display = f"QQ: {d.get('user_id','?')}, 昵称: {d.get('nickname','?')}"
    return result


# ── 新增 NapCat 扩展工具 handlers ──


async def _handle_forward_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """转发消息到群或私聊。"""
    message_id = int(args.get("message_id", 0))
    target_type = str(args.get("target_type", "")).strip().lower()
    target_id = int(args.get("target_id", 0))
    if not message_id or not target_id:
        return ToolCallResult(ok=False, error="missing message_id or target_id")
    if target_type == "group":
        return await _napcat_api_call(
            context, "forward_group_single_msg",
            f"已转发消息 {message_id} 到群 {target_id}",
            message_id=message_id, group_id=target_id,
        )
    elif target_type == "private":
        return await _napcat_api_call(
            context, "forward_friend_single_msg",
            f"已转发消息 {message_id} 到用户 {target_id}",
            message_id=message_id, user_id=target_id,
        )
    else:
        return ToolCallResult(ok=False, error=f"invalid target_type: {target_type}, must be 'group' or 'private'")


async def _handle_get_group_history(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """获取群历史消息。"""
    group_id = int(args.get("group_id", 0))
    count = max(1, min(50, int(args.get("count", 20) or 20)))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(
        context, "get_group_msg_history",
        f"获取群 {group_id} 历史消息成功",
        group_id=group_id, count=count,
    )
    if result.ok:
        messages = _extract_history_messages(result.data if result.data else {})
        if messages:
            summary_lines = []
            for m in messages[:count]:
                sender = m.get("sender", {}) if isinstance(m.get("sender"), dict) else {}
                name = sender.get("card", "") or sender.get("nickname", "") or str(sender.get("user_id", "?"))
                text = _render_onebot_message_text(m.get("raw_message", ""), m.get("message", []))
                summary_lines.append(f"[{name}]: {text[:80]}")
            result.display = f"群 {group_id} 最近 {len(summary_lines)} 条消息:\n" + "\n".join(summary_lines)
            result.data = {"messages": messages[:count], "count": len(messages)}
    return result


async def _handle_get_chat_history(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """获取私聊历史消息。"""
    user_id = _safe_user_id(args.get("user_id", 0))
    if user_id is None:
        return ToolCallResult(ok=False, error="invalid user_id")
    count = max(1, min(50, int(args.get("count", 20) or 20)))
    result = await _napcat_api_call(
        context, "get_friend_msg_history",
        f"获取与 {user_id} 的私聊历史成功",
        user_id=user_id, count=count,
    )
    if result.ok:
        messages = _extract_history_messages(result.data if result.data else {})
        if messages:
            summary_lines = []
            for m in messages[:count]:
                sender = m.get("sender", {}) if isinstance(m.get("sender"), dict) else {}
                name = sender.get("nickname", "") or str(sender.get("user_id", "?"))
                text = _render_onebot_message_text(m.get("raw_message", ""), m.get("message", []))
                summary_lines.append(f"[{name}]: {text[:80]}")
            result.display = f"与 {user_id} 的最近 {len(summary_lines)} 条消息:\n" + "\n".join(summary_lines)
            result.data = {"messages": messages[:count], "count": len(messages)}
    return result


async def _handle_get_group_files(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """获取群文件列表。"""
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(
        context, "get_group_root_files",
        f"获取群 {group_id} 文件列表成功",
        group_id=group_id,
    )
    if result.ok and result.data:
        files = result.data.get("files", [])
        folders = result.data.get("folders", [])
        parts = []
        if isinstance(folders, list):
            for f in folders[:20]:
                if isinstance(f, dict):
                    parts.append(f"📁 {f.get('folder_name', '?')}")
        if isinstance(files, list):
            for f in files[:30]:
                if isinstance(f, dict):
                    name = f.get("file_name", "?")
                    size = f.get("file_size", 0)
                    size_mb = round(int(size or 0) / 1048576, 1)
                    parts.append(f"📄 {name} ({size_mb}MB)")
        result.display = f"群 {group_id} 文件: {len(folders or [])} 个文件夹, {len(files or [])} 个文件\n" + "\n".join(parts)
    return result


async def _handle_get_group_file_url(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """获取群文件下载链接。"""
    group_id = int(args.get("group_id", 0))
    file_id = str(args.get("file_id", "")).strip()
    busid = int(args.get("busid", 0))
    if not group_id or not file_id:
        return ToolCallResult(ok=False, error="missing group_id or file_id")
    result = await _napcat_api_call(
        context, "get_group_file_url",
        f"获取群文件下载链接成功",
        group_id=group_id, file_id=file_id, busid=busid,
    )
    if result.ok and result.data:
        url = result.data.get("url", "")
        if url:
            result.display = f"下载链接: {url}"
    return result


async def _handle_get_muted_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """获取群禁言列表。"""
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(
        context, "get_group_shut_list",
        f"获取群 {group_id} 禁言列表成功",
        group_id=group_id,
    )
    if result.ok and result.data:
        items = result.data.get("items", [])
        if isinstance(items, list) and items:
            parts = []
            for m in items[:30]:
                if isinstance(m, dict):
                    uid = m.get("user_id", "?")
                    shut_time = m.get("shut_timestamp", 0)
                    parts.append(f"{uid} (解禁时间戳: {shut_time})")
            result.display = f"群 {group_id} 共 {len(items)} 人被禁言: " + ", ".join(parts)
        else:
            result.display = f"群 {group_id} 当前无人被禁言"
    return result


async def _handle_check_user_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """查看用户在线状态。"""
    user_id = _safe_user_id(args.get("user_id", 0))
    if user_id is None:
        return ToolCallResult(ok=False, error="invalid user_id")
    result = await _napcat_api_call(
        context, "nc_get_user_status",
        f"查询用户 {user_id} 状态成功",
        user_id=user_id,
    )
    if result.ok and result.data:
        status = result.data.get("status", "unknown")
        ext_status = result.data.get("ext_status", "")
        result.display = f"用户 {user_id}: 状态={status}, 扩展状态={ext_status}"
    return result


async def _handle_send_poke(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """统一戳一戳（群聊/私聊均可）。"""
    user_id = _safe_user_id(args.get("user_id", 0))
    if user_id is None:
        return ToolCallResult(ok=False, error="invalid user_id")
    group_id = int(args.get("group_id", 0) or 0)
    kwargs: dict[str, Any] = {"user_id": user_id}
    if group_id:
        kwargs["group_id"] = group_id
        display = f"已在群 {group_id} 戳了 {user_id}"
    else:
        display = f"已戳了 {user_id}"
    return await _napcat_api_call(context, "send_poke", display, **kwargs)

# ── NapCat 扩展工具注册 ──


def _register_napcat_extended_tools(registry: AgentToolRegistry) -> None:
    """注册 NapCat 扩展 API 工具（戳一戳、表情回应、聊天记录、转发、AI语音等）。"""

    _ext_tools = [
        ("group_poke", "在群里戳一戳某人。\n使用场景: 用户说'戳他一下'、'poke某人'、'拍一拍'时使用。\n效果: 对方会收到戳一戳动画提示",
        {"group_id": ("integer", "群号"), "user_id": ("integer", "要戳的人的QQ号")},
        ["group_id", "user_id"], _handle_group_poke),

        ("friend_poke", "私聊中戳一戳好友。\n使用场景: 用户说'戳他'且在私聊场景时使用",
        {"user_id": ("integer", "要戳的好友QQ号")},
        ["user_id"], _handle_friend_poke),

        ("set_msg_emoji_like", "给一条消息添加表情回应(emoji reaction)。\n使用场景: 用户说'给这条消息点个赞/点个表情'时使用。\n常用emoji_id: 76=赞, 63=玫瑰, 66=爱心, 4=得意, 124=OK手势",
        {"message_id": ("integer", "目标消息ID"), "emoji_id": ("integer", "表情ID，如76=赞, 66=爱心"), "set": ("boolean", "true=添加, false=取消，默认true")},
        ["message_id", "emoji_id"], _handle_set_msg_emoji_like),

        ("get_group_msg_history", "获取群的历史聊天记录(最近19条)。\n使用场景: 用户问'刚才群里说了什么'、'看看聊天记录'时使用。\n返回消息列表，每条包含发送者、时间、内容",
        {"group_id": ("integer", "群号"), "message_seq": ("integer", "起始消息序号，不填则获取最新的")},
        ["group_id"], _handle_get_group_msg_history),

        ("get_friend_msg_history", "获取与某好友的私聊历史记录。\n使用场景: 需要查看之前和某人的聊天内容时使用",
        {"user_id": ("integer", "好友QQ号"), "message_seq": ("integer", "起始消息序号，不填则获取最新的"), "count": ("integer", "获取条数，默认20")},
        ["user_id"], _handle_get_friend_msg_history),

        ("forward_group_single_msg", "把一条消息转发到指定群。\n使用场景: 用户说'把这条消息转发到XX群'时使用",
        {"message_id": ("integer", "要转发的消息ID"), "group_id": ("integer", "目标群号")},
        ["message_id", "group_id"], _handle_forward_group_single_msg),

        ("forward_friend_single_msg", "把一条消息转发给好友私聊。\n使用场景: 用户说'把这条消息转发给XX'时使用",
        {"message_id": ("integer", "要转发的消息ID"), "user_id": ("integer", "目标好友QQ号")},
        ["message_id", "user_id"], _handle_forward_friend_single_msg),

        ("get_essence_msg_list", "获取群精华消息列表。\n使用场景: 用户问'群精华有什么'、'看看精华消息'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_essence_msg_list),

        ("set_essence_msg", "将一条消息设为群精华。需要管理员权限。\n使用场景: 用户说'把这条消息设为精华'时使用",
        {"message_id": ("integer", "要设为精华的消息ID")},
        ["message_id"], _handle_set_essence_msg),

        ("ocr_image", "识别图片中的文字(OCR)。\n使用场景: 用户发了图片问'图片里写了什么'、'识别文字'时使用。\n参数image是图片的file字段(从消息段中获取)",
        {"image": ("string", "图片file标识(从消息的image段data.file获取)")},
        ["image"], _handle_ocr_image),

        ("get_ai_characters", "获取AI语音可用的角色列表。\n使用场景: 用户问'有哪些AI语音角色'时使用。\n返回角色ID和名称，用于 send_group_ai_record",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_ai_characters),

        ("send_group_ai_record", "用AI角色语音在群里发送一段语音消息(TTS文字转语音)。\n使用场景: 用户说'用XX角色说一段话'、'发个语音'时使用。\n先用 get_ai_characters 获取可用角色ID",
        {"group_id": ("integer", "目标群号"), "character": ("string", "角色ID(从get_ai_characters获取)"), "text": ("string", "要转为语音的文本内容")},
        ["group_id", "character", "text"], _handle_send_group_ai_record),

        ("mark_msg_as_read", "标记一条消息为已读",
        {"message_id": ("integer", "消息ID")},
        ["message_id"], _handle_mark_msg_as_read),

        ("get_group_shut_list", "获取群内当前被禁言的成员列表。\n使用场景: 用户问'谁被禁言了'、'禁言列表'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_group_shut_list),

        ("get_group_member_info", "获取群内某个成员的详细信息。\n返回: 昵称、群名片、角色、入群时间、最后发言时间等。\n使用场景: 用户问'XX是什么时候进群的'、'查一下XX的信息'时使用",
        {"group_id": ("integer", "群号"), "user_id": ("integer", "成员QQ号")},
        ["group_id", "user_id"], _handle_get_group_member_info),

        ("set_input_status", "设置对某用户显示'正在输入'状态。\nevent_type: 1=正在输入",
        {"user_id": ("integer", "目标用户QQ号"), "event_type": ("integer", "1=正在输入")},
        ["user_id", "event_type"], _handle_set_input_status),

        ("download_file", "下载URL文件到本地缓存，返回本地路径。\n兼容入口：内部会自动走智能下载链路（视频解析/网页提取下载链接）。\n使用场景: 需要先下载文件再上传到群文件时使用",
        {"url": ("string", "文件下载URL"), "thread_count": ("integer", "下载线程数，默认1"),
            "kind": ("string", "auto/video/audio/file，默认auto"),
            "prefer_ext": ("string", "优先扩展名，如 apk/mp4/mp3"),
            "query": ("string", "原始用户需求文本(可选，用于下载来源可信度判断)"),
            "allow_third_party": ("boolean", "是否允许切换到第三方下载源（默认false，需先征得用户同意）"),
            "upload": ("boolean", "是否下载后直接上传群文件，默认false"),
            "group_id": ("integer", "upload=true 时目标群号"),
            "file_name": ("string", "下载后/上传时文件名(可选)")},
        ["url"], _handle_download_file),

        ("smart_download", "母下载方法(统一下载入口)。\n会自动执行：媒体解析 -> 网页提取直链 -> 下载到可上传目录。\n可直接 upload=true 上传群文件。\n使用场景: 用户要你'直接发安装包/视频/音频文件'时优先用这个。",
        {"url": ("string", "资源链接(网页/视频链接/直链)"),
            "kind": ("string", "auto/video/audio/file，默认auto"),
            "prefer_ext": ("string", "优先扩展名，如 apk/mp4/mp3"),
            "thread_count": ("integer", "下载线程数，默认1"),
            "query": ("string", "原始用户需求文本(可选，用于下载来源可信度判断)"),
            "allow_third_party": ("boolean", "是否允许切换到第三方下载源（默认false，需先征得用户同意）"),
            "upload": ("boolean", "是否下载后直接上传群文件，默认false"),
            "group_id": ("integer", "upload=true 时目标群号"),
            "file_name": ("string", "下载后/上传时文件名(可选)")},
        ["url"], _handle_smart_download),

        ("nc_get_user_status", "查询某QQ用户的在线状态。\n使用场景: 用户问'XX在线吗'时使用",
        {"user_id": ("integer", "目标QQ号")},
        ["user_id"], _handle_nc_get_user_status),

        ("translate_en2zh", "将英文文本翻译为中文(QQ内置翻译)。\n使用场景: 用户发了英文让你翻译时可以用",
        {"words": ("array", "要翻译的英文文本数组，如 [\"hello\",\"world\"]")},
        ["words"], _handle_translate_en2zh),

        # ── 合并转发 ──
        ("send_group_forward_msg", "向群发送合并转发消息(多条消息合并成一个卡片)。\n"
        "使用场景: 用户说'把这些消息合并转发'、'发个合并消息'时使用。\n"
        "messages 是 node 数组，每个 node 格式:\n"
        '  引用已有消息: {"type":"node","data":{"id":"消息ID"}}\n'
        '  自定义内容: {"type":"node","data":{"name":"发送者名","uin":"发送者QQ","content":"内容"}}',
        {"group_id": ("integer", "目标群号"), "messages": ("array", "node数组，见描述")},
        ["group_id", "messages"], _handle_send_group_forward_msg),

        ("send_private_forward_msg", "向好友发送合并转发消息。参数同 send_group_forward_msg",
        {"user_id": ("integer", "目标好友QQ号"), "messages": ("array", "node数组")},
        ["user_id", "messages"], _handle_send_private_forward_msg),

        ("get_forward_msg", "根据转发消息ID获取合并转发的完整内容。\n使用场景: 收到合并转发消息后查看里面的内容",
        {"message_id": ("string", "合并转发消息的ID(从消息段的forward字段获取)")},
        ["message_id"], _handle_get_forward_msg),

        # ── 群操作 ──
        ("set_group_leave", "退出群聊。如果是群主可以解散群。\n使用场景: 用户说'退群'、'离开这个群'时使用。\n注意: 这是不可逆操作!",
        {"group_id": ("integer", "要退出的群号"), "is_dismiss": ("boolean", "是否解散群(仅群主)，默认false")},
        ["group_id"], _handle_set_group_leave),

        ("delete_essence_msg", "取消一条群精华消息。需要管理员权限。\n使用场景: 用户说'取消精华'、'移除精华消息'时使用",
        {"message_id": ("integer", "要取消精华的消息ID")},
        ["message_id"], _handle_delete_essence_msg),

        ("get_group_at_all_remain", "获取群内@全体成员的剩余次数。\n使用场景: 用户问'还能@全体几次'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_group_at_all_remain),

        # ── 请求处理 ──
        ("set_friend_add_request", "处理好友添加请求(同意/拒绝)。\n使用场景: 用户说'同意好友请求'、'拒绝加好友'时使用。\nflag 从好友请求事件中获取",
        {"flag": ("string", "请求标识(从事件获取)"), "approve": ("boolean", "true=同意, false=拒绝"), "remark": ("string", "好友备注(同意时可选)")},
        ["flag"], _handle_set_friend_add_request),

        ("set_group_add_request", "处理加群请求或邀请(同意/拒绝)。\n使用场景: 用户说'同意入群'、'拒绝加群请求'时使用。\nflag 从加群请求事件中获取",
        {"flag": ("string", "请求标识(从事件获取)"), "sub_type": ("string", "'add'=主动加群, 'invite'=被邀请"), "approve": ("boolean", "true=同意, false=拒绝"), "reason": ("string", "拒绝理由(拒绝时可选)")},
        ["flag", "sub_type"], _handle_set_group_add_request),

        # ── 好友操作 ──
        ("delete_friend", "删除好友。不可逆操作!\n使用场景: 用户说'删除好友XX'时使用",
        {"user_id": ("integer", "要删除的好友QQ号")},
        ["user_id"], _handle_delete_friend),

        # ── 群文件系统 ──
        ("get_group_file_system_info", "获取群文件系统信息(文件数量、已用空间等)。\n使用场景: 用户问'群文件还有多少空间'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_group_file_system_info),

        ("get_group_root_files", "获取群文件根目录的文件和文件夹列表。\n使用场景: 用户说'看看群文件'、'群文件有什么'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_group_root_files),

        ("get_group_file_url", "获取群文件的下载链接。\n使用场景: 用户说'下载群文件XX'时使用。\nfile_id 和 busid 从文件列表中获取",
        {"group_id": ("integer", "群号"), "file_id": ("string", "文件ID"), "busid": ("integer", "文件业务ID")},
        ["group_id", "file_id", "busid"], _handle_get_group_file_url),

        ("upload_private_file", "向好友发送文件(私聊文件)。\n使用场景: 用户说'给XX发个文件'时使用",
        {"user_id": ("integer", "好友QQ号"), "file": ("string", "本地文件路径"), "name": ("string", "文件名")},
        ["user_id", "file", "name"], _handle_upload_private_file),

        # ── NapCat 扩展 ──
        ("set_qq_avatar", "设置机器人的QQ头像。\n使用场景: 用户说'换个头像'时使用。\n参数是本地图片路径或图片URL",
        {"file": ("string", "图片路径或URL")},
        ["file"], _handle_set_qq_avatar),

        ("set_group_portrait", "设置群头像。需要管理员权限。\n使用场景: 用户说'换群头像'时使用",
        {"group_id": ("integer", "群号"), "file": ("string", "图片路径或URL")},
        ["group_id", "file"], _handle_set_group_portrait),

        ("set_online_status", "设置机器人的在线状态。\n使用场景: 用户说'设置在线/隐身/忙碌'时使用。\n"
        "status值: 11=在线, 21=离开, 31=隐身, 41=忙碌, 50=Q我吧, 60=请勿打扰",
        {"status": ("integer", "状态码: 11=在线, 21=离开, 31=隐身, 41=忙碌, 50=Q我吧, 60=请勿打扰"),
        "ext_status": ("integer", "扩展状态码，默认0"), "battery_status": ("integer", "电量状态，默认0")},
        ["status"], _handle_set_online_status),

        ("send_msg", "通用发消息接口，可发群消息或私聊消息。\n"
        "使用场景: 当不确定是群还是私聊时使用，或需要发送富文本(CQ码)消息时使用。\n"
        "message 支持 CQ 码，如:\n"
        '  图片: [CQ:image,file=https://xxx.jpg]\n'
        '  @某人: [CQ:at,qq=123456]\n'
        '  语音: [CQ:record,file=https://xxx.mp3]\n'
        '  表情: [CQ:face,id=178]',
        {"message_type": ("string", "'private'或'group'"), "user_id": ("integer", "私聊时的QQ号"),
        "group_id": ("integer", "群聊时的群号"), "message": ("string", "消息内容，支持CQ码")},
        ["message"], _handle_send_msg),

        ("check_url_safely", "检查URL是否安全(QQ安全检测)。\n使用场景: 用户发了链接想确认是否安全时使用",
        {"url": ("string", "要检查的URL")},
        ["url"], _handle_check_url_safely),

        ("get_status", "获取NapCat/OneBot运行状态。\n使用场景: 用户问'机器人状态怎么样'时使用。\n返回是否在线、收发消息统计等",
        {}, [], _handle_get_status),

        ("get_version_info", "获取NapCat/OneBot版本信息。\n使用场景: 用户问'什么版本'时使用",
        {}, [], _handle_get_version_info),

        # ── 第二批 NapCat 扩展 API ──

        ("set_self_longnick", "设置机器人的个性签名(长昵称)。\n使用场景: 用户说'改签名'、'设置个签'时使用",
        {"longnick": ("string", "新的个性签名内容")},
        ["longnick"], _handle_set_self_longnick),

        ("get_recent_contact", "获取最近的聊天联系人列表。\n使用场景: 用户问'最近和谁聊过'、'最近联系人'时使用",
        {"count": ("integer", "获取数量，默认10")},
        [], _handle_get_recent_contact),

        ("get_profile_like", "获取谁给机器人点了赞(名片赞列表)。\n使用场景: 用户问'谁给我点赞了'时使用",
        {}, [], _handle_get_profile_like),

        ("fetch_custom_face", "获取机器人收藏的自定义表情列表。\n使用场景: 用户问'有什么收藏表情'时使用",
        {"count": ("integer", "获取数量，默认48")},
        [], _handle_fetch_custom_face),

        ("fetch_emoji_like", "获取某条消息的表情回应详情(谁回应了什么表情)。\n使用场景: 用户问'这条消息谁点了表情'时使用",
        {"message_id": ("integer", "消息ID"), "emoji_id": ("string", "表情ID(可选)"), "emoji_type": ("string", "表情类型(可选)")},
        ["message_id"], _handle_fetch_emoji_like),

        ("get_group_info_ex", "获取群的扩展详细信息(比 get_group_info 更详细)。\n返回: 群等级、创建时间、最大成员数等。\n使用场景: 需要群的详细元数据时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_get_group_info_ex),

        ("get_group_files_by_folder", "获取群文件指定文件夹内的文件列表。\n使用场景: 用户说'看看XX文件夹里有什么'时使用。\nfolder_id 从 get_group_root_files 获取",
        {"group_id": ("integer", "群号"), "folder_id": ("string", "文件夹ID(从文件列表获取)")},
        ["group_id", "folder_id"], _handle_get_group_files_by_folder),

        ("delete_group_file", "删除群文件。需要管理员权限。\n使用场景: 用户说'删除群文件XX'时使用",
        {"group_id": ("integer", "群号"), "file_id": ("string", "文件ID"), "busid": ("integer", "文件业务ID")},
        ["group_id", "file_id"], _handle_delete_group_file),

        ("create_group_file_folder", "在群文件中创建文件夹。\n使用场景: 用户说'在群文件里建个文件夹'时使用",
        {"group_id": ("integer", "群号"), "name": ("string", "文件夹名称"), "parent_id": ("string", "父文件夹ID，默认'/'(根目录)")},
        ["group_id", "name"], _handle_create_group_file_folder),

        ("get_group_system_msg", "获取群系统消息(加群请求、邀请等待处理的通知)。\n使用场景: 用户问'有没有人申请加群'、'群通知'时使用",
        {}, [], _handle_get_group_system_msg),

        ("send_forward_msg", "通用合并转发消息接口(支持群和私聊)。\n"
        "使用场景: 需要发送合并转发消息时使用。\n"
        "messages 是 node 数组，格式同 send_group_forward_msg",
        {"message_type": ("string", "'group'或'private'"), "group_id": ("integer", "群号(群聊时)"),
        "user_id": ("integer", "QQ号(私聊时)"), "messages": ("array", "node数组")},
        ["messages"], _handle_send_forward_msg),

        ("mark_all_as_read", "标记所有消息为已读。\n使用场景: 用户说'全部已读'、'清除未读'时使用",
        {}, [], _handle_mark_all_as_read),

        ("get_friends_with_category", "获取带分组信息的好友列表。\n使用场景: 用户问'好友分组'、'哪些好友在哪个分组'时使用",
        {}, [], _handle_get_friends_with_category),

        ("get_image", "获取图片文件信息(本地路径和URL)。\n使用场景: 需要获取图片的实际文件路径或下载URL时使用。\nfile 参数从消息的 image 段获取",
        {"file": ("string", "图片file标识(从消息段获取)")},
        ["file"], _handle_get_image),

        ("get_record", "获取语音文件信息(转换格式并返回路径)。\n使用场景: 需要获取语音文件的本地路径时使用。\nfile 参数从消息的 record 段获取",
        {"file": ("string", "语音file标识(从消息段获取)"), "out_format": ("string", "输出格式: mp3/amr/wma/m4a/spx/ogg/wav/flac，默认mp3")},
        ["file"], _handle_get_record),

        ("get_ai_record", "生成AI语音(TTS)但不直接发送，返回语音文件信息。\n使用场景: 需要先生成语音再做其他处理时使用。\n先用 get_ai_characters 获取角色ID",
        {"group_id": ("integer", "群号(用于获取语音权限)"), "character": ("string", "角色ID"), "text": ("string", "要转为语音的文本")},
        ["group_id", "character", "text"], _handle_get_ai_record),

        ("ark_share_peer", "生成推荐联系人/群的分享卡片(Ark消息)。\n使用场景: 用户说'推荐XX给某人'、'分享群名片'时使用",
        {"user_id": ("string", "推荐的用户QQ号(可选)"), "group_id": ("string", "推荐的群号(可选)"), "phone_number": ("string", "手机号(可选)")},
        [], _handle_ark_share_peer),

        ("get_mini_app_ark", "签名小程序卡片(Ark消息)。\n使用场景: 需要发送小程序分享卡片时使用。\ncontent 是小程序的 JSON 配置",
        {"content": ("string", "小程序JSON配置字符串")},
        ["content"], _handle_get_mini_app_ark),

        ("create_collection", "创建QQ收藏。\n使用场景: 用户说'收藏这段话'、'帮我收藏'时使用",
        {"raw_data": ("string", "要收藏的文本内容"), "brief": ("string", "收藏摘要(可选)")},
        ["raw_data"], _handle_create_collection),

        ("get_collection_list", "获取QQ收藏列表。\n使用场景: 用户问'我的收藏有什么'时使用",
        {"category": ("integer", "收藏分类，0=全部"), "count": ("integer", "获取数量，默认20")},
        [], _handle_get_collection_list),

        ("del_group_notice", "删除群公告。需要管理员权限。\n使用场景: 用户说'删除群公告'时使用。\nnotice_id 从 get_group_notice 获取",
        {"group_id": ("integer", "群号"), "notice_id": ("string", "公告ID(从 get_group_notice 获取)")},
        ["group_id", "notice_id"], _handle_del_group_notice),

        ("nc_get_packet_status", "获取NapCat PacketServer状态(扩展功能是否可用)。\n使用场景: 需要检查戳一戳、AI语音等扩展功能是否可用时使用",
        {}, [], _handle_nc_get_packet_status),

        ("clean_cache", "清理NapCat缓存。\n使用场景: 用户说'清理缓存'、'清除缓存'时使用",
        {}, [], _handle_clean_cache),

        # ── 第三批: 补全所有缺失的 NapCat API ──

        ("set_qq_profile", "设置机器人的QQ资料(昵称、个签、性别等)。\n使用场景: 用户说'改昵称'、'改资料'时使用",
        {"nickname": ("string", "昵称"), "personal_note": ("string", "个性签名(可选)"),
         "sex": ("integer", "性别: 0=未知, 1=男, 2=女(可选)")},
        ["nickname"], _handle_generic_napcat_api),

        ("send_group_sign", "群签到/打卡(QQ群签到功能)。\n使用场景: 用户说'群签到'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_generic_napcat_api),

        ("delete_group_folder", "删除群文件夹。需要管理员权限。\n使用场景: 用户说'删除群文件夹XX'时使用",
        {"group_id": ("integer", "群号"), "folder_id": ("string", "文件夹ID")},
        ["group_id", "folder_id"], _handle_generic_napcat_api),

        ("get_file", "获取文件信息(通用)。\n使用场景: 需要获取文件的本地路径或URL时使用",
        {"file_id": ("string", "文件ID")},
        ["file_id"], _handle_generic_napcat_api),

        ("get_cookies", "获取QQ Cookies。\n使用场景: 需要获取QQ平台的Cookie用于第三方接口时使用",
        {"domain": ("string", "域名，如 qzone.qq.com")},
        ["domain"], _handle_generic_napcat_api),

        ("get_csrf_token", "获取CSRF Token。\n使用场景: 需要QQ平台的CSRF令牌时使用",
        {}, [], _handle_generic_napcat_api),

        ("get_credentials", "获取QQ凭证(Cookies + CSRF Token)。\n使用场景: 需要完整QQ凭证时使用",
        {"domain": ("string", "域名")},
        [], _handle_generic_napcat_api),

        ("can_send_image", "检查是否可以发送图片",
        {}, [], _handle_generic_napcat_api),

        ("can_send_record", "检查是否可以发送语音",
        {}, [], _handle_generic_napcat_api),

        ("mark_private_msg_as_read", "标记私聊消息为已读。\n使用场景: 需要标记某人的私聊为已读时使用",
        {"user_id": ("integer", "好友QQ号")},
        ["user_id"], _handle_generic_napcat_api),

        ("mark_group_msg_as_read", "标记群消息为已读。\n使用场景: 需要标记某群消息为已读时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_generic_napcat_api),

        ("send_poke", "通用戳一戳(自动判断群/私聊)。\n使用场景: 用户说'戳他'时使用",
        {"user_id": ("integer", "目标QQ号"), "group_id": ("integer", "群号(群聊时填，私聊不填)")},
        ["user_id"], _handle_generic_napcat_api),

        ("nc_get_rkey", "获取NapCat rkey(用于媒体资源访问)。\n使用场景: 内部使用，获取媒体访问密钥",
        {}, [], _handle_generic_napcat_api),

        ("get_robot_uin_range", "获取机器人QQ号段范围。\n使用场景: 需要判断某QQ号是否是机器人时使用",
        {}, [], _handle_generic_napcat_api),

        ("get_group_ignore_add_request", "获取被忽略的加群请求列表。\n使用场景: 用户问'有没有被忽略的加群请求'时使用",
        {"group_id": ("integer", "群号")},
        ["group_id"], _handle_generic_napcat_api),

        ("ArkShareGroup", "生成群分享卡片(Ark消息)。\n使用场景: 用户说'分享这个群'时使用",
        {"group_id": ("string", "要分享的群号")},
        ["group_id"], _handle_generic_napcat_api),
    ]

    for name, desc, props, required, handler in _ext_tools:
        parameters = {
            "type": "object",
            "properties": {k: {"type": v[0], "description": v[1]} for k, v in props.items()},
            "required": required,
        }
        registry.register(ToolSchema(name=name, description=desc, parameters=parameters, category="napcat"), handler)


# ── 新增 NapCat 扩展工具 handlers ──

async def _handle_group_poke(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    return await _napcat_api_call(
        context, "group_poke", f"已在群 {group_id} 戳了 {user_id}",
        group_id=group_id, user_id=user_id,
    )


async def _handle_friend_poke(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    return await _napcat_api_call(context, "friend_poke", f"已戳了 {user_id}", user_id=user_id)


async def _handle_set_msg_emoji_like(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    emoji_id = int(args.get("emoji_id", 0))
    set_flag = args.get("set", True)
    if not message_id or not emoji_id:
        return ToolCallResult(ok=False, error="missing message_id or emoji_id")
    action = "添加" if set_flag else "取消"
    return await _napcat_api_call(
        context, "set_msg_emoji_like", f"已{action}消息 {message_id} 的表情回应",
        message_id=message_id, emoji_id=emoji_id, set=set_flag,
    )


async def _handle_get_group_msg_history(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    kwargs: dict[str, Any] = {"group_id": group_id}
    if args.get("message_seq"):
        kwargs["message_seq"] = int(args["message_seq"])
    result = await _napcat_api_call(context, "get_group_msg_history", f"获取群 {group_id} 聊天记录成功", **kwargs)
    if result.ok and result.data:
        messages = result.data.get("messages") or result.data.get("items", [])
        if isinstance(messages, list):
            lines = []
            for m in messages[-15:]:
                if isinstance(m, dict):
                    sender = m.get("sender", {})
                    nick = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
                    content = str(m.get("raw_message", m.get("message", "")))[:80]
                    lines.append(f"[{nick}] {content}")
            result.display = "\n".join(lines) if lines else "无消息记录"
    return result


async def _handle_get_friend_msg_history(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    kwargs: dict[str, Any] = {"user_id": user_id}
    if args.get("message_seq"):
        kwargs["message_seq"] = int(args["message_seq"])
    if args.get("count"):
        kwargs["count"] = int(args["count"])
    return await _napcat_api_call(context, "get_friend_msg_history", f"获取与 {user_id} 的聊天记录成功", **kwargs)


async def _handle_forward_group_single_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    group_id = int(args.get("group_id", 0))
    if not message_id or not group_id:
        return ToolCallResult(ok=False, error="missing message_id or group_id")
    return await _napcat_api_call(
        context, "forward_group_single_msg", f"已转发消息到群 {group_id}",
        message_id=message_id, group_id=group_id,
    )


async def _handle_forward_friend_single_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    user_id = int(args.get("user_id", 0))
    if not message_id or not user_id:
        return ToolCallResult(ok=False, error="missing message_id or user_id")
    return await _napcat_api_call(
        context, "forward_friend_single_msg", f"已转发消息给 {user_id}",
        message_id=message_id, user_id=user_id,
    )


async def _handle_get_essence_msg_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_essence_msg_list", f"获取群 {group_id} 精华消息成功", group_id=group_id)
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        lines = []
        for m in items[:10]:
            if isinstance(m, dict):
                nick = m.get("sender_nick", "?")
                content = str(m.get("content", ""))[:60]
                lines.append(f"[{nick}] {content}")
        result.display = f"共 {len(items)} 条精华:\n" + "\n".join(lines)
    return result


async def _handle_set_essence_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    return await _napcat_api_call(context, "set_essence_msg", f"已将消息 {message_id} 设为精华", message_id=message_id)


async def _handle_ocr_image(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    image = str(args.get("image", ""))
    if not image:
        return ToolCallResult(ok=False, error="missing image")
    result = await _napcat_api_call(context, "ocr_image", "OCR识别成功", image=image)
    if result.ok and result.data:
        texts = result.data.get("texts", [])
        if isinstance(texts, list):
            ocr_text = " ".join(
                str(t.get("text", t) if isinstance(t, dict) else t) for t in texts
            )
            result.display = f"识别结果: {clip_text(ocr_text, 500)}"
    return result


async def _handle_get_ai_characters(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_ai_characters", "获取AI语音角色列表成功", group_id=group_id)
    if result.ok and result.data:
        items = result.data.get("items", [])
        if isinstance(items, list):
            lines = []
            for group in items[:5]:
                if isinstance(group, dict):
                    for char in (group.get("characters", []) or [])[:5]:
                        if isinstance(char, dict):
                            lines.append(f"{char.get('character_id','?')}: {char.get('character_name','?')}")
            result.display = "可用角色:\n" + "\n".join(lines) if lines else "无可用角色"
    return result


async def _handle_send_group_ai_record(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    character = str(args.get("character", ""))
    text = str(args.get("text", ""))
    if not group_id or not character or not text:
        return ToolCallResult(ok=False, error="missing group_id, character or text")
    return await _napcat_api_call(
        context, "send_group_ai_record", f"已发送AI语音到群 {group_id}",
        group_id=group_id, character=character, text=text,
    )


async def _handle_mark_msg_as_read(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    return await _napcat_api_call(context, "mark_msg_as_read", f"已标记消息 {message_id} 为已读", message_id=message_id)


async def _handle_get_group_shut_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_shut_list", f"获取群 {group_id} 禁言列表成功", group_id=group_id)
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        lines = [str(m.get("user_id", "?")) for m in items[:20] if isinstance(m, dict)]
        result.display = f"当前被禁言 {len(items)} 人: " + ", ".join(lines)
    return result


async def _handle_get_group_member_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    result = await _napcat_api_call(
        context, "get_group_member_info", f"获取 {user_id} 在群 {group_id} 的信息成功",
        group_id=group_id, user_id=user_id,
    )
    if result.ok and result.data:
        d = result.data
        result.display = (
            f"昵称: {d.get('nickname','?')}, 群名片: {d.get('card','')}, "
            f"角色: {d.get('role','?')}, 入群时间: {d.get('join_time','?')}, "
            f"最后发言: {d.get('last_sent_time','?')}"
        )
    return result


async def _handle_set_input_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    event_type = int(args.get("event_type", 1))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    return await _napcat_api_call(
        context, "set_input_status", f"已设置对 {user_id} 的输入状态",
        user_id=user_id, event_type=event_type,
    )


def _guess_download_filename(url: str, fallback: str = "download.bin") -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name.strip()
    if not name:
        return fallback
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    return name or fallback


def _normalize_download_http_url(url: str) -> str:
    """规范化下载 URL，主要修复 path 里未编码空格导致的 404。"""
    raw = normalize_text(url)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if parsed.scheme.lower() not in {"http", "https"}:
        return raw
    safe_path = quote(unquote(parsed.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    safe_query = quote(unquote(parsed.query or ""), safe="=&%:@!$'()*+,;/?-._~")
    return urlunparse((parsed.scheme, parsed.netloc, safe_path, parsed.params, safe_query, ""))


async def _download_file_via_http_fallback(
    url: str,
    *,
    file_name: str = "",
    max_size_mb: float = 1024.0,
) -> str:
    """当 NapCat download_file 失败时，用本地 HTTP 客户端兜底下载。"""
    normalized_url = _normalize_download_http_url(url)
    if not normalized_url or not re.match(r"^https?://", normalized_url, flags=re.IGNORECASE):
        return ""
    from utils.media import download_file

    guessed_name = normalize_text(file_name) or _guess_download_filename(normalized_url, fallback="download.bin")
    guessed_name = re.sub(r"[\\/:*?\"<>|]+", "_", guessed_name).strip() or "download.bin"
    download_dir = Path(tempfile.gettempdir()) / "yukiko_http_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    temp_name = f"{random.randint(100000, 999999)}_{guessed_name}"
    out_path = (download_dir / temp_name).resolve()
    ok = await download_file(
        normalized_url,
        out_path,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=120.0,
        max_size_mb=max_size_mb,
    )
    if not ok:
        out_path.unlink(missing_ok=True)
        return ""
    return str(out_path)


def _is_readable_regular_file(path: str) -> bool:
    raw = normalize_text(path)
    if not raw:
        return False
    try:
        candidate = Path(raw).expanduser()
        if not candidate.exists() or not candidate.is_file():
            return False
        with open(candidate, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


async def _ensure_download_path_readable(
    raw_path: str,
    source_url: str,
    *,
    file_name: str = "",
) -> tuple[str, bool]:
    """确保下载产物对当前进程可读；Linux/NapCat 临时目录无权限时回退 HTTP 下载。"""
    clean = normalize_text(raw_path)
    if _is_readable_regular_file(clean):
        return clean, False
    http_path = await _download_file_via_http_fallback(source_url, file_name=file_name)
    if _is_readable_regular_file(http_path):
        return http_path, True
    return clean, False


def _friendly_download_failure_display(error_text: str, url: str) -> str:
    err = normalize_text(error_text)
    lower = err.lower()
    host = normalize_text(urlparse(url).netloc) or "目标站点"
    if any(token in lower for token in ("enotfound", "getaddrinfo", "name or service not known", "nodename nor servname")):
        return f"下载失败：无法解析下载域名 {host}（DNS 解析失败）。请稍后重试，或切换网络/DNS。"
    if any(token in lower for token in ("connecttimeout", "connect timeout", "timed out", "timeout")):
        return f"下载失败：连接 {host} 超时，可能是源站不稳定或网络受限。请稍后重试。"
    if "connection refused" in lower:
        return f"下载失败：{host} 拒绝连接。"
    if err:
        return f"下载失败：{clip_text(err, 160)}"
    return "下载失败：源站当前不可用，请稍后再试。"


_DOWNLOAD_QUERY_STOPWORDS = {
    "下载", "安装", "安装包", "最新版", "最新", "官网", "官方", "desktop", "windows", "win", "mac", "linux",
    "android", "ios", "apk", "exe", "msi", "zip", "file", "包", "客户端", "版本", "v",
}

_TRUSTED_DISTRIBUTION_HOST_HINTS = (
    "github.com",
    "githubusercontent.com",
    "microsoft.com",
    "apple.com",
    "google.com",
    "steampowered.com",
)
_OFFICIAL_HOST_FAMILY_HINTS: tuple[tuple[str, ...], ...] = (
    ("bilibili.com", "hdslb.net", "biligame.com", "bilivideo.com"),
    ("qq.com", "gtimg.com", "myqcloud.com"),
    ("douyin.com", "bytedance.com", "byteimg.com"),
    ("kuaishou.com", "yximgs.com"),
    ("github.com", "githubusercontent.com", "githubassets.com"),
)


def _extract_download_query_keywords(query: str, *, extra: str = "") -> list[str]:
    text = normalize_text(f"{query} {extra}").lower()
    if not text:
        return []
    raw_tokens: list[str] = []
    raw_tokens.extend(re.findall(r"[a-z0-9][a-z0-9._+-]{1,32}", text))
    raw_tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,10}", text))
    out: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        t = token.strip()
        if not t or t in _DOWNLOAD_QUERY_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:12]


def _is_trusted_distribution_host(host: str) -> bool:
    h = normalize_text(host).lower()
    return bool(h) and any(h == hint or h.endswith(f".{hint}") for hint in _TRUSTED_DISTRIBUTION_HOST_HINTS)


def _is_wrapper_like_host(host: str) -> bool:
    h = normalize_text(host).lower()
    if not h:
        return False
    if _is_trusted_distribution_host(h):
        return False
    if h == "pc.qq.com":
        return True
    if any(hint in h for hint in ("qqpcmgr", "myapp", "2345", "apkpure", "downkuai", "pc6", "cr173")):
        return True
    # Generic mirror-like host cues.
    return bool(re.search(r"(?:^|[.\-])(down|download|xiazai|soft|apk)(?:[.\-]|$)", h))


def _root_domain(host: str) -> str:
    h = normalize_text(host).lower().strip(".")
    if not h:
        return ""
    parts = [item for item in h.split(".") if item]
    if len(parts) <= 2:
        return h
    return ".".join(parts[-2:])


def _same_distribution_family(host_a: str, host_b: str) -> bool:
    a = normalize_text(host_a).lower()
    b = normalize_text(host_b).lower()
    if not a or not b:
        return False
    if a == b or a.endswith(f".{b}") or b.endswith(f".{a}"):
        return True
    ra = _root_domain(a)
    rb = _root_domain(b)
    if ra and rb and ra == rb:
        return True
    for family in _OFFICIAL_HOST_FAMILY_HINTS:
        in_a = any(a == hint or a.endswith(f".{hint}") for hint in family)
        in_b = any(b == hint or b.endswith(f".{hint}") for hint in family)
        if in_a and in_b:
            return True
    return False


def _is_retryable_download_error(error_text: str) -> bool:
    text = normalize_text(error_text).lower()
    if not text:
        return False
    retry_cues = (
        "enotfound",
        "getaddrinfo",
        "name or service not known",
        "nodename nor servname",
        "timeout",
        "timed out",
        "connecttimeout",
        "connection reset",
        "connection aborted",
        "connection refused",
        "temporarily unavailable",
        "network is unreachable",
    )
    return any(cue in text for cue in retry_cues)


def _score_download_source_trust(
    *,
    url: str,
    title: str = "",
    snippet: str = "",
    query: str = "",
    extra_hint: str = "",
) -> tuple[int, list[str]]:
    parsed = urlparse(normalize_text(url))
    host = normalize_text(parsed.netloc).lower()
    path = normalize_text(parsed.path).lower()
    haystack = normalize_text(f"{title} {snippet}").lower()
    score = 0
    reasons: list[str] = []

    if _is_trusted_distribution_host(host):
        score += 6
        reasons.append("trusted_distribution_host")
    elif _is_wrapper_like_host(host):
        score -= 8
        reasons.append("wrapper_like_host")

    if any(token in haystack for token in ("官方", "官网", "official", "publisher", "developer")):
        score += 2
        reasons.append("official_terms")

    keywords = _extract_download_query_keywords(query, extra=extra_hint)
    if keywords:
        host_and_path = f"{host} {path}"
        matched = sum(1 for kw in keywords if kw in host_and_path or kw in haystack)
        if matched:
            score += min(6, matched * 2)
            reasons.append(f"keyword_match:{matched}")

    return score, reasons


def _pick_download_candidates(page_url: str, html_text: str, prefer_ext: str = "") -> list[str]:
    """从下载页里挑更像“真实可下载入口”的链接，尽量避开导航噪音。"""
    ext_tokens = (".apk", ".exe", ".msi", ".zip", ".7z", ".rar", ".mp4", ".mp3", ".m4a", ".wav")
    pref = normalize_text(prefer_ext).lower().strip()
    if pref and not pref.startswith("."):
        pref = f".{pref}"

    # 1) 常规 href/src
    raw_links: list[str] = []
    for m in re.finditer(r"""(?:href|src)\s*=\s*["']([^"'#]+)["']""", html_text, flags=re.IGNORECASE):
        raw = normalize_text(m.group(1))
        if raw:
            raw_links.append(raw)

    # 2) 兜底: 脚本中常见的协议相对下载地址或完整 URL（例如 "//get.xxx.com/download/123"）
    for m in re.finditer(r"""["'](//[^"']+/download/[^"']+)["']""", html_text, flags=re.IGNORECASE):
        raw = normalize_text(m.group(1))
        if raw:
            raw_links.append(raw)
    for m in re.finditer(r"""["'](https?://[^"']+/download/[^"']+)["']""", html_text, flags=re.IGNORECASE):
        raw = normalize_text(m.group(1))
        if raw:
            raw_links.append(raw)
    # 3) 兜底: 脚本变量/JSON 里直接给了文件地址（如 Address:"https://.../setup.exe"）
    for m in re.finditer(
        r"""["'](https?://[^"'\s]+\.(?:apk|exe|msi|zip|7z|rar|mp4|mp3|m4a|wav)(?:\?[^"']*)?)["']""",
        html_text,
        flags=re.IGNORECASE,
    ):
        raw = normalize_text(m.group(1))
        if raw:
            raw_links.append(raw)
    for m in re.finditer(
        r"""["'](//[^"'\s]+\.(?:apk|exe|msi|zip|7z|rar|mp4|mp3|m4a|wav)(?:\?[^"']*)?)["']""",
        html_text,
        flags=re.IGNORECASE,
    ):
        raw = normalize_text(m.group(1))
        if raw:
            raw_links.append(raw)

    candidates: list[tuple[int, str]] = []
    for raw_link in raw_links:
        lower_raw = raw_link.lower()
        if lower_raw.startswith(("javascript:", "mailto:", "tel:")):
            continue
        link = urljoin(page_url, raw_link)
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            continue

        host = normalize_text(parsed.netloc).lower()
        path_query = f"{normalize_text(parsed.path).lower()}?{normalize_text(parsed.query).lower()}"
        lower_link = link.lower()
        score = 0

        # 强信号: 路径/查询里包含下载入口标识
        if any(tok in path_query for tok in ("/download/", "download=", "/dl/", "downfile", "getfile", "apk")):
            score += 8
        # 次强信号: 域名像下载域名
        if host.startswith(("get.", "download.", "down.")) or "download" in host:
            score += 3
        if _is_trusted_distribution_host(host):
            score += 2
        if _is_wrapper_like_host(host):
            score -= 6
        # 文件扩展名
        if any(tok in lower_link for tok in ext_tokens):
            score += 8
        if pref and pref in lower_link:
            score += 10
        # 下载语义词（仅看 path/query，避免把 downkuai 域名误判）
        if any(k in path_query for k in ("download", "setup", "installer", "android", "apk")):
            score += 2

        # 明显噪音路径降权
        if parsed.path in {"/", ""}:
            score -= 6
        if re.search(r"/(game|soft|article|zt|gift|open|company|topdesc)/?$", parsed.path.lower()):
            score -= 5
        if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|webp)(?:$|[?&#])", lower_link):
            score -= 8

        if score < 4:
            continue
        candidates.append((score, link))

    # 按分数优先，其次路径长度（更具体）
    candidates.sort(key=lambda item: (-item[0], -len(urlparse(item[1]).path), len(item[1])))
    out: list[str] = []
    seen: set[str] = set()
    for _, link in candidates:
        key = link.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
        if len(out) >= 8:
            break
    return out


async def _resolve_redirect_download_url(url: str, referer: str = "") -> str:
    """解析短下载入口的 30x 链路，尽量拿到最终直链（不下载大文件主体）。"""
    current = normalize_text(url)
    if not current:
        return ""
    current_ref = normalize_text(referer)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.5),
            follow_redirects=False,
        ) as client:
            for _ in range(4):
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
                if current_ref:
                    headers["Referer"] = current_ref
                try:
                    resp = await client.request("HEAD", current, headers=headers)
                    status = int(resp.status_code)
                    resp_url = str(resp.url)
                    location = normalize_text(str(resp.headers.get("location", "")))
                except Exception:
                    status = 0
                    resp_url = current
                    location = ""

                # 某些站点禁用 HEAD，回退为只取响应头的 GET（Range 0-0）。
                if status in {0, 400, 403, 405, 500, 501}:
                    headers_get = dict(headers)
                    headers_get["Range"] = "bytes=0-0"
                    async with client.stream("GET", current, headers=headers_get) as resp2:
                        status = int(resp2.status_code)
                        resp_url = str(resp2.url)
                        location = normalize_text(str(resp2.headers.get("location", "")))

                if status not in {301, 302, 303, 307, 308}:
                    return resp_url
                if not location:
                    return resp_url
                nxt = urljoin(resp_url, location)
                current_ref = resp_url
                current = nxt
    except Exception:
        return normalize_text(url)
    return current


def _guess_download_os_hint(prefer_ext: str = "", file_name: str = "") -> str:
    ext = normalize_text(prefer_ext).lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if not ext:
        ext = Path(normalize_text(file_name)).suffix.lower()
    if ext in {".dmg", ".pkg"}:
        return "macos"
    if ext in {".appimage", ".deb", ".rpm", ".tar.gz", ".tgz"}:
        return "linux"
    return "windows"


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query_rows = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k == key for k, _ in query_rows):
        query_rows.append((key, value))
    rebuilt = list(parsed)
    rebuilt[4] = urlencode(query_rows)
    return urlunparse(rebuilt)


async def _extract_runtime_download_candidates_from_page(
    page_url: str,
    html_text: str,
    *,
    prefer_ext: str = "",
    file_name: str = "",
) -> list[str]:
    if not html_text:
        return []
    page_parsed = urlparse(page_url)
    page_host = normalize_text(page_parsed.netloc).lower()
    if not page_host:
        return []

    inline_patterns = (
        r"https?://[^\s\"']*download\?[^\s\"']*",
        r"https?://[^\s\"']+/download/[^\s\"']+",
        r"https?://[^\s\"']+\.(?:exe|msi|apk|zip|7z|rar|dmg|pkg)(?:\?[^\s\"']*)?",
        r"//[^\s\"']+/download/[^\s\"']+",
        r"//[^\s\"']+\.(?:exe|msi|apk|zip|7z|rar|dmg|pkg)(?:\?[^\s\"']*)?",
    )
    out: list[str] = []
    seen: set[str] = set()
    for pattern in inline_patterns:
        for raw in re.findall(pattern, html_text, flags=re.IGNORECASE):
            endpoint = normalize_text(raw)
            if not endpoint:
                continue
            if endpoint.startswith("//"):
                endpoint = f"{page_parsed.scheme or 'https'}:{endpoint}"
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"http", "https"}:
                continue
            key = endpoint.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(endpoint)
            if len(out) >= 10:
                return out

    script_srcs = re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html_text, flags=re.IGNORECASE)
    if not script_srcs:
        return out

    scored_scripts: list[tuple[int, str]] = []
    for src in script_srcs:
        script_url = normalize_text(urljoin(page_url, src))
        if not script_url:
            continue
        parsed = urlparse(script_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if normalize_text(parsed.netloc).lower() != page_host:
            continue
        lower = script_url.lower()
        score = 0
        if "download" in lower:
            score += 6
        if lower.endswith(".js"):
            score += 2
        if "/_next/static/" in lower:
            score += 1
        scored_scripts.append((score, script_url))
    if not scored_scripts:
        return out
    scored_scripts.sort(key=lambda item: item[0], reverse=True)

    os_hint = _guess_download_os_hint(prefer_ext=prefer_ext, file_name=file_name)
    endpoint_raw: list[str] = []
    patterns = (
        r"https?://[^\s\"']*download\?[^\s\"']*",
        r"https?://[^\s\"']+/download/[^\s\"']+",
        r"https?://[^\s\"']+\.(?:exe|msi|apk|zip|7z|rar|dmg|pkg)(?:\?[^\s\"']*)?",
    )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True) as client:
            for _, script_url in scored_scripts[:6]:
                try:
                    resp = await client.get(script_url, headers={"User-Agent": "Mozilla/5.0", "Referer": page_url})
                    js_text = normalize_text(resp.text)
                except Exception:
                    continue
                if not js_text:
                    continue
                for pattern in patterns:
                    endpoint_raw.extend(re.findall(pattern, js_text, flags=re.IGNORECASE))
    except Exception:
        return out

    if not endpoint_raw:
        return out

    for raw in endpoint_raw:
        endpoint = normalize_text(raw)
        if not endpoint:
            continue
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"}:
            continue
        endpoint_try = endpoint
        query_text = normalize_text(parsed.query).lower()
        if "download" in endpoint.lower() and "os=" not in query_text:
            endpoint_try = _append_query_param(endpoint_try, "os", os_hint)
        try:
            resolved = await asyncio.wait_for(
                _resolve_redirect_download_url(endpoint_try, referer=page_url),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            resolved = endpoint_try
        for candidate in (normalize_text(resolved), normalize_text(endpoint_try)):
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= 10:
                return out
    return out


def _extract_github_repo_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = normalize_text(parsed.netloc).lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "github.com":
        return ""
    parts = [p for p in normalize_text(parsed.path).split("/") if p]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    reserved = {
        "features", "topics", "marketplace", "orgs", "organizations", "login", "signup", "about", "explore",
        "contact", "pricing", "collections", "sponsors", "apps", "settings", "notifications", "new", "search",
    }
    if owner.lower() in reserved:
        return ""
    repo = repo.removesuffix(".git")
    return f"{owner}/{repo}" if owner and repo else ""


def _score_github_release_asset(name: str, prefer_ext: str = "") -> int:
    lower = normalize_text(name).lower()
    ext = normalize_text(prefer_ext).lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    score = 0
    if ext and lower.endswith(ext):
        score += 9
    if re.search(r"\.(exe|msi|zip|7z|rar|apk|dmg|pkg)(?:$|[?&#])", lower):
        score += 4
    if any(tok in lower for tok in ("installer", "setup", "portable", "windows", "win", "x64", "amd64")):
        score += 2
    if any(tok in lower for tok in ("sha256", "checksum", ".asc", ".sig", ".txt", "source code")):
        score -= 6
    return score


def _github_asset_matches_prefer_ext(name: str, prefer_ext: str = "") -> bool:
    ext = normalize_text(prefer_ext).lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if not ext:
        return True
    lower = normalize_text(name).lower()
    if ext == ".exe":
        return lower.endswith(".exe") or lower.endswith(".msi")
    if ext in {".zip", ".7z", ".rar"}:
        return lower.endswith(".zip") or lower.endswith(".7z") or lower.endswith(".rar")
    return lower.endswith(ext)


def _resolve_github_api_options(config: dict[str, Any] | None) -> tuple[str, dict[str, str]]:
    api_base = "https://api.github.com"
    token = ""
    cfg = config if isinstance(config, dict) else {}
    tool_iface_cfg: dict[str, Any] = {}
    if isinstance(cfg.get("tool_interface"), dict):
        tool_iface_cfg = cfg.get("tool_interface", {})
    elif isinstance(cfg.get("tools"), dict) and isinstance(cfg.get("tools", {}).get("tool_interface"), dict):
        tool_iface_cfg = cfg.get("tools", {}).get("tool_interface", {})
    if isinstance(tool_iface_cfg, dict):
        api_base = normalize_text(str(tool_iface_cfg.get("github_api_base", api_base))) or api_base
        token = normalize_text(str(tool_iface_cfg.get("github_token", "")))
    api_base = api_base.rstrip("/")
    headers = {
        "User-Agent": "YukikoBot/1.0 (+https://github.com)",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return api_base, headers


async def _resolve_github_release_direct_url(
    url: str,
    *,
    prefer_ext: str = "",
    config: dict[str, Any] | None = None,
) -> str:
    parsed = urlparse(url)
    host = normalize_text(parsed.netloc).lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "github.com":
        return ""
    path_lower = normalize_text(parsed.path).lower()
    if "/releases/download/" in path_lower:
        return normalize_text(url)

    repo = _extract_github_repo_from_url(url)
    if not repo:
        return ""
    api_base, headers = _resolve_github_api_options(config)

    def pick_asset_url(release_obj: dict[str, Any]) -> str:
        assets = release_obj.get("assets")
        if not isinstance(assets, list):
            return ""
        picked_url = ""
        picked_score = -10_000
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            asset_url = normalize_text(str(asset.get("browser_download_url", "")))
            asset_name = normalize_text(str(asset.get("name", "")))
            if not asset_url:
                continue
            if not _github_asset_matches_prefer_ext(asset_name, prefer_ext=prefer_ext):
                continue
            score = _score_github_release_asset(asset_name, prefer_ext=prefer_ext)
            if score > picked_score:
                picked_score = score
                picked_url = asset_url
        return picked_url if picked_score >= 0 else ""

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=6.0), follow_redirects=True) as client:
            for endpoint in (
                f"{api_base}/repos/{repo}/releases/latest",
                f"{api_base}/repos/{repo}/releases?per_page=6",
            ):
                try:
                    resp = await client.get(endpoint, headers=headers)
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue
                releases: list[dict[str, Any]] = []
                if isinstance(data, dict):
                    releases = [data]
                elif isinstance(data, list):
                    releases = [item for item in data if isinstance(item, dict)]
                for release_obj in releases:
                    asset_url = pick_asset_url(release_obj)
                    if asset_url:
                        return asset_url
                    zipball = normalize_text(str(release_obj.get("zipball_url", "")))
                    if zipball and (not prefer_ext or normalize_text(prefer_ext).lower() in {"zip", ".zip"}):
                        return zipball

            # 没有 release 资产时，回退到默认分支源码 zip（仅当用户没指定可执行扩展）。
            ext = normalize_text(prefer_ext).lower().strip()
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            if not ext or ext in {".zip", ".7z", ".rar"}:
                repo_resp = await client.get(f"{api_base}/repos/{repo}", headers=headers)
                if repo_resp.status_code == 200:
                    try:
                        repo_data = repo_resp.json()
                    except Exception:
                        repo_data = {}
                    default_branch = normalize_text(str(repo_data.get("default_branch", ""))) or "main"
                    return f"https://github.com/{repo}/archive/refs/heads/{default_branch}.zip"
    except Exception:
        return ""
    return ""


def _stage_download_file(raw_path: str, source_url: str, file_name: str = "") -> str:
    raw = normalize_text(raw_path)
    if not raw:
        return ""
    try:
        src = Path(raw).expanduser()
        if not src.exists() or not src.is_file():
            return ""
    except Exception as exc:
        _log.warning("stage_download_file_inaccessible | path=%s | err=%s", clip_text(raw, 180), exc)
        return ""
    project_root = Path(__file__).resolve().parents[1]
    target_dir = (project_root / "storage" / "tmp" / "downloads").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = normalize_text(file_name) or src.name or _guess_download_filename(source_url)
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    if not name:
        name = _guess_download_filename(source_url)
    suffix = Path(name).suffix or src.suffix or ".bin"
    stem = Path(name).stem or "download"
    dst = target_dir / f"{stem}{suffix}"
    if dst.exists():
        dst = target_dir / f"{stem}_{int(random.randint(1000, 9999))}{suffix}"
    try:
        shutil.copy2(src, dst)
    except Exception as exc:
        _log.warning(
            "stage_download_file_copy_failed | src=%s | dst=%s | err=%s",
            clip_text(str(src), 180),
            clip_text(str(dst), 180),
            exc,
        )
        return ""
    return str(dst)


def _read_file_head(path: str, size: int = 8192) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except Exception:
        return b""


def _looks_like_html_payload(head: bytes) -> bool:
    if not head:
        return False
    lower = head.lower().lstrip()
    if lower.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return True
    try:
        text = head.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False
    return "<html" in text and ("<head" in text or "<body" in text or "doctype html" in text)


def _guess_expected_ext(prefer_ext: str = "", file_name: str = "", candidate_url: str = "") -> str:
    ext = normalize_text(prefer_ext).lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if ext:
        return ext
    name_ext = Path(normalize_text(file_name)).suffix.lower()
    if name_ext:
        return name_ext
    url_ext = Path(urlparse(candidate_url).path).suffix.lower()
    return url_ext


def _matches_expected_signature(expected_ext: str, head: bytes) -> bool:
    if not expected_ext or not head:
        return True
    ext = expected_ext.lower()
    if ext == ".exe" or ext == ".msi":
        return head.startswith(b"MZ")
    if ext in {".apk", ".zip"}:
        return head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    if ext == ".rar":
        return head.startswith(b"Rar!\x1A\x07")
    if ext == ".7z":
        return head.startswith(b"7z\xBC\xAF\x27\x1C")
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    if ext == ".mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if ext in {".mp4", ".m4a"}:
        return len(head) >= 12 and b"ftyp" in head[:64]
    if ext == ".wav":
        return len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WAVE"
    return True


def _extract_download_candidates_from_html_file(source_url: str, raw_path: str, prefer_ext: str = "") -> list[str]:
    try:
        text = Path(raw_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if not text:
        return []
    return _pick_download_candidates(source_url, text, prefer_ext=prefer_ext)


async def _handle_smart_download(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """统一母下载入口：优先解析媒体，再下载并落地到可上传目录。"""
    original_url = normalize_text(str(args.get("url", "")))
    if not original_url:
        return ToolCallResult(ok=False, error="missing url")

    query = normalize_text(str(args.get("query", "")))
    kind = normalize_text(str(args.get("kind", "auto"))).lower() or "auto"  # auto/video/audio/file
    prefer_ext = normalize_text(str(args.get("prefer_ext", ""))).lower()
    upload_after = bool(args.get("upload", False))
    allow_third_party = bool(args.get("allow_third_party", False))
    file_name = normalize_text(str(args.get("file_name", "")))
    group_id = int(args.get("group_id", 0) or context.get("group_id", 0) or 0)
    try:
        thread_count = max(1, min(6, int(args.get("thread_count", 1) or 1)))
    except Exception:
        thread_count = 1

    tool_executor = context.get("tool_executor")
    candidate_url = original_url
    resolve_note = ""
    candidate_pool: list[str] = []
    candidate_seen: set[str] = set()

    def _push_candidate(url: str) -> None:
        clean = normalize_text(url)
        if not clean:
            return
        if re.match(r"^https?://", clean, flags=re.IGNORECASE):
            clean = _normalize_download_http_url(clean)
        key = clean.lower()
        if key in candidate_seen:
            return
        candidate_seen.add(key)
        candidate_pool.append(clean)

    _push_candidate(original_url)

    # 1) 视频/音频优先走解析链，避免直接下到网页壳
    if kind in {"auto", "video", "audio"} and tool_executor and re.match(r"^https?://", original_url, flags=re.IGNORECASE):
        try:
            resolved = await tool_executor._method_browser_resolve_video(
                method_name="smart_download",
                method_args={"url": original_url},
                query=original_url,
            )
            payload = resolved.payload or {}
            if resolved.ok and normalize_text(str(payload.get("video_url", ""))):
                candidate_url = normalize_text(str(payload.get("video_url", "")))
                resolve_note = "via_video_resolver"
                _push_candidate(candidate_url)
        except Exception as exc:
            _log.warning("smart_download_resolve_video_error | url=%s | %s", original_url[:120], exc)

    # 2) 如果还是网页链接，尝试抓网页提取真实下载地址
    parsed = urlparse(candidate_url)
    path_lower = normalize_text(parsed.path).lower()
    is_direct_file = bool(re.search(r"\.(apk|exe|msi|zip|7z|rar|mp4|mp3|m4a|wav)(?:$|[?&#])", path_lower))
    if re.match(r"^https?://", candidate_url, flags=re.IGNORECASE) and not is_direct_file:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=6.0), follow_redirects=True) as client:
                resp = await asyncio.wait_for(
                    client.get(candidate_url, headers={"User-Agent": "Mozilla/5.0"}),
                    timeout=8.0,
                )
            ctype = normalize_text(str(resp.headers.get("content-type", ""))).lower()
            text = resp.text if "text/html" in ctype or "<html" in resp.text[:2000].lower() else ""
            if text:
                picks = _pick_download_candidates(str(resp.url), text, prefer_ext=prefer_ext)
                if picks:
                    for pick in picks:
                        _push_candidate(pick)
                    candidate_url = picks[0]
                    resolve_note = "via_webpage_extract"
                else:
                    runtime_picks = await _extract_runtime_download_candidates_from_page(
                        str(resp.url),
                        text,
                        prefer_ext=prefer_ext,
                        file_name=file_name,
                    )
                    if runtime_picks:
                        for pick in runtime_picks:
                            _push_candidate(pick)
                        candidate_url = runtime_picks[0]
                        resolve_note = "via_runtime_script_extract"
        except Exception as exc:
            _log.warning("smart_download_extract_error | url=%s | %s", candidate_url[:120], exc)

    # 2.5) 典型下载中转链接（如 /download/123）先做重定向链解析，拿最终直链
    parsed = urlparse(candidate_url)
    host_lower = normalize_text(parsed.netloc).lower()
    path_lower = normalize_text(parsed.path).lower()
    if re.match(r"^https?://", candidate_url, flags=re.IGNORECASE) and (
        "/download/" in path_lower or host_lower.startswith("get.")
    ):
        try:
            resolved_final = await asyncio.wait_for(
                _resolve_redirect_download_url(candidate_url, referer=original_url),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            resolved_final = candidate_url
        resolved_final = normalize_text(resolved_final)
        if resolved_final and resolved_final != candidate_url:
            candidate_url = resolved_final
            resolve_note = "via_redirect_resolver"
            _push_candidate(candidate_url)

    # 2.6) GitHub 仓库/Release 页面优先走 Release API 拿真实资产链接。
    if re.match(r"^https?://", candidate_url, flags=re.IGNORECASE):
        gh_direct_url = await _resolve_github_release_direct_url(
            candidate_url,
            prefer_ext=prefer_ext,
            config=context.get("config"),
        )
        gh_direct_url = normalize_text(gh_direct_url)
        if gh_direct_url and gh_direct_url != candidate_url:
            candidate_url = gh_direct_url
            resolve_note = "via_github_release_api"
            _push_candidate(candidate_url)

    candidate_url = _normalize_download_http_url(candidate_url) if re.match(
        r"^https?://", candidate_url, flags=re.IGNORECASE
    ) else candidate_url
    _push_candidate(candidate_url)

    expected_ext = _guess_expected_ext(prefer_ext=prefer_ext, file_name=file_name, candidate_url=candidate_url)
    install_exts = {".exe", ".msi", ".apk", ".zip", ".7z", ".rar"}
    if expected_ext in install_exts:
        trust_score, trust_reasons = _score_download_source_trust(
            url=candidate_url,
            query=query,
            extra_hint=file_name,
        )
        source_score, source_reasons = _score_download_source_trust(
            url=original_url,
            query=query,
            extra_hint=file_name,
        )
        worst_score = min(trust_score, source_score)
        if worst_score <= -5:
            reason = ",".join(trust_reasons if trust_score <= source_score else source_reasons) or "low_trust_source"
            return ToolCallResult(
                ok=False,
                error=f"download_untrusted_source:{reason}",
                display=(
                    "检测到疑似下载中转/分发站，已拦截本次安装包下载。"
                    f" source={candidate_url}"
                ),
            )

    # 3) 用 NapCat 下载，再复制到可上传白名单目录
    download_attempts: list[str] = [candidate_url]
    for item in candidate_pool:
        if item.lower() == candidate_url.lower():
            continue
        download_attempts.append(item)
    download_attempts = download_attempts[:8]

    raw_path = ""
    last_download_error = ""
    last_download_display = ""
    source_host = normalize_text(urlparse(original_url).netloc).lower()

    for idx, attempt_url in enumerate(download_attempts):
        attempt_host = normalize_text(urlparse(attempt_url).netloc).lower()
        is_cross_host = bool(source_host and attempt_host and not _same_distribution_family(source_host, attempt_host))
        if expected_ext in install_exts and is_cross_host and not allow_third_party:
            return ToolCallResult(
                ok=False,
                error=f"download_requires_user_consent:third_party:{attempt_host or '-'}",
                display=(
                    "检测到将切换到第三方下载源，已暂停执行。"
                    f" 当前源: {attempt_host or attempt_url}\n"
                    "请先征求用户确认是否允许第三方下载；用户明确同意后，重试 smart_download 并传 allow_third_party=true。"
                ),
            )

        dl = await _napcat_api_call(
            context,
            "download_file",
            "文件下载成功",
            url=attempt_url,
            thread_count=thread_count,
        )
        raw_path = normalize_text(str((dl.data or {}).get("file", ""))) if dl.ok else ""
        if not raw_path:
            # NapCat 对部分 URL（尤其是包含空格/重定向链复杂的下载链接）会直接失败，走本地 HTTP 兜底。
            http_fallback_path = await _download_file_via_http_fallback(attempt_url, file_name=file_name)
            if http_fallback_path:
                raw_path = http_fallback_path
                candidate_url = attempt_url
                resolve_note = "via_http_fallback" if not resolve_note else f"{resolve_note}|via_http_fallback"
                break

            last_download_error = dl.error or "download_failed"
            last_download_display = _friendly_download_failure_display(dl.error or dl.display, attempt_url)
            if idx + 1 < len(download_attempts) and _is_retryable_download_error(last_download_error):
                _log.info(
                    "smart_download_retry_next_candidate | from=%s | err=%s | next=%s",
                    attempt_url[:140],
                    clip_text(last_download_error, 120),
                    download_attempts[idx + 1][:140],
                )
                continue
            return ToolCallResult(
                ok=False,
                error=last_download_error,
                display=last_download_display,
            )

        ensured_path, used_http_permission_fallback = await _ensure_download_path_readable(
            raw_path,
            attempt_url,
            file_name=file_name,
        )
        if used_http_permission_fallback:
            raw_path = ensured_path
            candidate_url = attempt_url
            resolve_note = "via_http_permission_fallback" if not resolve_note else f"{resolve_note}|via_http_permission_fallback"
        elif ensured_path:
            raw_path = ensured_path
        if not _is_readable_regular_file(raw_path):
            last_download_error = "download_path_permission_denied"
            last_download_display = f"下载完成但当前进程无法读取文件：{clip_text(raw_path, 140)}"
            if idx + 1 < len(download_attempts):
                continue
            return ToolCallResult(ok=False, error=last_download_error, display=last_download_display)

        candidate_url = attempt_url
        break

    if not raw_path:
        if last_download_error:
            return ToolCallResult(ok=False, error=last_download_error, display=last_download_display or "下载失败")
        return ToolCallResult(ok=False, error="download_path_missing", display="下载成功但没有返回本地路径")

    # 3.5) 安全防线：拦截“HTML 伪装成安装包/可执行文件”，并做多候选自动纠正。
    head = _read_file_head(raw_path)
    is_html_payload = _looks_like_html_payload(head)
    if is_html_payload:
        retry_queue: list[str] = []
        retry_seen: set[str] = set()

        def _push_retry_link(url: str) -> None:
            clean = normalize_text(url)
            if not clean:
                return
            if re.match(r"^https?://", clean, flags=re.IGNORECASE):
                clean = _normalize_download_http_url(clean)
            key = clean.lower()
            if key in retry_seen or key == normalize_text(candidate_url).lower():
                return
            retry_seen.add(key)
            retry_queue.append(clean)

        def _read_html_text(path: str) -> str:
            try:
                return Path(path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return ""

        def _collect_retry_links_from_html(source_url: str, html_path: str, html_text: str) -> None:
            for retry_link in _extract_download_candidates_from_html_file(
                source_url,
                html_path,
                prefer_ext=prefer_ext,
            ):
                _push_retry_link(retry_link)
            if html_text:
                for retry_link in _pick_download_candidates(source_url, html_text, prefer_ext=prefer_ext):
                    _push_retry_link(retry_link)

        seed_html_text = _read_html_text(raw_path)
        _collect_retry_links_from_html(candidate_url, raw_path, seed_html_text)
        runtime_retry_links = await _extract_runtime_download_candidates_from_page(
            candidate_url,
            seed_html_text,
            prefer_ext=prefer_ext,
            file_name=file_name,
        )
        for retry_link in runtime_retry_links:
            _push_retry_link(retry_link)

        retries = 0
        while retry_queue and is_html_payload and retries < 8:
            retries += 1
            retry_url = retry_queue.pop(0)

            retry_parsed = urlparse(retry_url)
            retry_host = normalize_text(retry_parsed.netloc).lower()
            retry_path_lower = normalize_text(retry_parsed.path).lower()
            if "/download/" in retry_path_lower or retry_host.startswith("get."):
                try:
                    retry_resolved = await asyncio.wait_for(
                        _resolve_redirect_download_url(retry_url, referer=candidate_url),
                        timeout=8.0,
                    )
                except asyncio.TimeoutError:
                    retry_resolved = retry_url
                retry_resolved = normalize_text(retry_resolved)
                if retry_resolved and retry_resolved != retry_url:
                    retry_url = _normalize_download_http_url(retry_resolved)
                    _push_retry_link(retry_url)
                    retry_parsed = urlparse(retry_url)
                    retry_host = normalize_text(retry_parsed.netloc).lower()

            is_cross_retry_host = bool(
                source_host and retry_host and not _same_distribution_family(source_host, retry_host)
            )
            if expected_ext in install_exts and is_cross_retry_host and not allow_third_party:
                return ToolCallResult(
                    ok=False,
                    error=f"download_requires_user_consent:third_party:{retry_host or '-'}",
                    display=(
                        "检测到将切换到第三方下载源，已暂停执行。"
                        f" 当前源: {retry_host or retry_url}\n"
                        "请先征求用户确认是否允许第三方下载；用户明确同意后，重试 smart_download 并传 allow_third_party=true。"
                    ),
                )

            dl_retry = await _napcat_api_call(
                context,
                "download_file",
                "文件下载成功",
                url=retry_url,
                thread_count=thread_count,
            )
            retry_path = normalize_text(str((dl_retry.data or {}).get("file", ""))) if dl_retry.ok else ""
            if not retry_path:
                http_retry_path = await _download_file_via_http_fallback(retry_url, file_name=file_name)
                if http_retry_path:
                    retry_path = http_retry_path
                    resolve_note = "via_http_fallback" if not resolve_note else f"{resolve_note}|via_http_fallback"
            if not retry_path:
                continue

            ensured_retry_path, used_retry_permission_fallback = await _ensure_download_path_readable(
                retry_path,
                retry_url,
                file_name=file_name,
            )
            if used_retry_permission_fallback:
                retry_path = ensured_retry_path
                resolve_note = "via_http_permission_fallback" if not resolve_note else f"{resolve_note}|via_http_permission_fallback"
            elif ensured_retry_path:
                retry_path = ensured_retry_path
            if not _is_readable_regular_file(retry_path):
                continue

            retry_head = _read_file_head(retry_path)
            if _looks_like_html_payload(retry_head):
                nested_html_text = _read_html_text(retry_path)
                _collect_retry_links_from_html(retry_url, retry_path, nested_html_text)
                nested_runtime_links = await _extract_runtime_download_candidates_from_page(
                    retry_url,
                    nested_html_text,
                    prefer_ext=prefer_ext,
                    file_name=file_name,
                )
                for retry_link in nested_runtime_links:
                    _push_retry_link(retry_link)
                continue

            candidate_url = retry_url
            raw_path = retry_path
            head = retry_head
            is_html_payload = False
            resolve_note = "via_downloaded_html_retry" if not resolve_note else f"{resolve_note}|via_downloaded_html_retry"
            break

    if is_html_payload:
        return ToolCallResult(
            ok=False,
            error="download_payload_is_html",
            display=(
                "下载到的是网页(HTML)不是文件本体，已拦截。"
                f" 当前URL: {candidate_url}"
            ),
        )

    if expected_ext and not _matches_expected_signature(expected_ext, head):
        mismatch_retry_urls: list[str] = []
        mismatch_seen: set[str] = set()

        def _push_mismatch_retry(url: str) -> None:
            clean = normalize_text(url)
            if not clean:
                return
            if re.match(r"^https?://", clean, flags=re.IGNORECASE):
                clean = _normalize_download_http_url(clean)
            key = clean.lower()
            if key in mismatch_seen or key == normalize_text(candidate_url).lower():
                return
            mismatch_seen.add(key)
            mismatch_retry_urls.append(clean)

        for retry_url in download_attempts:
            _push_mismatch_retry(retry_url)
        for retry_url in _extract_download_candidates_from_html_file(candidate_url, raw_path, prefer_ext=expected_ext):
            _push_mismatch_retry(retry_url)

        mismatch_fixed = False
        for retry_url in mismatch_retry_urls[:8]:
            retry_parsed = urlparse(retry_url)
            retry_host = normalize_text(retry_parsed.netloc).lower()
            retry_path_lower = normalize_text(retry_parsed.path).lower()
            if "/download/" in retry_path_lower or retry_host.startswith("get."):
                try:
                    retry_resolved = await asyncio.wait_for(
                        _resolve_redirect_download_url(retry_url, referer=candidate_url),
                        timeout=8.0,
                    )
                except asyncio.TimeoutError:
                    retry_resolved = retry_url
                retry_resolved = normalize_text(retry_resolved)
                if retry_resolved and retry_resolved != retry_url:
                    retry_url = _normalize_download_http_url(retry_resolved)
                    retry_parsed = urlparse(retry_url)
                    retry_host = normalize_text(retry_parsed.netloc).lower()

            is_cross_retry_host = bool(
                source_host and retry_host and not _same_distribution_family(source_host, retry_host)
            )
            if expected_ext in install_exts and is_cross_retry_host and not allow_third_party:
                return ToolCallResult(
                    ok=False,
                    error=f"download_requires_user_consent:third_party:{retry_host or '-'}",
                    display=(
                        "检测到将切换到第三方下载源，已暂停执行。"
                        f" 当前源: {retry_host or retry_url}\n"
                        "请先征求用户确认是否允许第三方下载；用户明确同意后，重试 smart_download 并传 allow_third_party=true。"
                    ),
                )

            dl_retry = await _napcat_api_call(
                context,
                "download_file",
                "文件下载成功",
                url=retry_url,
                thread_count=thread_count,
            )
            retry_path = normalize_text(str((dl_retry.data or {}).get("file", ""))) if dl_retry.ok else ""
            if not retry_path:
                retry_path = await _download_file_via_http_fallback(retry_url, file_name=file_name)
                if retry_path:
                    resolve_note = "via_http_fallback" if not resolve_note else f"{resolve_note}|via_http_fallback"
            if not retry_path:
                continue

            ensured_retry_path, used_retry_permission_fallback = await _ensure_download_path_readable(
                retry_path,
                retry_url,
                file_name=file_name,
            )
            if used_retry_permission_fallback:
                retry_path = ensured_retry_path
                resolve_note = "via_http_permission_fallback" if not resolve_note else f"{resolve_note}|via_http_permission_fallback"
            elif ensured_retry_path:
                retry_path = ensured_retry_path
            if not _is_readable_regular_file(retry_path):
                continue

            retry_head = _read_file_head(retry_path)
            if _looks_like_html_payload(retry_head):
                continue
            if not _matches_expected_signature(expected_ext, retry_head):
                continue

            candidate_url = retry_url
            raw_path = retry_path
            head = retry_head
            mismatch_fixed = True
            resolve_note = "via_signature_retry" if not resolve_note else f"{resolve_note}|via_signature_retry"
            break

        if not mismatch_fixed:
            return ToolCallResult(
                ok=False,
                error="download_signature_mismatch",
                display=(
                    f"下载文件头与期望扩展名不匹配 (expected={expected_ext})，已拦截。"
                    f" 当前URL: {candidate_url}"
                ),
            )

    staged_path = _stage_download_file(raw_path, candidate_url, file_name=file_name)
    if not staged_path:
        _log.warning("stage_download_file_failed | raw_path=%s | source=%s", clip_text(raw_path, 180), clip_text(candidate_url, 180))
        return ToolCallResult(ok=False, error="stage_download_file_failed", display="下载完成，但整理到上传目录失败")

    payload = {
        "source_url": original_url,
        "download_url": candidate_url,
        "local_file": staged_path,
        "resolve_note": resolve_note,
    }
    display_lines = [
        f"下载完成: {Path(staged_path).name}",
        f"下载URL: {candidate_url}",
        f"本地路径: {staged_path}",
    ]

    # 4) 可选：直接上传到群文件
    if upload_after:
        if not group_id:
            return ToolCallResult(ok=False, error="missing group_id_for_upload", display="需要 group_id 才能上传群文件")
        upload_name = file_name or Path(staged_path).name
        up = await _napcat_api_call(
            context,
            "upload_group_file",
            f"已上传文件 {upload_name} 到群 {group_id}",
            group_id=group_id,
            file=staged_path,
            name=upload_name,
        )
        if not up.ok:
            return ToolCallResult(
                ok=False,
                error=up.error or "upload_failed",
                data=payload,
                display=f"下载成功但上传失败: {up.error or up.display}",
            )
        payload["uploaded"] = True
        payload["upload_group_id"] = group_id
        display_lines.append(f"群文件上传成功: group={group_id}")

    return ToolCallResult(ok=True, data=payload, display="\n".join(display_lines))


async def _handle_download_file(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    """兼容旧接口：内部转到 smart_download。"""
    compat_args = {
        "url": args.get("url", ""),
        "thread_count": args.get("thread_count", 1),
        "kind": args.get("kind", "auto"),
        "prefer_ext": args.get("prefer_ext", ""),
        "query": args.get("query", ""),
        "upload": bool(args.get("upload", False)),
        "group_id": args.get("group_id", 0),
        "file_name": args.get("file_name", ""),
        "allow_third_party": bool(args.get("allow_third_party", False)),
    }
    return await _handle_smart_download(compat_args, context)


async def _handle_nc_get_user_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    result = await _napcat_api_call(context, "nc_get_user_status", f"查询 {user_id} 在线状态成功", user_id=user_id)
    if result.ok and result.data:
        status = result.data.get("status", "unknown")
        result.display = f"用户 {user_id} 状态: {status}"
    return result


async def _handle_translate_en2zh(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    words = args.get("words", [])
    if not words:
        return ToolCallResult(ok=False, error="missing words")
    if isinstance(words, str):
        words = [words]
    result = await _napcat_api_call(context, "translate_en2zh", "翻译成功", words=words)
    if result.ok and result.data:
        result.display = f"翻译结果: {json.dumps(result.data, ensure_ascii=False)[:300]}"
    return result


# ── 新增 handlers: 合并转发 / 群操作 / 请求处理 / 文件 / NapCat扩展 ──

async def _handle_send_group_forward_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    messages = args.get("messages", [])
    if not group_id or not messages:
        return ToolCallResult(ok=False, error="missing group_id or messages")
    return await _napcat_api_call(
        context, "send_group_forward_msg", f"已发送合并转发到群 {group_id}",
        group_id=group_id, messages=messages,
    )


async def _handle_send_private_forward_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    messages = args.get("messages", [])
    if not user_id or not messages:
        return ToolCallResult(ok=False, error="missing user_id or messages")
    return await _napcat_api_call(
        context, "send_private_forward_msg", f"已发送合并转发给 {user_id}",
        user_id=user_id, messages=messages,
    )


async def _handle_get_forward_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = str(args.get("message_id", ""))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    result = await _napcat_api_call(context, "get_forward_msg", "获取合并转发内容成功", message_id=message_id)
    if result.ok and result.data:
        msgs = result.data.get("messages") or result.data.get("items", [])
        if isinstance(msgs, list):
            lines = []
            for m in msgs[:15]:
                if isinstance(m, dict):
                    sender = m.get("sender", {})
                    nick = sender.get("nickname", str(sender.get("user_id", "?")))
                    content = str(m.get("content", ""))[:80]
                    lines.append(f"[{nick}] {content}")
            result.display = "\n".join(lines) if lines else "转发内容为空"
    return result


async def _handle_set_group_leave(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    is_dismiss = bool(args.get("is_dismiss", False))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    action = "解散" if is_dismiss else "退出"
    return await _napcat_api_call(
        context, "set_group_leave", f"已{action}群 {group_id}",
        group_id=group_id, is_dismiss=is_dismiss,
    )


async def _handle_delete_essence_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    return await _napcat_api_call(context, "delete_essence_msg", f"已取消消息 {message_id} 的精华", message_id=message_id)


async def _handle_get_group_at_all_remain(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_at_all_remain", f"获取群 {group_id} @全体剩余次数成功", group_id=group_id)
    if result.ok and result.data:
        remain = result.data.get("can_at_all", "?")
        remain_count = result.data.get("remain_at_all_count_for_group", "?")
        result.display = f"可@全体: {remain}, 群剩余次数: {remain_count}"
    return result


async def _handle_set_friend_add_request(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    flag = str(args.get("flag", ""))
    if not flag:
        return ToolCallResult(ok=False, error="missing flag")
    approve = args.get("approve", True)
    kwargs: dict[str, Any] = {"flag": flag, "approve": approve}
    if args.get("remark"):
        kwargs["remark"] = str(args["remark"])
    action = "同意" if approve else "拒绝"
    return await _napcat_api_call(context, "set_friend_add_request", f"已{action}好友请求", **kwargs)


async def _handle_set_group_add_request(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    flag = str(args.get("flag", ""))
    sub_type = str(args.get("sub_type", "add"))
    if not flag:
        return ToolCallResult(ok=False, error="missing flag")
    approve = args.get("approve", True)
    kwargs: dict[str, Any] = {"flag": flag, "sub_type": sub_type, "approve": approve}
    if args.get("reason"):
        kwargs["reason"] = str(args["reason"])
    action = "同意" if approve else "拒绝"
    return await _napcat_api_call(context, "set_group_add_request", f"已{action}加群请求", **kwargs)


async def _handle_delete_friend(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
    return await _napcat_api_call(context, "delete_friend", f"已删除好友 {user_id}", user_id=user_id)


async def _handle_get_group_file_system_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_file_system_info", f"获取群 {group_id} 文件系统信息成功", group_id=group_id)
    if result.ok and result.data:
        d = result.data
        result.display = f"文件数: {d.get('file_count','?')}, 文件夹数: {d.get('folder_count','?')}, 已用: {d.get('used_space','?')}B, 总: {d.get('total_space','?')}B"
    return result


async def _handle_get_group_root_files(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_root_files", f"获取群 {group_id} 根目录文件成功", group_id=group_id)
    if result.ok and result.data:
        files = result.data.get("files") or result.data.get("items", [])
        folders = result.data.get("folders", [])
        lines = []
        for f in (folders or [])[:10]:
            if isinstance(f, dict):
                lines.append(f"[文件夹] {f.get('folder_name','?')} (id:{f.get('folder_id','?')})")
        for f in (files if isinstance(files, list) else [])[:20]:
            if isinstance(f, dict):
                lines.append(f"[文件] {f.get('file_name','?')} ({f.get('file_size','?')}B)")
        result.display = "\n".join(lines) if lines else "群文件为空"
    return result


async def _handle_get_group_file_url(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    file_id = str(args.get("file_id", ""))
    busid = int(args.get("busid", 0))
    if not group_id or not file_id:
        return ToolCallResult(ok=False, error="missing group_id or file_id")
    result = await _napcat_api_call(
        context, "get_group_file_url", "获取文件下载链接成功",
        group_id=group_id, file_id=file_id, busid=busid,
    )
    if result.ok and result.data:
        result.display = f"下载链接: {result.data.get('url', '?')}"
    return result


async def _handle_upload_private_file(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = int(args.get("user_id", 0))
    file_path = str(args.get("file", ""))
    name = str(args.get("name", ""))
    if not user_id or not file_path or not name:
        return ToolCallResult(ok=False, error="missing user_id, file or name")
    return await _napcat_api_call(
        context, "upload_private_file", f"已发送文件 {name} 给 {user_id}",
        user_id=user_id, file=file_path, name=name,
    )


async def _handle_set_qq_avatar(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    file = str(args.get("file", ""))
    if not file:
        return ToolCallResult(ok=False, error="missing file")
    return await _napcat_api_call(context, "set_qq_avatar", "已更新QQ头像", file=file)


async def _handle_set_group_portrait(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    file = str(args.get("file", ""))
    if not group_id or not file:
        return ToolCallResult(ok=False, error="missing group_id or file")
    return await _napcat_api_call(
        context, "set_group_portrait", f"已更新群 {group_id} 头像",
        group_id=group_id, file=file,
    )


async def _handle_set_online_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    status = int(args.get("status", 11))
    ext_status = int(args.get("ext_status", 0))
    battery_status = int(args.get("battery_status", 0))
    status_names = {11: "在线", 21: "离开", 31: "隐身", 41: "忙碌", 50: "Q我吧", 60: "请勿打扰"}
    name = status_names.get(status, f"状态{status}")
    return await _napcat_api_call(
        context, "set_online_status", f"已设置在线状态为: {name}",
        status=status, ext_status=ext_status, battery_status=battery_status,
    )


async def _handle_send_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message = str(args.get("message", ""))
    if not message:
        return ToolCallResult(ok=False, error="missing message")
    kwargs: dict[str, Any] = {"message": message}
    msg_type = str(args.get("message_type", ""))
    if msg_type:
        kwargs["message_type"] = msg_type
    if args.get("user_id"):
        kwargs["user_id"] = int(args["user_id"])
    if args.get("group_id"):
        kwargs["group_id"] = int(args["group_id"])
    return await _napcat_api_call(context, "send_msg", "消息发送成功", **kwargs)


async def _handle_check_url_safely(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    url = str(args.get("url", ""))
    if not url:
        return ToolCallResult(ok=False, error="missing url")
    result = await _napcat_api_call(context, "check_url_safely", "URL安全检查完成", url=url)
    if result.ok and result.data:
        level = result.data.get("level", "?")
        result.display = f"安全等级: {level} (1=安全, 2=未知, 3=危险)"
    return result


async def _handle_get_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_status", "获取运行状态成功")
    if result.ok and result.data:
        d = result.data
        result.display = f"在线: {d.get('online','?')}, 状态良好: {d.get('good','?')}"
    return result


async def _handle_get_version_info(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_version_info", "获取版本信息成功")
    if result.ok and result.data:
        d = result.data
        result.display = f"应用: {d.get('app_name','?')}, 版本: {d.get('app_version','?')}, 协议: {d.get('protocol_version','?')}"
    return result


# ── NapCat 扩展 API handlers (第二批) ──


async def _handle_set_self_longnick(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    longnick = str(args.get("longnick", "")).strip()
    if not longnick:
        return ToolCallResult(ok=False, error="missing longnick")
    return await _napcat_api_call(context, "set_self_longnick", f"已设置个性签名: {longnick[:30]}", longNick=longnick)


async def _handle_get_recent_contact(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    count = int(args.get("count", 10) or 10)
    result = await _napcat_api_call(context, "get_recent_contact", "获取最近联系人成功", count=count)
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        lines = [f"最近联系人 ({len(items)}个):"]
        for c in items[:15]:
            if isinstance(c, dict):
                ctype = "群" if c.get("chatType") == 2 else "私聊"
                name = c.get("peerName") or c.get("remark") or str(c.get("peerUin", ""))
                lines.append(f"  [{ctype}] {name}")
        result.display = "\n".join(lines)
    return result


async def _handle_get_profile_like(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_profile_like", "获取点赞列表成功")
    if result.ok and result.data:
        total = result.data.get("total", 0)
        items = result.data.get("items") or result.data.get("favoriteInfo", {}).get("userInfos", [])
        if isinstance(items, list):
            result.display = f"共 {total or len(items)} 人点赞"
        else:
            result.display = f"点赞信息: {json.dumps(result.data, ensure_ascii=False)[:200]}"
    return result


async def _handle_fetch_custom_face(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    count = int(args.get("count", 48) or 48)
    result = await _napcat_api_call(context, "fetch_custom_face", "获取收藏表情成功", count=count)
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        result.display = f"收藏表情共 {len(items)} 个"
    return result


async def _handle_fetch_emoji_like(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    message_id = int(args.get("message_id", 0))
    if not message_id:
        return ToolCallResult(ok=False, error="missing message_id")
    emoji_id = str(args.get("emoji_id", ""))
    emoji_type = str(args.get("emoji_type", ""))
    params: dict[str, Any] = {"message_id": message_id}
    if emoji_id:
        params["emojiId"] = emoji_id
    if emoji_type:
        params["emojiType"] = emoji_type
    result = await _napcat_api_call(context, "fetch_emoji_like", f"获取消息 {message_id} 的表情回应成功", **params)
    return result


async def _handle_get_group_info_ex(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    result = await _napcat_api_call(context, "get_group_info_ex", f"获取群 {group_id} 扩展信息成功", group_id=group_id)
    if result.ok and result.data:
        d = result.data
        parts = [f"群 {group_id}"]
        if d.get("groupName"):
            parts.append(f"名称: {d['groupName']}")
        if d.get("memberCount"):
            parts.append(f"成员: {d['memberCount']}/{d.get('maxMemberCount', '?')}")
        result.display = " | ".join(parts)
    return result


async def _handle_get_group_files_by_folder(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    folder_id = str(args.get("folder_id", "")).strip()
    if not group_id:
        return ToolCallResult(ok=False, error="missing group_id")
    if not folder_id:
        return ToolCallResult(ok=False, error="missing folder_id, 请先用 get_group_root_files 获取文件夹ID")
    result = await _napcat_api_call(
        context, "get_group_files_by_folder", f"获取群 {group_id} 文件夹内容成功",
        group_id=group_id, folder_id=folder_id,
    )
    if result.ok and result.data:
        files = result.data.get("files") or result.data.get("items") or []
        folders = result.data.get("folders") or []
        lines = [f"文件夹 {folder_id} 内容:"]
        for f in (folders if isinstance(folders, list) else [])[:10]:
            if isinstance(f, dict):
                lines.append(f"  [文件夹] {f.get('folder_name', '?')} (id: {f.get('folder_id', '?')})")
        for f in (files if isinstance(files, list) else [])[:20]:
            if isinstance(f, dict):
                name = f.get("file_name") or f.get("name") or "?"
                size = f.get("file_size") or f.get("size") or 0
                size_mb = int(size) / 1024 / 1024 if size else 0
                lines.append(f"  [文件] {name} ({size_mb:.1f}MB)")
        result.display = "\n".join(lines)
    return result


async def _handle_delete_group_file(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    file_id = str(args.get("file_id", "")).strip()
    busid = int(args.get("busid", 0))
    if not group_id or not file_id:
        return ToolCallResult(ok=False, error="missing group_id or file_id")
    return await _napcat_api_call(
        context, "delete_group_file", f"已删除群 {group_id} 文件 {file_id}",
        group_id=group_id, file_id=file_id, busid=busid,
    )


async def _handle_create_group_file_folder(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    name = str(args.get("name", "")).strip()
    parent_id = str(args.get("parent_id", "/")).strip() or "/"
    if not group_id or not name:
        return ToolCallResult(ok=False, error="missing group_id or name")
    return await _napcat_api_call(
        context, "create_group_file_folder", f"已在群 {group_id} 创建文件夹: {name}",
        group_id=group_id, name=name, parent_id=parent_id,
    )


async def _handle_get_group_system_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_group_system_msg", "获取群系统消息成功")
    if result.ok and result.data:
        invited = result.data.get("InvitedRequest") or result.data.get("invited_requests") or []
        join_req = result.data.get("join_requests") or []
        lines = []
        if invited:
            lines.append(f"邀请请求: {len(invited)} 条")
        if join_req:
            lines.append(f"加群请求: {len(join_req)} 条")
        result.display = " | ".join(lines) if lines else "暂无待处理的群系统消息"
    return result


async def _handle_send_forward_msg(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    messages = args.get("messages", [])
    if not messages or not isinstance(messages, list):
        return ToolCallResult(ok=False, error="missing messages array")
    message_type = str(args.get("message_type", "group"))
    params: dict[str, Any] = {"messages": messages}
    if message_type == "private":
        user_id = int(args.get("user_id", 0))
        if not user_id:
            return ToolCallResult(ok=False, error="private forward requires user_id")
        params["user_id"] = user_id
        params["message_type"] = "private"
    else:
        group_id = int(args.get("group_id", 0))
        if not group_id:
            return ToolCallResult(ok=False, error="group forward requires group_id")
        params["group_id"] = group_id
        params["message_type"] = "group"
    return await _napcat_api_call(context, "send_forward_msg", "合并转发消息已发送", **params)


async def _handle_mark_all_as_read(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    return await _napcat_api_call(context, "_mark_all_as_read", "已标记所有消息为已读")


async def _handle_get_friends_with_category(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "get_friends_with_category", "获取分组好友列表成功")
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        lines = [f"好友分组 ({len(items)} 组):"]
        for cat in items[:20]:
            if isinstance(cat, dict):
                cat_name = cat.get("categoryName") or cat.get("categroyName") or "默认"
                buddies = cat.get("buddyList") or cat.get("categroyMbCount") or []
                count = len(buddies) if isinstance(buddies, list) else buddies
                lines.append(f"  [{cat_name}] {count}人")
        result.display = "\n".join(lines)
    return result


async def _handle_get_image(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    file = str(args.get("file", "")).strip()
    if not file:
        return ToolCallResult(ok=False, error="missing file")
    result = await _napcat_api_call(context, "get_image", "获取图片信息成功", file=file)
    if result.ok and result.data:
        d = result.data
        result.display = f"图片: {d.get('file', '?')} | 大小: {d.get('file_size', '?')} | URL: {d.get('url', '?')[:80]}"
    return result


async def _handle_get_record(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    file = str(args.get("file", "")).strip()
    out_format = str(args.get("out_format", "mp3")).strip()
    if not file:
        return ToolCallResult(ok=False, error="missing file")
    result = await _napcat_api_call(context, "get_record", "获取语音文件成功", file=file, out_format=out_format)
    if result.ok and result.data:
        d = result.data
        result.display = f"语音: {d.get('file', '?')} | URL: {d.get('url', '?')[:80]}"
    return result


async def _handle_get_ai_record(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    character = str(args.get("character", "")).strip()
    text = str(args.get("text", "")).strip()
    if not group_id or not character or not text:
        return ToolCallResult(ok=False, error="missing group_id, character, or text")
    result = await _napcat_api_call(
        context, "get_ai_record", f"AI语音生成成功",
        group_id=group_id, character=character, text=text,
    )
    if result.ok and result.data:
        result.display = f"AI语音已生成，可用于发送"
    return result


async def _handle_ark_share_peer(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    user_id = str(args.get("user_id", "")).strip()
    group_id = str(args.get("group_id", "")).strip()
    phone_number = str(args.get("phone_number", "")).strip()
    if not user_id and not group_id:
        return ToolCallResult(ok=False, error="需要 user_id 或 group_id")
    params: dict[str, Any] = {}
    if user_id:
        params["user_id"] = user_id
    if group_id:
        params["group_id"] = group_id
    if phone_number:
        params["phoneNumber"] = phone_number
    result = await _napcat_api_call(context, "ArkSharePeer", "推荐联系人/群卡片已生成", **params)
    return result


async def _handle_get_mini_app_ark(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    content = str(args.get("content", "")).strip()
    if not content:
        return ToolCallResult(ok=False, error="missing content (JSON string of mini app)")
    return await _napcat_api_call(context, "get_mini_app_ark", "小程序卡片已签名", content=content)


async def _handle_create_collection(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    raw_data = str(args.get("raw_data", "")).strip()
    brief = str(args.get("brief", "")).strip()
    if not raw_data:
        return ToolCallResult(ok=False, error="missing raw_data")
    return await _napcat_api_call(
        context, "create_collection", f"已创建收藏: {brief[:30] if brief else raw_data[:30]}",
        rawData=raw_data, brief=brief or raw_data[:50],
    )


async def _handle_get_collection_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    category = int(args.get("category", 0))
    count = int(args.get("count", 20) or 20)
    result = await _napcat_api_call(
        context, "get_collection_list", "获取收藏列表成功",
        category=category, count=count,
    )
    if result.ok and result.data.get("items"):
        items = result.data["items"]
        result.display = f"收藏列表共 {len(items)} 条"
    return result


async def _handle_del_group_notice(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    notice_id = str(args.get("notice_id", "")).strip()
    if not group_id or not notice_id:
        return ToolCallResult(ok=False, error="missing group_id or notice_id")
    return await _napcat_api_call(
        context, "_del_group_notice", f"已删除群 {group_id} 公告",
        group_id=group_id, notice_id=notice_id,
    )


async def _handle_nc_get_packet_status(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    result = await _napcat_api_call(context, "nc_get_packet_status", "获取PacketServer状态成功")
    if result.ok and result.data:
        status = result.data.get("status") or result.data.get("available")
        result.display = f"PacketServer 状态: {'可用' if status else '不可用'}"
    return result


async def _handle_clean_cache(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    return await _napcat_api_call(context, "clean_cache", "缓存已清理")


# ── 搜索工具 ──
