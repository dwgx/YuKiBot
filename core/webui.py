"""WebUI 管理 API — 挂载到 nonebot FastAPI 上的管理接口。

提供配置编辑、提示词编辑、日志查看、状态查询等 REST + WebSocket 端点。
认证方式：Bearer token 或同源 HttpOnly cookie，配置在 .env WEBUI_TOKEN。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import inspect
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import yaml
from dotenv import dotenv_values, set_key, unset_key
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import Request

from core.napcat_compat import (
    build_napcat_diagnostics,
    build_napcat_file_reference,
    call_napcat_bot_api,
    napcat_file_uri_to_path,
)
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    list_recalled_messages as _list_recalled_messages,
    record_recalled_message as _record_recalled_message,
)
from core.config_templates import (
    deep_merge_dict as _deep_merge_template,
    ensure_prompts_file as _ensure_prompts_file_from_template,
    load_config_template,
    load_prompts_template,
)
from core.image_gen import ImageGenEngine
from core.prompt_navigator import validate_prompt_navigator_payload
from core.webui_auth_routes import build_auth_status_router
from core.webui_cookie_routes import build_cookie_router
from core.webui_log_routes import build_log_router
from core.webui_route_context import WebUIRouteContext
from core.webui_setup_support import WebUISetupSupport
from core import prompt_loader as _pl
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.webui")
router = APIRouter(prefix="/api/webui", tags=["webui"])


def _safe_write_yaml(filepath: Path, data: dict[str, Any]) -> None:
    """写入 YAML 前先备份，使用原子写入防止数据损坏。"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if filepath.exists():
        bak = filepath.with_suffix(filepath.suffix + ".bak")
        shutil.copy2(filepath, bak)
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        os.replace(tmp, filepath)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

_engine: Any = None
_start_time: float = time.time()
_LOG_SPLIT_RE = re.compile(r'(?=(?:\d{4}-\d{2}-\d{2}|(?<!\d{4}-)\d{2}-\d{2}) \d{2}:\d{2}:\d{2} (?:\||\[))')
_GROUP_ROLE_CACHE: dict[str, tuple[float, str]] = {}
_GROUP_ROLE_CACHE_OK_TTL_SECONDS = 30
_GROUP_ROLE_CACHE_MISS_TTL_SECONDS = 300
_SENSITIVE_PATHS = frozenset({
    "api.api_key",
    "api.openai_key",
    "api.deepseek_key",
    "api.anthropic_key",
    "api.gemini_key",
    "music.api_key",
    "image_gen.models.*.api_key",
    "video_analysis.bilibili.cookie",
    "video_analysis.douyin.cookie",
    "video_analysis.kuaishou.cookie",
    "video_analysis.qzone.cookie",
})

_ROOT_DIR = Path(__file__).resolve().parents[1]
_ENV_FILE = _ROOT_DIR / ".env"
_ENV_EXAMPLE_FILE = _ROOT_DIR / ".env.example"
_PROMPTS_FILE = _ROOT_DIR / "config" / "prompts.yml"
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQLITE_HEADER = b"SQLite format 3\x00"
_WEBUI_AUTH_COOKIE = "yukiko_webui_session"
_WEBUI_AUTH_COOKIE_MAX_AGE = 7 * 24 * 3600
_UPDATE_TASKS: dict[str, dict[str, Any]] = {}
_UPDATE_TASKS_LOCK = asyncio.Lock()
_UPDATE_TASK_MAX_LOG_ENTRIES = 400
_UPDATE_TASK_RETENTION_SECONDS = 24 * 3600
_UPDATE_STAGE_PROGRESS: dict[str, int] = {
    "queued": 0,
    "checking": 8,
    "pulling": 28,
    "pip_install": 52,
    "npm_install": 72,
    "npm_build": 88,
    "finalize": 96,
    "completed": 100,
    "failed": 100,
}
_ENV_EDITABLE_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "HOST",
        "label": "监听地址",
        "description": "WebUI / OneBot 反向 WS 绑定地址。修改后通常需要重启服务。",
        "secret": False,
        "restart_required": True,
    },
    {
        "key": "PORT",
        "label": "监听端口",
        "description": "WebUI / OneBot 反向 WS 监听端口。修改后需要重启服务。",
        "secret": False,
        "restart_required": True,
    },
    {
        "key": "DRIVER",
        "label": "NoneBot 驱动",
        "description": "默认保持 ~fastapi。修改后需要重启服务。",
        "secret": False,
        "restart_required": True,
    },
    {
        "key": "ONEBOT_API_TIMEOUT",
        "label": "OneBot API 超时(秒)",
        "description": "大文件、视频、上传场景建议适当拉长。修改后建议重启服务。",
        "secret": False,
        "restart_required": True,
    },
    {
        "key": "ONEBOT_ACCESS_TOKEN",
        "label": "OneBot Access Token",
        "description": "必须与 NapCat OneBot V11 侧 token 保持一致。",
        "secret": True,
        "restart_required": True,
    },
    {
        "key": "WEBUI_TOKEN",
        "label": "WebUI Token",
        "description": "修改后当前登录会话会失效，需要重新登录。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "SKIAPI_KEY",
        "label": "SKIAPI Key",
        "description": "若 config.yml 使用 ${SKIAPI_KEY} 占位符，保存后会自动热重载。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "OPENAI_API_KEY",
        "label": "OpenAI API Key",
        "description": "若 config.yml 使用 ${OPENAI_API_KEY} 占位符，保存后会自动热重载。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "DEEPSEEK_API_KEY",
        "label": "DeepSeek API Key",
        "description": "若 config.yml 使用 ${DEEPSEEK_API_KEY} 占位符，保存后会自动热重载。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "NEWAPI_API_KEY",
        "label": "NEWAPI API Key",
        "description": "聚合网关密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API Key",
        "description": "Claude 系列模型密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "GEMINI_API_KEY",
        "label": "Gemini API Key",
        "description": "Gemini 系列模型密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "OPENROUTER_API_KEY",
        "label": "OpenRouter API Key",
        "description": "OpenRouter 网关密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "XAI_API_KEY",
        "label": "xAI API Key",
        "description": "Grok / xAI 密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "QWEN_API_KEY",
        "label": "Qwen API Key",
        "description": "通义千问密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "MOONSHOT_API_KEY",
        "label": "Moonshot API Key",
        "description": "Moonshot / Kimi 密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "MISTRAL_API_KEY",
        "label": "Mistral API Key",
        "description": "Mistral 密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "ZHIPU_API_KEY",
        "label": "Zhipu API Key",
        "description": "智谱清言密钥。",
        "secret": True,
        "restart_required": False,
    },
    {
        "key": "SILICONFLOW_API_KEY",
        "label": "SiliconFlow API Key",
        "description": "SiliconFlow 密钥。",
        "secret": True,
        "restart_required": False,
    },
)
_ENV_FIELDS_BY_KEY = {str(item["key"]): item for item in _ENV_EDITABLE_FIELDS}
_ENV_RELOAD_SKIP_KEYS = frozenset({
    "HOST",
    "PORT",
    "DRIVER",
    "ONEBOT_API_TIMEOUT",
    "ONEBOT_ACCESS_TOKEN",
    "WEBUI_TOKEN",
})


def _default_prompts() -> dict:
    """提示词默认值，从模板文件。"""
    return load_prompts_template()


def _read_prompts_payload() -> tuple[str, dict, bool]:
    """
    返回 (yaml_text, parsed_dict, generated_default)。
    prompts.yml 缺失时，自动创建默认模板，避免 WebUI 空白。
    """
    if _PROMPTS_FILE.exists():
        try:
            text = _PROMPTS_FILE.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
            if isinstance(parsed, dict):
                return text, parsed, False
        except Exception:
            pass

    # 生成默认
    generated_default = _ensure_prompts_file_from_template(_PROMPTS_FILE)
    if _PROMPTS_FILE.exists():
        try:
            text = _PROMPTS_FILE.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
            return text, parsed, generated_default
        except Exception:
            pass

    # 最后兜底
    default_dict = _default_prompts()
    default_text = yaml.safe_dump(default_dict, allow_unicode=True, default_flow_style=False)
    return default_text, default_dict, True


def _prompt_validation_tool_names() -> set[str]:
    registry = getattr(_engine, "agent_tool_registry", None)
    if registry is None:
        return set()
    if hasattr(registry, "list_tool_names"):
        try:
            return {normalize_text(str(name)) for name in registry.list_tool_names()}
        except Exception:
            return set()
    schemas = getattr(registry, "_schemas", {})
    if isinstance(schemas, dict):
        return {normalize_text(str(name)) for name in schemas.keys()}
    return set()


def _validate_prompts_payload(parsed: dict[str, Any]) -> list[str]:
    errors, warnings = validate_prompt_navigator_payload(
        parsed.get("prompt_navigator"),
        known_tools=_prompt_validation_tool_names(),
    )
    if errors:
        raise HTTPException(400, "；".join(errors))
    return warnings


def init_webui(engine: Any) -> APIRouter:
    """初始化 WebUI，返回 FastAPI router。"""
    global _engine, _start_time
    _engine = engine
    _start_time = time.time()
    # Wire engine reference into chat helpers module
    import core.webui_chat_helpers as _chat_mod
    _chat_mod._engine = engine
    _log.info("WebUI API 已初始化")
    return router


def _ensure_env_file() -> None:
    if _ENV_FILE.exists():
        return
    if _ENV_EXAMPLE_FILE.exists():
        shutil.copy2(_ENV_EXAMPLE_FILE, _ENV_FILE)
        return
    _ENV_FILE.write_text("", encoding="utf-8")


def _read_env_values() -> dict[str, str]:
    _ensure_env_file()
    raw = dotenv_values(_ENV_FILE)
    result: dict[str, str] = {}
    for key, value in raw.items():
        safe_key = normalize_text(str(key))
        if not safe_key:
            continue
        result[safe_key] = normalize_text(str(value)) if value is not None else ""
    return result


def _serialize_env_entries() -> list[dict[str, Any]]:
    raw = _read_env_values()
    entries: list[dict[str, Any]] = []
    for item in _ENV_EDITABLE_FIELDS:
        key = str(item["key"])
        actual = normalize_text(raw.get(key, ""))
        secret = bool(item.get("secret", False))
        entries.append(
            {
                "key": key,
                "label": str(item.get("label", key)),
                "description": str(item.get("description", "")),
                "secret": secret,
                "restart_required": bool(item.get("restart_required", False)),
                "present": bool(actual),
                "value": "***" if secret and actual else actual,
            }
        )
    return entries


def _normalize_env_update_payload(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    raw_env = body.get("env", body)
    if isinstance(raw_env, dict):
        payload = raw_env
    elif isinstance(raw_env, list):
        payload = {}
        for item in raw_env:
            if not isinstance(item, dict):
                continue
            key = normalize_text(str(item.get("key", "")))
            if not key:
                continue
            payload[key] = str(item.get("value", ""))
    else:
        raise HTTPException(400, "env 必须是对象或数组")

    normalized: dict[str, str] = {}
    for raw_key, raw_value in payload.items():
        key = normalize_text(str(raw_key))
        if not key:
            continue
        if key not in _ENV_FIELDS_BY_KEY:
            raise HTTPException(400, f"不允许修改环境变量: {key}")
        normalized[key] = str(raw_value) if raw_value is not None else ""
    return normalized


def _validate_env_value(key: str, value: str) -> str:
    safe_key = normalize_text(key)
    text = str(value).strip()
    if safe_key == "PORT":
        if not text.isdigit():
            raise HTTPException(400, "PORT 必须是 1-65535 的数字")
        port = int(text)
        if port < 1 or port > 65535:
            raise HTTPException(400, "PORT 必须是 1-65535")
        return str(port)
    if safe_key == "ONEBOT_API_TIMEOUT":
        if not text.isdigit():
            raise HTTPException(400, "ONEBOT_API_TIMEOUT 必须是正整数秒数")
        timeout = int(text)
        if timeout < 5 or timeout > 3600:
            raise HTTPException(400, "ONEBOT_API_TIMEOUT 建议在 5-3600 秒之间")
        return str(timeout)
    if safe_key in {"HOST", "DRIVER"} and not text:
        raise HTTPException(400, f"{safe_key} 不能为空")
    return text


async def _apply_env_updates(updates: dict[str, str]) -> dict[str, Any]:
    env_before = _read_env_values()
    changed_keys: list[str] = []

    for key, raw_value in updates.items():
        meta = _ENV_FIELDS_BY_KEY[key]
        current_value = normalize_text(env_before.get(key, ""))
        next_value = str(raw_value)
        if bool(meta.get("secret", False)) and next_value == "***":
            next_value = current_value
        next_value = _validate_env_value(key, next_value)

        if next_value == current_value:
            continue

        if next_value:
            set_key(str(_ENV_FILE), key, next_value, quote_mode="never")
            os.environ[key] = next_value
        else:
            with contextlib.suppress(Exception):
                unset_key(str(_ENV_FILE), key)
            os.environ.pop(key, None)
        changed_keys.append(key)

    reload_required = any(key not in _ENV_RELOAD_SKIP_KEYS for key in changed_keys)
    restart_required = any(bool(_ENV_FIELDS_BY_KEY[key].get("restart_required", False)) for key in changed_keys)
    reauth_required = "WEBUI_TOKEN" in changed_keys

    reload_ok = True
    reload_message = ""
    if changed_keys and reload_required and _engine is not None:
        reload_ok, reload_message = await _reload_engine_config(_engine)

    return {
        "changed_keys": changed_keys,
        "restart_required": restart_required,
        "reauth_required": reauth_required,
        "reload_ok": reload_ok,
        "reload_message": reload_message,
    }


def _get_token() -> str:
    """从环境变量获取 WEBUI_TOKEN。"""
    return os.environ.get("WEBUI_TOKEN", "")


def _extract_bearer_token(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _extract_request_auth_token(request: Request) -> str:
    header_token = _extract_bearer_token(request.headers.get("Authorization", ""))
    if header_token:
        return header_token
    return normalize_text(request.cookies.get(_WEBUI_AUTH_COOKIE, ""))


def _extract_websocket_auth_token(ws: WebSocket) -> str:
    header_token = _extract_bearer_token(ws.headers.get("Authorization", ""))
    if header_token:
        return header_token
    return normalize_text(ws.cookies.get(_WEBUI_AUTH_COOKIE, ""))


def _is_valid_auth_token(candidate: str, expected: str | None = None) -> bool:
    import hmac
    token = str(expected or _get_token() or "").strip()
    if not token:
        return False
    return hmac.compare_digest(normalize_text(candidate), token)


def _auth_cookie_secure_from_scheme(scheme: str) -> bool:
    return str(scheme or "").lower() == "https"


def _set_auth_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=_WEBUI_AUTH_COOKIE,
        value=str(token),
        httponly=True,
        samesite="strict",
        secure=_auth_cookie_secure_from_scheme(request.url.scheme),
        max_age=_WEBUI_AUTH_COOKIE_MAX_AGE,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_WEBUI_AUTH_COOKIE,
        httponly=True,
        samesite="strict",
        path="/",
    )


async def _reload_engine_config(engine: Any) -> tuple[bool, str]:
    """兼容同步/异步的配置重载入口。"""
    reloader = getattr(engine, "reload_config", None)
    if not callable(reloader):
        return False, "引擎不支持 reload_config"

    result = reloader()
    if inspect.isawaitable(result):
        result = await result

    if isinstance(result, tuple):
        if len(result) >= 2:
            return bool(result[0]), str(result[1])
        if len(result) == 1:
            return bool(result[0]), "ok" if bool(result[0]) else "reload_failed"
        return True, "ok"
    if result is None:
        return True, "ok"
    return bool(result), "ok" if bool(result) else "reload_failed"


async def _check_auth(request: Request) -> None:
    """检查 Bearer token / HttpOnly cookie 认证。"""
    token = _get_token()
    if not token:
        raise HTTPException(403, "WEBUI_TOKEN 未配置")
    auth_token = _extract_request_auth_token(request)
    if not _is_valid_auth_token(auth_token, token):
        raise HTTPException(401, "认证失败")


async def _check_ws_auth(ws: WebSocket) -> bool:
    token = _get_token()
    if not token:
        await ws.close(code=1008, reason="WEBUI_TOKEN 未配置")
        return False
    auth_token = _extract_websocket_auth_token(ws)
    if _is_valid_auth_token(auth_token, token):
        return True
    await ws.close(code=1008, reason="Unauthorized")
    return False


def _mask_sensitive(data: dict) -> dict:
    """遮蔽 config dict 中的敏感字段，替换为 ***。"""
    result = copy.deepcopy(data)
    for dotpath in _SENSITIVE_PATHS:
        keys = dotpath.split(".")
        _mask_sensitive_walk(result, keys, 0)
    return result


def _mask_sensitive_walk(node: Any, keys: list[str], idx: int) -> None:
    """递归遍历 keys 路径并遮蔽叶子值，支持通配符 *。"""
    if not isinstance(node, dict) or idx >= len(keys):
        return
    k = keys[idx]
    if idx == len(keys) - 1:
        # 叶子层：执行遮蔽
        if k == "*":
            for sub_key in node:
                val = node[sub_key]
                if isinstance(val, str) and val.strip():
                    node[sub_key] = "***"
        elif k in node:
            val = node[k]
            if isinstance(val, str) and val.strip():
                node[k] = "***"
    else:
        # 中间层：继续递归
        if k == "*":
            for sub in node.values():
                if isinstance(sub, dict):
                    _mask_sensitive_walk(sub, keys, idx + 1)
        elif k in node:
            _mask_sensitive_walk(node[k], keys, idx + 1)


def _deep_merge(base: dict, patch: dict, sensitive_paths: set[str], prefix: str = "") -> dict:
    """深度合并两个字典，跳过敏感路径。"""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        current_path = f"{prefix}.{key}" if prefix else key

        if current_path in sensitive_paths:
            # 跳过敏感字段
            continue

        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value, sensitive_paths, current_path)
        else:
            result[key] = copy.deepcopy(value)

    return result


def _is_masked_secret_placeholder(value: Any) -> bool:
    return isinstance(value, str) and normalize_text(value) == "***"


def _restore_masked_sensitive_values(
    submitted: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """将提交配置中的敏感占位符 *** 恢复为当前真实值，避免覆盖密钥。"""
    restored = copy.deepcopy(submitted)
    base = current if isinstance(current, dict) else {}
    for dotpath in _SENSITIVE_PATHS:
        keys = [k for k in dotpath.split(".") if k]
        if not keys:
            continue
        _restore_masked_path(restored, base, keys)
    return restored


def _restore_masked_path(target: Any, source: Any, keys: list[str]) -> None:
    if not keys:
        return

    head = keys[0]
    if head == "*":
        if isinstance(target, list):
            source_list = source if isinstance(source, list) else []
            for idx, item in enumerate(target):
                source_item = source_list[idx] if idx < len(source_list) else None
                _restore_masked_path(item, source_item, keys[1:])
        elif isinstance(target, dict):
            source_dict = source if isinstance(source, dict) else {}
            for key, item in list(target.items()):
                _restore_masked_path(item, source_dict.get(key), keys[1:])
        return

    if not isinstance(target, dict):
        return

    if len(keys) == 1:
        if head not in target:
            return
        if not _is_masked_secret_placeholder(target.get(head)):
            return
        replacement = source.get(head) if isinstance(source, dict) else None
        if isinstance(replacement, str) and replacement.strip():
            target[head] = replacement
        else:
            target.pop(head, None)
        return

    next_target = target.get(head)
    next_source = source.get(head) if isinstance(source, dict) else None
    _restore_masked_path(next_target, next_source, keys[1:])


def _read_log_tail(log_path: Path, lines: int = 100) -> list[str]:
    """读取日志文件的最后 N 行。"""
    if not log_path.exists():
        return []

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return all_lines[-lines:] if lines > 0 else all_lines
    except Exception as e:
        _log.error(f"读取日志失败: {e}")
        return []


def _resolve_log_file_path() -> Path:
    """解析 WebUI 日志文件路径，优先使用实际生效的 FileHandler 路径。"""
    candidates: list[Path] = []

    engine_logger = getattr(getattr(_engine, "logger", None), "handlers", None)
    if isinstance(engine_logger, list):
        for handler in engine_logger:
            raw_path = normalize_text(str(getattr(handler, "baseFilename", "")))
            if not raw_path:
                continue
            path = Path(raw_path).expanduser().resolve()
            if path not in candidates:
                candidates.append(path)

    storage_dir = getattr(_engine, "storage_dir", None)
    if storage_dir:
        storage_log = (Path(str(storage_dir)).expanduser().resolve() / "logs" / "yukiko.log")
        if storage_log not in candidates:
            candidates.append(storage_log)

    default_candidates = [
        (_ROOT_DIR / "storage" / "logs" / "yukiko.log").resolve(),
        (_ROOT_DIR / "yukiko.log").resolve(),
    ]
    for path in default_candidates:
        if path not in candidates:
            candidates.append(path)

    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _resolve_auth_attempt_store_path() -> Path:
    """登录失败限速持久化文件。"""
    return _ROOT_DIR / "storage" / "webui_auth_attempts.json"


def _split_log_chunks(raw_line: str) -> list[str]:
    """将日志行按时间戳分割成多个条目。"""
    if not raw_line:
        return []

    parts = _LOG_SPLIT_RE.split(raw_line)
    return [p.strip() for p in parts if p.strip()]


def _resolve_webui_db_path(db_name: str) -> Path:
    """解析数据库名称到实际路径。"""
    raw_name = db_name.strip()
    # 路径穿越防护：拒绝 .. 段和绝对路径
    if ".." in raw_name or raw_name.startswith("/") or raw_name.startswith("\\") or ":" in raw_name:
        raise HTTPException(400, "数据库名称包含非法字符")
    db_name = raw_name.lower()
    storage_dir = _ROOT_DIR / "storage"

    # 特殊名称映射
    if db_name in ("main", "yukiko", "bot"):
        default_main_db = storage_dir / "yukiko.db"
        if _is_allowed_webui_db_path(default_main_db, storage_dir):
            return default_main_db

    # 直接 .db 路径
    db_path = storage_dir / f"{db_name}.db"
    if _is_allowed_webui_db_path(db_path, storage_dir):
        return db_path

    # 允许传入完整文件名（包含后缀）
    db_path_no_ext = storage_dir / db_name
    if _is_allowed_webui_db_path(db_path_no_ext, storage_dir):
        return db_path_no_ext

    # 兼容 storage/<name>/<name>.db
    if db_path_no_ext.is_dir():
        nested_named_db = db_path_no_ext / f"{db_name}.db"
        if _is_allowed_webui_db_path(nested_named_db, storage_dir):
            return nested_named_db

        nested_db_files = sorted(
            p for p in db_path_no_ext.glob("*.db")
            if _is_allowed_webui_db_path(p, storage_dir)
        )
        if len(nested_db_files) == 1:
            return nested_db_files[0]

    # 递归匹配 storage/**/<name>.db（例如 storage/memory/vector/memory.db）
    target_file_name = db_name if db_name.endswith(".db") else f"{db_name}.db"
    recursive_matches = [
        p for p in sorted(storage_dir.rglob("*.db"))
        if _is_allowed_webui_db_path(p, storage_dir)
        and (p.name.lower() == target_file_name or p.stem.lower() == db_name)
    ]
    if len(recursive_matches) == 1:
        return recursive_matches[0]
    if len(recursive_matches) > 1:
        _log.warning(
            "数据库名称匹配到多个文件，使用首个: name=%s selected=%s total=%d",
            raw_name or db_name,
            recursive_matches[0],
            len(recursive_matches),
        )
        return recursive_matches[0]

    raise HTTPException(404, f"数据库 {raw_name or db_name} 不存在")


def _is_allowed_webui_db_path(db_path: Path, storage_dir: Path | None = None) -> bool:
    """限制 WebUI 可浏览的数据库范围，排除内部备份库。"""
    try:
        resolved = db_path.resolve()
    except Exception:
        return False
    root = (storage_dir or (_ROOT_DIR / "storage")).resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root):
        return False
    if resolved.suffix.lower() != ".db":
        return False
    rel_parts = [part.lower() for part in resolved.relative_to(root).parts]
    if rel_parts[:1] == ["backups"]:
        return False
    return True


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开 SQLite 数据库。"""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _open_sqlite_readwrite(db_path: Path) -> sqlite3.Connection:
    """以读写模式打开 SQLite 数据库。"""
    return sqlite3.connect(str(db_path))


def _normalize_repo_http_url(remote_url: str) -> str:
    remote = normalize_text(str(remote_url)).strip()
    if not remote:
        return ""

    if remote.startswith(("http://", "https://")):
        return remote.removesuffix(".git").rstrip("/")

    ssh_match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", remote)
    if ssh_match:
        host = ssh_match.group(1).strip()
        path = ssh_match.group(2).strip().strip("/")
        if host and path:
            return f"https://{host}/{path}".rstrip("/")

    parsed = urlparse(remote)
    if parsed.scheme in {"ssh", "git"} and parsed.hostname and parsed.path:
        path = parsed.path.strip("/").removesuffix(".git")
        if path:
            return f"https://{parsed.hostname}/{path}".rstrip("/")

    return ""


def _build_repo_artifact_urls(repo_http_url: str, branch: str) -> dict[str, str]:
    repo_url = normalize_text(repo_http_url).strip().rstrip("/")
    branch_name = normalize_text(branch).strip() or "main"
    urls = {
        "repo_http_url": repo_url,
        "windows_zip_url": "",
        "bootstrap_url": "",
        "guide_url": "",
    }
    if not repo_url:
        return urls

    urls["windows_zip_url"] = f"{repo_url}/archive/refs/heads/{branch_name}.zip"
    urls["guide_url"] = f"{repo_url}/blob/{branch_name}/docs/zh-CN/GUIDE.md"

    github_match = re.match(r"^https://github\.com/([^/]+)/([^/]+)$", repo_url)
    if github_match:
        owner = github_match.group(1).strip()
        repo = github_match.group(2).strip()
        urls["bootstrap_url"] = (
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch_name}/bootstrap.sh"
        )
    return urls


def _resolve_command(candidates: list[str]) -> str:
    for candidate in candidates:
        text = normalize_text(str(candidate)).strip()
        if not text:
            continue
        path = Path(text)
        if path.is_file():
            return str(path)
        found = shutil.which(text)
        if found:
            return found
    return ""


def _resolve_python_command() -> str:
    return _resolve_command(
        [
            str(_ROOT_DIR / ".venv" / "Scripts" / "python.exe"),
            str(_ROOT_DIR / ".venv" / "bin" / "python"),
            "python3",
            "python",
        ]
    )


def _resolve_npm_command() -> str:
    return _resolve_command(["npm.cmd", "npm"])


def _clip_command_output(text: str, *, max_lines: int = 120, max_chars: int = 12_000) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    rows = [line.rstrip() for line in raw.splitlines()]
    if len(rows) > max_lines:
        rows = rows[:max_lines] + ["...(output truncated)"]
    clipped = "\n".join(rows)
    if len(clipped) > max_chars:
        clipped = clipped[:max_chars] + "\n...(output truncated)"
    return clipped


def _run_command_sync(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 60.0,
) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=str((cwd or _ROOT_DIR).resolve()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=max(1.0, float(timeout_seconds)),
        check=False,
    )
    merged = "\n".join(
        part.strip()
        for part in (proc.stdout or "", proc.stderr or "")
        if normalize_text(part).strip()
    ).strip()
    return int(proc.returncode), _clip_command_output(merged)


async def _run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 60.0,
) -> tuple[int, str]:
    return await asyncio.to_thread(
        _run_command_sync,
        args,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_update_task_logs(logs: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in logs:
        text = _clip_command_output(normalize_text(str(item)))
        if text:
            cleaned.append(text)
    if len(cleaned) > _UPDATE_TASK_MAX_LOG_ENTRIES:
        return cleaned[-_UPDATE_TASK_MAX_LOG_ENTRIES:]
    return cleaned


def _task_progress_for_stage(stage: str, fallback: int = 0) -> int:
    stage_key = normalize_text(stage).strip().lower()
    if stage_key in _UPDATE_STAGE_PROGRESS:
        return _UPDATE_STAGE_PROGRESS[stage_key]
    return max(0, min(100, int(fallback)))


def _snapshot_update_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(task.get("task_id", "")),
        "status": str(task.get("status", "unknown")),
        "stage": str(task.get("stage", "")),
        "progress": int(task.get("progress", 0) or 0),
        "logs": list(task.get("logs", [])),
        "error": str(task.get("error", "")),
        "started_at": str(task.get("started_at", "")),
        "updated_at": str(task.get("updated_at", "")),
        "ended_at": str(task.get("ended_at", "")),
        "options": dict(task.get("options", {})),
        "result": task.get("result"),
    }


def _cleanup_update_tasks_locked(now_ts: float) -> None:
    stale_ids: list[str] = []
    for task_id, task in _UPDATE_TASKS.items():
        ended_ts = float(task.get("ended_ts", 0.0) or 0.0)
        if ended_ts <= 0:
            continue
        if now_ts - ended_ts > _UPDATE_TASK_RETENTION_SECONDS:
            stale_ids.append(task_id)
    for task_id in stale_ids:
        _UPDATE_TASKS.pop(task_id, None)

    if len(_UPDATE_TASKS) <= 100:
        return

    ended_tasks = sorted(
        (
            (tid, float(info.get("ended_ts", 0.0) or 0.0))
            for tid, info in _UPDATE_TASKS.items()
            if str(info.get("status", "")) != "running"
        ),
        key=lambda row: row[1],
    )
    for task_id, _ in ended_tasks:
        if len(_UPDATE_TASKS) <= 60:
            break
        _UPDATE_TASKS.pop(task_id, None)


async def _create_update_task(*, allow_dirty: bool, sync_python: bool, build_webui: bool) -> dict[str, Any]:
    now_ts = time.time()
    now_iso = _utc_iso_now()
    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id,
        "status": "running",
        "stage": "queued",
        "progress": 0,
        "logs": [],
        "error": "",
        "started_at": now_iso,
        "updated_at": now_iso,
        "ended_at": "",
        "started_ts": now_ts,
        "updated_ts": now_ts,
        "ended_ts": 0.0,
        "result": None,
        "options": {
            "allow_dirty": bool(allow_dirty),
            "sync_python": bool(sync_python),
            "build_webui": bool(build_webui),
        },
    }
    async with _UPDATE_TASKS_LOCK:
        _cleanup_update_tasks_locked(now_ts)
        _UPDATE_TASKS[task_id] = task
        return _snapshot_update_task(task)


async def _find_running_update_task() -> dict[str, Any] | None:
    async with _UPDATE_TASKS_LOCK:
        for task in _UPDATE_TASKS.values():
            if str(task.get("status", "")) == "running":
                return _snapshot_update_task(task)
    return None


async def _get_update_task(task_id: str) -> dict[str, Any] | None:
    safe_task_id = normalize_text(task_id).strip()
    if not safe_task_id:
        return None
    async with _UPDATE_TASKS_LOCK:
        task = _UPDATE_TASKS.get(safe_task_id)
        return _snapshot_update_task(task) if isinstance(task, dict) else None


async def _update_update_task(
    task_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    log: str | None = None,
    error: str | None = None,
    result: dict[str, Any] | None = None,
    logs: list[str] | None = None,
) -> dict[str, Any] | None:
    safe_task_id = normalize_text(task_id).strip()
    if not safe_task_id:
        return None

    now_ts = time.time()
    now_iso = _utc_iso_now()
    async with _UPDATE_TASKS_LOCK:
        task = _UPDATE_TASKS.get(safe_task_id)
        if not isinstance(task, dict):
            return None

        if status:
            task["status"] = normalize_text(status).strip() or task.get("status", "running")
        if stage:
            task["stage"] = normalize_text(stage).strip() or task.get("stage", "")
        if progress is not None:
            task["progress"] = max(0, min(100, int(progress)))
        if logs is not None:
            task["logs"] = _normalize_update_task_logs(logs)
        if log:
            text = _clip_command_output(normalize_text(log))
            if text:
                task_logs = list(task.get("logs", []))
                task_logs.append(text)
                task["logs"] = _normalize_update_task_logs(task_logs)
        if error is not None:
            task["error"] = normalize_text(str(error))
        if result is not None:
            task["result"] = result

        if str(task.get("status", "")) in {"done", "failed"}:
            if not str(task.get("ended_at", "")):
                task["ended_at"] = now_iso
            task["ended_ts"] = now_ts
            if not task.get("progress"):
                task["progress"] = 100

        task["updated_at"] = now_iso
        task["updated_ts"] = now_ts
        _cleanup_update_tasks_locked(now_ts)
        return _snapshot_update_task(task)


async def _run_update_task(
    task_id: str,
    *,
    allow_dirty: bool,
    sync_python: bool,
    build_webui: bool,
) -> None:
    async def _progress_hook(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        stage = normalize_text(str(event.get("stage", ""))).strip().lower()
        progress_raw = event.get("progress", None)
        progress_value: int | None = None
        if progress_raw is not None:
            try:
                progress_value = int(progress_raw)
            except (TypeError, ValueError):
                progress_value = None
        if progress_value is None and stage:
            progress_value = _task_progress_for_stage(stage)
        log_text = event.get("log", None)
        payload_log = normalize_text(str(log_text)) if log_text is not None else None
        await _update_update_task(
            task_id,
            stage=stage or None,
            progress=progress_value,
            log=payload_log,
        )

    await _update_update_task(
        task_id,
        stage="checking",
        progress=_task_progress_for_stage("checking"),
        log="更新任务已开始，正在检查远程状态",
    )
    try:
        result = await _execute_update(
            allow_dirty=allow_dirty,
            sync_python=sync_python,
            build_webui=build_webui,
            progress_hook=_progress_hook,
        )
        ok = bool(result.get("ok", False))
        final_status = "done" if ok else "failed"
        final_stage = "completed" if ok else "failed"
        final_error = "" if ok else str(result.get("message", "更新失败"))
        final_logs = list(result.get("logs", []))
        await _update_update_task(
            task_id,
            status=final_status,
            stage=final_stage,
            progress=_task_progress_for_stage(final_stage),
            error=final_error,
            result=result,
            logs=final_logs,
        )
    except Exception as exc:
        _log.exception("webui async update task crashed: %s", exc)
        await _update_update_task(
            task_id,
            status="failed",
            stage="failed",
            progress=_task_progress_for_stage("failed"),
            error=str(exc),
            log=f"更新任务异常中断: {exc}",
        )


async def _collect_update_status(fetch_remote: bool = True) -> dict[str, Any]:
    git_cmd = _resolve_command(["git.exe", "git"])
    platform_name = "windows" if os.name == "nt" else "linux"
    base = {
        "ok": False,
        "platform": platform_name,
        "update_supported": False,
        "git_available": bool(git_cmd),
        "repo_available": bool((_ROOT_DIR / ".git").is_dir()),
        "branch": "",
        "upstream": "",
        "local_commit": "",
        "remote_commit": "",
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "repo_http_url": "",
        "windows_zip_url": "",
        "bootstrap_url": "",
        "guide_url": "",
        "message": "",
        "logs": [],
    }
    if not git_cmd:
        base["message"] = "当前环境缺少 git，无法检查远程更新"
        return base
    if not (_ROOT_DIR / ".git").is_dir():
        base["message"] = f"当前目录不是 git 仓库: {_ROOT_DIR}"
        return base

    logs: list[str] = []
    if fetch_remote:
        rc, out = await _run_command(
            [git_cmd, "-C", str(_ROOT_DIR), "fetch", "--prune", "--tags", "origin"],
            timeout_seconds=180,
        )
        if out:
            logs.append(out)
        if rc != 0:
            base["message"] = out or "git fetch 失败"
            base["logs"] = logs
            return base

    rc, branch = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout_seconds=30,
    )
    branch_name = normalize_text(branch)
    if rc != 0 or not branch_name:
        base["message"] = branch or "无法识别当前分支"
        base["logs"] = logs
        return base

    rc, upstream_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        timeout_seconds=30,
    )
    upstream_name = normalize_text(upstream_out) if rc == 0 else f"origin/{branch_name}"

    rc, local_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "rev-parse", "--short", "HEAD"],
        timeout_seconds=30,
    )
    local_commit = normalize_text(local_out) if rc == 0 else ""

    rc, remote_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "rev-parse", "--short", upstream_name],
        timeout_seconds=30,
    )
    remote_commit = normalize_text(remote_out) if rc == 0 else ""

    ahead = 0
    behind = 0
    rc, counts_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "rev-list", "--left-right", "--count", f"{upstream_name}...HEAD"],
        timeout_seconds=30,
    )
    if rc == 0:
        parts = [p for p in counts_out.split() if p]
        if len(parts) >= 2:
            try:
                behind = int(parts[0] or 0)
                ahead = int(parts[1] or 0)
            except ValueError:
                ahead = 0
                behind = 0

    rc, dirty_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "status", "--porcelain"],
        timeout_seconds=30,
    )
    dirty = bool(normalize_text(dirty_out)) if rc == 0 else False

    rc, remote_url_out = await _run_command(
        [git_cmd, "-C", str(_ROOT_DIR), "remote", "get-url", "origin"],
        timeout_seconds=30,
    )
    repo_http_url = _normalize_repo_http_url(remote_url_out if rc == 0 else "")
    artifacts = _build_repo_artifact_urls(repo_http_url, branch_name)

    base.update(
        {
            "ok": True,
            "update_supported": True,
            "branch": branch_name,
            "upstream": upstream_name,
            "local_commit": local_commit,
            "remote_commit": remote_commit,
            "ahead": ahead,
            "behind": behind,
            "dirty": dirty,
            "repo_http_url": artifacts["repo_http_url"],
            "windows_zip_url": artifacts["windows_zip_url"],
            "bootstrap_url": artifacts["bootstrap_url"],
            "guide_url": artifacts["guide_url"],
            "message": "ok",
            "logs": logs,
        }
    )
    return base


async def _execute_update(
    *,
    allow_dirty: bool = False,
    sync_python: bool = True,
    build_webui: bool = True,
    progress_hook: Any = None,
) -> dict[str, Any]:
    async def _emit(stage: str, *, progress: int | None = None, log: str = "") -> None:
        if not callable(progress_hook):
            return
        payload: dict[str, Any] = {"stage": normalize_text(stage).strip().lower()}
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        text = _clip_command_output(normalize_text(log))
        if text:
            payload["log"] = text
        try:
            result = progress_hook(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    await _emit("checking", progress=_task_progress_for_stage("checking"), log="开始检查远程更新状态")
    status = await _collect_update_status(fetch_remote=True)
    if not bool(status.get("ok")):
        fail_message = str(status.get("message", "无法检查更新"))
        await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
        return {
            "ok": False,
            "message": fail_message,
            "logs": list(status.get("logs", [])),
            "status": status,
            "restart_required": False,
            "restart_hint": "",
        }

    logs = list(status.get("logs", []))
    logs.append(
        "branch={branch} upstream={upstream} local={local} remote={remote} ahead={ahead} behind={behind} dirty={dirty}".format(
            branch=status.get("branch", "-"),
            upstream=status.get("upstream", "-"),
            local=status.get("local_commit", "-"),
            remote=status.get("remote_commit", "-"),
            ahead=int(status.get("ahead", 0) or 0),
            behind=int(status.get("behind", 0) or 0),
            dirty=int(bool(status.get("dirty", False))),
        )
    )
    await _emit("checking", progress=_task_progress_for_stage("checking"), log=logs[-1])

    if bool(status.get("dirty")) and not allow_dirty:
        fail_message = "工作区存在未提交改动，已阻止自动更新。需要的话可在 WebUI 强制允许 dirty 更新。"
        await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
        return {
            "ok": False,
            "message": fail_message,
            "logs": logs,
            "status": status,
            "restart_required": False,
            "restart_hint": "",
        }

    git_cmd = _resolve_command(["git.exe", "git"])
    assert git_cmd
    pulled = False
    if int(status.get("behind", 0) or 0) > 0:
        await _emit(
            "pulling",
            progress=_task_progress_for_stage("pulling"),
            log=f"检测到本地落后 {int(status.get('behind', 0) or 0)} 个提交，开始拉取远程更新",
        )
        rc, out = await _run_command(
            [git_cmd, "-C", str(_ROOT_DIR), "pull", "--ff-only"],
            timeout_seconds=600,
        )
        if out:
            logs.append(out)
            await _emit("pulling", progress=_task_progress_for_stage("pulling"), log=out)
        if rc != 0:
            fail_message = out or "git pull 失败"
            await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
            return {
                "ok": False,
                "message": fail_message,
                "logs": logs,
                "status": status,
                "restart_required": False,
                "restart_hint": "",
            }
        pulled = True
    else:
        up_to_date = f"Already up to date with {status.get('upstream') or 'origin'}"
        logs.append(up_to_date)
        await _emit("pulling", progress=_task_progress_for_stage("pulling"), log=up_to_date)

    if sync_python:
        await _emit("pip_install", progress=_task_progress_for_stage("pip_install"), log="开始同步 Python 依赖")
        python_cmd = _resolve_python_command()
        if not python_cmd:
            skip_message = "跳过 Python 依赖同步：未找到 python"
            logs.append(skip_message)
            await _emit("pip_install", progress=_task_progress_for_stage("pip_install"), log=skip_message)
        else:
            rc, out = await _run_command(
                [python_cmd, "-m", "pip", "install", "-r", str(_ROOT_DIR / "requirements.txt")],
                timeout_seconds=1800,
            )
            if out:
                logs.append(out)
                await _emit("pip_install", progress=_task_progress_for_stage("pip_install"), log=out)
            if rc != 0:
                fail_message = out or "pip install 失败"
                await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
                return {
                    "ok": False,
                    "message": fail_message,
                    "logs": logs,
                    "status": status,
                    "restart_required": pulled,
                    "restart_hint": "部分文件可能已更新，请在确认后手动重启服务。",
                }

    if build_webui and (_ROOT_DIR / "webui" / "package.json").is_file():
        npm_cmd = _resolve_npm_command()
        if not npm_cmd:
            skip_message = "跳过 WebUI 构建：未找到 npm"
            logs.append(skip_message)
            await _emit("npm_install", progress=_task_progress_for_stage("npm_install"), log=skip_message)
        else:
            await _emit("npm_install", progress=_task_progress_for_stage("npm_install"), log="开始安装 WebUI 依赖")
            rc, out = await _run_command(
                [npm_cmd, "install", "--no-audit", "--no-fund"],
                cwd=_ROOT_DIR / "webui",
                timeout_seconds=1800,
            )
            if out:
                logs.append(out)
                await _emit("npm_install", progress=_task_progress_for_stage("npm_install"), log=out)
            if rc != 0:
                fail_message = out or "npm install 失败"
                await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
                return {
                    "ok": False,
                    "message": fail_message,
                    "logs": logs,
                    "status": status,
                    "restart_required": pulled,
                    "restart_hint": "代码可能已更新，但前端构建失败，请处理后再重启。",
                }

            await _emit("npm_build", progress=_task_progress_for_stage("npm_build"), log="开始构建 WebUI 产物")
            rc, out = await _run_command(
                [npm_cmd, "run", "build"],
                cwd=_ROOT_DIR / "webui",
                timeout_seconds=1800,
            )
            if out:
                logs.append(out)
                await _emit("npm_build", progress=_task_progress_for_stage("npm_build"), log=out)
            if rc != 0:
                fail_message = out or "npm run build 失败"
                await _emit("failed", progress=_task_progress_for_stage("failed"), log=fail_message)
                return {
                    "ok": False,
                    "message": fail_message,
                    "logs": logs,
                    "status": status,
                    "restart_required": pulled,
                    "restart_hint": "代码可能已更新，但前端构建失败，请处理后再重启。",
                }

    await _emit("finalize", progress=_task_progress_for_stage("finalize"), log="更新步骤完成，正在刷新仓库状态")
    updated_status = await _collect_update_status(fetch_remote=False)
    restart_required = pulled
    restart_hint = (
        "代码已拉取到最新版本。为了让 Python 代码更新真正生效，请手动重启当前服务/进程。"
        if restart_required
        else "当前已经是最新版本，没有新的代码需要重启生效。"
    )
    await _emit("completed", progress=_task_progress_for_stage("completed"), log=restart_hint)
    return {
        "ok": True,
        "message": "更新流程完成",
        "logs": logs,
        "status": updated_status,
        "restart_required": restart_required,
        "restart_hint": restart_hint,
    }


def _validate_sqlite_upload(db_file: Path) -> tuple[bool, list[str], str]:
    try:
        if not db_file.is_file():
            return False, [], "上传文件不存在"
        if db_file.stat().st_size < len(_SQLITE_HEADER):
            return False, [], "文件太小，不像有效的 SQLite 数据库"
        with db_file.open("rb") as fh:
            header = fh.read(len(_SQLITE_HEADER))
        if header != _SQLITE_HEADER:
            return False, [], "文件头不是 SQLite 数据库"

        conn = _open_sqlite_readonly(db_file)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA quick_check(1)")
            row = cursor.fetchone()
            check_value = normalize_text(str(row[0] if row else "")).lower()
            if check_value and check_value != "ok":
                return False, [], f"SQLite quick_check 未通过: {check_value}"

            # 安全: 拒绝含 trigger 的数据库 — 防止恶意代码在后续写入时自动执行
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'")
            trigger_count = int(cursor.fetchone()[0] or 0)
            if trigger_count > 0:
                return False, [], f"上传的数据库包含 {trigger_count} 个 trigger，已拒绝导入"

            # 安全: 拒绝含 view 的数据库 — 防止恶意 view 引用不存在的表或注入查询
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='view'")
            view_count = int(cursor.fetchone()[0] or 0)
            if view_count > 0:
                _log.warning("security | db_import_has_views | count=%d | file=%s", view_count, db_file)

            tables = _list_tables(conn, include_system=True)
            return True, tables, ""
        finally:
            conn.close()
    except Exception as exc:
        return False, [], f"无法读取 SQLite 数据库: {exc}"


def _build_db_backup_path(db_path: Path, *, label: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = _ROOT_DIR / "storage" / "backups" / "db"
    backup_dir.mkdir(parents=True, exist_ok=True)
    suffix = db_path.suffix or ".db"
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", normalize_text(label).strip() or "backup")
    return backup_dir / f"{db_path.stem}-{safe_label}-{stamp}{suffix}"


def _restore_sqlite_database(src_db: Path, target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if not target_db.exists():
        shutil.copy2(src_db, target_db)
        return

    src_conn = _open_sqlite_readonly(src_db)
    dest_conn = _open_sqlite_readwrite(target_db)
    try:
        src_conn.backup(dest_conn)
        dest_conn.commit()
    finally:
        with contextlib.suppress(Exception):
            src_conn.close()
        with contextlib.suppress(Exception):
            dest_conn.close()


def _quote_ident(name: str) -> str:
    """安全引用 SQL 标识符。"""
    if _SQL_IDENT_RE.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _list_tables(conn: sqlite3.Connection, include_system: bool = False) -> list[str]:
    """列出数据库中的所有表。"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]

    if not include_system:
        tables = [t for t in tables if not t.startswith("sqlite_")]

    return tables


def _table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    """获取表的列信息。"""
    cursor = conn.cursor()
    quoted_table = _quote_ident(table)
    cursor.execute(f"PRAGMA table_info({quoted_table})")

    columns = []
    for row in cursor.fetchall():
        columns.append({
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default_value": row[4],
            "pk": bool(row[5]),
        })

    return columns


def _json_safe_value(value: Any, max_len: int = 1000) -> Any:
    """将值转换为 JSON 安全的格式。"""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")[:max_len]
        except UnicodeDecodeError:
            return f"<binary {len(value)} bytes>"

    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _deep_merge_plain(base: dict, patch: dict) -> dict:
    """简单的深度合并，不考虑敏感路径。"""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_plain(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _control_defaults() -> dict:
    """WebUI 控制面板的默认值。"""
    return {
        "temperature": 0.8,
        "max_tokens": 8192,
        "image_gen_enable": False,
        "image_gen_default_model": "dall-e-3",
        "image_gen_default_size": "1024x1024",
        "image_gen_nsfw_filter": True,
    }


def _to_float(value: Any) -> float | None:
    """安全转换为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _control_value_missing(value: Any) -> bool:
    """判断控制值是否缺失。"""
    return value is None or (isinstance(value, str) and not value.strip())


def _derive_chat_mode_from_runtime(routing: Any, self_check: Any) -> str:
    """从运行时配置推导聊天模式。"""
    # 根据 routing 和 self_check 的状态判断模式
    if routing and getattr(routing, "enable", False):
        if self_check and getattr(self_check, "enable", False):
            return "agent_with_selfcheck"
        return "agent"

    if self_check and getattr(self_check, "enable", False):
        return "selfcheck"

    return "direct"


def _derive_control_defaults_from_runtime(config: dict) -> dict:
    """从运行时配置推导控制面板默认值。"""
    e = _engine
    if not e:
        return _control_defaults()

    result = {}

    # API 配置
    api_cfg = config.get("api", {})
    result["temperature"] = _to_float(api_cfg.get("temperature")) or 0.8
    result["max_tokens"] = int(api_cfg.get("max_tokens", 8192))

    # Agent 配置
    agent = getattr(e, "agent", None)
    if agent:
        result["agent_enable"] = getattr(agent, "enable", False)
        routing = getattr(agent, "routing", None)
        self_check = getattr(agent, "self_check", None)
        result["chat_mode"] = _derive_chat_mode_from_runtime(routing, self_check)

        if routing:
            result["routing_enable"] = getattr(routing, "enable", False)
            result["routing_threshold"] = _to_float(getattr(routing, "threshold", 0.5)) or 0.5

        if self_check:
            result["selfcheck_enable"] = getattr(self_check, "enable", False)
            result["selfcheck_threshold"] = _to_float(getattr(self_check, "threshold", 0.5)) or 0.5
    else:
        result["agent_enable"] = False
        result["chat_mode"] = "direct"

    # Safety 配置
    safety = getattr(e, "safety", None)
    if safety:
        result["safety_enable"] = getattr(safety, "enable", False)
        result["safety_scale"] = int(getattr(safety, "scale", 2))
    else:
        result["safety_enable"] = False
        result["safety_scale"] = 2

    # Output 配置
    output_cfg = config.get("output", {})
    result["verbosity"] = output_cfg.get("verbosity", "medium")
    result["token_saving"] = output_cfg.get("token_saving", False)

    # 图片生成配置
    image_gen_cfg = config.get("image_gen", {})
    result["image_gen_enable"] = image_gen_cfg.get("enable", False)
    result["image_gen_default_model"] = image_gen_cfg.get("default_model", "dall-e-3")
    result["image_gen_default_size"] = image_gen_cfg.get("default_size", "1024x1024")
    result["image_gen_nsfw_filter"] = image_gen_cfg.get("nsfw_filter", True)

    return result


def _webui_config_defaults() -> dict:
    """WebUI 配置的默认值结构。"""
    return {
        "webui_controls": _control_defaults(),
    }


def _inject_webui_config_defaults(config: dict) -> dict:
    """注入 WebUI 特有的配置默认值。"""
    if "webui_controls" not in config:
        config["webui_controls"] = _derive_control_defaults_from_runtime(config)
    return config


def _inject_control_defaults(config: dict) -> dict:
    """注入控制面板默认值到配置中。"""
    controls = _derive_control_defaults_from_runtime(config)
    if "webui_controls" not in config:
        config["webui_controls"] = {}
    config["webui_controls"].update(controls)
    return config


def _apply_control_mapping(config: dict) -> dict:
    """将 WebUI 控制面板的值映射回实际配置字段。"""
    controls = config.get("webui_controls", {})
    if not controls:
        return config

    # API 配置
    if "api" not in config:
        config["api"] = {}

    if "temperature" in controls and _control_value_missing(config["api"].get("temperature")) and not _control_value_missing(controls["temperature"]):
        config["api"]["temperature"] = _to_float(controls["temperature"]) or 0.8

    if "max_tokens" in controls and _control_value_missing(config["api"].get("max_tokens")) and not _control_value_missing(controls["max_tokens"]):
        config["api"]["max_tokens"] = int(controls["max_tokens"])

    # Agent 配置（重要）
    # webui_controls 曾包含旧版 chat_mode/routing_threshold/selfcheck_threshold 映射，
    # 会在保存时覆盖 agent.*，导致“明明改了却没生效”。
    # 当前配置页已直接编辑 agent.* / control.*，这里不再做隐式覆盖。

    # Safety 配置
    if "safety" not in config:
        config["safety"] = {}

    if "safety_enable" in controls and "enable" not in config["safety"]:
        config["safety"]["enable"] = bool(controls["safety_enable"])

    if "safety_scale" in controls and _control_value_missing(config["safety"].get("scale")) and not _control_value_missing(controls["safety_scale"]):
        config["safety"]["scale"] = int(controls["safety_scale"])

    # Output 配置
    if "output" not in config:
        config["output"] = {}

    if "verbosity" in controls and _control_value_missing(config["output"].get("verbosity")) and not _control_value_missing(controls["verbosity"]):
        config["output"]["verbosity"] = controls["verbosity"]

    if "token_saving" in controls and "token_saving" not in config["output"]:
        config["output"]["token_saving"] = bool(controls["token_saving"])

    return config


def _strip_deprecated_local_paths_config(config: dict) -> dict:
    """清理已废弃的本地关键词/本地方法相关配置键。"""
    payload = copy.deepcopy(config) if isinstance(config, dict) else {}

    control = payload.get("control")
    if isinstance(control, dict):
        control.pop("heuristic_rules_enable", None)
        control.pop("disable_local_keyword_paths", None)

    routing = payload.get("routing")
    if isinstance(routing, dict):
        routing.pop("enable_keyword_heuristics", None)
        mode = normalize_text(str(routing.get("mode", ""))).lower()
        if mode and mode != "ai_full":
            routing["mode"] = "ai_full"

    search = payload.get("search")
    if isinstance(search, dict):
        tool_interface = search.get("tool_interface")
        if isinstance(tool_interface, dict):
            tool_interface.pop("local_enable", None)
            tool_interface.pop("local_allowed_roots", None)
            tool_interface.pop("local_allow_project_root", None)
            tool_interface.pop("local_allow_sensitive_files", None)
            tool_interface.pop("local_read_max_chars", None)

    # 纯 AI 路由后废弃的本地 cue/intent 配置键
    payload.pop("intent", None)

    memory = payload.get("memory")
    if isinstance(memory, dict):
        memory.pop("agent_directive_cues", None)
        memory.pop("agent_directive_target_cues", None)

    knowledge = payload.get("knowledge_update")
    if isinstance(knowledge, dict):
        knowledge.pop("explicit_fact_cues", None)
        knowledge.pop("speculative_cues", None)
        knowledge.pop("fragment_short_cues", None)
        knowledge.pop("question_prefixes", None)
        knowledge.pop("question_tokens", None)

    agent = payload.get("agent")
    if isinstance(agent, dict):
        high_risk = agent.get("high_risk_control")
        if isinstance(high_risk, dict):
            high_risk.pop("confirm_cues", None)
            high_risk.pop("cancel_cues", None)

    followup = payload.get("search_followup")
    if isinstance(followup, dict):
        followup.pop("resend_media_cues", None)
    return payload


def _normalize_plugin_rules(raw: Any) -> list[str]:
    """规范化插件 rules 字段。"""
    if isinstance(raw, str):
        item = normalize_text(raw)
        return [item] if item else []

    if isinstance(raw, list):
        return [normalize_text(str(item)) for item in raw if normalize_text(str(item))]

    if isinstance(raw, dict):
        items: list[str] = []
        for key, value in raw.items():
            left = normalize_text(str(key))
            right = normalize_text(str(value))
            if left and right:
                items.append(f"{left}: {right}")
            elif left:
                items.append(left)
        return items

    return []


def _resolve_unified_plugins_file(registry: Any) -> Path:
    """解析统一插件配置文件路径（优先已有文件）。"""
    config_dir = Path(str(getattr(registry, "_config_dir", _ROOT_DIR / "config")))
    candidates = [
        config_dir / "plugins.yml",
        config_dir / "Plugins.yml",
        config_dir / "plugin.yml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _display_path(path: Path) -> str:
    """优先返回相对项目根目录的路径，便于 WebUI 展示。"""
    with contextlib.suppress(Exception):
        return str(path.resolve().relative_to(_ROOT_DIR.resolve())).replace("\\", "/")
    return str(path)


def _plugin_source_info(engine: Any, registry: Any, name: str) -> tuple[str, str]:
    """推断插件配置来源与配置文件路径。"""
    unified_cfg = getattr(registry, "_unified_plugin_config", {})
    unified_path = _resolve_unified_plugins_file(registry)
    if isinstance(unified_cfg, dict):
        unified_item = unified_cfg.get(name)
        if isinstance(unified_item, dict) and unified_item:
            return _display_path(unified_path), str(unified_path)

    plugin_cfg_dir = Path(str(getattr(registry, "_plugin_config_dir", _ROOT_DIR / "plugins" / "config")))
    plugin_cfg_path = plugin_cfg_dir / f"{name}.yml"
    if plugin_cfg_path.is_file():
        return _display_path(plugin_cfg_path), str(plugin_cfg_path)

    global_plugins = {}
    if isinstance(getattr(engine, "config", None), dict):
        raw_plugins = engine.config.get("plugins", {})
        if isinstance(raw_plugins, dict):
            global_plugins = raw_plugins
    global_item = global_plugins.get(name)
    global_cfg_file = _ROOT_DIR / "config" / "config.yml"
    if isinstance(global_item, dict) and global_item:
        return f"{_display_path(global_cfg_file)}:plugins.{name}", str(global_cfg_file)

    return "default", ""


def _resolve_plugin_local_config_file(registry: Any, plugin_name: str) -> Path:
    plugin_cfg_dir = Path(str(getattr(registry, "_plugin_config_dir", _ROOT_DIR / "plugins" / "config")))
    return plugin_cfg_dir / f"{plugin_name}.yml"


def _should_write_plugin_local_config(current: dict[str, Any], registry: Any, plugin_name: str) -> bool:
    config_target = normalize_text(str(current.get("config_target", "")))
    if config_target.startswith(f"plugins/config/{plugin_name}.yml"):
        return True

    config_file = normalize_text(str(current.get("config_file", "")))
    if config_file:
        with contextlib.suppress(Exception):
            current_path = Path(config_file).resolve()
            local_path = _resolve_plugin_local_config_file(registry, plugin_name).resolve()
            if current_path == local_path:
                return True

    return bool(current.get("supports_interactive_setup", False))


def _collect_plugins_payload(engine: Any) -> list[dict[str, Any]]:
    """构建插件列表响应。"""
    registry = getattr(engine, "plugins", None)
    plugin_map = getattr(registry, "plugins", {}) if registry else {}
    if not isinstance(plugin_map, dict):
        return []

    schema_map: dict[str, dict[str, Any]] = {}
    schemas = getattr(registry, "schemas", []) if registry else []
    if isinstance(schemas, list):
        for item in schemas:
            if not isinstance(item, dict):
                continue
            name = normalize_text(str(item.get("name", "")))
            if name:
                schema_map[name] = item

    cfg_map = getattr(registry, "_plugin_configs", {}) if registry else {}
    if not isinstance(cfg_map, dict):
        cfg_map = {}
    plugin_meta_map = getattr(registry, "_plugin_meta", {}) if registry else {}
    if not isinstance(plugin_meta_map, dict):
        plugin_meta_map = {}

    payload: list[dict[str, Any]] = []
    for name in sorted(plugin_map.keys(), key=lambda x: normalize_text(str(x)).lower()):
        plugin_obj = plugin_map.get(name)
        schema = schema_map.get(name, {})
        meta = plugin_meta_map.get(name, {})
        if not isinstance(meta, dict):
            meta = {}

        config = copy.deepcopy(cfg_map.get(name, {}))
        if not isinstance(config, dict):
            config = {}

        description = normalize_text(str(getattr(plugin_obj, "description", "")))
        if not description:
            description = normalize_text(str(schema.get("description", "")))

        args_schema = getattr(plugin_obj, "args_schema", None)
        if not isinstance(args_schema, dict):
            args_schema = schema.get("args_schema", {})
        if not isinstance(args_schema, dict):
            args_schema = {}

        config_schema = getattr(plugin_obj, "config_schema", None)
        if not isinstance(config_schema, dict):
            config_schema = schema.get("config_schema", {})
        if not isinstance(config_schema, dict):
            config_schema = {}

        rules = _normalize_plugin_rules(getattr(plugin_obj, "rules", None))
        if not rules:
            rules = _normalize_plugin_rules(schema.get("rules", []))

        source, config_file = _plugin_source_info(engine, registry, name)
        payload.append({
            "name": name,
            "description": description,
            "enabled": bool(config.get("enabled", True)),
            "source": source,
            "config_file": config_file,
            "config": config,
            "args_schema": args_schema,
            "config_schema": config_schema,
            "rules": rules,
            "internal_only": bool(getattr(plugin_obj, "internal_only", False)),
            "agent_tool": bool(getattr(plugin_obj, "agent_tool", False)),
            "configurable": bool(meta.get("configurable", False)),
            "config_target": normalize_text(str(meta.get("config_target", ""))),
            "config_guide": [normalize_text(str(item)) for item in (meta.get("config_guide") or []) if normalize_text(str(item))],
            "editable_keys": [normalize_text(str(item)) for item in (meta.get("editable_keys") or []) if normalize_text(str(item))],
            "supports_interactive_setup": bool(meta.get("supports_interactive_setup", False)),
            "needs_setup": bool(meta.get("needs_setup", False)),
            "using_defaults": bool(meta.get("using_defaults", False)),
            "setup_mode": normalize_text(str(meta.get("setup_mode", ""))),
        })

    return payload


def _get_plugin_payload(engine: Any, plugin_name: str) -> dict[str, Any] | None:
    """按名称获取单个插件信息。"""
    target = normalize_text(plugin_name)
    for item in _collect_plugins_payload(engine):
        if normalize_text(str(item.get("name", ""))) == target:
            return item
    return None


def _load_yaml_dict(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """读取 YAML 对象，strict=True 时失败抛错。"""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        if strict:
            raise
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError(f"YAML 根节点必须是对象: {path}")
    return {}


def _save_yaml_dict(path: Path, data: dict[str, Any]) -> None:
    """保存 YAML 对象。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


_setup_support = WebUISetupSupport(
    root_dir=_ROOT_DIR,
    prompts_file=_PROMPTS_FILE,
    logger=_log,
    load_yaml_dict=_load_yaml_dict,
    restore_masked_sensitive_values=_restore_masked_sensitive_values,
    is_masked_secret_placeholder=_is_masked_secret_placeholder,
    strip_deprecated_local_paths_config=_strip_deprecated_local_paths_config,
)
setup_router = _setup_support.router
_normalize_image_gen_models_for_save = _setup_support._normalize_image_gen_models_for_save
_ensure_image_gen_default_model = _setup_support._ensure_image_gen_default_model
_setup_resolve_image_gen_base_url = _setup_support._setup_resolve_image_gen_base_url


_route_context = WebUIRouteContext(
    get_engine=lambda: _engine,
    get_start_time=lambda: _start_time,
    get_token=_get_token,
    check_auth=_check_auth,
    check_ws_auth=_check_ws_auth,
    set_auth_cookie=_set_auth_cookie,
    clear_auth_cookie=_clear_auth_cookie,
    count_registered_napcat_tools=lambda: _count_registered_napcat_tools(),
    collect_napcat_status=lambda bot_id="": _collect_napcat_status(bot_id=bot_id),
    resolve_log_file_path=_resolve_log_file_path,
    resolve_auth_attempt_store_path=_resolve_auth_attempt_store_path,
    read_log_tail=_read_log_tail,
    split_log_chunks=_split_log_chunks,
    cookie_capabilities_payload=_setup_support.cookie_capabilities_payload,
    start_bilibili_qr_session=_setup_support.start_bilibili_qr_session,
    bilibili_qr_status=_setup_support.bilibili_qr_status,
    cancel_bilibili_qr_session=_setup_support.cancel_bilibili_qr_session,
    logger=_log,
)
router.include_router(build_auth_status_router(_route_context))
router.include_router(build_log_router(_route_context))
router.include_router(build_cookie_router(_route_context))


# ============================================================================
# API Endpoints
# ============================================================================


@router.get("/env", dependencies=[Depends(_check_auth)])
async def get_env_settings():
    return {
        "env_file": str(_ENV_FILE),
        "entries": _serialize_env_entries(),
    }


@router.put("/env", dependencies=[Depends(_check_auth)])
async def put_env_settings(request: Request):
    body = await request.json()
    updates = _normalize_env_update_payload(body)
    result = await _apply_env_updates(updates)
    if result["changed_keys"] and not result["reload_ok"]:
        raise HTTPException(500, result["reload_message"] or "环境变量已写入，但热重载失败")
    message = "环境变量未变化"
    if result["changed_keys"]:
        message = "环境变量已更新"
        if result["restart_required"]:
            message += "，部分变更需要重启服务生效"
        elif result["reload_ok"]:
            message += "，已完成热重载"
    return {
        "ok": True,
        "message": message,
        "changed_keys": result["changed_keys"],
        "restart_required": result["restart_required"],
        "reauth_required": result["reauth_required"],
        "reload_ok": result["reload_ok"],
        "reload_message": result["reload_message"],
        "entries": _serialize_env_entries(),
    }


@router.get("/system/update/status", dependencies=[Depends(_check_auth)])
async def system_update_status():
    status = await _collect_update_status(fetch_remote=True)
    return {"status": status}


@router.post("/system/update/start", dependencies=[Depends(_check_auth)])
async def system_update_start(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    running = await _find_running_update_task()
    if running:
        return {
            "ok": True,
            "task_id": running.get("task_id", ""),
            "task": running,
            "reused": True,
        }

    allow_dirty = bool(body.get("allow_dirty", False))
    sync_python = bool(body.get("sync_python", True))
    build_webui = bool(body.get("build_webui", True))
    created = await _create_update_task(
        allow_dirty=allow_dirty,
        sync_python=sync_python,
        build_webui=build_webui,
    )
    task_id = str(created.get("task_id", "")).strip()
    asyncio.create_task(
        _run_update_task(
            task_id,
            allow_dirty=allow_dirty,
            sync_python=sync_python,
            build_webui=build_webui,
        )
    )
    return {
        "ok": True,
        "task_id": task_id,
        "task": created,
        "reused": False,
    }


@router.get("/system/update/task", dependencies=[Depends(_check_auth)])
async def system_update_task(task_id: str = Query("", description="更新任务ID")):
    safe_task_id = normalize_text(task_id).strip()
    if not safe_task_id:
        raise HTTPException(400, "task_id 不能为空")
    task = await _get_update_task(safe_task_id)
    if not task:
        raise HTTPException(404, "更新任务不存在或已过期")
    return {"task": task}


@router.post("/system/update/run", dependencies=[Depends(_check_auth)])
async def system_update_run(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    result = await _execute_update(
        allow_dirty=bool(body.get("allow_dirty", False)),
        sync_python=bool(body.get("sync_python", True)),
        build_webui=bool(body.get("build_webui", True)),
    )
    if not bool(result.get("ok")):
        raise HTTPException(400, str(result.get("message", "更新失败")))
    return result


@router.get("/config", dependencies=[Depends(_check_auth)])
async def get_config():
    """获取当前配置（已脱敏）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    raw = getattr(e.config_manager, "raw", {})
    masked = _mask_sensitive(raw if isinstance(raw, dict) else {})
    masked = _strip_deprecated_local_paths_config(masked)
    masked = _inject_webui_config_defaults(masked)

    # 注入 admin 相关信息
    admin = getattr(e, "admin", None)
    admin_cfg = masked.get("admin")
    if not isinstance(admin_cfg, dict):
        admin_cfg = {}
        masked["admin"] = admin_cfg

    if admin:
        white = sorted(int(x) for x in getattr(admin, "_white", set()) if str(x).strip())
        if white:
            admin_cfg["whitelist_groups"] = white

        super_users = sorted(str(x).strip() for x in getattr(admin, "_super_users", set()) if str(x).strip())
        if super_users:
            admin_cfg["super_users"] = super_users
            # 如果 super_admin_qq 为空，用第一个 super_user 填充
            if not str(admin_cfg.get("super_admin_qq", "")).strip():
                admin_cfg["super_admin_qq"] = super_users[0]

    return {"config": masked}


@router.put("/config", dependencies=[Depends(_check_auth)])
async def put_config(request: Request):
    """更新配置并热重载。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    body = await request.json()
    new_config = body.get("config", {})
    if not isinstance(new_config, dict):
        raise HTTPException(400, "config 必须是对象")

    current_config_raw = getattr(getattr(e, "config_manager", None), "raw", {})
    if isinstance(current_config_raw, dict):
        new_config = _restore_masked_sensitive_values(new_config, current_config_raw)

    new_config = _strip_deprecated_local_paths_config(new_config)

    # 应用控制面板映射
    new_config = _apply_control_mapping(new_config)

    # 合并到模板
    template = load_config_template()
    merged = _deep_merge_template(template, new_config)

    # 写入文件（原子操作）
    config_file = _ROOT_DIR / "config" / "config.yml"
    _safe_write_yaml(config_file, merged)

    # 热重载
    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "配置热重载失败")
        _log.info("配置已更新并重载")
        return {"ok": True, "message": "配置已保存并重载"}
    except Exception as ex:
        _log.error(f"重载配置失败: {ex}")
        raise HTTPException(500, f"重载失败: {ex}")


@router.get("/prompts", dependencies=[Depends(_check_auth)])
async def get_prompts():
    """获取提示词配置。"""
    text, parsed, generated = _read_prompts_payload()
    return {
        "content": text,
        "yaml_text": text,
        "parsed": parsed,
        "generated_default": generated,
    }


@router.put("/prompts", dependencies=[Depends(_check_auth)])
async def put_prompts(request: Request):
    """完整替换提示词配置。"""
    raw_body = await request.body()
    body: Any = {}
    if raw_body:
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            body = {}

    yaml_text = ""
    if isinstance(body, str):
        yaml_text = body
    elif isinstance(body, dict):
        yaml_text = body.get("yaml_text", "")
        if not isinstance(yaml_text, str) or not yaml_text.strip():
            yaml_text = body.get("content", "")
    elif raw_body:
        with contextlib.suppress(Exception):
            yaml_text = raw_body.decode("utf-8")

    if not isinstance(yaml_text, str):
        yaml_text = ""

    if not yaml_text.strip():
        raise HTTPException(400, "yaml_text/content 不能为空")

    # 验证 YAML 格式
    try:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(400, "YAML 必须是对象")
    except Exception as e:
        raise HTTPException(400, f"YAML 格式错误: {e}")
    warnings = _validate_prompts_payload(parsed)

    # 写入文件
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPTS_FILE.write_text(yaml_text, encoding="utf-8")

    # 重载提示词
    _pl.reload()
    _log.info("提示词已更新并重载")

    message = "提示词已保存并重载"
    if warnings:
        message += "；警告: " + "；".join(warnings[:6])
    return {"ok": True, "message": message, "parsed": parsed, "warnings": warnings}


@router.patch("/prompts", dependencies=[Depends(_check_auth)])
async def patch_prompts(request: Request):
    """部分更新提示词配置。"""
    body = await request.json()
    patch = body.get("patch", {})

    if not isinstance(patch, dict):
        raise HTTPException(400, "patch 必须是对象")

    # 读取当前配置
    _, current, _ = _read_prompts_payload()

    # 合并
    merged = _deep_merge_plain(current, patch)
    warnings = _validate_prompts_payload(merged)

    # 写入
    yaml_text = yaml.safe_dump(merged, allow_unicode=True, default_flow_style=False, sort_keys=False)
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPTS_FILE.write_text(yaml_text, encoding="utf-8")

    # 重载
    _pl.reload()
    _log.info("提示词已部分更新并重载")

    message = "提示词已更新并重载"
    if warnings:
        message += "；警告: " + "；".join(warnings[:6])
    return {"ok": True, "message": message, "parsed": merged, "warnings": warnings}


@router.post("/reload", dependencies=[Depends(_check_auth)])
async def reload():
    """手动触发配置和提示词重载。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "配置热重载失败")
        _pl.reload()
        return {"ok": True, "message": "配置和提示词已重载"}
    except Exception as ex:
        raise HTTPException(500, f"重载失败: {ex}")


@router.post("/system/restart", dependencies=[Depends(_check_auth)])
async def system_restart():
    """一键重启整个 bot 服务（通过退出进程让 systemd/supervisor 自动拉起）。"""
    import sys as _sys
    _log.warning("webui_restart_requested | 即将重启服务")

    async def _delayed_exit():
        await asyncio.sleep(1.0)
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return {"ok": True, "message": "服务正在重启，请稍后刷新页面"}


@router.post("/system/api-check", dependencies=[Depends(_check_auth)])
async def api_self_check():
    """自检所有已配置的 API 是否可用。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    results: list[dict[str, Any]] = []

    # 检查主模型
    if hasattr(e, "model_client") and e.model_client:
        try:
            test_messages = [{"role": "user", "content": "ping"}]
            reply = await asyncio.wait_for(
                e.model_client.chat_text(test_messages, max_tokens=10),
                timeout=15,
            )
            results.append({
                "name": "主模型 (chat)",
                "status": "ok" if reply else "empty_response",
                "detail": clip_text(normalize_text(reply or ""), 50),
            })
        except Exception as ex:
            results.append({
                "name": "主模型 (chat)",
                "status": "error",
                "detail": str(ex)[:120],
            })

    # 检查图片生成
    if hasattr(e, "image_gen") and e.image_gen:
        try:
            health_check = getattr(e.image_gen, "health_check", None)
            model_results: list[dict[str, Any]] = []
            if callable(health_check):
                model_results = await asyncio.wait_for(health_check(), timeout=20)
            if model_results:
                for item in model_results[:12]:
                    name = str(item.get("name", "图片模型"))
                    status = str(item.get("status", "unknown"))
                    detail = str(item.get("detail", ""))[:120]
                    results.append(
                        {
                            "name": f"图片生成/{name}",
                            "status": status,
                            "detail": detail,
                        }
                    )
            else:
                models = getattr(e.image_gen, "models", []) or []
                results.append({
                    "name": "图片生成",
                    "status": "ok" if models else "no_models",
                    "detail": f"{len(models)} 个模型已配置",
                })
        except Exception as ex:
            results.append({
                "name": "图片生成",
                "status": "error",
                "detail": str(ex)[:120],
            })

    # 检查搜索引擎
    if hasattr(e, "search") and e.search:
        try:
            test_result = await asyncio.wait_for(
                e.search.search("test", max_results=1),
                timeout=10,
            )
            results.append({
                "name": "搜索引擎",
                "status": "ok" if test_result else "no_results",
                "detail": f"返回 {len(test_result) if test_result else 0} 条结果",
            })
        except Exception as ex:
            results.append({
                "name": "搜索引擎",
                "status": "error",
                "detail": str(ex)[:120],
            })

    return {"ok": True, "results": results}


@router.get("/plugins", dependencies=[Depends(_check_auth)])
async def get_plugins():
    """获取插件管理信息（兼容旧版 WebUI）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")
    return {"plugins": _collect_plugins_payload(e)}


@router.post("/plugins/reload", dependencies=[Depends(_check_auth)])
async def reload_plugins():
    """重载插件配置（兼容旧版 WebUI）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "插件重载失败")
        return {
            "ok": True,
            "message": "插件已重载",
            "plugins": _collect_plugins_payload(e),
        }
    except Exception as ex:
        raise HTTPException(500, f"插件重载失败: {ex}")


@router.get("/plugins/{plugin_name}", dependencies=[Depends(_check_auth)])
async def get_plugin(plugin_name: str):
    """获取单个插件详情（兼容旧版 WebUI）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    item = _get_plugin_payload(e, plugin_name)
    if not item:
        raise HTTPException(404, f"插件不存在: {plugin_name}")
    return {"plugin": item}


@router.put("/plugins/{plugin_name}", dependencies=[Depends(_check_auth)])
async def put_plugin(plugin_name: str, request: Request):
    """更新插件配置并可选热重载（兼容旧版 WebUI）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    current = _get_plugin_payload(e, plugin_name)
    if not current:
        raise HTTPException(404, f"插件不存在: {plugin_name}")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    patch_cfg = body.get("config")
    if patch_cfg is not None and not isinstance(patch_cfg, dict):
        raise HTTPException(400, "config 必须是对象")

    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(400, "enabled 必须是布尔值")

    reload_after = bool(body.get("reload", True))

    # 兼容旧版行为：传 config 时按完整对象覆盖；仅传 enabled 时只改 enabled。
    if isinstance(patch_cfg, dict):
        new_cfg = copy.deepcopy(patch_cfg)
    else:
        new_cfg = copy.deepcopy(current.get("config", {}))
        if not isinstance(new_cfg, dict):
            new_cfg = {}
    if enabled is not None:
        new_cfg["enabled"] = enabled

    registry = getattr(e, "plugins", None)
    unified_path = _resolve_unified_plugins_file(registry)
    write_local = _should_write_plugin_local_config(current, registry, plugin_name)
    local_path = _resolve_plugin_local_config_file(registry, plugin_name)

    try:
        unified_data = _load_yaml_dict(unified_path, strict=True)
    except Exception as ex:
        raise HTTPException(500, f"读取插件配置失败: {ex}")

    try:
        if write_local:
            _save_yaml_dict(local_path, new_cfg)
            if plugin_name in unified_data:
                unified_data.pop(plugin_name, None)
                _save_yaml_dict(unified_path, unified_data)
        else:
            unified_data[plugin_name] = new_cfg
            _save_yaml_dict(unified_path, unified_data)
    except Exception as ex:
        raise HTTPException(500, f"写入插件配置失败: {ex}")

    message = f"插件 {plugin_name} 配置已保存"
    if reload_after:
        try:
            ok, msg = await _reload_engine_config(e)
            if not ok:
                raise RuntimeError(msg or "插件重载失败")
            message = f"插件 {plugin_name} 配置已保存并重载"
        except Exception as ex:
            raise HTTPException(500, f"插件重载失败: {ex}")

    updated = _get_plugin_payload(e, plugin_name)
    if not updated:
        updated = copy.deepcopy(current)
        updated["config"] = new_cfg
        updated["enabled"] = bool(new_cfg.get("enabled", True))
        target_path = local_path if write_local else unified_path
        updated["source"] = _display_path(target_path)
        updated["config_file"] = str(target_path)

    return {"ok": True, "message": message, "plugin": updated}


@router.get("/db/overview", dependencies=[Depends(_check_auth)])
async def db_overview():
    """获取所有数据库概览。"""
    storage_dir = _ROOT_DIR / "storage"
    if not storage_dir.exists():
        return {"databases": []}

    databases = []
    seen_names: set[str] = set()
    for db_file in sorted(storage_dir.rglob("*.db")):
        if not _is_allowed_webui_db_path(db_file, storage_dir):
            continue
        db_name = db_file.stem.lower()
        if db_name in seen_names:
            _log.warning(f"数据库名称冲突，已跳过: name={db_name} path={db_file}")
            continue

        try:
            conn = _open_sqlite_readonly(db_file)
            tables = _list_tables(conn, include_system=False)
            conn.close()
            stat = db_file.stat()

            databases.append({
                "name": db_name,
                "path": str(db_file),
                "exists": True,
                "size_bytes": int(stat.st_size or 0),
                "modified_at": int(stat.st_mtime or 0),
                "table_count": len(tables),
                "tables": tables,
            })
            seen_names.add(db_name)
        except Exception as e:
            _log.warning(f"无法读取数据库 {db_file}: {e}")

    return {"databases": databases}


@router.get("/db/{db_name}/tables", dependencies=[Depends(_check_auth)])
async def db_tables(
    db_name: str,
    include_system: bool = Query(False),
    with_counts: bool = Query(False),
):
    """获取数据库的表列表。"""
    db_path = _resolve_webui_db_path(db_name)
    conn = _open_sqlite_readonly(db_path)

    tables = _list_tables(conn, include_system)
    result = []

    for table in tables:
        columns = _table_columns(conn, table)
        item = {
            "name": table,
            "column_count": len(columns),
            "columns": columns,
        }
        if with_counts:
            try:
                cursor = conn.cursor()
                quoted = _quote_ident(table)
                cursor.execute(f"SELECT COUNT(*) FROM {quoted}")
                item["row_count"] = cursor.fetchone()[0]
            except sqlite3.Error:
                item["row_count"] = None
        result.append(item)

    conn.close()
    return {"db": db_name, "path": str(db_path), "tables": result}


@router.get("/db/{db_name}/rows", dependencies=[Depends(_check_auth)])
async def db_rows(
    db_name: str,
    table: str = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    q: str = Query(""),
    include_system: bool = Query(False),
):
    """获取表的行数据（分页）。"""
    db_path = _resolve_webui_db_path(db_name)
    conn = _open_sqlite_readonly(db_path)

    # 验证表名
    tables = _list_tables(conn, include_system)
    if table not in tables:
        conn.close()
        raise HTTPException(404, f"表 {table} 不存在")

    # 获取列信息
    columns = _table_columns(conn, table)
    column_names = [col["name"] for col in columns]

    # 构建查询
    quoted_table = _quote_ident(table)
    cursor = conn.cursor()

    # 总行数
    cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
    total = cursor.fetchone()[0]

    # 分页查询
    offset = (page - 1) * page_size
    query = f"SELECT * FROM {quoted_table}"

    # 简单搜索（如果提供了 q）
    if q.strip():
        # 在所有文本列中搜索
        text_cols = [col["name"] for col in columns if col["type"].upper() in ("TEXT", "VARCHAR", "CHAR", "")]
        if text_cols:
            conditions = [f"{_quote_ident(col)} LIKE ?" for col in text_cols]
            query += f" WHERE {' OR '.join(conditions)}"
            search_pattern = f"%{q}%"
            cursor.execute(f"{query} LIMIT ? OFFSET ?", [search_pattern] * len(text_cols) + [page_size, offset])
        else:
            cursor.execute(f"{query} LIMIT ? OFFSET ?", (page_size, offset))
    else:
        cursor.execute(f"{query} LIMIT ? OFFSET ?", (page_size, offset))

    rows = cursor.fetchall()
    conn.close()

    # 转换为 JSON 安全格式
    result_rows = []
    for row in rows:
        row_dict = {}
        for i, col_name in enumerate(column_names):
            row_dict[col_name] = _json_safe_value(row[i])
        result_rows.append(row_dict)

    return {
        "db": db_name,
        "table": table,
        "columns": columns,
        "rows": result_rows,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/db/{db_name}/clear", dependencies=[Depends(_check_auth)])
async def db_clear_rows(request: Request, db_name: str):
    """清空指定表的全部数据。"""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    table = normalize_text(str(body.get("table", "")))
    if not table:
        raise HTTPException(400, "table 不能为空")
    if table.lower().startswith("sqlite_"):
        raise HTTPException(400, "不允许清空系统表")

    db_path = _resolve_webui_db_path(db_name)
    conn = _open_sqlite_readwrite(db_path)
    try:
        tables = _list_tables(conn, include_system=False)
        if table not in tables:
            raise HTTPException(404, f"表 {table} 不存在")

        quoted_table = _quote_ident(table)
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")
        before_count = int(cursor.fetchone()[0] or 0)
        cursor.execute(f"DELETE FROM {quoted_table}")
        with contextlib.suppress(Exception):
            cursor.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        conn.commit()
        return {
            "ok": True,
            "message": f"已清空 {table}",
            "db": db_name,
            "table": table,
            "deleted": before_count,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(500, f"清空失败: {exc}") from exc
    finally:
        conn.close()


@router.get("/db/{db_name}/export", dependencies=[Depends(_check_auth)])
async def db_export(db_name: str):
    db_path = _resolve_webui_db_path(db_name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = db_path.suffix or ".db"
    filename = f"{db_path.stem}-{stamp}{suffix}"
    return FileResponse(
        path=str(db_path),
        filename=filename,
        media_type="application/octet-stream",
    )


@router.post("/db/{db_name}/import", dependencies=[Depends(_check_auth)])
async def db_import(db_name: str, file: UploadFile = File(...)):
    db_path = _resolve_webui_db_path(db_name)
    suffix = Path(normalize_text(file.filename or "upload.db")).suffix or ".db"
    temp_dir = _ROOT_DIR / "storage" / "tmp" / "db-imports"
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_path_obj = tempfile.NamedTemporaryFile(
        prefix=f"{db_name}-",
        suffix=suffix,
        dir=temp_dir,
        delete=False,
    )
    temp_path = Path(temp_path_obj.name)
    temp_path_obj.close()

    _DB_IMPORT_MAX_SIZE = 512 * 1024 * 1024  # 512 MB

    try:
        total_written = 0
        with temp_path.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > _DB_IMPORT_MAX_SIZE:
                    raise HTTPException(413, f"上传文件过大，最大允许 {_DB_IMPORT_MAX_SIZE // (1024*1024)} MB")
                fh.write(chunk)

        ok, tables, error = _validate_sqlite_upload(temp_path)
        if not ok:
            raise HTTPException(400, error or "上传文件不是有效的 SQLite 数据库")

        backup_path = _build_db_backup_path(db_path, label="before-import")
        try:
            shutil.copy2(db_path, backup_path)
            _restore_sqlite_database(temp_path, db_path)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"导入数据库失败: {exc}") from exc

        verified_ok, verified_tables, verified_error = _validate_sqlite_upload(db_path)
        if not verified_ok:
            raise HTTPException(500, verified_error or "导入后校验失败")

        return {
            "ok": True,
            "message": f"数据库 {db_name} 已导入成功",
            "db": db_name,
            "path": str(db_path),
            "backup_path": str(backup_path),
            "table_count": len(verified_tables),
            "tables": verified_tables[:40],
            "size_bytes": int(db_path.stat().st_size or 0),
            "restart_recommended": True,
        }
    finally:
        with contextlib.suppress(Exception):
            await file.close()
        with contextlib.suppress(Exception):
            temp_path.unlink(missing_ok=True)


from core.webui_chat_helpers import (  # noqa: E402
    _unwrap_onebot_payload,
    _normalize_chat_type,
    _get_onebot_runtime,
    _onebot_call,
    _count_registered_napcat_tools,
    _collect_napcat_status,
    _render_message_text,
    _recent_contact_chat_type,
    _recent_contact_peer_id,
    _recent_contact_peer_name,
    _recent_contact_int,
    _recent_contact_last_message,
    _format_chat_message_item,
    _resolve_group_bot_role,
    _resolve_group_essence_message_ids,
    _chat_message_item_key,
    _unwrap_onebot_message_payload,
    _resolve_message_scope_from_raw,
    _build_recall_payload_from_message,
    _format_recalled_record_item,
    _normalize_message_segments,
    _resolve_local_path_from_file_uri,
    _decode_base64_payload,
    _guess_media_type_from_hint,
    _is_private_ip,
)



async def _download_image_bytes(url: str) -> tuple[bytes | None, str]:
    target = normalize_text(url)
    if not target:
        return None, "empty_url"

    if target.startswith("base64://") or target.startswith("data:"):
        blob = _decode_base64_payload(target)
        if blob:
            return blob, "base64"
        return None, "invalid_base64"

    # 仅允许 http/https，禁止 file:// 和本地路径读取
    if not (target.startswith("http://") or target.startswith("https://")):
        return None, "unsupported_url"

    # SSRF 防护：拒绝私有 IP
    try:
        parsed = urlparse(target)
        hostname = parsed.hostname or ""
        if _is_private_ip(hostname):
            _log.warning("SSRF blocked: %s", target)
            return None, "ssrf_blocked"
    except Exception:
        return None, "invalid_url"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
            resp = await client.get(target)
        if resp.status_code >= 400:
            return None, f"http_{resp.status_code}"
        if resp.content:
            return resp.content, "http"
        return None, "empty_http_body"
    except Exception as exc:
        return None, f"http_error:{normalize_text(str(exc))}"


async def _resolve_image_preview_bytes(
    file_token: str, *, bot_id: str = ""
) -> tuple[bytes | None, str, str]:
    token = normalize_text(file_token)
    if not token:
        return None, "", "empty_file"

    blob, source = await _download_image_bytes(token)
    if blob:
        return blob, _guess_media_type_from_hint(token, "image/png"), f"token_{source}"

    get_image_payload: Any = None
    get_image_error = ""
    for kwargs in (
        {"file": token},
        {"file_id": token},
        {"id": token},
    ):
        try:
            get_image_payload = await _onebot_call("get_image", bot_id=bot_id, **kwargs)
            get_image_error = ""
            break
        except HTTPException as exc:
            get_image_error = normalize_text(str(exc.detail))
        except Exception as exc:
            get_image_error = normalize_text(str(exc))

    if isinstance(get_image_payload, dict):
        for key in ("path", "file", "file_path", "local_path", "filename"):
            raw_local = normalize_text(str(get_image_payload.get(key, "")))
            if not raw_local:
                continue
            local_file = _resolve_local_path_from_file_uri(raw_local)
            if local_file is None:
                continue
            with contextlib.suppress(Exception):
                data = local_file.read_bytes()
                if data:
                    mime = _guess_media_type_from_hint(
                        local_file.name, "image/png"
                    )
                    return data, mime, f"get_image_{key}"

        for key in ("url", "image_url", "download_url", "src"):
            remote = normalize_text(str(get_image_payload.get(key, "")))
            if not remote:
                continue
            blob, source = await _download_image_bytes(remote)
            if blob:
                mime = _guess_media_type_from_hint(remote, "image/png")
                return blob, mime, f"get_image_{key}_{source}"

    return None, "", get_image_error or "image_not_found"


async def _extract_image_bytes_from_message(payload: dict[str, Any], *, bot_id: str = "") -> tuple[bytes | None, str]:
    if not isinstance(payload, dict):
        return None, "invalid_message_payload"

    segments = _normalize_message_segments(payload.get("message"))
    if not segments:
        segments = _normalize_message_segments(payload.get("raw_message"))

    image_seg = next((seg for seg in segments if normalize_text(str(seg.get("type", ""))).lower() == "image"), None)
    if not image_seg:
        return None, "message_has_no_image_segment"

    data = image_seg.get("data", {}) if isinstance(image_seg, dict) else {}
    if not isinstance(data, dict):
        data = {}

    url_value = normalize_text(str(data.get("url", "") or data.get("image_url", "")))
    if url_value:
        blob, source = await _download_image_bytes(url_value)
        if blob:
            return blob, f"segment_{source}"

    file_value = normalize_text(str(data.get("file", "")))
    if file_value:
        blob, source = await _download_image_bytes(file_value)
        if blob:
            return blob, f"segment_file_{source}"

        get_image_payload = None
        if file_value:
            with contextlib.suppress(Exception):
                get_image_payload = await _onebot_call("get_image", bot_id=bot_id, file=file_value)
        if isinstance(get_image_payload, dict):
            for key in ("path", "file", "file_path", "local_path", "filename"):
                local_path = normalize_text(str(get_image_payload.get(key, "")))
                if not local_path:
                    continue
                local_file = _resolve_local_path_from_file_uri(local_path)
                if local_file is None:
                    continue
                with contextlib.suppress(Exception):
                    bytes_data = local_file.read_bytes()
                    if bytes_data:
                        return bytes_data, f"get_image_{key}"
            for key in ("url", "image_url", "download_url"):
                remote = normalize_text(str(get_image_payload.get(key, "")))
                if not remote:
                    continue
                blob, source = await _download_image_bytes(remote)
                if blob:
                    return blob, f"get_image_{key}_{source}"

    return None, "image_download_failed"


async def _get_onebot_message_detail(message_id: str, *, bot_id: str = "") -> dict[str, Any]:
    mid = normalize_text(message_id)
    if not mid:
        raise HTTPException(400, "message_id 不能为空")
    try:
        result = await _onebot_call("get_msg", bot_id=bot_id, message_id=int(mid))
    except Exception:
        result = await _onebot_call("get_msg", bot_id=bot_id, message_id=mid)
    if not isinstance(result, dict):
        raise HTTPException(502, "get_msg 返回异常")
    return result


def _resolve_sticker_manager() -> Any:
    sticker = getattr(_engine, "sticker", None) if _engine is not None else None
    if sticker is None:
        raise HTTPException(503, "表情包系统未初始化")
    saver = getattr(sticker, "_save_chat_emoji", None)
    if not callable(saver):
        raise HTTPException(503, "表情包系统不支持快捷添加")
    return sticker


@router.get("/chat/conversations", dependencies=[Depends(_check_auth)])
async def chat_conversations(
    limit: int = Query(100, ge=1, le=500),
    bot_id: str = Query(""),
):
    recent_raw = await _onebot_call("get_recent_contact", bot_id=bot_id, count=int(limit))
    recent = recent_raw.get("items", []) if isinstance(recent_raw, dict) else recent_raw
    if not isinstance(recent, list):
        recent = []

    rows: list[dict[str, Any]] = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        chat_type = _recent_contact_chat_type(item)
        peer_id = _recent_contact_peer_id(item)
        if not peer_id:
            continue
        peer_name = _recent_contact_peer_name(item, peer_id)
        ts = _recent_contact_int(item, ["msgTime", "lastTime", "last_time", "time", "timestamp"])
        unread = _recent_contact_int(item, ["unreadCnt", "unread_count", "unreadNum", "unread"])
        last_message = _recent_contact_last_message(item)
        rows.append(
            {
                "conversation_id": f"{chat_type}:{peer_id}",
                "chat_type": chat_type,
                "peer_id": peer_id,
                "peer_name": peer_name,
                "last_time": ts,
                "unread_count": unread,
                "last_message": last_message,
            }
        )

    rows.sort(key=lambda item: int(item.get("last_time", 0) or 0), reverse=True)
    return {"items": rows[: int(limit)], "total": len(rows)}


@router.get("/chat/history", dependencies=[Depends(_check_auth)])
async def chat_history(
    chat_type: str = Query(...),
    peer_id: str = Query(...),
    limit: int = Query(30, ge=1, le=120),
    message_seq: str = Query(""),
    bot_id: str = Query(""),
):
    resolved_type = _normalize_chat_type(chat_type)
    target_id = normalize_text(peer_id)
    conversation_id = _build_recall_conversation_id(resolved_type, target_id)
    if resolved_type not in {"group", "private"}:
        raise HTTPException(400, "chat_type 仅支持 group/private")
    if not target_id:
        raise HTTPException(400, "peer_id 不能为空")

    kwargs: dict[str, Any] = {}
    if normalize_text(message_seq):
        with contextlib.suppress(Exception):
            kwargs["message_seq"] = int(str(message_seq))

    if resolved_type == "group":
        try:
            kwargs["group_id"] = int(target_id)
        except Exception as exc:
            raise HTTPException(400, "group peer_id 必须是数字") from exc
        raw = await _onebot_call("get_group_msg_history", bot_id=bot_id, **kwargs)
    else:
        try:
            kwargs["user_id"] = int(target_id)
        except Exception as exc:
            raise HTTPException(400, "private peer_id 必须是数字") from exc
        kwargs["count"] = int(limit)
        raw = await _onebot_call("get_friend_msg_history", bot_id=bot_id, **kwargs)

    bot = await _get_onebot_runtime(bot_id=bot_id)
    items = raw.get("messages", []) if isinstance(raw, dict) else raw
    if isinstance(raw, dict) and not items:
        items = raw.get("items", [])
    if not isinstance(items, list):
        items = []
    mapped = [_format_chat_message_item(row, bot_self_id=str(getattr(bot, "self_id", ""))) for row in items if isinstance(row, dict)]
    mapped.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
    permission = {
        "bot_role": "",
        "can_recall": False,
        "can_set_essence": False,
    }
    if resolved_type == "group":
        role = await _resolve_group_bot_role(int(target_id), bot_id=bot_id)
        can_group_manage = role in {"owner", "admin"}
        essence_ids = await _resolve_group_essence_message_ids(int(target_id), bot_id=bot_id)
        if essence_ids:
            for item in mapped:
                message_id = normalize_text(str(item.get("message_id", "")))
                seq = normalize_text(str(item.get("seq", "")))
                item["is_essence"] = bool(
                    (message_id and message_id in essence_ids)
                    or (seq and f"seq:{seq}" in essence_ids)
                )
        permission = {
            "bot_role": role,
            "can_recall": can_group_manage,
            "can_set_essence": can_group_manage,
        }
    recalled_items = _list_recalled_messages(conversation_id, limit=max(120, int(limit) * 3))
    if recalled_items:
        existing: dict[str, dict[str, Any]] = {}
        for item in mapped:
            key = _chat_message_item_key(item)
            if key:
                existing[key] = item
        for raw in recalled_items:
            recalled = _format_recalled_record_item(raw)
            key = _chat_message_item_key(recalled)
            current = existing.get(key)
            if current is not None:
                current["is_recalled"] = True
                current["recalled_at"] = int(raw.get("recalled_at", 0) or 0)
                current["recalled_source"] = normalize_text(str(raw.get("source", "")))
                if not normalize_text(str(current.get("text", ""))) and normalize_text(str(recalled.get("text", ""))):
                    current["text"] = recalled["text"]
                if not isinstance(current.get("segments"), list) or not current.get("segments"):
                    current["segments"] = recalled["segments"]
                continue
            mapped.append(recalled)
            if key:
                existing[key] = recalled
        mapped.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
    return {
        "conversation_id": conversation_id,
        "chat_type": resolved_type,
        "peer_id": target_id,
        "items": mapped[-int(limit):],
        "permission": permission,
    }


@router.get("/chat/media/image")
async def chat_media_image(
    request: Request,
    file: str = Query(""),
    bot_id: str = Query(""),
):
    await _check_auth(request)

    file_token = normalize_text(file)
    if not file_token:
        raise HTTPException(400, "file 不能为空")

    blob, media_type, source = await _resolve_image_preview_bytes(
        file_token, bot_id=bot_id
    )
    if not blob:
        raise HTTPException(404, f"图片预览解析失败: {source}")

    return Response(
        content=blob,
        media_type=media_type or "image/png",
        headers={
            "Cache-Control": "private, max-age=20",
            "X-Media-Source": source,
        },
    )


@router.post("/chat/send-text", dependencies=[Depends(_check_auth)])
async def chat_send_text(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")
    resolved_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    text = str(body.get("text", ""))
    reply_to_message_id = normalize_text(str(body.get("reply_to_message_id", "")))
    if resolved_type not in {"group", "private"}:
        raise HTTPException(400, "chat_type 仅支持 group/private")
    if not peer_id:
        raise HTTPException(400, "peer_id 不能为空")
    if not normalize_text(text):
        raise HTTPException(400, "text 不能为空")
    if reply_to_message_id and not reply_to_message_id.isdigit():
        raise HTTPException(400, "reply_to_message_id 必须是数字")
    bot_id = normalize_text(str(body.get("bot_id", "")))

    try:
        peer_num = int(peer_id)
    except Exception as exc:
        raise HTTPException(400, "peer_id 必须是数字") from exc

    message_text = text
    if reply_to_message_id:
        message_text = f"[CQ:reply,id={reply_to_message_id}] {text}"

    if resolved_type == "group":
        result = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=message_text)
    else:
        result = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=message_text)
    message_id = ""
    if isinstance(result, dict):
        message_id = normalize_text(str(result.get("message_id", "") or result.get("id", "")))
    elif isinstance(result, int):
        message_id = str(result)
    return {"ok": True, "message_id": message_id}


@router.post("/chat/agent-text", dependencies=[Depends(_check_auth)])
async def chat_agent_text(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    resolved_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    text = normalize_text(str(body.get("text", "")))
    reply_to_message_id = normalize_text(str(body.get("reply_to_message_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    context_user_id = normalize_text(str(body.get("context_user_id", "")))
    context_user_name = normalize_text(str(body.get("context_user_name", "")))
    context_sender_role = normalize_text(str(body.get("context_sender_role", "")))

    if resolved_type not in {"group", "private"}:
        raise HTTPException(400, "chat_type 仅支持 group/private")
    if not peer_id:
        raise HTTPException(400, "peer_id 不能为空")
    if not text:
        raise HTTPException(400, "text 不能为空")
    if reply_to_message_id and not reply_to_message_id.isdigit():
        raise HTTPException(400, "reply_to_message_id 必须是数字")

    try:
        peer_num = int(peer_id)
    except Exception as exc:
        raise HTTPException(400, "peer_id 必须是数字") from exc

    if _engine is None:
        raise HTTPException(503, "引擎未初始化")

    bot_runtime = await _get_onebot_runtime(bot_id=bot_id)
    bot_self_id = normalize_text(str(getattr(bot_runtime, "self_id", "")))

    queue_cfg = _engine.config.get("queue", {}) if isinstance(getattr(_engine, "config", None), dict) else {}
    if not isinstance(queue_cfg, dict):
        queue_cfg = {}
    isolate_group_by_user = bool(queue_cfg.get("group_isolate_by_user", True))
    if resolved_type == "group" and isolate_group_by_user:
        scoped_user = context_user_id or "webui"
        conversation_id = f"group:{peer_id}:user:{scoped_user}"
    else:
        conversation_id = f"{resolved_type}:{peer_id}"

    inferred_user_id = context_user_id or (peer_id if resolved_type == "private" else "webui")
    inferred_user_name = context_user_name or inferred_user_id or "WebUI 用户"
    trace_id = f"webui-{uuid.uuid4().hex[:10]}"
    message_id = f"webui-{int(time.time() * 1000)}"

    async def runtime_api_call(api: str, **kwargs: Any) -> Any:
        payload = await call_napcat_bot_api(bot_runtime, api, **kwargs)
        return _unwrap_onebot_payload(payload)

    resolved_reply_user_id = context_user_id
    resolved_reply_user_name = context_user_name
    resolved_reply_text = ""
    resolved_reply_media: list[dict[str, Any]] = []
    if reply_to_message_id:
        try:
            from app_helpers import _resolve_reply_context

            (
                reply_user_id,
                reply_user_name,
                resolved_reply_text,
                resolved_reply_media,
            ) = await _resolve_reply_context(bot_runtime, reply_to_message_id)
            resolved_reply_user_id = reply_user_id or resolved_reply_user_id
            resolved_reply_user_name = reply_user_name or resolved_reply_user_name
        except Exception as exc:
            _log.warning(
                "webui_reply_context_resolve_failed | trace=%s | message_id=%s | error=%s",
                trace_id,
                reply_to_message_id,
                clip_text(normalize_text(str(exc)), 180),
            )

    try:
        from core.engine import EngineMessage

        result = await _engine.handle_message(
            EngineMessage(
                conversation_id=conversation_id,
                user_id=inferred_user_id,
                user_name=inferred_user_name,
                text=text,
                message_id=message_id,
                seq=int(time.time() * 1000) % 1_000_000_000,
                queue_depth=0,
                mentioned=True,
                is_private=resolved_type == "private",
                timestamp=datetime.now(timezone.utc),
                group_id=peer_num if resolved_type == "group" else 0,
                bot_id=bot_self_id,
                reply_to_message_id=reply_to_message_id,
                reply_to_user_id=resolved_reply_user_id,
                reply_to_user_name=resolved_reply_user_name,
                reply_to_text=resolved_reply_text,
                reply_media_segments=resolved_reply_media,
                api_call=runtime_api_call,
                trace_id=trace_id,
                sender_role=context_sender_role,
                event_payload={
                    "post_type": "message",
                    "message_type": resolved_type,
                    "sub_type": "webui",
                    "user_id": inferred_user_id,
                    "group_id": peer_num if resolved_type == "group" else 0,
                    "group_name": "",
                    "message_id": message_id,
                    "raw_message": text,
                    "sender": {
                        "user_id": inferred_user_id,
                        "nickname": inferred_user_name,
                        "card": inferred_user_name,
                        "role": context_sender_role or "member",
                    },
                    "raw": {"source": "webui"},
                },
            )
        )
    except Exception as exc:
        tail = clip_text(normalize_text(str(exc)), 260)
        raise HTTPException(502, f"AI 处理失败: {tail}") from exc

    if str(getattr(result, "action", "")) == "ignore":
        return {
            "ok": True,
            "status": "ignored",
            "reason": str(getattr(result, "reason", "")),
            "conversation_id": conversation_id,
            "trace_id": trace_id,
        }

    reply_text = normalize_text(str(getattr(result, "reply_text", "")))
    image_url = normalize_text(str(getattr(result, "image_url", "")))
    image_urls = getattr(result, "image_urls", []) or []
    if not isinstance(image_urls, list):
        image_urls = []
    image_urls = [normalize_text(str(x)) for x in image_urls if normalize_text(str(x))]
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    video_url = normalize_text(str(getattr(result, "video_url", "")))
    audio_file = normalize_text(str(getattr(result, "audio_file", "")))
    if video_url and reply_text:
        local_video_ref = (
            video_url.lower().startswith("file://")
            or bool(re.match(r"^(?:/|[A-Za-z]:[\\/])", video_url))
        )
        local_video_pattern = re.compile(
            r"(?:file://)?(?:/[^\s`，。；、]+|[A-Za-z]:[\\/][^\s`，。；、]+?)\.(?:mp4|webm|mov|m4v)",
            flags=re.IGNORECASE,
        )
        if local_video_ref or local_video_pattern.search(reply_text):
            reply_text = "解析好了，正在投递视频。"

    sent_message_id = ""
    if reply_text:
        send_text = f"[CQ:reply,id={reply_to_message_id}] {reply_text}" if reply_to_message_id else reply_text
        if resolved_type == "group":
            sent = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=send_text)
        else:
            sent = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=send_text)
        if isinstance(sent, dict):
            sent_message_id = normalize_text(str(sent.get("message_id", "") or sent.get("id", "")))
        elif isinstance(sent, int):
            sent_message_id = str(sent)

    # 轻量媒体下发（避免 404 后完全无反馈）
    for img in image_urls[:3]:
        payload: Any = f"[CQ:image,file={img}]"
        try:
            from app_helpers import _build_image_segment

            seg = await _build_image_segment(img)
            if seg is not None:
                seg_type = normalize_text(str(getattr(seg, "type", ""))) or "image"
                seg_data = dict(getattr(seg, "data", {}) or {})
                payload = [{"type": seg_type, "data": seg_data}]
        except Exception as exc:
            _log.warning(
                "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=image_build | peer=%s | image=%s | error=%s",
                trace_id,
                peer_id,
                clip_text(img, 180),
                exc,
            )
        _log.info(
            "media_delivery_image_attempt | source=webui_agent_text | trace=%s | chat=%s | peer=%s | image=%s",
            trace_id,
            resolved_type,
            peer_id,
            clip_text(img, 180),
        )
        api_name = "send_group_msg" if resolved_type == "group" else "send_private_msg"
        try:
            if resolved_type == "group":
                sent = await _onebot_call(api_name, bot_id=bot_id, group_id=peer_num, message=payload)
            else:
                sent = await _onebot_call(api_name, bot_id=bot_id, user_id=peer_num, message=payload)
        except Exception as exc:
            _log.warning(
                "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=image | api=%s | peer=%s | image=%s | error=%s",
                trace_id,
                api_name,
                peer_id,
                clip_text(img, 180),
                exc,
            )
            continue
        image_message_id = ""
        if isinstance(sent, dict):
            image_message_id = normalize_text(str(sent.get("message_id", "") or sent.get("id", "")))
        elif isinstance(sent, int):
            image_message_id = str(sent)
        _log.info(
            "media_delivery_image_ok | source=webui_agent_text | trace=%s | peer=%s | message_id=%s | image=%s",
            trace_id,
            peer_id,
            image_message_id,
            clip_text(img, 180),
        )

    if audio_file:
        audio_ref = audio_file
        audio_ref_plain = ""
        lower_audio = audio_file.lower()
        if not lower_audio.startswith(("http://", "https://", "base64://", "data:")):
            try:
                audio_path = (
                    napcat_file_uri_to_path(audio_file)
                    if lower_audio.startswith("file://")
                    else Path(audio_file)
                )
                if audio_path is not None and audio_path.exists() and audio_path.is_file():
                    if audio_path.suffix.lower() != ".silk":
                        silk_sibling = audio_path.with_suffix(".silk")
                        try:
                            if silk_sibling.exists() and silk_sibling.is_file() and silk_sibling.stat().st_size > 1024:
                                _log.info(
                                    "media_delivery_record_silk_prefer | source=webui_agent_text | trace=%s | src=%s | silk=%s",
                                    trace_id,
                                    clip_text(str(audio_path), 160),
                                    clip_text(str(silk_sibling), 160),
                                )
                                audio_path = silk_sibling
                        except Exception:
                            pass
                    from app_helpers import _stage_media_for_napcat

                    staged_audio = _stage_media_for_napcat(audio_path, trace_id=trace_id) or audio_path
                    audio_ref = build_napcat_file_reference(staged_audio, require_exists=True) or str(staged_audio)
                    audio_ref_plain = str(staged_audio.expanduser().resolve())
            except Exception as exc:
                _log.warning(
                    "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=record_stage | peer=%s | file=%s | error=%s",
                    trace_id,
                    peer_id,
                    clip_text(audio_file, 180),
                    exc,
                )
        if audio_ref:
            refs = [audio_ref]
            if audio_ref_plain and audio_ref_plain not in refs:
                refs.append(audio_ref_plain)
            for ref_index, ref in enumerate(refs, 1):
                payload = [{"type": "record", "data": {"file": ref}}]
                try:
                    _log.info(
                        "media_delivery_record_attempt | source=webui_agent_text | trace=%s | chat=%s | peer=%s | attempt=%d/%d | file=%s",
                        trace_id,
                        resolved_type,
                        peer_id,
                        ref_index,
                        len(refs),
                        clip_text(ref, 180),
                    )
                    if resolved_type == "group":
                        sent = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=payload)
                    else:
                        sent = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=payload)
                    if isinstance(sent, dict):
                        sent_message_id = normalize_text(str(sent.get("message_id", "") or sent.get("id", "") or sent_message_id))
                    elif isinstance(sent, int):
                        sent_message_id = str(sent)
                    _log.info(
                        "media_delivery_record_ok | source=webui_agent_text | trace=%s | peer=%s | message_id=%s | file=%s",
                        trace_id,
                        peer_id,
                        sent_message_id,
                        clip_text(ref, 180),
                    )
                    break
                except Exception as exc:
                    _log.warning(
                        "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=record | peer=%s | attempt=%d/%d | file=%s | error=%s",
                        trace_id,
                        peer_id,
                        ref_index,
                        len(refs),
                        clip_text(ref, 180),
                        exc,
                    )
                    continue
            else:
                _log.info(
                    "media_delivery_record_give_up | source=webui_agent_text | trace=%s | chat=%s | peer=%s | file=%s",
                    trace_id,
                    resolved_type,
                    peer_id,
                    clip_text(audio_ref, 180),
                )

    if video_url:
        from app_helpers import (
            _build_video_segment,
            _should_upload_video_file_first,
            _stage_media_for_napcat,
            _video_strategy_allows_upload_fallback,
            _video_strategy_upload_first,
        )

        staged_video_ref = ""
        delivery_errors: list[str] = []
        bot_cfg = _engine.config.get("bot", {}) if isinstance(getattr(_engine, "config", None), dict) else {}
        if not isinstance(bot_cfg, dict):
            bot_cfg = {}
        video_send_strategy = normalize_text(str(bot_cfg.get("video_send_strategy", "direct_first"))).lower() or "direct_first"
        napcat_media_stage_dir = normalize_text(str(bot_cfg.get("napcat_media_stage_dir", "")))
        prefer_upload_first = (
            _video_strategy_upload_first(video_send_strategy)
            or _should_upload_video_file_first(video_url)
        )
        allow_upload_fallback = (
            prefer_upload_first
            or _video_strategy_allows_upload_fallback(video_send_strategy)
        )

        async def _send_video_segment(*, prefer_plain_path: bool) -> bool:
            nonlocal staged_video_ref, sent_message_id
            segment = await _build_video_segment(
                video_url,
                stage_dir=napcat_media_stage_dir,
                prefer_plain_path=prefer_plain_path,
                trace_id=trace_id,
            )
            if segment is None:
                reason = f"build_video_segment_returned_none:plain={prefer_plain_path}"
                delivery_errors.append(reason)
                _log.warning(
                    "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=inline_video_build | plain=%s | peer=%s | error=%s",
                    trace_id,
                    prefer_plain_path,
                    peer_id,
                    reason,
                )
                return False
            seg_type = normalize_text(str(getattr(segment, "type", ""))) or "video"
            seg_data = dict(getattr(segment, "data", {}) or {})
            staged_video_ref = normalize_text(str(seg_data.get("file", "") or staged_video_ref))
            payload = [{"type": seg_type, "data": seg_data}]
            _log.info(
                "media_delivery_inline_attempt | source=webui_agent_text | trace=%s | chat=%s | peer=%s | file=%s",
                trace_id,
                resolved_type,
                peer_id,
                clip_text(staged_video_ref, 180),
            )
            if resolved_type == "group":
                sent = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=payload)
            else:
                sent = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=payload)
            if isinstance(sent, dict):
                sent_message_id = normalize_text(str(sent.get("message_id", "") or sent.get("id", "") or sent_message_id))
            elif isinstance(sent, int):
                sent_message_id = str(sent)
            _log.info(
                "media_delivery_inline_ok | source=webui_agent_text | trace=%s | chat=%s | peer=%s | message_id=%s | file=%s",
                trace_id,
                resolved_type,
                peer_id,
                sent_message_id,
                clip_text(staged_video_ref, 180),
            )
            return True

        video_delivered = False
        if not prefer_upload_first:
            for prefer_plain in (True, False):
                try:
                    if await _send_video_segment(prefer_plain_path=prefer_plain):
                        video_delivered = True
                        break
                except Exception as exc:
                    delivery_errors.append(str(exc))
                    _log.warning(
                        "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=inline_video | plain=%s | peer=%s | error=%s",
                        trace_id,
                        prefer_plain,
                        peer_id,
                        exc,
                    )
        else:
            _log.info(
                "media_delivery_inline_skip | source=webui_agent_text | trace=%s | strategy=%s | reason=upload_first",
                trace_id,
                video_send_strategy,
            )

        if not video_delivered and allow_upload_fallback:
            upload_ref = staged_video_ref or video_url
            upload_path = napcat_file_uri_to_path(upload_ref) if upload_ref.lower().startswith("file://") else Path(upload_ref)
            if upload_path is not None and upload_path.exists() and upload_path.is_file():
                staged_upload_path = (
                    _stage_media_for_napcat(upload_path, napcat_media_stage_dir, trace_id=trace_id)
                    or upload_path
                )
                abs_path = str(staged_upload_path.expanduser().resolve())
                filename = Path(video_url).name or upload_path.name
                try:
                    if resolved_type == "group":
                        _log.info(
                            "media_delivery_upload_attempt | source=webui_agent_text | trace=%s | channel=upload_group_file | group=%s | file=%s",
                            trace_id,
                            peer_num,
                            clip_text(abs_path, 180),
                        )
                        sent = await _onebot_call(
                            "upload_group_file",
                            bot_id=bot_id,
                            group_id=peer_num,
                            file=abs_path,
                            name=filename,
                        )
                    else:
                        _log.info(
                            "media_delivery_upload_attempt | source=webui_agent_text | trace=%s | channel=upload_private_file | user=%s | file=%s",
                            trace_id,
                            peer_num,
                            clip_text(abs_path, 180),
                        )
                        sent = await _onebot_call(
                            "upload_private_file",
                            bot_id=bot_id,
                            user_id=peer_num,
                            file=abs_path,
                            name=filename,
                        )
                    if isinstance(sent, dict):
                        sent_message_id = normalize_text(str(sent.get("message_id", "") or sent.get("id", "") or sent_message_id))
                    elif isinstance(sent, int):
                        sent_message_id = str(sent)
                    _log.info(
                        "media_delivery_upload_ok | source=webui_agent_text | trace=%s | peer=%s | message_id=%s | file=%s",
                        trace_id,
                        peer_id,
                        sent_message_id,
                        clip_text(abs_path, 180),
                    )
                    video_delivered = True
                except Exception as exc:
                    delivery_errors.append(str(exc))
                    _log.warning(
                        "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=upload_file | peer=%s | file=%s | error=%s",
                        trace_id,
                        peer_id,
                        clip_text(abs_path, 180),
                        exc,
                    )

        if not video_delivered and not allow_upload_fallback:
            _log.warning(
                "media_delivery_upload_skip | source=webui_agent_text | trace=%s | strategy=%s | reason=inline_failed_no_file_fallback | peer=%s",
                trace_id,
                video_send_strategy,
                peer_id,
            )

        if not video_delivered:
            _log.warning(
                "media_delivery_failed_exact | source=webui_agent_text | trace=%s | channel=all | peer=%s | errors=%s",
                trace_id,
                peer_id,
                clip_text(" | ".join(delivery_errors), 500),
            )
            failure_text = "视频解析成功，但投递失败；NapCat 具体错误已经写进日志 media_delivery_failed_exact。"
            if resolved_type == "group":
                await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=failure_text)
            else:
                await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=failure_text)

    if audio_file:
        cq = f"[CQ:record,file={audio_file}]"
        try:
            if resolved_type == "group":
                await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=cq)
            else:
                await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=cq)
        except Exception:
            pass

    if not reply_text and not image_urls and not video_url and not audio_file:
        fallback_text = "处理完成。"
        if resolved_type == "group":
            await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=fallback_text)
        else:
            await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=fallback_text)

    return {
        "ok": True,
        "status": "submitted",
        "reason": str(getattr(result, "reason", "")),
        "conversation_id": conversation_id,
        "trace_id": trace_id,
        "message_id": sent_message_id,
    }


@router.post("/chat/send-image", dependencies=[Depends(_check_auth)])
async def chat_send_image(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")
    resolved_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    image_url = normalize_text(str(body.get("image_url", "")))
    image_base64 = normalize_text(str(body.get("image_base64", "")))

    if resolved_type not in {"group", "private"}:
        raise HTTPException(400, "chat_type 仅支持 group/private")
    if not peer_id:
        raise HTTPException(400, "peer_id 不能为空")

    file_value = ""
    if image_url:
        file_value = image_url
    elif image_base64:
        file_value = _stage_webui_base64_image_for_napcat(image_base64)
    if not file_value:
        raise HTTPException(400, "image_url 或 image_base64 至少提供一个")

    try:
        peer_num = int(peer_id)
    except Exception as exc:
        raise HTTPException(400, "peer_id 必须是数字") from exc

    message_payload = [{"type": "image", "data": {"file": file_value}}]
    if resolved_type == "group":
        result = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=message_payload)
    else:
        result = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=message_payload)
    message_id = ""
    if isinstance(result, dict):
        message_id = normalize_text(str(result.get("message_id", "") or result.get("id", "")))
    elif isinstance(result, int):
        message_id = str(result)
    return {"ok": True, "message_id": message_id}


def _stage_webui_base64_image_for_napcat(image_base64: str) -> str:
    raw = normalize_text(image_base64)
    if raw.startswith("data:image") and ";base64," in raw:
        raw = raw.split(";base64,", 1)[1]
    elif raw.startswith("base64://"):
        raw = raw[len("base64://") :]
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(400, "image_base64 不是有效 base64") from exc
    max_bytes = 8 * 1024 * 1024
    if not blob or len(blob) > max_bytes:
        raise HTTPException(400, "image_base64 图片为空或超过 8MB")
    if blob.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif blob.startswith(b"GIF8"):
        suffix = ".gif"
    elif blob.startswith(b"RIFF") and len(blob) >= 12 and blob[8:12] == b"WEBP":
        suffix = ".webp"
    elif blob.startswith(b"BM"):
        suffix = ".bmp"
    else:
        suffix = ".png"
    bot_cfg = {}
    if _engine is not None and isinstance(getattr(_engine, "config", None), dict):
        raw_cfg = _engine.config.get("bot", {})
        if isinstance(raw_cfg, dict):
            bot_cfg = raw_cfg
    stage_dir_raw = normalize_text(str(bot_cfg.get("napcat_media_stage_dir", "")))
    try:
        from app_helpers import _resolve_napcat_media_stage_dir

        target_dir = _resolve_napcat_media_stage_dir(stage_dir_raw).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(blob).hexdigest()[:16]
        target = (target_dir / f"webui_image_{digest}{suffix}").resolve()
        if not target.exists() or target.stat().st_size != len(blob):
            target.write_bytes(blob)
        try:
            target.chmod(0o644)
        except Exception:
            pass
        _log.info(
            "media_stage_for_napcat | trace=webui_send_image | src=base64 | staged=%s | size=%d",
            clip_text(str(target), 180),
            len(blob),
        )
        ref = build_napcat_file_reference(target, require_exists=True)
        if ref:
            return ref
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning("media_stage_for_napcat_fail | trace=webui_send_image | src=base64 | err=%s", exc)
    return f"base64://{raw}"


@router.post("/chat/message/recall", dependencies=[Depends(_check_auth)])
async def chat_message_recall(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    message_id = normalize_text(str(body.get("message_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    chat_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    if not message_id:
        raise HTTPException(400, "message_id 不能为空")
    if chat_type and chat_type not in {"group", "private"}:
        raise HTTPException(400, "chat_type 仅支持 group/private")

    if chat_type == "group" and peer_id:
        if not peer_id.isdigit():
            raise HTTPException(400, "group peer_id 必须是数字")
        role = await _resolve_group_bot_role(int(peer_id), bot_id=bot_id)
        if role not in {"owner", "admin"}:
            raise HTTPException(403, "当前机器人不是群管理员/群主，不能在 WebUI 执行撤回")

    message_id_arg: Any = int(message_id) if message_id.isdigit() else message_id
    bot = await _get_onebot_runtime(bot_id=bot_id)
    bot_self_id = normalize_text(str(getattr(bot, "self_id", "")))
    raw_message = {}
    with contextlib.suppress(Exception):
        raw_message = _unwrap_onebot_message_payload(
            await _onebot_call("get_msg", bot_id=bot_id, message_id=message_id_arg)
        )

    resolved_type = chat_type
    resolved_peer_id = peer_id
    if raw_message and (not resolved_type or not resolved_peer_id):
        inferred_type, inferred_peer_id = _resolve_message_scope_from_raw(raw_message)
        resolved_type = resolved_type or inferred_type
        resolved_peer_id = resolved_peer_id or inferred_peer_id

    if raw_message and resolved_type in {"group", "private"} and resolved_peer_id:
        with contextlib.suppress(Exception):
            _record_recalled_message(
                _build_recall_payload_from_message(
                    raw_message,
                    bot_self_id=bot_self_id,
                    chat_type=resolved_type,
                    peer_id=resolved_peer_id,
                    bot_id=bot_id,
                    operator_id=bot_self_id,
                    operator_name="WebUI",
                    source="webui",
                    note="webui recall",
                )
            )

    await _onebot_call("delete_msg", bot_id=bot_id, message_id=message_id_arg)
    return {"ok": True, "message": "撤回成功", "message_id": message_id}


@router.post("/chat/message/essence", dependencies=[Depends(_check_auth)])
async def chat_message_essence(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    message_id = normalize_text(str(body.get("message_id", "")))
    chat_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    if not message_id:
        raise HTTPException(400, "message_id 不能为空")
    if chat_type and chat_type != "group":
        raise HTTPException(400, "设为精华仅支持群聊")
    if peer_id and not peer_id.isdigit():
        raise HTTPException(400, "group peer_id 必须是数字")
    if peer_id:
        role = await _resolve_group_bot_role(int(peer_id), bot_id=bot_id)
        if role not in {"owner", "admin"}:
            raise HTTPException(403, "当前机器人不是群管理员/群主，不能在 WebUI 设置精华")

    message_id_arg: Any = int(message_id) if message_id.isdigit() else message_id
    await _onebot_call("set_essence_msg", bot_id=bot_id, message_id=message_id_arg)
    return {"ok": True, "message": "已设为群精华", "message_id": message_id}


@router.post("/chat/message/essence/remove", dependencies=[Depends(_check_auth)])
async def chat_message_remove_essence(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    message_id = normalize_text(str(body.get("message_id", "")))
    chat_type = _normalize_chat_type(str(body.get("chat_type", "")))
    peer_id = normalize_text(str(body.get("peer_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    if not message_id:
        raise HTTPException(400, "message_id 不能为空")
    if chat_type and chat_type != "group":
        raise HTTPException(400, "移除精华仅支持群聊")
    if peer_id and not peer_id.isdigit():
        raise HTTPException(400, "group peer_id 必须是数字")
    if peer_id:
        role = await _resolve_group_bot_role(int(peer_id), bot_id=bot_id)
        if role not in {"owner", "admin"}:
            raise HTTPException(403, "当前机器人不是群管理员/群主，不能在 WebUI 移除精华")

    message_id_arg: Any = int(message_id) if message_id.isdigit() else message_id
    await _onebot_call("delete_essence_msg", bot_id=bot_id, message_id=message_id_arg)
    return {"ok": True, "message": "已移除群精华", "message_id": message_id}


@router.post("/chat/message/add-sticker", dependencies=[Depends(_check_auth)])
async def chat_message_add_sticker(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    message_id = normalize_text(str(body.get("message_id", "")))
    bot_id = normalize_text(str(body.get("bot_id", "")))
    prefer_source_user = normalize_text(str(body.get("source_user_id", "")))
    description = normalize_text(str(body.get("description", "")))
    if not message_id:
        raise HTTPException(400, "message_id 不能为空")

    detail = await _get_onebot_message_detail(message_id, bot_id=bot_id)
    sender = detail.get("sender", {}) if isinstance(detail.get("sender"), dict) else {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    sender_name = (
        normalize_text(str(sender.get("card", "")))
        or normalize_text(str(sender.get("nickname", "")))
        or sender_id
        or "用户"
    )

    img_bytes, source = await _extract_image_bytes_from_message(detail, bot_id=bot_id)
    if not img_bytes:
        raise HTTPException(400, f"该消息没有可提取的图片（{source}）")
    if len(img_bytes) > 8 * 1024 * 1024:
        raise HTTPException(400, "图片超过 8MB，无法加入表情包")

    sticker = _resolve_sticker_manager()
    save_owner = prefer_source_user or sender_id or "webui"
    final_desc = description or f"{sender_name} 的聊天表情"
    saver = getattr(sticker, "_save_chat_emoji")
    key = saver(
        user_id=save_owner,
        img_data=img_bytes,
        description=final_desc,
        emotions=[],
        category="其他",
        tags=["webui", "右键添加"],
        registered=True,
    )
    return {
        "ok": True,
        "message": "已添加到表情包",
        "key": key,
        "owner": save_owner,
        "source": source,
        "description": final_desc,
    }


@router.post("/chat/interrupt", dependencies=[Depends(_check_auth)])
async def chat_interrupt(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")
    conversation_id = normalize_text(str(body.get("conversation_id", "")))
    if not conversation_id:
        raise HTTPException(400, "conversation_id 不能为空")

    runtime_interrupt = getattr(_engine, "runtime_agent_interrupt", None) if _engine is not None else None
    if not callable(runtime_interrupt):
        return {"ok": False, "message": "当前运行时不支持会话中断", "result": {}}
    result = runtime_interrupt(conversation_id, "cancelled_by_webui_interrupt")
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        result = {}
    return {"ok": True, "message": "中断请求已提交", "result": result}


@router.get("/chat/agent-state", dependencies=[Depends(_check_auth)])
async def chat_agent_state(
    conversation_id: str = Query(""),
    limit: int = Query(200, ge=1, le=500),
):
    provider = getattr(_engine, "runtime_agent_state_provider", None) if _engine is not None else None
    if not callable(provider):
        return {"items": [], "total": 0}
    rows = provider(limit=max(1, int(limit)))
    if inspect.isawaitable(rows):
        rows = await rows
    if not isinstance(rows, list):
        rows = []
    target = normalize_text(conversation_id)
    if target:
        rows = [item for item in rows if normalize_text(str((item or {}).get("conversation_id", ""))) == target]
    return {"items": rows, "total": len(rows)}


@router.get("/memory/records", dependencies=[Depends(_check_auth)])
async def get_memory_records(
    conversation_id: str = Query(""),
    user_id: str = Query(""),
    role: str = Query(""),
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    offset = (int(page) - 1) * int(page_size)
    items, total = memory.list_memory_records(
        conversation_id=normalize_text(conversation_id),
        user_id=normalize_text(user_id),
        role=normalize_text(role).lower(),
        keyword=normalize_text(keyword),
        limit=int(page_size),
        offset=offset,
    )
    return {"items": items, "total": total, "page": int(page), "page_size": int(page_size)}


@router.post("/memory/records", dependencies=[Depends(_check_auth)])
async def add_memory_record(request: Request):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")

    ok, message, payload = memory.add_memory_record(
        conversation_id=normalize_text(str(body.get("conversation_id", ""))),
        user_id=normalize_text(str(body.get("user_id", ""))),
        role=normalize_text(str(body.get("role", "user"))).lower() or "user",
        content=normalize_text(str(body.get("content", ""))),
        actor=f"webui:{normalize_text(str(body.get('actor', 'admin'))) or 'admin'}",
        note=normalize_text(str(body.get("note", ""))),
        reason=normalize_text(str(body.get("reason", ""))),
    )
    if not ok:
        raise HTTPException(400, message)
    return {"ok": True, "message": "记忆已新增", "item": payload}


@router.put("/memory/records/{record_id}", dependencies=[Depends(_check_auth)])
async def update_memory_record(record_id: int, request: Request):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是对象")
    ok, message, payload = memory.update_memory_record(
        record_id=int(record_id),
        content=normalize_text(str(body.get("content", ""))),
        actor=f"webui:{normalize_text(str(body.get('actor', 'admin'))) or 'admin'}",
        note=normalize_text(str(body.get("note", ""))),
        reason=normalize_text(str(body.get("reason", ""))),
    )
    if not ok:
        raise HTTPException(400, message)
    return {"ok": True, "message": "记忆已更新", "item": payload}


@router.delete("/memory/records/{record_id}", dependencies=[Depends(_check_auth)])
async def delete_memory_record(record_id: int, request: Request):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    body = {}
    with contextlib.suppress(Exception):
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed

    ok, message, payload = memory.delete_memory_record(
        record_id=int(record_id),
        actor=f"webui:{normalize_text(str(body.get('actor', 'admin'))) or 'admin'}",
        note=normalize_text(str(body.get("note", ""))),
        reason=normalize_text(str(body.get("reason", ""))),
    )
    if not ok:
        raise HTTPException(400, message)
    return {"ok": True, "message": "记忆已删除", "item": payload}


@router.get("/memory/audit", dependencies=[Depends(_check_auth)])
async def get_memory_audit(
    record_id: int = Query(0, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    offset = (int(page) - 1) * int(page_size)
    rid = int(record_id) if int(record_id) > 0 else None
    items, total = memory.list_memory_audit_logs(record_id=rid, limit=int(page_size), offset=offset)
    return {"items": items, "total": total, "page": int(page), "page_size": int(page_size)}


@router.post("/memory/compact", dependencies=[Depends(_check_auth)])
async def post_memory_compact(request: Request):
    e = _engine
    if not e or not hasattr(e, "memory"):
        raise HTTPException(503, "记忆引擎未初始化")
    memory = getattr(e, "memory", None)
    if memory is None:
        raise HTTPException(503, "记忆引擎未初始化")

    body = {}
    with contextlib.suppress(Exception):
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed

    dry_run = bool(body.get("dry_run", True))
    ok, message, payload = memory.compact_memory_records(
        conversation_id=normalize_text(str(body.get("conversation_id", ""))),
        user_id=normalize_text(str(body.get("user_id", ""))),
        role=normalize_text(str(body.get("role", ""))).lower(),
        actor=f"webui:{normalize_text(str(body.get('actor', 'admin'))) or 'admin'}",
        note=normalize_text(str(body.get("note", ""))),
        reason=normalize_text(str(body.get("reason", ""))),
        dry_run=dry_run,
        keep_latest=int(body.get("keep_latest", 1) or 1),
    )
    if not ok:
        raise HTTPException(400, message)
    return {
        "ok": True,
        "message": ("记忆整理预览完成" if dry_run else "记忆整理执行完成"),
        "result": payload,
    }


@router.get("/image-gen", dependencies=[Depends(_check_auth)])
async def get_image_gen():
    """获取图片生成配置。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    raw = getattr(e.config_manager, "raw", {})
    image_gen_cfg = raw.get("image_gen", {}) if isinstance(raw, dict) else {}

    # 脱敏处理
    if isinstance(image_gen_cfg, dict):
        image_gen_cfg = copy.deepcopy(image_gen_cfg)
        models = image_gen_cfg.get("models", [])
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict) and "api_key" in model:
                    if str(model["api_key"]).strip():
                        model["api_key"] = "***"

    return {"image_gen": image_gen_cfg}


@router.put("/image-gen", dependencies=[Depends(_check_auth)])
async def put_image_gen(request: Request):
    """更新图片生成配置。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    body = await request.json()
    image_gen_cfg = body.get("image_gen", {})
    if not isinstance(image_gen_cfg, dict):
        raise HTTPException(400, "image_gen 必须是对象")

    # 读取当前配置
    config_file = _ROOT_DIR / "config" / "config.yml"
    if config_file.exists():
        current_config = _load_yaml_dict(config_file)
    else:
        current_config = load_config_template()

    # 更新 image_gen 配置
    if "image_gen" not in current_config:
        current_config["image_gen"] = {}

    old_image_cfg = current_config.get("image_gen", {})
    old_models = old_image_cfg.get("models", []) if isinstance(old_image_cfg, dict) else []
    merged_image_cfg = dict(old_image_cfg) if isinstance(old_image_cfg, dict) else {}
    merged_image_cfg.update(image_gen_cfg)

    default_provider = "openai"
    if isinstance(merged_image_cfg, dict):
        default_provider = normalize_text(str(merged_image_cfg.get("provider", ""))).lower() or "openai"

    if "models" in image_gen_cfg:
        merged_image_cfg["models"] = _normalize_image_gen_models_for_save(
            image_gen_cfg.get("models"),
            old_models,
            default_provider=default_provider,
        )

    # 保证 default_model 可命中 models，避免保存后运行期回退到错误模型。
    ensured_default, default_changed = _ensure_image_gen_default_model(merged_image_cfg)
    if default_changed and ensured_default:
        _log.warning("image_gen_default_model_adjusted | default_model=%s", ensured_default)

    # 保存前加密 image_gen.models.*.api_key（占位符/已加密值除外）。
    try:
        from core.crypto import SecretManager

        sm = SecretManager(_ROOT_DIR / "storage" / ".secret_key")
        models = merged_image_cfg.get("models", []) if isinstance(merged_image_cfg, dict) else []
        if isinstance(models, list):
            for model_cfg in models:
                if not isinstance(model_cfg, dict):
                    continue
                api_key = normalize_text(str(model_cfg.get("api_key", "")))
                if not api_key:
                    continue
                if api_key.startswith("${") and api_key.endswith("}"):
                    continue
                if SecretManager.is_encrypted(api_key):
                    continue
                model_cfg["api_key"] = sm.encrypt(api_key)
    except Exception:
        _log.warning("image_gen_api_key_encrypt_failed", exc_info=True)

    current_config["image_gen"] = merged_image_cfg

    # 写入文件
    _safe_write_yaml(config_file, current_config)

    # 热重载
    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "配置热重载失败")
        _log.info("图片生成配置已更新并重载")
        return {"ok": True, "message": "图片生成配置已保存并重载"}
    except Exception as ex:
        _log.error(f"重载配置失败: {ex}")
        raise HTTPException(500, f"重载失败: {ex}")


@router.post("/image-gen/test", dependencies=[Depends(_check_auth)])
async def test_image_gen(request: Request):
    """测试图片生成（支持传入未保存的 image_gen 覆盖配置）。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    body = await request.json()
    if not isinstance(body, dict):
        body = {}

    prompt = normalize_text(str(body.get("prompt", ""))) or (
        "A cute anime catgirl maid with pink hair, soft lighting, high quality illustration"
    )
    requested_model = normalize_text(str(body.get("model", "")))
    size = normalize_text(str(body.get("size", ""))) or None
    style = normalize_text(str(body.get("style", ""))) or None
    image_override = body.get("image_gen")

    raw_cfg = getattr(e.config_manager, "raw", {})
    effective_cfg = copy.deepcopy(raw_cfg) if isinstance(raw_cfg, dict) else {}
    current_image_cfg = effective_cfg.get("image_gen", {})
    if not isinstance(current_image_cfg, dict):
        current_image_cfg = {}

    working_image_cfg = copy.deepcopy(current_image_cfg)
    default_adjusted = False

    if isinstance(image_override, dict):
        old_models = working_image_cfg.get("models", []) if isinstance(working_image_cfg, dict) else []
        working_image_cfg.update(image_override)

        default_provider = normalize_text(str(working_image_cfg.get("provider", ""))).lower() or "openai"
        if "models" in image_override:
            working_image_cfg["models"] = _normalize_image_gen_models_for_save(
                image_override.get("models"),
                old_models,
                default_provider=default_provider,
            )

        _, default_adjusted = _ensure_image_gen_default_model(working_image_cfg)
    else:
        _, default_adjusted = _ensure_image_gen_default_model(working_image_cfg)

    effective_cfg["image_gen"] = working_image_cfg

    models = working_image_cfg.get("models", [])
    configured_models = len(models) if isinstance(models, list) else 0

    try:
        image_engine = ImageGenEngine(effective_cfg, model_client=getattr(e, "model_client", None))
        result = await image_engine.generate(
            prompt=prompt,
            model=requested_model or None,
            size=size,
            style=style,
        )

        image_url = normalize_text(result.url)
        if not image_url and result.base64_data:
            image_url = f"data:image/png;base64,{result.base64_data}"

        return {
            "ok": bool(result.ok),
            "message": result.message,
            "image_url": image_url,
            "model_used": result.model_used,
            "revised_prompt": result.revised_prompt,
            "requested_model": requested_model,
            "default_model": normalize_text(str(working_image_cfg.get("default_model", ""))),
            "configured_models": configured_models,
            "default_adjusted": default_adjusted,
        }
    except Exception as exc:
        _log.warning("image_gen_test_failed | %s", exc, exc_info=True)
        return {
            "ok": False,
            "message": str(exc),
            "requested_model": requested_model,
            "default_model": normalize_text(str(working_image_cfg.get("default_model", ""))),
            "configured_models": configured_models,
            "default_adjusted": default_adjusted,
        }


@router.post("/image-gen/models", dependencies=[Depends(_check_auth)])
async def add_image_gen_model(request: Request):
    """添加图片生成模型。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    body = await request.json()
    model_cfg = body.get("model", {})
    if not isinstance(model_cfg, dict):
        raise HTTPException(400, "model 必须是对象")

    model_cfg = copy.deepcopy(model_cfg)
    model_cfg["provider"] = normalize_text(str(model_cfg.get("provider", ""))).lower() or "openai"
    model_cfg["model"] = normalize_text(str(model_cfg.get("model", ""))) or normalize_text(str(model_cfg.get("name", "")))
    model_cfg["name"] = normalize_text(str(model_cfg.get("name", ""))) or normalize_text(str(model_cfg.get("model", "")))
    if not model_cfg.get("api_base"):
        auto_base = _setup_resolve_image_gen_base_url(
            image_provider=str(model_cfg.get("provider", "openai")),
            image_base_url_raw="",
            resolved_api_key=normalize_text(str(model_cfg.get("api_key", ""))),
        )
        if auto_base:
            model_cfg["api_base"] = auto_base

    # 验证必填字段
    if not model_cfg.get("name"):
        raise HTTPException(400, "模型名称不能为空")
    if not model_cfg.get("api_base"):
        raise HTTPException(400, "API 地址不能为空")
    if not model_cfg.get("model"):
        raise HTTPException(400, "模型 ID 不能为空")

    # 加密 API Key
    if model_cfg.get("api_key"):
        try:
            from core.crypto import SecretManager
            sm = SecretManager(_ROOT_DIR / "storage" / ".secret_key")
            model_cfg["api_key"] = sm.encrypt(str(model_cfg["api_key"]))
        except Exception:
            pass

    # 读取当前配置
    config_file = _ROOT_DIR / "config" / "config.yml"
    if config_file.exists():
        current_config = _load_yaml_dict(config_file)
    else:
        current_config = load_config_template()

    # 添加模型
    if "image_gen" not in current_config:
        current_config["image_gen"] = {}
    if "models" not in current_config["image_gen"]:
        current_config["image_gen"]["models"] = []

    current_config["image_gen"]["models"].append(model_cfg)

    # 写入文件
    _safe_write_yaml(config_file, current_config)

    # 热重载
    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "配置热重载失败")
        _log.info(f"图片生成模型已添加: {model_cfg.get('name')}")
        return {"ok": True, "message": f"模型 {model_cfg.get('name')} 已添加"}
    except Exception as ex:
        _log.error(f"重载配置失败: {ex}")
        raise HTTPException(500, f"重载失败: {ex}")


@router.delete("/image-gen/models/{model_name}", dependencies=[Depends(_check_auth)])
async def delete_image_gen_model(model_name: str):
    """删除图片生成模型。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    # 读取当前配置
    config_file = _ROOT_DIR / "config" / "config.yml"
    if not config_file.exists():
        raise HTTPException(404, "配置文件不存在")

    current_config = _load_yaml_dict(config_file)

    # 删除模型
    if "image_gen" not in current_config or "models" not in current_config["image_gen"]:
        raise HTTPException(404, "未找到图片生成配置")

    models = current_config["image_gen"]["models"]
    if not isinstance(models, list):
        raise HTTPException(404, "模型列表格式错误")

    # 查找并删除
    found = False
    for i, model in enumerate(models):
        if isinstance(model, dict) and model.get("name") == model_name:
            models.pop(i)
            found = True
            break

    if not found:
        raise HTTPException(404, f"未找到模型: {model_name}")

    # 写入文件
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(current_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 热重载
    try:
        ok, msg = await _reload_engine_config(e)
        if not ok:
            raise RuntimeError(msg or "配置热重载失败")
        _log.info(f"图片生成模型已删除: {model_name}")
        return {"ok": True, "message": f"模型 {model_name} 已删除"}
    except Exception as ex:
        _log.error(f"重载配置失败: {ex}")
        raise HTTPException(500, f"重载失败: {ex}")


# ============================================================================
# Setup Mode Endpoints
# ============================================================================

def run_setup_server(host: str = "127.0.0.1", port: int = 8081):
    """运行 Setup 服务器。"""
    return _setup_support.run_setup_server(host=host, port=port)
