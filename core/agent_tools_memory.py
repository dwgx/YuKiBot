"""Auto-split from core/agent_tools.py — 记忆管理工具"""
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


def _has_cross_user_memory_access(context: dict[str, Any]) -> bool:
    level = normalize_text(str(context.get("permission_level", ""))).lower()
    return level == "super_admin"


def _current_memory_scope(context: dict[str, Any]) -> tuple[str, str]:
    conversation_id = normalize_text(str(context.get("conversation_id", "")))
    user_id = normalize_text(str(context.get("user_id", "")))
    return conversation_id, user_id


def _scope_denied(message: str) -> ToolCallResult:
    return ToolCallResult(
        ok=False,
        error="permission_denied:memory_scope",
        display=message,
    )


def _resolve_memory_query_scope(
    args: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, str, ToolCallResult | None]:
    requested_conversation = normalize_text(str(args.get("conversation_id", "")))
    requested_user = normalize_text(str(args.get("user_id", "")))
    current_conversation, current_user = _current_memory_scope(context)
    if _has_cross_user_memory_access(context):
        return requested_conversation or current_conversation, requested_user, None
    if not current_conversation or not current_user:
        return "", "", _scope_denied("当前上下文缺少用户或会话信息，无法安全访问记忆。")
    if requested_conversation and requested_conversation != current_conversation:
        return "", "", _scope_denied("普通用户只能访问当前会话里的自己的记忆。")
    if requested_user and requested_user != current_user:
        return "", "", _scope_denied("普通用户只能访问或修改自己的记忆。")
    return current_conversation, current_user, None


def _memory_record_in_scope(record: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    if _has_cross_user_memory_access(context):
        return True
    current_conversation, current_user = _current_memory_scope(context)
    if not current_conversation or not current_user:
        return False
    return (
        normalize_text(str(record.get("conversation_id", ""))) == current_conversation
        and normalize_text(str(record.get("user_id", ""))) == current_user
    )

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

        conversation_id, user_id, denied = _resolve_memory_query_scope(args, context)
        if denied is not None:
            return denied
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

        conversation_id, user_id, denied = _resolve_memory_query_scope(args, context)
        if denied is not None:
            return denied
        role = normalize_text(str(args.get("role", ""))).lower() or "user"
        if not _has_cross_user_memory_access(context) and role != "user":
            return _scope_denied("普通用户只能新增自己的 user 记忆。")
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
        record = memory.get_memory_record(record_id) if hasattr(memory, "get_memory_record") else None
        if record_id > 0 and record is None:
            return ToolCallResult(ok=False, error="memory_not_found")
        if record_id > 0 and not _memory_record_in_scope(record, context):
            return _scope_denied("普通用户只能修改当前会话中属于自己的记忆。")

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
        record = memory.get_memory_record(record_id) if hasattr(memory, "get_memory_record") else None
        if record_id > 0 and record is None:
            return ToolCallResult(ok=False, error="memory_not_found")
        if record_id > 0 and not _memory_record_in_scope(record, context):
            return _scope_denied("普通用户只能删除当前会话中属于自己的记忆。")

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
        if not _has_cross_user_memory_access(context):
            if rid is None:
                return _scope_denied("普通用户查看记忆审计时必须指定自己的记录。")
            record = memory.get_memory_record(rid) if hasattr(memory, "get_memory_record") else None
            if record is None:
                return ToolCallResult(ok=False, error="memory_not_found")
            if not _memory_record_in_scope(record, context):
                return _scope_denied("普通用户只能查看当前会话中属于自己的记忆审计。")
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

        conversation_id, user_id, denied = _resolve_memory_query_scope(args, context)
        if denied is not None:
            return denied
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

