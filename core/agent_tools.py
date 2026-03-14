"""Agent 工具注册表 — 让 LLM 像 Agent 一样自主调用工具。

每个工具是一个 dict schema + 一个 async handler。
LLM 看到 schema 列表后，输出 JSON tool_call，Agent loop 执行并把结果喂回 LLM。

插件扩展能力:
- register_prompt_hint: 插件注入静态提示词到 Agent 系统提示
- register_context_provider: 插件注入动态上下文（每次构建 prompt 时调用）
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from core.napcat_compat import call_napcat_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning, looks_like_preferred_name_knowledge
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")


@dataclass(slots=True)
class ToolSchema:
    """描述一个可被 Agent 调用的工具。"""
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    category: str = "general"  # general / napcat / search / media / admin
    group: str = ""  # backward-compat metadata only; not used for local intent routing


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    display: str = ""  # 给 LLM 看的摘要


@dataclass(slots=True)
class PromptHint:
    """插件注入到 Agent 系统提示的静态文本块。

    section:
        - "rules": 出现在 ## 规则 区域
        - "tools_guidance": 出现在 ## 工具使用指南 区域
        - "context": 出现在 ## 上下文 区域
    priority: 数字越小越靠前，默认 50
    """
    source: str
    section: str
    content: str
    priority: int = 50
    tool_names: tuple[str, ...] = ()


ToolHandler = Callable[..., Awaitable[ToolCallResult]]
ContextProvider = Callable[[dict[str, Any]], str | Awaitable[str]]


class AgentToolRegistry:
    """工具注册中心，管理所有可用工具的 schema、handler、提示词和上下文提供者。"""

    _QQ_ID_PATTERN = re.compile(r"^[1-9]\d{5,11}$")
    _MESSAGE_ID_PATTERN = re.compile(r"^\d{4,20}$")
    _STRICT_QQ_FIELDS = {"user_id", "group_id", "target_user_id", "qq", "qq_number", "bot_id"}
    _STRICT_MESSAGE_FIELDS = {"message_id"}
    _TOOL_ARG_ALIASES: dict[str, dict[str, str]] = {
        "send_emoji": {"keyword": "query", "name": "query"},
        "send_sticker": {"keyword": "query", "name": "query"},
        "send_face": {"name": "query", "emoji": "query"},
        "correct_sticker": {"target": "key", "name": "key"},
        "memory_update": {"id": "record_id", "text": "content"},
        "memory_delete": {"id": "record_id"},
        "memory_audit": {"id": "record_id"},
        "web_search": {"q": "query", "keyword": "query"},
        "music_play": {"query": "keyword", "song": "keyword", "name": "keyword"},
        "music_search": {"query": "keyword", "song": "keyword", "name": "keyword"},
    }

    # ── 三级权限模型 ──
    # super_admin: 超级管理员，凌驾一切规则之上，无任何限制
    # group_admin: 群管理员（加白群的群主/管理员），可执行群管理类高级操作
    # user: 普通用户，只能使用基础工具

    # 仅超级管理员可用 — 影响全局/不可逆/跨群操作
    _SUPER_ADMIN_TOOLS = {
        "set_group_leave",
        "delete_friend",
        "cli_invoke",
        "config_update",
        "admin_command",
        "clean_cache",
        "set_qq_avatar",
        "set_online_status",
        "set_self_longnick",
    }

    # 群管理员 + 超级管理员可用 — 群内管理操作
    _GROUP_ADMIN_TOOLS = {
        "set_group_ban",
        "set_group_kick",
        "set_group_whole_ban",
        "set_group_admin",
        "set_group_name",
        "send_group_notice",
        "delete_message",
        "set_group_special_title",
        "set_essence_msg",
        "delete_essence_msg",
        "recall_recent_messages",
        "set_group_card",
        "set_group_portrait",
        "delete_group_file",
        "create_group_file_folder",
        "del_group_notice",
    }

    # 向后兼容: 合并集合
    _ADMIN_ONLY_TOOLS = _SUPER_ADMIN_TOOLS | _GROUP_ADMIN_TOOLS

    def __init__(self) -> None:
        self._schemas: dict[str, ToolSchema] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._prompt_hints: list[PromptHint] = []
        self._context_providers: dict[str, tuple[ContextProvider, int, tuple[str, ...]]] = {}
        self._intent_keyword_routing_enabled = False

    def register(self, schema: ToolSchema, handler: ToolHandler) -> None:
        self._schemas[schema.name] = schema
        self._handlers[schema.name] = handler

    def register_prompt_hint(self, hint: PromptHint) -> None:
        """注册静态提示词块，会被注入到 Agent 系统提示的对应 section。"""
        self._prompt_hints.append(hint)

    @staticmethod
    def _normalize_tool_names(tool_names: list[str] | tuple[str, ...] | set[str] | None) -> tuple[str, ...]:
        if not tool_names:
            return ()
        out: list[str] = []
        seen: set[str] = set()
        for name in tool_names:
            normalized = normalize_text(str(name)).lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return tuple(out)

    def get_prompt_hints(
        self,
        section: str | None = None,
        tool_names: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[PromptHint]:
        """获取提示词块，按 priority 排序。"""
        hints = self._prompt_hints if section is None else [
            h for h in self._prompt_hints if h.section == section
        ]
        selected_tools = set(self._normalize_tool_names(tool_names))
        if selected_tools:
            hints = [
                h
                for h in hints
                if not h.tool_names
                or bool(selected_tools & set(self._normalize_tool_names(h.tool_names)))
            ]
        return sorted(hints, key=lambda h: h.priority)

    def register_context_provider(
        self,
        name: str,
        provider: ContextProvider,
        priority: int = 50,
        tool_names: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> None:
        """注册动态上下文提供者，每次构建 prompt 时调用。"""
        self._context_providers[name] = (provider, priority, self._normalize_tool_names(tool_names))

    async def gather_dynamic_context(self, runtime_info: dict[str, Any]) -> list[str]:
        """调用所有上下文提供者，返回按 priority 排序的文本列表。"""
        results: list[tuple[int, str]] = []
        selected_tools = set(self._normalize_tool_names(runtime_info.get("selected_tools")))
        for name, (provider, prio, related_tools) in self._context_providers.items():
            try:
                if related_tools and selected_tools and not (selected_tools & set(related_tools)):
                    continue
                result = provider(runtime_info)
                if inspect.isawaitable(result):
                    result = await result
                text = str(result).strip()
                if text:
                    results.append((prio, text))
            except Exception:
                _log.warning("context_provider_error | name=%s", name, exc_info=True)
        results.sort(key=lambda x: x[0])
        return [text for _, text in results]

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

    def get_prompt_hints_text(
        self,
        section: str,
        tool_names: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> str:
        """获取指定 section 的提示词块，拼接为文本。"""
        hints = self.get_prompt_hints(section, tool_names=tool_names)
        if not hints:
            return ""
        return "\n".join(h.content for h in hints if h.content.strip())

    def get_dynamic_context(
        self,
        runtime_info: dict[str, Any],
        tool_names: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> str:
        """同步包装: 调用所有上下文提供者，返回拼接文本。"""
        parts: list[str] = []
        selected_tools = set(self._normalize_tool_names(tool_names or runtime_info.get("selected_tools")))
        for name, (provider, prio, related_tools) in sorted(self._context_providers.items(), key=lambda x: x[1][1]):
            try:
                if related_tools and selected_tools and not (selected_tools & set(related_tools)):
                    continue
                result = provider(runtime_info)
                if inspect.isawaitable(result):
                    continue  # 同步调用跳过 async provider
                text = str(result).strip()
                if text:
                    parts.append(text)
            except Exception:
                _log.warning("context_provider_error | name=%s", name, exc_info=True)
        return "\n".join(parts)

    # 每个分组始终包含的工具名
    _ALWAYS_INCLUDE = {"final_answer", "think"}

    def set_intent_keyword_routing_enabled(self, enabled: bool) -> None:
        # 保留兼容入口，但 Agent 不再根据本地关键词裁剪工具。
        self._intent_keyword_routing_enabled = bool(enabled)

    def _tool_visible_for_permission(self, name: str, permission_level: str) -> bool:
        level = normalize_text(permission_level or "user").lower() or "user"
        is_super = level == "super_admin"
        is_group_admin = level in {"group_admin", "super_admin"}
        if name in self._SUPER_ADMIN_TOOLS and not is_super:
            return False
        if name == "set_group_ban":
            return True
        if name in self._GROUP_ADMIN_TOOLS and not is_group_admin:
            return False
        return True

    def _list_tools_for_permission(self, permission_level: str) -> list[str]:
        selected: list[str] = []
        for name in self._schemas.keys():
            if not self._tool_visible_for_permission(name, permission_level):
                continue
            selected.append(name)
        for must_keep in self._ALWAYS_INCLUDE:
            if must_keep in self._schemas and must_keep not in selected:
                selected.append(must_keep)
        return selected

    def select_tools_for_intent(
        self,
        message_text: str,
        permission_level: str = "user",
        force_filter: bool = False,
    ) -> list[str]:
        """返回当前权限可见的完整工具列表，不做本地关键词裁剪。"""
        _ = (message_text, force_filter)
        return self._list_tools_for_permission(permission_level)

    def get_schemas_for_prompt_filtered(self, tool_names: list[str]) -> str:
        """只渲染指定工具名的 schema 文档。"""
        lines: list[str] = []
        for name in tool_names:
            schema = self._schemas.get(name)
            if not schema:
                continue
            params = schema.parameters if isinstance(schema.parameters, dict) else {}
            props = params.get("properties", {})
            required = params.get("required", [])
            param_parts: list[str] = []
            for pname, pinfo in props.items():
                req_mark = "*" if pname in required else ""
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                param_parts.append(f"  - {pname}{req_mark} ({ptype}): {pdesc}")
            param_block = "\n".join(param_parts) if param_parts else "  (无参数)"
            lines.append(f"### {schema.name}\n{schema.description}\n参数:\n{param_block}")
        return "\n\n".join(lines)

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    def get_schema(self, name: str) -> ToolSchema | None:
        return self._schemas.get(name)

    @classmethod
    def _coerce_basic_type(cls, value: Any, expected_type: str) -> tuple[Any, bool]:
        if not expected_type:
            return value, True
        if expected_type == "string":
            return str(value), True
        if expected_type == "integer":
            if isinstance(value, bool):
                return value, False
            if isinstance(value, int):
                return value, True
            if isinstance(value, str):
                text = normalize_text(value)
                if re.fullmatch(r"-?\d+", text):
                    return int(text), True
            return value, False
        if expected_type == "number":
            if isinstance(value, bool):
                return value, False
            if isinstance(value, (int, float)):
                return value, True
            if isinstance(value, str):
                text = normalize_text(value)
                try:
                    return float(text), True
                except ValueError:
                    return value, False
            return value, False
        if expected_type == "boolean":
            if isinstance(value, bool):
                return value, True
            if isinstance(value, str):
                text = normalize_text(value).lower()
                if text in {"true", "1", "yes", "on"}:
                    return True, True
                if text in {"false", "0", "no", "off"}:
                    return False, True
            return value, False
        if expected_type == "array":
            return value, isinstance(value, list)
        if expected_type == "object":
            return value, isinstance(value, dict)
        return value, True

    @classmethod
    def _normalize_qq_id(cls, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = normalize_text(value)
        else:
            return None
        if not cls._QQ_ID_PATTERN.fullmatch(text):
            return None
        try:
            return int(text)
        except ValueError:
            return None

    @classmethod
    def _normalize_message_id(cls, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = normalize_text(value)
        else:
            return None
        if not cls._MESSAGE_ID_PATTERN.fullmatch(text):
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _sanitize_and_validate_args(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        schema = self._schemas.get(tool_name)
        params = schema.parameters if schema is not None and isinstance(schema.parameters, dict) else {}
        props = params.get("properties", {}) if isinstance(params.get("properties", {}), dict) else {}
        required_raw = params.get("required", [])
        required = [str(item) for item in required_raw] if isinstance(required_raw, list) else []

        alias_map = self._TOOL_ARG_ALIASES.get(tool_name, {})
        normalized_args: dict[str, Any] = dict(args)
        if alias_map:
            for src_key, dst_key in alias_map.items():
                if src_key not in normalized_args:
                    continue
                if dst_key in normalized_args:
                    continue
                if props and dst_key not in props:
                    continue
                normalized_args[dst_key] = normalized_args[src_key]

        sanitized: dict[str, Any] = {}
        dropped_keys: list[str] = []
        if props:
            for key, value in normalized_args.items():
                if key in props:
                    sanitized[key] = value
                else:
                    if key in alias_map and alias_map.get(key) in props:
                        continue
                    dropped_keys.append(str(key))
        else:
            sanitized = dict(normalized_args)

        if dropped_keys:
            _log.warning(
                "tool_args_unknown_dropped | tool=%s | dropped=%s",
                tool_name,
                ",".join(sorted(set(dropped_keys))),
            )

        for key in list(sanitized.keys()):
            expected_type = ""
            if key in props and isinstance(props.get(key), dict):
                expected_type = normalize_text(str(props[key].get("type", ""))).lower()

            if key in self._STRICT_QQ_FIELDS:
                qq_id = self._normalize_qq_id(sanitized[key])
                if qq_id is None:
                    return {}, f"invalid_{key}"
                sanitized[key] = str(qq_id) if expected_type == "string" else qq_id
                continue

            if key in self._STRICT_MESSAGE_FIELDS:
                message_id = self._normalize_message_id(sanitized[key])
                if message_id is None:
                    return {}, f"invalid_{key}"
                sanitized[key] = str(message_id) if expected_type == "string" else message_id
                continue

            coerced, ok = self._coerce_basic_type(sanitized[key], expected_type)
            if not ok:
                return {}, f"invalid_type:{key}:{expected_type}"
            sanitized[key] = coerced

        missing: list[str] = []
        for key in required:
            value = sanitized.get(key)
            if value is None:
                missing.append(key)
                continue
            if isinstance(value, str) and not normalize_text(value):
                missing.append(key)
                continue
            if isinstance(value, list) and not value:
                missing.append(key)
                continue
        if missing:
            return {}, f"missing_required_args:{','.join(missing)}"

        return sanitized, ""

    async def call(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolCallResult(ok=False, error=f"unknown_tool: {name}")

        # ── 三级权限检查 ──
        # permission_level: "super_admin" > "group_admin" > "user"
        perm = str(context.get("permission_level", "user")).strip()
        is_super = perm == "super_admin"
        is_group_admin = perm in ("group_admin", "super_admin")

        if name in self._SUPER_ADMIN_TOOLS and not is_super:
            _log.warning(
                "tool_permission_denied | tool=%s | actor=%s | level=%s | need=super_admin",
                name,
                normalize_text(str(context.get("user_id", ""))) or "?",
                perm,
            )
            return ToolCallResult(ok=False, error="permission_denied:need_super_admin")

        if name in self._GROUP_ADMIN_TOOLS and not is_group_admin and name != "set_group_ban":
            _log.warning(
                "tool_permission_denied | tool=%s | actor=%s | level=%s | need=group_admin",
                name,
                normalize_text(str(context.get("user_id", ""))) or "?",
                perm,
            )
            return ToolCallResult(ok=False, error="permission_denied:need_group_admin")
        safe_args = args if isinstance(args, dict) else {}
        sanitized_args, validation_error = self._sanitize_and_validate_args(name, safe_args)
        if validation_error:
            _log.warning(
                "tool_args_rejected | tool=%s | error=%s | args=%s",
                name,
                validation_error,
                json.dumps(safe_args, ensure_ascii=False)[:200],
            )
            return ToolCallResult(ok=False, error=f"invalid_args:{validation_error}")
        try:
            return await handler(args=sanitized_args, context=context)
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
    _register_crawler_tools(registry)
    _register_memory_tools(registry)
    _register_ai_method_tools(registry)
    _register_qzone_tools(registry, config)
    _register_scrapy_llm_tools(registry, model_client)

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
    if not callable(api_call):
        return ToolCallResult(ok=False, error="no_api_call_available")
    group_id = int(args.get("group_id", 0))
    message = str(args.get("message", ""))
    if not group_id or not message:
        return ToolCallResult(ok=False, error="missing group_id or message")
    if len(message) > 4000:
        message = message[:3997] + "..."
    try:
        await call_napcat_api(api_call, "send_group_msg", group_id=group_id, message=message)
        return ToolCallResult(ok=True, display=f"已发送群消息到 {group_id}")
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
    try:
        await call_napcat_api(api_call, "send_private_msg", user_id=user_id, message=message)
        return ToolCallResult(ok=True, display=f"已发送私聊消息到 {user_id}")
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
        return ToolCallResult(ok=False, error=f"文件不存在: {file_path}")
    return await _napcat_api_call(
        context, "upload_group_file", f"已上传文件 {name} 到群 {group_id}",
        group_id=group_id, file=str(resolved), name=name,
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
        return ToolCallResult(ok=False, error="stage_download_file_failed", display=f"下载到 {raw_path}，但整理到上传目录失败")

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

    # ── 视频解析工具 ──
    registry.register(
        ToolSchema(
            name="parse_video",
            description=(
                "解析短视频链接，返回可发送的视频URL。\n"
                "支持平台: 抖音(douyin/v.douyin.com)、快手(kuaishou)、B站(bilibili/b23.tv)、AcFun、直链视频(.mp4等)。\n"
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


def _make_image_gen_handler(model_client: Any) -> ToolHandler:
    """创建图片生成工具的 handler。"""
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
    trusted_platforms = {"bilibili", "douyin", "kuaishou", "acfun"}
    if platform and platform not in trusted_platforms:
        reasons.append(f"非主流平台({platform})，QQ可能拦截外链")

    # 6. 直链检查
    if re.search(r"\.(?:mp4|flv|mkv|avi|mov|webm)(?:\?|$)", lower_url, re.IGNORECASE):
        if not any(trusted in lower_url for trusted in (
            "bilibili", "douyin", "kuaishou", "acfun", "douyinvod", "bilivideo",
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
            display="parse_video 只支持抖音/快手/B站/AcFun/直链视频，不支持该链接类型。",
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
    import tempfile
    from pathlib import Path as _Path

    explicit_url = normalize_text(str(args.get("url", "")))
    raw_segments = context.get("raw_segments", [])
    reply_media_segments = context.get("reply_media_segments", [])
    api_call = context.get("api_call")

    # 收集所有语音 URL
    voice_url = explicit_url
    voice_file_id = ""
    if not voice_url:
        for segs in (raw_segments, reply_media_segments):
            for seg in (segs or []):
                if not isinstance(seg, dict):
                    continue
                seg_type = normalize_text(str(seg.get("type", ""))).lower()
                if seg_type not in ("record", "audio"):
                    continue
                data = seg.get("data", {}) or {}
                url = normalize_text(str(data.get("url", "")))
                fid = normalize_text(str(data.get("file", "") or data.get("file_id", "")))
                if url:
                    voice_url = url
                    break
                if fid:
                    voice_file_id = fid
                    break
            if voice_url or voice_file_id:
                break

    if not voice_url and not voice_file_id:
        return ToolCallResult(
            ok=False,
            error="voice_not_found",
            display="没有找到语音消息。请发送语音或回复一条语音消息再试。",
        )

    # 尝试通过 NapCat API 获取语音文件
    if not voice_url and voice_file_id and api_call:
        try:
            result = await call_napcat_api(api_call, "get_record", file=voice_file_id, out_format="mp3")
            if isinstance(result, dict):
                # 某些实现直接返回转录文本
                text = str(result.get("text", "")).strip()
                if text:
                    return ToolCallResult(ok=True, data={"text": text, "source": "napcat_stt"}, display=f"语音内容: {text}")
                voice_url = str(result.get("url", "") or result.get("file", "")).strip()
        except Exception as exc:
            _log.warning("voice_get_record_error | %s", exc)

    if not voice_url:
        return ToolCallResult(ok=False, error="voice_url_unavailable", display="无法获取语音文件地址")

    # 下载语音文件
    try:
        from utils.media import download_file, extract_audio, transcribe_audio

        cache_dir = _Path("storage/cache/voice")
        cache_dir.mkdir(parents=True, exist_ok=True)
        import hashlib
        fname = hashlib.md5(voice_url.encode()).hexdigest()
        voice_path = cache_dir / f"{fname}.mp3"

        if not voice_path.is_file():
            ok = await download_file(voice_url, voice_path, timeout=20.0)
            if not ok:
                return ToolCallResult(ok=False, error="voice_download_failed", display="语音文件下载失败")

        # 转换为 WAV
        wav_path = await extract_audio(voice_path, voice_path.with_suffix(".wav"))
        if not wav_path:
            wav_path = str(voice_path)  # 尝试直接用 mp3

        # Whisper 转录
        text = await transcribe_audio(wav_path, language="zh")
        if not text:
            return ToolCallResult(ok=False, error="whisper_transcribe_empty", display="语音转录结果为空，可能是静音或无法识别")

        return ToolCallResult(
            ok=True,
            data={"text": text, "source": "whisper"},
            display=f"语音内容: {text}",
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

        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video_path)],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
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
    keyword = normalize_matching_text(str(args.get("keyword", "")))
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
    keyword = normalize_matching_text(str(args.get("keyword", "")))
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
        except Exception:
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

def _register_memory_tools(registry: AgentToolRegistry) -> None:
    registry.register_prompt_hint(
        PromptHint(
            source="memory",
            section="tools_guidance",
            content=(
                "当用户要求整理/修正记忆库时，优先使用 memory_list/memory_add/memory_update/memory_delete/memory_compact。"
                "注意：update/delete 必须提供 note 备注，说明改动原因。"
                "若需要去重整理，先 memory_compact dry_run 预览，再带 note 执行。"
            ),
            priority=30,
            tool_names=("memory_list", "memory_add", "memory_update", "memory_delete", "memory_compact"),
        )
    )

    async def _handle_memory_list(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")

        conversation_id = normalize_text(str(args.get("conversation_id", ""))) or normalize_text(
            str(context.get("conversation_id", ""))
        )
        user_id = normalize_text(str(args.get("user_id", "")))
        role = normalize_text(str(args.get("role", ""))).lower()
        keyword = normalize_text(str(args.get("keyword", "")))
        limit = int(args.get("limit", 30) or 30)
        page = int(args.get("page", 1) or 1)
        page = max(1, page)
        offset = (page - 1) * max(1, limit)

        items, total = memory.list_memory_records(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )
        lines = [f"记忆记录: {total} 条（第 {page} 页）"]
        for item in items[:10]:
            lines.append(
                f"#{item.get('id')} [{item.get('role')}] {item.get('user_id')}: "
                f"{clip_text(str(item.get('content', '')), 80)}"
            )
        return ToolCallResult(
            ok=True,
            data={
                "items": items,
                "total": total,
                "page": page,
                "limit": max(1, limit),
            },
            display="\n".join(lines),
        )

    async def _handle_memory_add(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")

        content = normalize_text(str(args.get("content", "")))
        if not content:
            return ToolCallResult(ok=False, error="missing_content")

        conversation_id = normalize_text(str(args.get("conversation_id", ""))) or normalize_text(
            str(context.get("conversation_id", ""))
        )
        user_id = normalize_text(str(args.get("user_id", ""))) or normalize_text(str(context.get("user_id", "")))
        role = normalize_text(str(args.get("role", ""))).lower() or "user"
        note = normalize_text(str(args.get("note", "")))
        reason = normalize_text(str(args.get("reason", "")))
        actor = f"agent:{normalize_text(str(context.get('user_id', '')))}"

        ok, message, payload = memory.add_memory_record(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content,
            actor=actor,
            note=note,
            reason=reason,
        )
        return ToolCallResult(
            ok=ok,
            data=payload,
            error="" if ok else message,
            display=(f"已新增记忆 #{payload.get('id')}" if ok else message),
        )

    async def _handle_memory_update(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        record_id = int(args.get("record_id", 0) or 0)
        content = normalize_text(str(args.get("content", "")))
        note = normalize_text(str(args.get("note", "")))
        reason = normalize_text(str(args.get("reason", "")))
        if record_id <= 0:
            return ToolCallResult(ok=False, error="missing_record_id")
        if not content:
            return ToolCallResult(ok=False, error="missing_content")
        if not note:
            return ToolCallResult(ok=False, error="missing_note")

        actor = f"agent:{normalize_text(str(context.get('user_id', '')))}"
        ok, message, payload = memory.update_memory_record(
            record_id=record_id,
            content=content,
            actor=actor,
            note=note,
            reason=reason,
        )
        return ToolCallResult(
            ok=ok,
            data=payload,
            error="" if ok else message,
            display=(f"记忆 #{record_id} 已更新（备注: {note}）" if ok else message),
        )

    async def _handle_memory_delete(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        record_id = int(args.get("record_id", 0) or 0)
        note = normalize_text(str(args.get("note", "")))
        reason = normalize_text(str(args.get("reason", "")))
        if record_id <= 0:
            return ToolCallResult(ok=False, error="missing_record_id")
        if not note:
            return ToolCallResult(ok=False, error="missing_note")

        actor = f"agent:{normalize_text(str(context.get('user_id', '')))}"
        ok, message, payload = memory.delete_memory_record(
            record_id=record_id,
            actor=actor,
            note=note,
            reason=reason,
        )
        return ToolCallResult(
            ok=ok,
            data=payload,
            error="" if ok else message,
            display=(f"记忆 #{record_id} 已删除（备注: {note}）" if ok else message),
        )

    async def _handle_memory_audit(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        record_id = int(args.get("record_id", 0) or 0)
        limit = int(args.get("limit", 30) or 30)
        page = int(args.get("page", 1) or 1)
        page = max(1, page)
        offset = (page - 1) * max(1, limit)
        rid = record_id if record_id > 0 else None
        items, total = memory.list_memory_audit_logs(record_id=rid, limit=limit, offset=offset)
        lines = [f"记忆审计: {total} 条（第 {page} 页）"]
        for item in items[:10]:
            lines.append(
                f"#{item.get('id')} rec={item.get('record_id')} "
                f"{item.get('action')} by {item.get('actor')} note={clip_text(str(item.get('note', '')), 24)}"
            )
        return ToolCallResult(
            ok=True,
            data={"items": items, "total": total, "page": page, "limit": max(1, limit)},
            display="\n".join(lines),
        )

    async def _handle_memory_compact(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")

        conversation_id = normalize_text(str(args.get("conversation_id", ""))) or normalize_text(
            str(context.get("conversation_id", ""))
        )
        user_id = normalize_text(str(args.get("user_id", "")))
        role = normalize_text(str(args.get("role", ""))).lower()
        dry_run = bool(args.get("dry_run", True))
        keep_latest = int(args.get("keep_latest", 1) or 1)
        note = normalize_text(str(args.get("note", "")))
        reason = normalize_text(str(args.get("reason", "")))

        if not dry_run and not note:
            return ToolCallResult(ok=False, error="missing_note")

        actor = f"agent:{normalize_text(str(context.get('user_id', '')))}"
        ok, message, payload = memory.compact_memory_records(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            actor=actor,
            note=note,
            reason=reason,
            dry_run=dry_run,
            keep_latest=keep_latest,
        )
        if not ok:
            return ToolCallResult(ok=False, error=message, data=payload)
        return ToolCallResult(
            ok=True,
            data=payload,
            display=(
                f"记忆整理预览完成：扫描 {payload.get('scanned', 0)} 条，"
                f"可去重 {payload.get('duplicates', 0)} 条"
                if dry_run
                else f"记忆整理已执行：扫描 {payload.get('scanned', 0)} 条，"
                    f"已去重 {payload.get('duplicates', 0)} 条（备注: {note}）"
            ),
        )

    registry.register(
        ToolSchema(
            name="memory_list",
            description="查询记忆库记录（可按会话、用户、角色、关键词过滤）。",
            parameters={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "会话ID，默认当前会话"},
                    "user_id": {"type": "string", "description": "用户ID(可选)"},
                    "role": {"type": "string", "description": "角色过滤 user/assistant/system(可选)"},
                    "keyword": {"type": "string", "description": "内容关键词(可选)"},
                    "limit": {"type": "integer", "description": "每页条数(默认30，最大200)"},
                    "page": {"type": "integer", "description": "页码(默认1)"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_memory_list,
    )

    registry.register(
        ToolSchema(
            name="memory_add",
            description="新增一条记忆记录到记忆库。",
            parameters={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "会话ID，默认当前会话"},
                    "user_id": {"type": "string", "description": "用户ID，默认当前用户"},
                    "role": {"type": "string", "description": "角色 user/assistant/system，默认user"},
                    "content": {"type": "string", "description": "记忆内容"},
                    "note": {"type": "string", "description": "备注(可选)"},
                    "reason": {"type": "string", "description": "原因(可选)"},
                },
                "required": ["content"],
            },
            category="utility",
        ),
        _handle_memory_add,
    )

    registry.register(
        ToolSchema(
            name="memory_update",
            description="修改指定记忆记录。必须填写 note 备注。",
            parameters={
                "type": "object",
                "properties": {
                    "record_id": {"type": "integer", "description": "记录ID"},
                    "content": {"type": "string", "description": "修改后的内容"},
                    "note": {"type": "string", "description": "修改备注（必填）"},
                    "reason": {"type": "string", "description": "修改原因（可选）"},
                },
                "required": ["record_id", "content", "note"],
            },
            category="utility",
        ),
        _handle_memory_update,
    )

    registry.register(
        ToolSchema(
            name="memory_delete",
            description="删除指定记忆记录。必须填写 note 备注。",
            parameters={
                "type": "object",
                "properties": {
                    "record_id": {"type": "integer", "description": "记录ID"},
                    "note": {"type": "string", "description": "删除备注（必填）"},
                    "reason": {"type": "string", "description": "删除原因（可选）"},
                },
                "required": ["record_id", "note"],
            },
            category="utility",
        ),
        _handle_memory_delete,
    )

    registry.register(
        ToolSchema(
            name="memory_audit",
            description="查看记忆库增删改审计日志。",
            parameters={
                "type": "object",
                "properties": {
                    "record_id": {"type": "integer", "description": "指定记录ID(可选)"},
                    "limit": {"type": "integer", "description": "每页条数(默认30，最大500)"},
                    "page": {"type": "integer", "description": "页码(默认1)"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_memory_audit,
    )

    registry.register(
        ToolSchema(
            name="memory_compact",
            description="自动整理记忆库：按会话/用户/角色去重。建议先 dry_run 预览，再带 note 执行。",
            parameters={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "会话ID，默认当前会话"},
                    "user_id": {"type": "string", "description": "用户ID(可选)"},
                    "role": {"type": "string", "description": "角色过滤 user/assistant/system(可选)"},
                    "dry_run": {"type": "boolean", "description": "是否仅预览(默认true)"},
                    "keep_latest": {"type": "integer", "description": "每组重复内容保留最新N条(默认1)"},
                    "note": {"type": "string", "description": "执行整理时的备注（dry_run=false 时必填）"},
                    "reason": {"type": "string", "description": "整理原因（可选）"},
                },
                "required": [],
            },
            category="utility",
        ),
        _handle_memory_compact,
    )


# ── 爬虫 / 知识库工具 ──

def _register_crawler_tools(registry: AgentToolRegistry) -> None:
    """注册知乎/百科/热搜/知识库工具。"""

    registry.register(
        ToolSchema(
            name="get_hot_trends",
            description=(
                "获取全网热搜热榜: 微博热搜、B站热门、抖音热榜、百度热搜。\n"
                "可指定平台(weibo/bilibili/douyin/baidu)或不指定获取全部。\n"
                "使用场景: 用户问最近有什么热点/新闻/热搜时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "平台(可选): weibo/bilibili/douyin/baidu，不填获取全部",
                    },
                    "limit": {"type": "integer", "description": "每个平台返回条数(默认10)"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_get_hot_trends,
    )

    registry.register(
        ToolSchema(
            name="search_zhihu",
            description=(
                "搜索知乎内容或获取知乎热榜。\n"
                "mode=hot 获取热榜，mode=search 搜索内容，mode=answers 获取问题高赞回答。\n"
                "使用场景: 用户问知乎相关问题、想了解某个话题的讨论时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "模式: hot(热榜)/search(搜索)/answers(回答)"},
                    "query": {"type": "string", "description": "搜索关键词(search/answers模式必填)"},
                    "question_id": {"type": "string", "description": "知乎问题ID(answers模式)"},
                },
                "required": ["mode"],
            },
            category="search",
        ),
        _handle_search_zhihu,
    )

    registry.register(
        ToolSchema(
            name="lookup_wiki",
            description=(
                "查询百科知识: 同时搜索百度百科和维基百科。\n"
                "使用场景: 用户问某个概念/人物/事件的定义或背景知识时使用。\n"
                "返回百度百科和维基百科的摘要。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要查询的关键词"},
                },
                "required": ["keyword"],
            },
            category="search",
        ),
        _handle_lookup_wiki,
    )

    registry.register(
        ToolSchema(
            name="search_knowledge",
            description=(
                "搜索知识库: 查找已学习的知识、热梗、百科、事实。\n"
                "知识库独立于对话记忆，存储持久化知识。\n"
                "category可选: fact(事实)/meme(热梗)/wiki(百科)/trend(热搜)/learned(学习)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "category": {"type": "string", "description": "分类(可选)"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _handle_search_knowledge,
    )

    registry.register(
        ToolSchema(
            name="learn_knowledge",
            description=(
                "学习新知识: 将信息存入知识库。\n"
                "使用场景: 用户教你新知识、新梗、新概念时使用。\n"
                "category: fact(事实)/meme(热梗)/learned(学习到的)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "知识标题/名称"},
                    "content": {"type": "string", "description": "知识内容"},
                    "category": {"type": "string", "description": "分类: fact/meme/learned"},
                    "tags": {"type": "string", "description": "标签(逗号分隔)"},
                },
                "required": ["title", "content"],
            },
            category="search",
        ),
        _handle_learn_knowledge,
    )


async def _handle_get_hot_trends(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable", display="爬虫模块未初始化")

    platform = str(args.get("platform", "")).strip().lower()
    limit = min(20, max(3, int(args.get("limit", 10) or 10)))

    try:
        if platform:
            method_map = {
                "weibo": crawler_hub.trends.weibo_hot,
                "bilibili": crawler_hub.trends.bilibili_hot,
                "douyin": crawler_hub.trends.douyin_hot,
                "baidu": crawler_hub.trends.baidu_hot,
            }
            func = method_map.get(platform)
            if not func:
                return ToolCallResult(ok=False, error=f"unknown_platform: {platform}")
            items = await func(limit)
            lines = [f"【{platform}热搜 Top{len(items)}】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"platform": platform, "count": len(items)},
                                display="\n".join(lines))
        else:
            trends = await crawler_hub.get_trends_cached()
            text = crawler_hub.format_trends_text(trends, limit=limit)
            # 同时存入知识库
            kb = context.get("knowledge_base")
            if kb:
                for plat, items in trends.items():
                    for item in items[:limit]:
                        kb.add("trend", item.title, item.snippet or "", source=plat,
                                tags=[plat], extra={"heat": item.heat, "url": item.url})
            return ToolCallResult(ok=True, data={"platforms": list(trends.keys())}, display=text)
    except Exception as e:
        _log.warning("get_hot_trends_error | %s", e)
        return ToolCallResult(ok=False, error=f"trends_error: {e}")


async def _handle_search_zhihu(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    mode = str(args.get("mode", "hot")).strip().lower()
    query = str(args.get("query", "")).strip()
    question_id = str(args.get("question_id", "")).strip()

    try:
        if mode == "hot":
            items = await crawler_hub.zhihu.hot_list(limit=15)
            lines = ["【知乎热榜】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "search" and query:
            items = await crawler_hub.zhihu.search(query, limit=8)
            lines = [f"【知乎搜索: {query}】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                if item.snippet:
                    lines.append(f"   {clip_text(item.snippet, 100)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "answers" and question_id:
            items = await crawler_hub.zhihu.get_top_answers(question_id, limit=3)
            lines = [f"【知乎问题 {question_id} 高赞回答】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                lines.append(f"   {clip_text(item.snippet, 300)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        return ToolCallResult(ok=False, error="invalid mode or missing query")
    except Exception as e:
        _log.warning("search_zhihu_error | %s", e)
        return ToolCallResult(ok=False, error=f"zhihu_error: {e}")


async def _handle_lookup_wiki(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    keyword = str(args.get("keyword", "")).strip()
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    try:
        results = await crawler_hub.wiki.lookup(keyword)
        if not results:
            return ToolCallResult(ok=False, error="not_found", display=f"未找到 '{keyword}' 的百科信息")

        lines: list[str] = []
        for r in results:
            source_name = "百度百科" if r.source == "baike" else "维基百科"
            lines.append(f"【{source_name}: {r.title}】")
            lines.append(clip_text(r.snippet, 400))
            if r.url:
                lines.append(f"来源: {r.url}")
            lines.append("")

        # 存入知识库
        kb = context.get("knowledge_base")
        if kb:
            for r in results:
                kb.add("wiki", r.title, r.snippet, source=r.source, tags=[keyword])

        return ToolCallResult(ok=True, data={"results": len(results)}, display="\n".join(lines))
    except Exception as e:
        _log.warning("lookup_wiki_error | %s", e)
        return ToolCallResult(ok=False, error=f"wiki_error: {e}")


async def _handle_search_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    query = str(args.get("query", "")).strip()
    category = str(args.get("category", "")).strip()
    if not query:
        return ToolCallResult(ok=False, error="missing query")
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = normalize_text(str(context.get("group_id", "")))

    def _build_query_variants(raw_query: str) -> list[str]:
        base = normalize_text(raw_query)
        if not base:
            return []

        variants: list[str] = []
        seen: set[str] = set()

        def _add(item: str) -> None:
            text = normalize_text(item)
            if not text:
                return
            key = text.lower()
            if key in seen:
                return
            seen.add(key)
            variants.append(text)

        _add(base)

        compact = re.sub(r"[，。！？!?,.;；:：\"'“”‘’（）()【】\[\]<>]+", " ", base)
        compact = normalize_text(compact)
        _add(compact)

        stop_words = {
            "喜欢",
            "最喜欢",
            "歌曲",
            "音乐",
            "听",
            "听歌",
            "查询",
            "搜索",
            "查",
            "查下",
            "查一下",
            "相关",
            "内容",
            "信息",
            "什么",
            "哪个",
            "是谁",
            "有吗",
            "一下",
            "给我",
            "帮我",
            "用户",
            "user",
        }

        user_id = normalize_text(str(context.get("user_id", "")))
        user_name_tokens = set(tokenize(normalize_text(str(context.get("user_name", "")))))

        core_terms: list[str] = []
        for token in tokenize(compact):
            if token in stop_words:
                continue
            if token in user_name_tokens:
                continue
            if user_id and token == user_id:
                continue
            if token.isdigit() and len(token) < 4:
                continue
            core_terms.append(token)
            if len(core_terms) >= 6:
                break

        if core_terms:
            _add(" ".join(core_terms))
            for term in core_terms[:4]:
                _add(term)

        if user_id:
            # 配合 user:<id> 标签做用户画像类检索。
            _add(f"user:{user_id}")
            if core_terms:
                _add(f"user:{user_id} {' '.join(core_terms[:3])}")

        return variants[:10]

    def _normalize_entry_tags(entry: Any) -> set[str]:
        raw_tags = getattr(entry, "tags", [])
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        if not isinstance(raw_tags, list):
            return set()
        out: set[str] = set()
        for raw in raw_tags:
            text = normalize_text(str(raw)).lower()
            if text:
                out.add(text)
        return out

    def _scope_score(entry: Any) -> int:
        tags = _normalize_entry_tags(entry)
        score = 0
        if current_user_id and f"user:{current_user_id}".lower() in tags:
            score += 100
        if current_conversation_id and f"conversation:{current_conversation_id}".lower() in tags:
            score += 40
        if current_group_id and f"group:{current_group_id}".lower() in tags:
            score += 20
        return score

    try:
        query_variants = _build_query_variants(query)
        category_variants: list[str] = [category] if category else [""]
        if category:
            category_variants.append("")  # category 限制命不中时自动放宽到全库

        entries: list[Any] = []
        seen_ids: set[int] = set()
        for cat in category_variants:
            for q in query_variants:
                try:
                    rows = kb.search(q, category=cat, limit=8)
                except Exception:
                    rows = []
                for row in rows:
                    rid = int(getattr(row, "id", 0) or 0)
                    if rid and rid in seen_ids:
                        continue
                    if rid:
                        seen_ids.add(rid)
                    entries.append(row)
                if len(entries) >= 8:
                    break
            if len(entries) >= 8:
                break

        if entries:
            entries = sorted(
                entries,
                key=lambda row: (
                    _scope_score(row),
                    float(getattr(row, "created_at", 0.0) or 0.0),
                ),
                reverse=True,
            )

        if not entries:
            return ToolCallResult(
                ok=True,
                data={"count": 0, "query_variants": query_variants},
                display=f"知识库中未找到 '{query}' 相关内容",
            )

        lines = [f"【知识库搜索: {query}】"]
        result_rows: list[dict[str, Any]] = []
        scoped_hits = 0
        for e in entries:
            cat_tag = f"[{e.category}]" if e.category else ""
            scope_score = _scope_score(e)
            if scope_score >= 100:
                scoped_hits += 1
            scope_tag = " (当前用户)" if scope_score >= 100 else ""
            lines.append(f"- {cat_tag} {e.title}{scope_tag}")
            if e.content:
                lines.append(f"  {clip_text(e.content, 200)}")
            tags = [normalize_text(str(item)) for item in (getattr(e, "tags", []) or []) if normalize_text(str(item))]
            result_rows.append(
                {
                    "id": int(getattr(e, "id", 0) or 0),
                    "category": normalize_text(str(getattr(e, "category", ""))),
                    "title": normalize_text(str(getattr(e, "title", ""))),
                    "content": normalize_text(str(getattr(e, "content", ""))),
                    "source": normalize_text(str(getattr(e, "source", ""))),
                    "tags": tags,
                }
            )
        return ToolCallResult(
            ok=True,
            data={
                "count": len(entries),
                "results": result_rows,
                "query_variants": query_variants,
                "scoped_hits": scoped_hits,
            },
            display="\n".join(lines),
        )
    except Exception as e:
        _log.warning("search_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"knowledge_error: {e}")


async def _handle_learn_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    def _infer_title_from_content(text: str) -> str:
        body = normalize_text(text)
        if not body:
            return ""
        m = re.match(r"([^，。！？\n:：]{1,40})(?:是|指|叫|一般是|通常是)", body)
        if m:
            return normalize_text(m.group(1))[:40]
        m2 = re.match(r"([^，。！？\n]{1,40})[:：]", body)
        if m2:
            return normalize_text(m2.group(1))[:40]
        fallback = normalize_text(body.split("，", 1)[0].split("。", 1)[0])
        return fallback[:40]

    title = normalize_text(str(args.get("title", "")))
    content = normalize_text(str(args.get("content", "")))
    if not content:
        content = normalize_text(str(args.get("text", "")))
    category = str(args.get("category", "learned")).strip()
    tags_value = args.get("tags", "")
    tags: list[str] = []
    if isinstance(tags_value, str):
        tags = [t.strip() for t in tags_value.split(",") if t.strip()]
    elif isinstance(tags_value, list):
        tags = [normalize_text(str(t)) for t in tags_value if normalize_text(str(t))]

    if not title and content:
        title = _infer_title_from_content(content)
        if title:
            tags = list(dict.fromkeys(tags + ["auto_title"]))

    if not title:
        return ToolCallResult(ok=False, error="missing title")
    if not content:
        return ToolCallResult(ok=False, error="missing content")
    if looks_like_preferred_name_knowledge(title, content, tags):
        cfg = context.get("config", {})
        bot_cfg = cfg.get("bot", {}) if isinstance(cfg, dict) and isinstance(cfg.get("bot"), dict) else {}
        bot_aliases = [bot_cfg.get("name", ""), *(bot_cfg.get("nicknames", []) or []), "yuki", "yukiko", "雪"]
        source_text = normalize_text(str(context.get("message_text", ""))) or normalize_text(
            str(context.get("original_message_text", ""))
        )
        decision = assess_preferred_name_learning(
            source_text or content,
            is_private=bool(context.get("is_private", False)),
            mentioned=bool(context.get("mentioned", False)),
            explicit_bot_addressed=bool(context.get("explicit_bot_addressed", False)),
            bot_aliases=bot_aliases,
            at_other_user_ids=context.get("at_other_user_ids", []) or [],
            reply_to_user_id=normalize_text(str(context.get("reply_to_user_id", ""))),
            bot_id=normalize_text(str(context.get("bot_id", ""))),
        )
        if not decision.allow:
            return ToolCallResult(
                ok=False,
                error=f"preferred_name_guard:{decision.reason}",
                display="群聊称呼学习需要明确点名我、明确声明，并且不能在起哄语境里。",
            )
        memory = context.get("memory_engine")
        if memory is None or not hasattr(memory, "set_preferred_name"):
            return ToolCallResult(ok=False, error="memory_engine_unavailable", display="称呼记忆模块未初始化")
        ok, message, payload = memory.set_preferred_name(
            target_user_id=normalize_text(str(context.get("user_id", ""))),
            preferred_name=decision.candidate,
            actor="agent.learn_knowledge",
            conversation_id=normalize_text(str(context.get("conversation_id", ""))),
            note="Agent 显式学习用户偏好称呼",
            reason="agent_learn_preferred_name",
        )
        if not ok:
            return ToolCallResult(ok=False, error="preferred_name_update_failed", data=payload or {}, display=message)
        preferred_name = normalize_text(str(payload.get("preferred_name", decision.candidate)))
        return ToolCallResult(
            ok=True,
            data=payload or {},
            display=f"已更新用户偏好称呼: {preferred_name or decision.candidate}",
        )
    merged_text = normalize_text(f"{title} {content}")
    if _looks_like_harmful_knowledge_payload(merged_text):
        return ToolCallResult(ok=False, error="unsafe_knowledge_content")
    if category not in ("fact", "meme", "learned"):
        category = "learned"

    normalized_tags: list[str] = []
    seen_tags: set[str] = set()

    def _append_tag(raw: str) -> None:
        tag = normalize_text(str(raw))
        if not tag:
            return
        key = tag.lower()
        if key in seen_tags:
            return
        seen_tags.add(key)
        normalized_tags.append(tag)

    for item in tags:
        _append_tag(item)
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = int(context.get("group_id", 0) or 0)
    if current_user_id:
        _append_tag(f"user:{current_user_id}")
    if current_conversation_id:
        _append_tag(f"conversation:{current_conversation_id}")
    if current_group_id > 0:
        _append_tag(f"group:{current_group_id}")
    normalized_tags = normalized_tags[:20]

    try:
        entry_id = kb.add(category=category, title=title, content=content,
                        source="chat", tags=normalized_tags)
        return ToolCallResult(
            ok=True,
            data={"id": entry_id, "category": category, "tags": normalized_tags},
            display=f"已学习: [{category}] {title}",
        )
    except Exception as e:
        _log.warning("learn_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"learn_error: {e}")


def _looks_like_harmful_knowledge_payload(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return True
    abusive_tokens = (
        "大便",
        "傻逼",
        "弱智",
        "智障",
        "脑残",
        "废物",
        "狗东西",
        "滚",
    )
    if any(token in content for token in abusive_tokens):
        return True
    # 阻断“以后你叫XX叫YY”这类强制羞辱称呼写入。
    if "以后你叫" in content and "叫他" in content:
        return True
    return False


# ─────────────────────────────────────────────
# AI Method 桥接工具 — 将 tools.py 的 AI method 暴露给 Agent
# ─────────────────────────────────────────────

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
