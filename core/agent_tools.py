"""Agent 工具注册表 — 让 LLM 像 Agent 一样自主调用工具。

每个工具是一个 dict schema + 一个 async handler。
LLM 看到 schema 列表后，输出 JSON tool_call，Agent loop 执行并把结果喂回 LLM。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.agent_tools")


@dataclass(slots=True)
class ToolSchema:
    """描述一个可被 Agent 调用的工具。"""
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    category: str = "general"  # general / napcat / search / media / admin


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    display: str = ""  # 给 LLM 看的摘要


ToolHandler = Callable[..., Awaitable[ToolCallResult]]


class AgentToolRegistry:
    """工具注册中心，管理所有可用工具的 schema 和 handler。"""

    def __init__(self) -> None:
        self._schemas: dict[str, ToolSchema] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, schema: ToolSchema, handler: ToolHandler) -> None:
        self._schemas[schema.name] = schema
        self._handlers[schema.name] = handler

    def get_schemas(self, categories: list[str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, schema in self._schemas.items():
            if categories and schema.category not in categories:
                continue
            out.append({
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            })
        return out

    def get_schemas_for_prompt(self, categories: list[str] | None = None) -> str:
        schemas = self.get_schemas(categories)
        lines: list[str] = []
        for s in schemas:
            params = s.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])
            param_parts: list[str] = []
            for pname, pinfo in props.items():
                req_mark = "*" if pname in required else ""
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                param_parts.append(f"  - {pname}{req_mark} ({ptype}): {pdesc}")
            param_block = "\n".join(param_parts) if param_parts else "  (无参数)"
            lines.append(f"### {s['name']}\n{s['description']}\n参数:\n{param_block}")
        return "\n\n".join(lines)

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    async def call(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolCallResult(ok=False, error=f"unknown_tool: {name}")
        try:
            return await handler(args=args, context=context)
        except Exception as exc:
            _log.exception("tool_call_error | tool=%s | error=%s", name, exc)
            return ToolCallResult(ok=False, error=f"tool_exception: {type(exc).__name__}: {exc}")

    @property
    def tool_count(self) -> int:
        return len(self._schemas)


# ─────────────────────────────────────────────
# 内置工具注册函数
# ─────────────────────────────────────────────

def register_builtin_tools(
    registry: AgentToolRegistry,
    search_engine: Any,
    image_engine: Any,
    model_client: Any,
    config: dict[str, Any],
) -> None:
    """注册所有内置工具到 registry。"""
    _register_napcat_tools(registry)
    _register_napcat_extended_tools(registry)
    _register_search_tools(registry, search_engine)
    _register_media_tools(registry, model_client, config)
    _register_admin_tools(registry)
    _register_utility_tools(registry)


# ── NapCat / OneBot V11 API 工具 ──

def _register_napcat_tools(registry: AgentToolRegistry) -> None:
    """注册 NapCat OneBot V11 API 工具，让 Agent 可以直接操作 QQ。"""

    # 发送群消息
    registry.register(
        ToolSchema(
            name="send_group_message",
            description=(
                "向QQ群发送一条文本消息。\n"
                "使用场景: 用户让你在群里说话、发通知、回复群消息时使用。\n"
                "注意: 只发纯文本，不能发图片/语音。发图片用 final_answer 的 image_url 参数"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "目标群号，如 901738883"},
                    "message": {"type": "string", "description": "要发送的消息文本内容"},
                },
                "required": ["group_id", "message"],
            },
            category="napcat",
        ),
        _handle_send_group_message,
    )

    # 发送私聊消息
    registry.register(
        ToolSchema(
            name="send_private_message",
            description=(
                "向QQ用户发送私聊消息。\n"
                "使用场景: 用户让你私聊某人、悄悄告诉某人消息时使用。\n"
                "注意: 对方必须是机器人好友才能发送"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "目标用户QQ号，如 ***REMOVED***"},
                    "message": {"type": "string", "description": "要发送的消息文本内容"},
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

    # 群禁言
    registry.register(
        ToolSchema(
            name="set_group_ban",
            description="对群成员禁言（需要管理员权限）。\n使用场景: 用户说'禁言XX'、'把XX禁言10分钟'、'解除禁言'时使用。\nduration=0 为解除禁言，单位是秒(如600=10分钟, 3600=1小时)",
            parameters={
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "duration": {"type": "integer", "description": "禁言时长（秒），0=解除禁言"},
                },
                "required": ["group_id", "user_id", "duration"],
            },
            category="napcat",
        ),
        _handle_set_group_ban,
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


# ── NapCat tool handlers ──

async def _handle_send_group_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not api_call:
        return ToolCallResult(ok=False, error="no_api_call_available")
    group_id = int(args.get("group_id", 0))
    message = str(args.get("message", ""))
    if not group_id or not message:
        return ToolCallResult(ok=False, error="missing group_id or message")
    try:
        await api_call("send_group_msg", group_id=group_id, message=message)
        return ToolCallResult(ok=True, display=f"已发送群消息到 {group_id}")
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


async def _handle_send_private_message(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    api_call = context.get("api_call")
    if not api_call:
        return ToolCallResult(ok=False, error="no_api_call_available")
    user_id = int(args.get("user_id", 0))
    message = str(args.get("message", ""))
    if not user_id or not message:
        return ToolCallResult(ok=False, error="missing user_id or message")
    try:
        await api_call("send_private_msg", user_id=user_id, message=message)
        return ToolCallResult(ok=True, display=f"已发送私聊消息到 {user_id}")
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


# ── 通用 NapCat API 调用封装 ──

async def _napcat_api_call(
    context: dict[str, Any], api: str, display_ok: str, **kwargs: Any
) -> ToolCallResult:
    """通用 NapCat API 调用封装，减少重复代码。"""
    api_call = context.get("api_call")
    if not api_call:
        return ToolCallResult(ok=False, error="no_api_call_available")
    try:
        result = await api_call(api, **kwargs)
        data = {}
        if isinstance(result, dict):
            data = result
        elif isinstance(result, list):
            data = {"items": result[:50], "total": len(result)}
        return ToolCallResult(ok=True, data=data, display=display_ok)
    except Exception as exc:
        return ToolCallResult(ok=False, error=str(exc))


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
    user_id = int(args.get("user_id", 0))
    if not user_id:
        return ToolCallResult(ok=False, error="missing user_id")
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
    return await _napcat_api_call(context, "delete_msg", f"已撤回消息 {message_id}", message_id=message_id)


async def _handle_set_group_ban(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    group_id = int(args.get("group_id", 0))
    user_id = int(args.get("user_id", 0))
    duration = int(args.get("duration", 0))
    if not group_id or not user_id:
        return ToolCallResult(ok=False, error="missing group_id or user_id")
    action = "解除禁言" if duration == 0 else f"禁言 {duration}秒"
    return await _napcat_api_call(
        context, "set_group_ban", f"已对 {user_id} {action}",
        group_id=group_id, user_id=user_id, duration=duration,
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
    if not group_id or not file_path or not name:
        return ToolCallResult(ok=False, error="missing group_id, file or name")
    return await _napcat_api_call(
        context, "upload_group_file", f"已上传文件 {name} 到群 {group_id}",
        group_id=group_id, file=file_path, name=name,
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
    return await _napcat_api_call(context, "set_group_sign", f"已在群 {group_id} 打卡", group_id=group_id)


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

        ("download_file", "下载URL文件到本地缓存，返回本地路径。\n使用场景: 需要先下载文件再上传到群文件时使用",
         {"url": ("string", "文件下载URL"), "thread_count": ("integer", "下载线程数，默认1")},
         ["url"], _handle_download_file),

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


async def _handle_download_file(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    url = str(args.get("url", ""))
    if not url:
        return ToolCallResult(ok=False, error="missing url")
    thread_count = int(args.get("thread_count", 1))
    result = await _napcat_api_call(
        context, "download_file", "文件下载成功",
        url=url, thread_count=thread_count,
    )
    if result.ok and result.data:
        result.display = f"已下载到: {result.data.get('file', '?')}"
    return result


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


# ── 搜索工具 ──

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


def _make_search_handler(search_engine: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        query = str(args.get("query", "")).strip()
        mode = str(args.get("mode", "text")).strip()
        if not query:
            return ToolCallResult(ok=False, error="empty query")
        try:
            results = await search_engine.search(query=query, mode=mode)
            if not results:
                return ToolCallResult(ok=True, data={"results": []}, display=f"搜索 '{query}' 无结果")
            items = []
            display_lines = []
            for i, r in enumerate(results[:8]):
                item = {
                    "title": getattr(r, "title", ""),
                    "url": getattr(r, "url", ""),
                    "snippet": getattr(r, "snippet", ""),
                }
                items.append(item)
                display_lines.append(f"{i+1}. {item['title']}: {clip_text(item['snippet'], 120)}")
            return ToolCallResult(
                ok=True,
                data={"results": items, "query": query, "mode": mode},
                display="\n".join(display_lines),
            )
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"search_error: {exc}")
    return handler


# ── 媒体工具 ──

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
                },
                "required": ["prompt"],
            },
            category="media",
        ),
        _make_image_gen_handler(model_client),
    )


def _make_image_gen_handler(model_client: Any) -> ToolHandler:
    async def handler(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        prompt = str(args.get("prompt", "")).strip()
        size = str(args.get("size", "1024x1024")).strip()
        if not prompt:
            return ToolCallResult(ok=False, error="empty prompt")
        try:
            url = await model_client.generate_image(prompt=prompt, size=size)
            if url:
                return ToolCallResult(ok=True, data={"image_url": url}, display=f"图片已生成")
            return ToolCallResult(ok=False, error="image generation returned empty")
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"image_gen_error: {exc}")
    return handler


# ── 管理指令工具 ──

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
                "- white_add: 加白本群\n"
                "- white_rm: 拉黑本群\n"
                "- white_list: 查看白名单\n"
                "- scale <0-3>: 设置安全尺度\n"
                "- sensitive [添加|删除] <词>: 管理敏感词\n"
                "- poke <QQ>: 戳一戳\n"
                "- dice: 骰子\n"
                "- rps: 猜拳\n"
                "- music_card <歌名>: 音乐卡片\n"
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


# ── 实用工具 ──

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
                    "image_url": {"type": "string", "description": "可选，附带的图片URL"},
                    "video_url": {"type": "string", "description": "可选，附带的视频URL"},
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


async def _handle_final_answer(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    text = str(args.get("text", "")).strip()
    image_url = str(args.get("image_url", "")).strip()
    video_url = str(args.get("video_url", "")).strip()
    return ToolCallResult(
        ok=True,
        data={"text": text, "image_url": image_url, "video_url": video_url, "is_final": True},
        display=text,
    )


async def _handle_think(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    thought = str(args.get("thought", "")).strip()
    return ToolCallResult(ok=True, data={"thought": thought}, display=f"[思考] {clip_text(thought, 200)}")
