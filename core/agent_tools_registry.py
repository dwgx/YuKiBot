"""Agent 工具注册中心 — 管理所有工具的 schema、handler、提示词和上下文。

从 agent_tools.py 拆分。
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any

from utils.text import normalize_text

from core.agent_tools_types import (
    ContextProvider,
    PromptHint,
    ToolCallResult,
    ToolHandler,
    ToolSchema,
)

_log = logging.getLogger("yukiko.agent_tools")


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
        "create_skill",
        "test_in_sandbox",
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
    _ALWAYS_INCLUDE = {"final_answer", "think", "navigate_section"}

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

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        """Return the permission-filtered full tool pool before Navigator scoping."""
        return self._list_tools_for_permission(permission_level)

    def set_intent_keyword_routing_enabled(self, enabled: bool) -> None:
        """兼容旧版 Engine: 仅保留开关 API，不再启用本地关键词路由。"""
        self._intent_keyword_routing_enabled = bool(enabled)

    def get_intent_keyword_routing_enabled(self) -> bool:
        """返回旧版关键词路由开关状态（兼容字段）。"""
        return bool(self._intent_keyword_routing_enabled)

    def select_tools_for_intent(
        self,
        message_text: str = "",
        permission_level: str = "user",
        **_kwargs: Any,
    ) -> list[str]:
        """Return all tools visible to the current permission level.

        Tool selection is fully LLM-driven via the system prompt.
        No local keyword filtering is performed.
        """
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

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """提取指定工具的 OpenAI 原生 Schema 格式。"""
        out: list[dict[str, Any]] = []
        for name in tool_names:
            schema = self._schemas.get(name)
            if not schema:
                continue
            
            out.append({
                "type": "function",
                "function": {
                    "name": schema.name,
                    "description": schema.description,
                    "parameters": self._normalize_native_parameters(schema.parameters),
                }
            })
        return out

    @classmethod
    def _normalize_native_parameters(cls, parameters: dict[str, Any] | None) -> dict[str, Any]:
        """Return an OpenAI-compatible JSON schema for native function calling."""
        if not isinstance(parameters, dict) or not parameters:
            return {"type": "object", "properties": {}}
        return cls._normalize_native_schema_node(parameters)

    @classmethod
    def _normalize_native_schema_node(cls, node: Any) -> Any:
        if isinstance(node, list):
            return [cls._normalize_native_schema_node(item) for item in node]
        if not isinstance(node, dict):
            return node

        normalized = {str(key): cls._normalize_native_schema_node(value) for key, value in node.items()}
        if normalized.get("type") == "array" and "items" not in normalized:
            normalized["items"] = {"type": "string"}
        return normalized

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    def get_schema(self, name: str) -> ToolSchema | None:
        return self._schemas.get(name)

    def list_tool_names(self) -> list[str]:
        return list(self._schemas.keys())

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
            context["_tool_name"] = name
            return await handler(args=sanitized_args, context=context)
        except Exception as exc:
            _log.exception("tool_call_error | tool=%s | error=%s", name, exc)
            return ToolCallResult(ok=False, error=f"tool_exception: {type(exc).__name__}: {exc}")

    @property
    def tool_count(self) -> int:
        return len(self._schemas)


# ─────────────────────────────────────────────
# 内置工具注册入口
# ─────────────────────────────────────────────

def register_builtin_tools(
    registry: AgentToolRegistry,
    search_engine: Any,
    image_engine: Any,
    model_client: Any,
    config: dict[str, Any],
) -> None:
    """注册所有内置工具到 registry。"""
    from core.agent_tools_admin import _register_admin_tools
    from core.agent_tools_knowledge import _register_crawler_tools
    from core.agent_tools_media import _register_media_tools
    from core.agent_tools_memory import _register_memory_tools
    from core.agent_tools_napcat import _register_napcat_extended_tools, _register_napcat_tools
    from core.agent_tools_search import _register_search_tools
    from core.agent_tools_social import _register_daily_report_tools, _register_qzone_tools
    from core.agent_tools_utility import _register_utility_tools
    from core.agent_tools_web import _register_ai_method_tools, _register_scrapy_llm_tools

    _register_napcat_tools(registry)
    _register_napcat_extended_tools(registry)
    _register_search_tools(registry, search_engine)
    _register_media_tools(registry, model_client, config)
    _register_admin_tools(registry)
    _register_utility_tools(registry)
    _register_crawler_tools(registry)
    _register_memory_tools(registry)
    _register_daily_report_tools(registry)
    _register_ai_method_tools(registry)
    _register_qzone_tools(registry, config)
    _register_scrapy_llm_tools(registry, model_client)
