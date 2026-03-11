"""WebUI 管理 API — 挂载到 nonebot FastAPI 上的管理接口。

提供配置编辑、提示词编辑、日志查看、状态查询等 REST + WebSocket 端点。
认证方式：Bearer token，配置在 .env WEBUI_TOKEN。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import inspect
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.requests import Request

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
from core import prompt_loader as _pl
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.webui")
router = APIRouter(prefix="/api/webui", tags=["webui"])

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
_PROMPTS_FILE = _ROOT_DIR / "config" / "prompts.yml"
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def init_webui(engine: Any) -> APIRouter:
    """初始化 WebUI，返回 FastAPI router。"""
    global _engine, _start_time
    _engine = engine
    _start_time = time.time()
    _log.info("WebUI API 已初始化")
    return router


def _get_token() -> str:
    """从环境变量获取 WEBUI_TOKEN。"""
    return os.environ.get("WEBUI_TOKEN", "")


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
    """检查 Bearer token 认证。"""
    token = _get_token()
    if not token:
        raise HTTPException(403, "WEBUI_TOKEN 未配置")
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {token}":
        raise HTTPException(401, "认证失败")


def _mask_sensitive(data: dict) -> dict:
    """遮蔽 config dict 中的敏感字段，替换为 ***。"""
    result = copy.deepcopy(data)
    for dotpath in _SENSITIVE_PATHS:
        keys = dotpath.split(".")
        node = result
        for k in keys[:-1]:
            if k == "*":
                # 通配符：遍历所有子字典
                if isinstance(node, dict):
                    for sub in node.values():
                        if isinstance(sub, dict):
                            # 递归处理
                            pass
                break
            else:
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    break
        else:
            # 到达最后一个 key
            last = keys[-1]
            if isinstance(node, dict) and last in node:
                val = node[last]
                if isinstance(val, str) and val.strip():
                    node[last] = "***"
    return result


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


def _split_log_chunks(raw_line: str) -> list[str]:
    """将日志行按时间戳分割成多个条目。"""
    if not raw_line:
        return []

    parts = _LOG_SPLIT_RE.split(raw_line)
    return [p.strip() for p in parts if p.strip()]


def _resolve_webui_db_path(db_name: str) -> Path:
    """解析数据库名称到实际路径。"""
    raw_name = db_name.strip()
    db_name = raw_name.lower()
    storage_dir = _ROOT_DIR / "storage"

    # 特殊名称映射
    if db_name in ("main", "yukiko", "bot"):
        default_main_db = storage_dir / "yukiko.db"
        if default_main_db.is_file():
            return default_main_db

    # 直接 .db 路径
    db_path = storage_dir / f"{db_name}.db"
    if db_path.is_file():
        return db_path

    # 允许传入完整文件名（包含后缀）
    db_path_no_ext = storage_dir / db_name
    if db_path_no_ext.is_file():
        return db_path_no_ext

    # 兼容 storage/<name>/<name>.db
    if db_path_no_ext.is_dir():
        nested_named_db = db_path_no_ext / f"{db_name}.db"
        if nested_named_db.is_file():
            return nested_named_db

        nested_db_files = sorted(p for p in db_path_no_ext.glob("*.db") if p.is_file())
        if len(nested_db_files) == 1:
            return nested_db_files[0]

    # 递归匹配 storage/**/<name>.db（例如 storage/memory/vector/memory.db）
    target_file_name = db_name if db_name.endswith(".db") else f"{db_name}.db"
    recursive_matches = [
        p for p in sorted(storage_dir.rglob("*.db"))
        if p.is_file() and (p.name.lower() == target_file_name or p.stem.lower() == db_name)
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


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开 SQLite 数据库。"""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _open_sqlite_readwrite(db_path: Path) -> sqlite3.Connection:
    """以读写模式打开 SQLite 数据库。"""
    return sqlite3.connect(str(db_path))


def _quote_ident(name: str) -> str:
    """安全引用 SQL 标识符。"""
    if _SQL_IDENT_RE.match(name):
        return name
    return f'"{name.replace('"', '""')}"'


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
        except:
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
    except:
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


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/health")
async def health():
    """健康检查端点。"""
    return {"status": "ok"}


@router.post("/auth")
async def auth(request: Request):
    """认证端点，验证 token。"""
    body = await request.json()
    token = str(body.get("token", ""))
    expected = _get_token()

    if not expected:
        raise HTTPException(403, "WEBUI_TOKEN 未配置")

    if token != expected:
        raise HTTPException(401, "Token 错误")

    return {"ok": True}


@router.get("/status")
async def status():
    """获取 Bot 运行状态。"""
    e = _engine
    if not e:
        raise HTTPException(503, "引擎未初始化")

    admin = getattr(e, "admin", None)
    uptime = int(time.time() - getattr(admin, "_started", _start_time))
    msg_count = getattr(admin, "_count", 0)
    white = list(getattr(admin, "_white", set()))

    mc = getattr(e, "model_client", None)
    provider = getattr(mc, "provider", "?") if mc else "?"
    model = getattr(mc, "model", "?") if mc else "?"

    agent = getattr(e, "agent", None)
    agent_enable = getattr(agent, "enable", False) if agent else False

    safety = getattr(e, "safety", None)
    scale = int(getattr(safety, "scale", 2)) if safety else 2

    reg = getattr(e, "agent_tool_registry", None)
    tool_count = getattr(reg, "tool_count", 0) if reg else 0

    plugins_obj = getattr(e, "plugins", None)
    plugin_map = getattr(plugins_obj, "plugins", {}) if plugins_obj else {}
    plugin_list = []
    if isinstance(plugin_map, dict):
        for name, obj in plugin_map.items():
            desc = getattr(obj, "description", "") or ""
            plugin_list.append({"name": name, "description": str(desc)})

    return {
        "uptime_seconds": uptime,
        "message_count": msg_count,
        "whitelist_groups": white,
        "model": f"{provider}/{model}",
        "agent_enabled": agent_enable,
        "tool_count": tool_count,
        "safety_scale": scale,
        "bot_name": getattr(e, "bot_name", "YuKiKo"),
        "plugins": plugin_list,
    }


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
    new_config = _strip_deprecated_local_paths_config(new_config)

    # 应用控制面板映射
    new_config = _apply_control_mapping(new_config)

    # 合并到模板
    template = load_config_template()
    merged = _deep_merge_template(template, new_config)

    # 写入文件
    config_file = _ROOT_DIR / "config" / "config.yml"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

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

    # 写入文件
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPTS_FILE.write_text(yaml_text, encoding="utf-8")

    # 重载提示词
    _pl.reload()
    _log.info("提示词已更新并重载")

    return {"ok": True, "message": "提示词已保存并重载", "parsed": parsed}


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

    # 写入
    yaml_text = yaml.safe_dump(merged, allow_unicode=True, default_flow_style=False, sort_keys=False)
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROMPTS_FILE.write_text(yaml_text, encoding="utf-8")

    # 重载
    _pl.reload()
    _log.info("提示词已部分更新并重载")

    return {"ok": True, "message": "提示词已更新并重载", "parsed": merged}


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
    try:
        unified_data = _load_yaml_dict(unified_path, strict=True)
    except Exception as ex:
        raise HTTPException(500, f"读取插件配置失败: {ex}")
    unified_data[plugin_name] = new_cfg

    try:
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
        updated["source"] = _display_path(unified_path)
        updated["config_file"] = str(unified_path)

    return {"ok": True, "message": message, "plugin": updated}


@router.get("/logs", dependencies=[Depends(_check_auth)])
async def get_logs(lines: int = Query(100, ge=1, le=10000)):
    """获取最近的日志。"""
    log_file = _resolve_log_file_path()
    log_lines = _read_log_tail(log_file, lines)
    return {"lines": log_lines}


@router.get("/db/overview", dependencies=[Depends(_check_auth)])
async def db_overview():
    """获取所有数据库概览。"""
    storage_dir = _ROOT_DIR / "storage"
    if not storage_dir.exists():
        return {"databases": []}

    databases = []
    seen_names: set[str] = set()
    for db_file in sorted(storage_dir.rglob("*.db")):
        db_name = db_file.stem.lower()
        if db_name in seen_names:
            _log.warning(f"数据库名称冲突，已跳过: name={db_name} path={db_file}")
            continue

        try:
            conn = _open_sqlite_readonly(db_file)
            tables = _list_tables(conn, include_system=False)
            conn.close()

            databases.append({
                "name": db_name,
                "path": str(db_file),
                "table_count": len(tables),
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
        item = {"name": table}
        if with_counts:
            try:
                cursor = conn.cursor()
                quoted = _quote_ident(table)
                cursor.execute(f"SELECT COUNT(*) FROM {quoted}")
                item["row_count"] = cursor.fetchone()[0]
            except:
                item["row_count"] = None
        result.append(item)

    conn.close()
    return {"tables": result}


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


def _unwrap_onebot_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "data" in payload and ("retcode" in payload or "status" in payload):
            return payload.get("data")
    return payload


def _normalize_chat_type(value: str) -> str:
    raw = normalize_text(str(value)).lower()
    if raw in {"group", "group_chat", "2", "grp"}:
        return "group"
    if raw in {"private", "friend", "dm", "1", "single"}:
        return "private"
    return ""


async def _get_onebot_runtime(bot_id: str = "") -> Any:
    try:
        import nonebot
    except Exception as exc:
        raise HTTPException(503, f"NoneBot 不可用: {exc}") from exc

    bots = nonebot.get_bots()
    if not isinstance(bots, dict) or not bots:
        raise HTTPException(503, "未检测到在线 OneBot 实例")

    prefer_id = normalize_text(str(bot_id))
    if prefer_id and prefer_id in bots:
        return bots[prefer_id]
    for _, bot in bots.items():
        return bot
    raise HTTPException(503, "未检测到在线 OneBot 实例")


async def _onebot_call(api: str, *, bot_id: str = "", **kwargs: Any) -> Any:
    bot = await _get_onebot_runtime(bot_id=bot_id)
    try:
        payload = await bot.call_api(api, **kwargs)
    except Exception as exc:
        tail = clip_text(normalize_text(str(exc)), 220)
        raise HTTPException(502, f"调用 {api} 失败: {tail}") from exc
    return _unwrap_onebot_payload(payload)


def _render_message_text(raw_message: Any, segments: Any) -> str:
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
            part = normalize_text(str(data.get("text", "")))
            if part:
                parts.append(part)
        elif seg_type in {"image", "video", "record", "audio", "file"}:
            parts.append(f"[{seg_type}]")
        elif seg_type == "at":
            qq = normalize_text(str(data.get("qq", "")))
            parts.append(f"@{qq or 'someone'}")
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return normalize_text(" ".join(parts))


def _format_chat_message_item(item: dict[str, Any], *, bot_self_id: str) -> dict[str, Any]:
    sender = item.get("sender", {}) if isinstance(item, dict) else {}
    if not isinstance(sender, dict):
        sender = {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    sender_name = (
        normalize_text(str(sender.get("card", "")))
        or normalize_text(str(sender.get("nickname", "")))
        or sender_id
    )
    role = normalize_text(str(sender.get("role", ""))).lower()
    segments = item.get("message", [])
    if not isinstance(segments, list):
        segments = []
    text = _render_message_text(item.get("raw_message", ""), segments)
    ts = int(item.get("time", 0) or 0)
    return {
        "message_id": normalize_text(str(item.get("message_id", "") or item.get("real_id", "") or item.get("id", ""))),
        "seq": normalize_text(str(item.get("message_seq", "") or item.get("real_seq", ""))),
        "timestamp": ts,
        "time_iso": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts > 0 else ""),
        "sender_id": sender_id,
        "sender_name": sender_name or "未知用户",
        "sender_role": role,
        "is_self": bool(sender_id and sender_id == normalize_text(bot_self_id)),
        "is_essence": False,
        "is_recalled": False,
        "recalled_at": 0,
        "recalled_source": "",
        "text": text,
        "segments": segments,
    }


async def _resolve_group_bot_role(group_id: int, *, bot_id: str = "") -> str:
    """查询当前机器人在群内的角色（owner/admin/member）。"""
    if group_id <= 0:
        return ""

    cache_key = f"{normalize_text(bot_id) or '-'}:{int(group_id)}"
    cached = _GROUP_ROLE_CACHE.get(cache_key)
    if cached:
        expires_at, cached_role = cached
        if time.time() < float(expires_at):
            return cached_role
        _GROUP_ROLE_CACHE.pop(cache_key, None)

    bot = await _get_onebot_runtime(bot_id=bot_id)
    self_id = normalize_text(str(getattr(bot, "self_id", "")))
    if not self_id.isdigit():
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + 60, "")
        return ""
    try:
        info = await _onebot_call(
            "get_group_member_info",
            bot_id=bot_id,
            group_id=int(group_id),
            user_id=int(self_id),
            no_cache=True,
        )
    except Exception as exc:
        err_text = normalize_text(str(exc))
        err_lower = err_text.lower()
        miss_ttl = 60
        if ("成员" in err_text and "不存在" in err_text) or (
            "member" in err_lower and ("not exists" in err_lower or "not exist" in err_lower or "not found" in err_lower)
        ):
            # 机器人不在群里时，前端轮询会频繁命中该接口；加长负缓存避免刷屏日志。
            miss_ttl = _GROUP_ROLE_CACHE_MISS_TTL_SECONDS
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + miss_ttl, "")
        return ""
    if not isinstance(info, dict):
        _GROUP_ROLE_CACHE[cache_key] = (time.time() + 60, "")
        return ""
    role = normalize_text(str(info.get("role", ""))).lower()
    ttl = _GROUP_ROLE_CACHE_OK_TTL_SECONDS if role else 60
    _GROUP_ROLE_CACHE[cache_key] = (time.time() + ttl, role)
    return role


async def _resolve_group_essence_message_ids(group_id: int, *, bot_id: str = "") -> set[str]:
    try:
        raw = await _onebot_call("get_essence_msg_list", bot_id=bot_id, group_id=int(group_id))
    except Exception:
        return set()
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return set()

    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("message_id", "msg_id", "id", "messageId"):
            value = normalize_text(str(item.get(key, "")))
            if value:
                ids.add(value)
        for key in ("message_seq", "msg_seq", "seq"):
            value = normalize_text(str(item.get(key, "")))
            if value:
                ids.add(f"seq:{value}")
    return ids


def _chat_message_item_key(item: dict[str, Any]) -> str:
    message_id = normalize_text(str(item.get("message_id", "")))
    if message_id:
        return message_id
    seq = normalize_text(str(item.get("seq", "")))
    return f"seq:{seq}" if seq else ""


def _unwrap_onebot_message_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and (
            data.get("message_id")
            or data.get("real_id")
            or data.get("message")
            or data.get("raw_message")
        ):
            return data
        return raw
    return {}


def _resolve_message_scope_from_raw(item: dict[str, Any]) -> tuple[str, str]:
    message_type = normalize_text(str(item.get("message_type", ""))).lower()
    group_id = normalize_text(str(item.get("group_id", "")))
    user_id = normalize_text(str(item.get("user_id", "")))
    sender = item.get("sender", {}) if isinstance(item.get("sender"), dict) else {}
    sender_id = normalize_text(str(sender.get("user_id", "")))
    if message_type == "group" or group_id:
        return "group", group_id
    if message_type in {"private", "friend"}:
        peer_id = user_id or sender_id
        return "private", peer_id
    return "", ""


def _build_recall_payload_from_message(
    item: dict[str, Any],
    *,
    bot_self_id: str,
    chat_type: str,
    peer_id: str,
    bot_id: str = "",
    operator_id: str = "",
    operator_name: str = "",
    source: str = "",
    note: str = "",
) -> dict[str, Any]:
    mapped = _format_chat_message_item(item, bot_self_id=bot_self_id)
    mapped.update(
        {
            "conversation_id": _build_recall_conversation_id(chat_type, peer_id),
            "chat_type": chat_type,
            "peer_id": peer_id,
            "bot_id": bot_id,
            "operator_id": operator_id,
            "operator_name": operator_name,
            "source": source,
            "note": note,
        }
    )
    return mapped


def _format_recalled_record_item(item: dict[str, Any]) -> dict[str, Any]:
    ts = int(item.get("timestamp", 0) or 0)
    recalled_at = int(item.get("recalled_at", 0) or 0)
    segments = item.get("segments", [])
    return {
        "message_id": normalize_text(str(item.get("message_id", ""))),
        "seq": normalize_text(str(item.get("seq", ""))),
        "timestamp": ts,
        "time_iso": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts > 0 else ""),
        "sender_id": normalize_text(str(item.get("sender_id", ""))),
        "sender_name": normalize_text(str(item.get("sender_name", ""))) or "未知用户",
        "sender_role": normalize_text(str(item.get("sender_role", ""))).lower(),
        "is_self": bool(item.get("is_self")),
        "is_essence": False,
        "is_recalled": True,
        "recalled_at": recalled_at,
        "recalled_source": normalize_text(str(item.get("source", ""))),
        "text": str(item.get("text", "") or _render_message_text("", segments)),
        "segments": segments if isinstance(segments, list) else [],
    }


def _normalize_message_segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        items: list[dict[str, Any]] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            raw_data = seg.get("data", {}) or {}
            seg_data = raw_data if isinstance(raw_data, dict) else {}
            if seg_type:
                items.append({"type": seg_type, "data": seg_data})
        return items

    if not isinstance(message, str) or not message:
        return []

    items: list[dict[str, Any]] = []
    for m in re.finditer(r"\[CQ:([a-zA-Z0-9_]+)(?:,([^\]]*))?\]", message):
        seg_type = normalize_text(m.group(1)).lower()
        raw_data = m.group(2) or ""
        seg_data: dict[str, Any] = {}
        if raw_data:
            for pair in raw_data.split(","):
                if "=" not in pair:
                    continue
                key, value = pair.split("=", 1)
                seg_data[normalize_text(key)] = normalize_text(value)
        if seg_type:
            items.append({"type": seg_type, "data": seg_data})
    return items


def _resolve_local_path_from_file_uri(raw: str) -> Path | None:
    value = normalize_text(raw)
    if not value:
        return None
    if value.lower().startswith("file://"):
        path_text = unquote(value[7:])
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        return Path(path_text)
    return Path(unquote(value))


def _decode_base64_payload(value: str) -> bytes | None:
    raw = normalize_text(value)
    if not raw:
        return None
    if raw.startswith("base64://"):
        raw = raw[len("base64://") :]
    if raw.startswith("data:"):
        _, _, tail = raw.partition(",")
        raw = tail
    if not raw:
        return None
    with contextlib.suppress(Exception):
        return base64.b64decode(raw)
    return None


async def _download_image_bytes(url: str) -> tuple[bytes | None, str]:
    target = normalize_text(url)
    if not target:
        return None, "empty_url"

    if target.startswith("base64://") or target.startswith("data:"):
        blob = _decode_base64_payload(target)
        if blob:
            return blob, "base64"
        return None, "invalid_base64"

    local_candidate = _resolve_local_path_from_file_uri(target)
    if local_candidate is not None and (target.lower().startswith("file://") or local_candidate.exists()):
        with contextlib.suppress(Exception):
            data = local_candidate.read_bytes()
            if data:
                return data, "local_file"

    if target.startswith("http://") or target.startswith("https://"):
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

    return None, "unsupported_url"


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
        chat_type = "group" if int(item.get("chatType", 0) or 0) == 2 else "private"
        peer_id = normalize_text(str(item.get("peerUin", "") or item.get("peerUid", "") or item.get("peer_id", "")))
        if not peer_id:
            continue
        peer_name = (
            normalize_text(str(item.get("peerName", "")))
            or normalize_text(str(item.get("remark", "")))
            or peer_id
        )
        ts = int(item.get("msgTime", 0) or 0)
        rows.append(
            {
                "conversation_id": f"{chat_type}:{peer_id}",
                "chat_type": chat_type,
                "peer_id": peer_id,
                "peer_name": peer_name,
                "last_time": ts,
                "unread_count": int(item.get("unreadCnt", 0) or 0),
                "last_message": _render_message_text(item.get("lastMsg", ""), item.get("lastMsgSegs", [])),
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
        file_value = image_base64 if image_base64.startswith("base64://") else f"base64://{image_base64}"
    if not file_value:
        raise HTTPException(400, "image_url 或 image_base64 至少提供一个")

    try:
        peer_num = int(peer_id)
    except Exception as exc:
        raise HTTPException(400, "peer_id 必须是数字") from exc

    cq = f"[CQ:image,file={file_value}]"
    if resolved_type == "group":
        result = await _onebot_call("send_group_msg", bot_id=bot_id, group_id=peer_num, message=cq)
    else:
        result = await _onebot_call("send_private_msg", bot_id=bot_id, user_id=peer_num, message=cq)
    message_id = ""
    if isinstance(result, dict):
        message_id = normalize_text(str(result.get("message_id", "") or result.get("id", "")))
    elif isinstance(result, int):
        message_id = str(result)
    return {"ok": True, "message_id": message_id}


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
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(current_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

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
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(current_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

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


@router.websocket("/logs/stream")
async def ws_log_stream(ws: WebSocket, token: str = Query("")):
    """WebSocket 日志流。"""
    # 验证 token
    expected = _get_token()
    if not expected or token != expected:
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()

    log_file = _resolve_log_file_path()
    last_file = log_file
    last_size = log_file.stat().st_size if log_file.exists() else 0

    try:
        while True:
            await asyncio.sleep(1)
            log_file = _resolve_log_file_path()
            if log_file != last_file:
                last_file = log_file
                last_size = 0

            if not log_file.exists():
                continue

            current_size = log_file.stat().st_size
            if current_size > last_size:
                # 读取新增内容
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    new_content = f.read()
                    if new_content:
                        for raw_line in new_content.splitlines():
                            for line in _split_log_chunks(raw_line):
                                await ws.send_text(json.dumps({"line": line}, ensure_ascii=False))
                last_size = current_size
            elif current_size < last_size:
                # 文件被截断或重新创建
                last_size = 0

    except WebSocketDisconnect:
        # 浏览器主动断开属于正常行为，不记错误日志。
        _log.debug("WebSocket 日志流断开")
    except RuntimeError as e:
        msg = normalize_text(str(e)).lower()
        if "websocket" in msg and ("disconnect" in msg or "close" in msg):
            _log.debug("WebSocket 日志流关闭: %s", e)
        else:
            _log.error(f"WebSocket 日志流错误: {e}")
    except Exception as e:
        _log.error(f"WebSocket 日志流错误: {e}")
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


# ============================================================================
# Cookie 管理 API
# ============================================================================

@router.get("/cookies/capabilities", dependencies=[Depends(_check_auth)])
async def cookies_capabilities():
    """返回 Cookie 自动提取能力（按当前部署环境）。"""
    return {"ok": True, "data": _cookie_capabilities_payload()}


@router.post("/cookies/bilibili-qr/start", dependencies=[Depends(_check_auth)])
async def cookies_bilibili_qr_start():
    """开始 B站二维码登录会话（Cookie 页面）。"""
    result = await _start_bilibili_qr_session()
    if not result.get("ok"):
        return JSONResponse(result, status_code=503)
    return result


@router.get("/cookies/bilibili-qr/status", dependencies=[Depends(_check_auth)])
async def cookies_bilibili_qr_status(session_id: str = Query("")):
    """轮询 B站二维码状态（Cookie 页面）。"""
    sid = normalize_text(session_id)
    if not sid:
        return JSONResponse({"ok": False, "status": "error", "message": "缺少 session_id"}, status_code=400)
    result = await _bilibili_qr_status(sid)
    if not result.get("ok") and str(result.get("status", "") or "") in {"expired", "error"}:
        return JSONResponse(result, status_code=410 if result.get("status") == "expired" else 400)
    return result


@router.post("/cookies/bilibili-qr/cancel", dependencies=[Depends(_check_auth)])
async def cookies_bilibili_qr_cancel(request: Request):
    """取消 B站二维码会话（Cookie 页面）。"""
    body = await request.json()
    sid = normalize_text(str(body.get("session_id", "")))
    if not sid:
        return JSONResponse({"ok": False, "message": "缺少 session_id"}, status_code=400)
    return _cancel_bilibili_qr_session(sid)


@router.post("/cookies/extract", dependencies=[Depends(_check_auth)])
async def extract_cookie(request: Request):
    """提取平台 Cookie。"""
    def _cookie_error(
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str = "",
        detail: str = "",
    ) -> JSONResponse:
        payload: dict[str, Any] = {"ok": False, "error_code": code, "message": message}
        if hint:
            payload["hint"] = hint
        if detail:
            payload["detail"] = detail
        return JSONResponse(payload, status_code=status_code)

    try:
        body = await request.json()
        platform = normalize_text(str(body.get("platform", "bilibili"))).lower() or "bilibili"
        if platform == "qq":
            platform = "qzone"
        browser = normalize_text(str(body.get("browser", "edge"))).lower() or "edge"
        allow_close = bool(body.get("allow_close", False))
        loop = asyncio.get_running_loop()

        from core.cookie_auth import (
            extract_bilibili_cookies,
            extract_douyin_cookie,
            extract_kuaishou_cookie,
            extract_qzone_cookies,
        )

        if platform == "bilibili":
            result = await loop.run_in_executor(
                None,
                lambda: extract_bilibili_cookies(browser=browser, auto_close=allow_close),
            )
            if result and isinstance(result, dict):
                sessdata = normalize_text(str(result.get("sessdata", "") or result.get("SESSDATA", "")))
                bili_jct = normalize_text(str(result.get("bili_jct", "") or result.get("BILI_JCT", "")))
                if sessdata:
                    cookie_dict = {"SESSDATA": sessdata}
                    if bili_jct:
                        cookie_dict["bili_jct"] = bili_jct
                    return JSONResponse(
                        {
                            "ok": True,
                            "cookie": json.dumps(cookie_dict, ensure_ascii=False),
                            "message": "B站 Cookie 提取成功（浏览器）",
                            "sessdata": sessdata,
                            "bili_jct": bili_jct,
                        }
                    )
            return _cookie_error(
                status_code=400,
                code="bilibili_extract_failed",
                message="B站 Cookie 提取失败",
                hint="请先在浏览器登录 B站，或改用“B站扫码登录”。",
            )

        elif platform == "douyin":
            cookie = await loop.run_in_executor(
                None,
                lambda: extract_douyin_cookie(browser=browser, auto_close=allow_close),
            )
            if cookie:
                return JSONResponse({"ok": True, "cookie": cookie, "message": "抖音 Cookie 提取成功"})
            return _cookie_error(
                status_code=400,
                code="douyin_extract_failed",
                message="抖音 Cookie 提取失败",
                hint="请确认已在当前浏览器登录抖音账号后重试。",
            )

        elif platform == "kuaishou":
            cookie = await loop.run_in_executor(
                None,
                lambda: extract_kuaishou_cookie(browser=browser, auto_close=allow_close),
            )
            if cookie:
                return JSONResponse({"ok": True, "cookie": cookie, "message": "快手 Cookie 提取成功"})
            return _cookie_error(
                status_code=400,
                code="kuaishou_extract_failed",
                message="快手 Cookie 提取失败",
                hint="请确认已在当前浏览器登录快手账号后重试。",
            )

        elif platform == "qzone":
            cookie = await loop.run_in_executor(
                None,
                lambda: extract_qzone_cookies(browser=browser, auto_close=allow_close),
            )
            if cookie:
                return JSONResponse({"ok": True, "cookie": cookie, "message": "QQ空间 Cookie 提取成功"})
            return _cookie_error(
                status_code=400,
                code="qzone_extract_failed",
                message="QQ空间 Cookie 提取失败",
                hint="请先登录 qzone.qq.com / user.qzone.qq.com，再重试提取。",
            )

        else:
            return _cookie_error(
                status_code=400,
                code="unsupported_platform",
                message="不支持的平台",
            )

    except Exception as e:
        _log.error(f"Cookie 提取失败: {e}", exc_info=True)
        return _cookie_error(
            status_code=500,
            code="internal_error",
            message="Cookie 提取失败（内部错误）",
            hint="请查看日志并检查浏览器登录状态后重试。",
            detail=str(e),
        )


@router.post("/cookies/save", dependencies=[Depends(_check_auth)])
async def save_cookie(request: Request):
    """保存 Cookie 到配置文件。"""
    try:
        body = await request.json()
        platform = normalize_text(str(body.get("platform", "bilibili"))).lower() or "bilibili"
        if platform == "qq":
            platform = "qzone"
        cookie = body.get("cookie", "")

        if not cookie:
            return JSONResponse({"error": "Cookie 不能为空"}, status_code=400)

        config_file = _ROOT_DIR / "config" / "config.yml"
        if not config_file.exists():
            return JSONResponse({"error": "配置文件不存在"}, status_code=404)

        config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            config = {}

        if platform == "bilibili":
            # B站支持 JSON 或标准 cookie 字符串。
            try:
                cookie_dict: dict[str, Any]
                if isinstance(cookie, dict):
                    cookie_dict = cookie
                else:
                    cookie_text = normalize_text(str(cookie))
                    if cookie_text.startswith("{") and cookie_text.endswith("}"):
                        parsed = json.loads(cookie_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("invalid_cookie_json")
                        cookie_dict = parsed
                    else:
                        cookie_dict = {}
                        for part in cookie_text.split(";"):
                            seg = part.strip()
                            if not seg or "=" not in seg:
                                continue
                            k, v = seg.split("=", 1)
                            key = normalize_text(k)
                            if key:
                                cookie_dict[key] = v.strip()
                sessdata = normalize_text(
                    str(cookie_dict.get("SESSDATA", "") or cookie_dict.get("sessdata", ""))
                )
                bili_jct = normalize_text(
                    str(cookie_dict.get("bili_jct", "") or cookie_dict.get("BILI_JCT", ""))
                )
                if not sessdata:
                    return JSONResponse({"error": "B站 Cookie 缺少 SESSDATA"}, status_code=400)

                if "video_analysis" not in config:
                    config["video_analysis"] = {}
                if "bilibili" not in config["video_analysis"]:
                    config["video_analysis"]["bilibili"] = {}

                config["video_analysis"]["bilibili"]["sessdata"] = sessdata
                config["video_analysis"]["bilibili"]["bili_jct"] = bili_jct
            except Exception:
                return JSONResponse({"error": "B站 Cookie 格式错误"}, status_code=400)

        elif platform in ["douyin", "kuaishou", "qzone"]:
            if "video_analysis" not in config:
                config["video_analysis"] = {}
            if platform not in config["video_analysis"]:
                config["video_analysis"][platform] = {}

            config["video_analysis"][platform]["cookie"] = cookie
        else:
            return JSONResponse({"error": "不支持的平台"}, status_code=400)

        # 保存配置
        config_file.write_text(
            yaml.safe_dump(config, allow_unicode=True, default_flow_style=False),
            encoding="utf-8"
        )

        return JSONResponse({"message": f"{platform} Cookie 保存成功"})

    except Exception as e:
        _log.error(f"Cookie 保存失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ============================================================================
# Setup Mode Endpoints
# ============================================================================

_setup_done = False
_setup_uvicorn_server: Any | None = None
_setup_bili_qr_sessions: dict[str, dict[str, Any]] = {}
_SETUP_BILI_QR_TTL_SECONDS = 150

setup_router = APIRouter(prefix="/api/webui/setup", tags=["setup"])

_SETUP_COOKIE_PLATFORM_DOMAINS: dict[str, list[str]] = {
    "bilibili": [".bilibili.com"],
    "douyin": [".douyin.com"],
    "kuaishou": [".kuaishou.com"],
    "qzone": [".qq.com", ".i.qq.com", ".qzone.qq.com"],
}

_SETUP_COOKIE_PLATFORM_SITES: dict[str, str] = {
    "bilibili": "bilibili.com",
    "douyin": "douyin.com",
    "kuaishou": "kuaishou.com",
    "qzone": "qzone.qq.com",
}

_SETUP_COOKIE_IMPORTANT_KEYS = {
    "bilibili": ["SESSDATA", "bili_jct"],
    "douyin": ["sessionid", "ttwid"],
    "kuaishou": ["kuaishou.sid", "userId"],
    "qzone": ["p_skey", "p_uin"],
}

_SETUP_API_ENV_MAP = {
    "skiapi": "${SKIAPI_KEY}",
    "openai": "${OPENAI_API_KEY}",
    "deepseek": "${DEEPSEEK_API_KEY}",
    "newapi": "${NEWAPI_API_KEY}",
    "anthropic": "${ANTHROPIC_API_KEY}",
    "gemini": "${GEMINI_API_KEY}",
    "openrouter": "${OPENROUTER_API_KEY}",
    "xai": "${XAI_API_KEY}",
    "qwen": "${QWEN_API_KEY}",
    "moonshot": "${MOONSHOT_API_KEY}",
    "mistral": "${MISTRAL_API_KEY}",
    "zhipu": "${ZHIPU_API_KEY}",
    "siliconflow": "${SILICONFLOW_API_KEY}",
}


def _cleanup_bilibili_qr_sessions() -> None:
    now = time.time()
    expired = [
        sid for sid, data in _setup_bili_qr_sessions.items()
        if now - float(data.get("created_at", 0.0) or 0.0) >= _SETUP_BILI_QR_TTL_SECONDS
    ]
    for sid in expired:
        _setup_bili_qr_sessions.pop(sid, None)


def _cookie_capabilities_payload() -> dict[str, Any]:
    from core.cookie_auth import get_cookie_runtime_capabilities
    payload = get_cookie_runtime_capabilities()
    payload["qr_session_ttl_seconds"] = _SETUP_BILI_QR_TTL_SECONDS
    return payload


async def _start_bilibili_qr_session() -> dict[str, Any]:
    from core.cookie_auth import bilibili_qr_create_session

    _cleanup_bilibili_qr_sessions()
    session = await bilibili_qr_create_session()
    if not session:
        return {"ok": False, "message": "当前环境未启用 B站扫码依赖，请安装 bilibili-api-python 后重试"}

    session_id = uuid.uuid4().hex
    _setup_bili_qr_sessions[session_id] = {
        "qr": session.get("qr"),
        "created_at": time.time(),
    }
    return {
        "ok": True,
        "session_id": session_id,
        "qr_url": str(session.get("qr_url", "") or ""),
        "qr_image_data_uri": str(session.get("qr_image_data_uri", "") or ""),
        "qr_terminal": str(session.get("qr_terminal", "") or ""),
        "expires_in_seconds": int(session.get("timeout_seconds", 120) or 120),
        "message": "请使用 B站 App 扫描二维码并在手机确认",
    }


async def _bilibili_qr_status(session_id: str) -> dict[str, Any]:
    from core.cookie_auth import bilibili_qr_check_state

    _cleanup_bilibili_qr_sessions()
    item = _setup_bili_qr_sessions.get(session_id)
    if not item:
        return {"ok": False, "status": "expired", "message": "二维码会话不存在或已过期，请重新获取"}

    qr = item.get("qr")
    if qr is None:
        _setup_bili_qr_sessions.pop(session_id, None)
        return {"ok": False, "status": "error", "message": "二维码会话异常，请重新获取"}

    result = await bilibili_qr_check_state(qr)
    status = str(result.get("status", "") or "")
    if status in {"done", "expired", "error"}:
        _setup_bili_qr_sessions.pop(session_id, None)
    return result


def _cancel_bilibili_qr_session(session_id: str) -> dict[str, Any]:
    _cleanup_bilibili_qr_sessions()
    existed = bool(_setup_bili_qr_sessions.pop(session_id, None))
    return {"ok": True, "cancelled": existed}

_SETUP_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "skiapi": {"model": "claude-opus-4-6", "base_url": "https://skiapi.dev", "endpoint_type": "openai"},
    "openai": {"model": "gpt-5.2", "base_url": "https://api.openai.com", "endpoint_type": "openai_response"},
    "anthropic": {"model": "claude-sonnet-4-5-20250929", "base_url": "https://api.anthropic.com", "endpoint_type": "anthropic"},
    "gemini": {"model": "gemini-2.5-pro", "base_url": "https://generativelanguage.googleapis.com", "endpoint_type": "gemini"},
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com", "endpoint_type": "openai"},
    "newapi": {"model": "gpt-5-codex", "base_url": "https://api.openai.com/v1", "endpoint_type": "openai"},
    "openrouter": {"model": "openrouter/auto", "base_url": "https://openrouter.ai/api/v1", "endpoint_type": "openai"},
    "xai": {"model": "grok-4.1-mini", "base_url": "https://api.x.ai/v1", "endpoint_type": "openai"},
    "qwen": {"model": "qwen-max-latest", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "endpoint_type": "openai"},
    "moonshot": {"model": "kimi-thinking-preview", "base_url": "https://api.moonshot.cn/v1", "endpoint_type": "openai"},
    "mistral": {"model": "mistral-medium-latest", "base_url": "https://api.mistral.ai", "endpoint_type": "openai"},
    "zhipu": {"model": "glm-4-plus", "base_url": "https://open.bigmodel.cn/api/paas/v4", "endpoint_type": "openai"},
    "siliconflow": {"model": "Qwen/Qwen2.5-72B-Instruct", "base_url": "https://api.siliconflow.cn/v1", "endpoint_type": "openai"},
}

_SETUP_IMAGE_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "skiapi": {"model": "grok-imagine-1.0", "base_url": "https://skiapi.dev/v1", "env": "${SKIAPI_KEY}"},
    "openai": {"model": "dall-e-3", "base_url": "https://api.openai.com/v1", "env": "${OPENAI_API_KEY}"},
    "xai": {"model": "grok-imagine-1.0", "base_url": "https://api.x.ai/v1", "env": "${XAI_API_KEY}"},
    "flux": {"model": "flux-1-schnell", "base_url": "https://api.siliconflow.cn/v1", "env": "${SILICONFLOW_API_KEY}"},
    "sd": {"model": "stable-diffusion-xl", "base_url": "http://127.0.0.1:7860", "env": "${API_KEY}"},
    "custom": {"model": "dall-e-3", "base_url": "", "env": "${API_KEY}"},
}

_SETUP_ENDPOINT_TYPE_OPTIONS = [
    {"value": "openai_response", "label": "OpenAI-Response"},
    {"value": "openai", "label": "OpenAI"},
    {"value": "anthropic", "label": "Anthropic"},
    {"value": "dmxapi", "label": "DMXAPI"},
    {"value": "gemini", "label": "Gemini"},
    {"value": "weiyi_ai", "label": "唯—AI (A)"},
]


def _setup_candidate_base_urls(base_url: str, prefer_v1: bool = True) -> list[str]:
    base = normalize_text(base_url).rstrip("/")
    if not base:
        return []
    with_v1 = base if base.endswith("/v1") else f"{base}/v1"
    without_v1 = base[:-3] if base.endswith("/v1") else base
    candidates = [with_v1, without_v1] if prefer_v1 else [without_v1, with_v1]
    uniq: list[str] = []
    for item in candidates:
        value = item.rstrip("/")
        if value and value not in uniq:
            uniq.append(value)
    return uniq


def _setup_strip_api_version_suffix(base_url: str) -> str:
    base = normalize_text(base_url).rstrip("/")
    for suffix in ("/v1beta", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _setup_normalize_endpoint_type(raw: str, provider: str) -> str:
    value = normalize_text(raw).lower().replace("-", "_")
    aliases = {
        "openairesponse": "openai_response",
        "openai_response": "openai_response",
        "responses": "openai_response",
        "openai": "openai",
        "chat_completions": "openai",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "dmxapi": "dmxapi",
        "weiyi": "weiyi_ai",
        "weiyi_ai": "weiyi_ai",
        "jina": "jina",
        "openai_image": "openai_image",
        "image_openai": "openai_image",
    }
    normalized = aliases.get(value, value)
    if normalized:
        return normalized
    return _SETUP_PROVIDER_DEFAULTS.get(provider, {}).get("endpoint_type", "openai")


def _setup_resolve_api_key(provider: str, raw_api_key: str) -> str:
    key = normalize_text(raw_api_key)
    if key.startswith("${") and key.endswith("}"):
        env_name = key[2:-1].strip()
        return normalize_text(os.environ.get(env_name, ""))
    if key:
        return key
    placeholder = _SETUP_API_ENV_MAP.get(provider, "")
    if placeholder.startswith("${") and placeholder.endswith("}"):
        env_name = placeholder[2:-1].strip()
        return normalize_text(os.environ.get(env_name, ""))
    return ""


def _setup_resolve_image_gen_api_key(
    *,
    image_provider: str,
    image_api_key_raw: str,
    primary_provider: str,
    primary_api_key_raw: str,
) -> str:
    key = normalize_text(image_api_key_raw)
    if key:
        return key

    primary_key = normalize_text(primary_api_key_raw)
    # 主模型与生图同 provider 时，优先复用主模型密钥/占位符。
    if image_provider == primary_provider:
        if primary_key:
            return primary_key
        return _SETUP_API_ENV_MAP.get(primary_provider, "${API_KEY}")

    # skiapi 密钥（sk-O...）可作为聚合网关生图密钥使用。
    if primary_key.startswith("sk-O"):
        return primary_key

    if image_provider in _SETUP_API_ENV_MAP:
        return _SETUP_API_ENV_MAP.get(image_provider, "${API_KEY}")
    return _SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_provider, {}).get("env", "${API_KEY}")


def _setup_resolve_image_gen_base_url(*, image_provider: str, image_base_url_raw: str, resolved_api_key: str) -> str:
    base = normalize_text(image_base_url_raw).rstrip("/")
    if base:
        return base

    # skiapi 密钥优先落到 skiapi 域名，避免 provider 选项与密钥来源不一致。
    if normalize_text(resolved_api_key).startswith("sk-O"):
        return "https://skiapi.dev/v1"

    provider_default = _SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_provider, {})
    base = normalize_text(provider_default.get("base_url", "")).rstrip("/")
    if not base:
        base = normalize_text(_SETUP_PROVIDER_DEFAULTS.get(image_provider, {}).get("base_url", "")).rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _normalize_image_gen_models_for_save(
    incoming_models: Any,
    existing_models: Any,
    default_provider: str = "openai",
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    old_lookup: dict[str, dict[str, Any]] = {}

    if isinstance(existing_models, list):
        for item in existing_models:
            if not isinstance(item, dict):
                continue
            for key in (
                normalize_text(str(item.get("name", ""))).lower(),
                normalize_text(str(item.get("model", ""))).lower(),
            ):
                if key:
                    old_lookup[key] = item

    if not isinstance(incoming_models, list):
        return normalized

    for raw in incoming_models:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        provider = normalize_text(str(item.get("provider", ""))).lower() or default_provider or "openai"
        model_name = normalize_text(str(item.get("model", ""))) or normalize_text(str(item.get("name", "")))
        if not model_name:
            continue
        name = normalize_text(str(item.get("name", ""))) or model_name
        item["provider"] = provider
        item["model"] = model_name
        item["name"] = name

        lookup = old_lookup.get(name.lower()) or old_lookup.get(model_name.lower())
        api_key = str(item.get("api_key", "")).strip()
        if api_key == "***":
            if isinstance(lookup, dict) and lookup.get("api_key"):
                item["api_key"] = lookup.get("api_key")
            else:
                item.pop("api_key", None)
        elif not api_key and isinstance(lookup, dict) and lookup.get("api_key"):
            item["api_key"] = lookup.get("api_key")

        resolved_key = normalize_text(str(item.get("api_key", "")))
        api_base = normalize_text(str(item.get("api_base", ""))).rstrip("/")
        if not api_base:
            old_base = normalize_text(str(lookup.get("api_base", ""))).rstrip("/") if isinstance(lookup, dict) else ""
            if old_base:
                item["api_base"] = old_base
            else:
                auto_base = _setup_resolve_image_gen_base_url(
                    image_provider=provider,
                    image_base_url_raw="",
                    resolved_api_key=resolved_key,
                )
                if auto_base:
                    item["api_base"] = auto_base
        else:
            item["api_base"] = api_base

        normalized.append(item)

    return normalized


def _ensure_image_gen_default_model(image_cfg: dict[str, Any]) -> tuple[str, bool]:
    """确保 default_model 命中 models；未命中时自动回填首个模型。"""
    if not isinstance(image_cfg, dict):
        return "", False

    models = image_cfg.get("models", [])
    if not isinstance(models, list) or not models:
        return normalize_text(str(image_cfg.get("default_model", ""))), False

    valid_keys: set[str] = set()
    first_model = ""
    for item in models:
        if not isinstance(item, dict):
            continue
        model_name = normalize_text(str(item.get("model", "")))
        display_name = normalize_text(str(item.get("name", "")))
        if model_name:
            if not first_model:
                first_model = model_name
            valid_keys.add(model_name)
            valid_keys.add(model_name.lower())
        if display_name:
            if not first_model:
                first_model = display_name
            valid_keys.add(display_name)
            valid_keys.add(display_name.lower())

    current_default = normalize_text(str(image_cfg.get("default_model", "")))
    if not first_model:
        return current_default, False

    if not current_default:
        image_cfg["default_model"] = first_model
        return first_model, True

    if current_default in valid_keys or current_default.lower() in valid_keys:
        return current_default, False

    image_cfg["default_model"] = first_model
    return first_model, True


def _setup_extract_response_text_openai(data: dict[str, Any]) -> str:
    output_text = normalize_text(str(data.get("output_text", "")))
    if output_text:
        return output_text

    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = normalize_text(str(block.get("text", "") or block.get("output_text", "")))
                if text:
                    parts.append(text)
        merged = normalize_text("\n".join(parts))
        if merged:
            return merged
    return ""


def _setup_build_config_from_legacy_payload(body: dict[str, Any]) -> dict[str, Any]:
    provider = normalize_text(str(body.get("provider", "skiapi"))).lower() or "skiapi"
    defaults = _SETUP_PROVIDER_DEFAULTS.get(provider, _SETUP_PROVIDER_DEFAULTS["skiapi"])
    model = normalize_text(str(body.get("model", ""))) or defaults.get("model", "")
    base_url = normalize_text(str(body.get("base_url", "")))
    endpoint_type = _setup_normalize_endpoint_type(str(body.get("endpoint_type", "")), provider)
    api_key_raw = normalize_text(str(body.get("api_key", "")))

    api_cfg: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "temperature": 0.7,
        "max_tokens": 1200,
        "timeout_seconds": 120,
        "endpoint_type": endpoint_type,
    }
    if base_url:
        api_cfg["base_url"] = base_url
    if api_key_raw:
        api_cfg["api_key"] = api_key_raw
    else:
        api_cfg["api_key"] = _SETUP_API_ENV_MAP.get(provider, "${API_KEY}")

    bot_name = normalize_text(str(body.get("bot_name", ""))) or "YuKiKo"
    allow_search = bool(body.get("search", True))
    allow_image = bool(body.get("image", True))
    allow_markdown = bool(body.get("markdown", True))
    super_admin_qq = normalize_text(str(body.get("super_admin_qq", "")))
    verbosity = normalize_text(str(body.get("verbosity", "medium"))).lower() or "medium"
    token_saving = bool(body.get("token_saving", False))
    music_enable = bool(body.get("music", True))
    music_api_base = normalize_text(str(body.get("music_api_base", ""))) or "http://mc.alger.fun/api"

    # 图片生成配置
    image_gen_enable = bool(body.get("image_gen_enable", True))
    image_gen_provider = normalize_text(str(body.get("image_gen_provider", ""))).lower()
    if not image_gen_provider:
        image_gen_provider = provider if provider in _SETUP_IMAGE_PROVIDER_DEFAULTS else "openai"
    image_defaults = _SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_gen_provider, {})
    image_gen_api_key = normalize_text(str(body.get("image_gen_api_key", "")))
    image_gen_base_url = normalize_text(str(body.get("image_gen_base_url", "")))
    image_gen_model = normalize_text(str(body.get("image_gen_model", ""))) or image_defaults.get("model", "dall-e-3")
    image_gen_size = normalize_text(str(body.get("image_gen_size", ""))) or "1024x1024"

    resolved_image_gen_api_key = _setup_resolve_image_gen_api_key(
        image_provider=image_gen_provider,
        image_api_key_raw=image_gen_api_key,
        primary_provider=provider,
        primary_api_key_raw=api_key_raw,
    )
    resolved_image_gen_base_url = _setup_resolve_image_gen_base_url(
        image_provider=image_gen_provider,
        image_base_url_raw=image_gen_base_url,
        resolved_api_key=resolved_image_gen_api_key,
    )

    # 构建图片生成模型配置
    image_gen_models = []
    if image_gen_enable:
        model_config: dict[str, Any] = {
            # name 统一用模型名，避免与 default_model 脱节导致运行时匹配失败
            "name": image_gen_model,
            "provider": image_gen_provider,
            "model": image_gen_model,
            "default_size": image_gen_size,
        }
        if resolved_image_gen_base_url:
            model_config["api_base"] = resolved_image_gen_base_url
        if resolved_image_gen_api_key:
            model_config["api_key"] = resolved_image_gen_api_key
        image_gen_models.append(model_config)

    config_data: dict[str, Any] = {
        "api": api_cfg,
        "bot": {
            "name": bot_name,
            "allow_search": allow_search,
            "allow_image": allow_image,
            "allow_markdown": allow_markdown,
        },
        "admin": {
            "super_admin_qq": super_admin_qq,
            "super_users": [super_admin_qq] if super_admin_qq else [],
        },
        "output": {
            "verbosity": verbosity,
            "token_saving": token_saving,
            "style_instruction": "",
            "group_overrides": {},
            "group_style_overrides": {},
        },
        "music": {
            "enable": music_enable,
            "api_base": music_api_base,
        },
        "video_analysis": {
            "bilibili": {
                "enable": True,
                "sessdata": normalize_text(str(body.get("bili_sessdata", ""))),
                "bili_jct": normalize_text(str(body.get("bili_jct", ""))),
            },
            "douyin": {
                "enable": True,
                "cookie": normalize_text(str(body.get("douyin_cookie", ""))),
            },
            "kuaishou": {
                "enable": True,
                "cookie": normalize_text(str(body.get("kuaishou_cookie", ""))),
            },
            "qzone": {
                "enable": True,
                "cookie": normalize_text(str(body.get("qzone_cookie", ""))),
            },
        },
        "image_gen": {
            "enable": image_gen_enable,
            "default_model": image_gen_model,
            "default_size": image_gen_size,
            "nsfw_filter": True,
            "max_prompt_length": 1000,
            "models": image_gen_models,
        },
    }
    return config_data


@setup_router.get("/health")
async def setup_health():
    """Setup 模式健康检查。"""
    return {"status": "setup_mode"}


@setup_router.get("/status")
async def setup_status():
    """Setup 模式状态。"""
    config_file = _ROOT_DIR / "config" / "config.yml"
    return {"setup_done": config_file.exists()}


@setup_router.get("/defaults")
async def setup_defaults():
    """获取 Setup 默认选项。"""
    providers = []
    for key in [
        "skiapi", "openai", "anthropic", "gemini", "deepseek", "newapi",
        "openrouter", "xai", "qwen", "moonshot", "mistral", "zhipu", "siliconflow",
    ]:
        default_item = _SETUP_PROVIDER_DEFAULTS.get(key, {})
        label = {
            "skiapi": "SKIAPI",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
            "gemini": "Gemini",
            "deepseek": "DeepSeek",
            "newapi": "NEWAPI",
            "openrouter": "OpenRouter",
            "xai": "xAI (Grok)",
            "qwen": "Qwen",
            "moonshot": "Moonshot (Kimi)",
            "mistral": "Mistral",
            "zhipu": "Zhipu",
            "siliconflow": "SiliconFlow",
        }.get(key, key)
        providers.append(
            {
                "value": key,
                "label": label,
                "default_model": default_item.get("model", ""),
                "default_base_url": default_item.get("base_url", ""),
                "default_endpoint_type": default_item.get("endpoint_type", "openai"),
            }
        )
    return {
        "providers": providers,
        "endpoint_types": _SETUP_ENDPOINT_TYPE_OPTIONS,
        "verbosity_options": [
            {"value": "verbose", "label": "详细"},
            {"value": "medium", "label": "中等"},
            {"value": "brief", "label": "偏短"},
            {"value": "minimal", "label": "极简"},
        ],
    }


@setup_router.get("/cookie-capabilities")
async def setup_cookie_capabilities():
    """返回 Cookie 自动提取能力（按当前部署环境）。"""
    return {"ok": True, "data": _cookie_capabilities_payload()}


@setup_router.post("/bilibili-qr/start")
async def setup_bilibili_qr_start():
    """开始 B站二维码登录会话（Setup 页面）。"""
    result = await _start_bilibili_qr_session()
    if not result.get("ok"):
        return JSONResponse(result, status_code=503)
    return result


@setup_router.get("/bilibili-qr/status")
async def setup_bilibili_qr_status(session_id: str = Query("")):
    """轮询 B站二维码状态（Setup 页面）。"""
    sid = normalize_text(session_id)
    if not sid:
        return JSONResponse({"ok": False, "status": "error", "message": "缺少 session_id"}, status_code=400)
    result = await _bilibili_qr_status(sid)
    if not result.get("ok") and str(result.get("status", "") or "") in {"expired", "error"}:
        return JSONResponse(result, status_code=410 if result.get("status") == "expired" else 400)
    return result


@setup_router.post("/bilibili-qr/cancel")
async def setup_bilibili_qr_cancel(request: Request):
    """取消 B站二维码会话（Setup 页面）。"""
    body = await request.json()
    sid = normalize_text(str(body.get("session_id", "")))
    if not sid:
        return JSONResponse({"ok": False, "message": "缺少 session_id"}, status_code=400)
    return _cancel_bilibili_qr_session(sid)


@setup_router.post("/test-api")
async def setup_test_api(request: Request):
    """测试 API 配置连通性。"""
    body = await request.json()
    provider = normalize_text(str(body.get("provider", "skiapi"))).lower() or "skiapi"
    defaults = _SETUP_PROVIDER_DEFAULTS.get(provider, _SETUP_PROVIDER_DEFAULTS["skiapi"])
    endpoint_type = _setup_normalize_endpoint_type(str(body.get("endpoint_type", "")), provider)
    model = normalize_text(str(body.get("model", ""))) or defaults.get("model", "")
    base_url = normalize_text(str(body.get("base_url", ""))) or defaults.get("base_url", "")
    api_key = _setup_resolve_api_key(provider, str(body.get("api_key", "")))

    try:
        timeout_seconds = max(5.0, min(60.0, float(body.get("timeout_seconds", 18))))
    except Exception:
        timeout_seconds = 18.0

    if not model:
        return {"ok": False, "message": "模型名称不能为空"}
    if not base_url:
        return {"ok": False, "message": "Base URL 不能为空"}
    if not api_key:
        env_hint = _SETUP_API_ENV_MAP.get(provider, "${API_KEY}")
        return {"ok": False, "message": f"API Key 为空（可设置环境变量 {env_hint}）"}

    async def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds / 2))) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            detail = ""
            try:
                data = response.json()
                if isinstance(data, dict):
                    err = data.get("error")
                    if isinstance(err, dict):
                        detail = normalize_text(str(err.get("message", "")))
                    if not detail:
                        detail = normalize_text(str(data.get("message", "")))
            except Exception:
                detail = ""
            if not detail:
                detail = normalize_text((response.text or "")[:220])
            raise RuntimeError(f"HTTP {response.status_code}: {detail or '请求失败'}")
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"返回非 JSON: {(response.text or '')[:180]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("返回格式异常：顶层不是对象")
        return response.status_code, data

    async def _post_sse_collect(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, str]:
        """流式(SSE)连通性探测：聚合文本增量，返回最终文本。"""
        text_parts: list[str] = []
        status_code = 0
        last_response_obj: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds / 2))) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                status_code = int(response.status_code)
                if response.status_code >= 400:
                    body = normalize_text((await response.aread()).decode(errors="ignore")[:220])
                    raise RuntimeError(f"HTTP {response.status_code}: {body or '请求失败'}")

                async for raw_line in response.aiter_lines():
                    line = normalize_text(raw_line)
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = normalize_text(line[5:])
                    if not line:
                        continue
                    if line == "[DONE]":
                        break
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue

                    # OpenAI Responses 流式事件
                    event_type = normalize_text(str(event.get("type", ""))).lower()
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if delta is not None:
                            text_parts.append(str(delta))
                        continue
                    if event_type == "response.completed":
                        resp = event.get("response")
                        if isinstance(resp, dict):
                            last_response_obj = resp
                        continue
                    if event_type in {"error", "response.error"}:
                        err = event.get("error")
                        if isinstance(err, dict):
                            msg = normalize_text(str(err.get("message", "")))
                            raise RuntimeError(msg or "流式接口返回错误事件")
                        raise RuntimeError(normalize_text(str(err)) or "流式接口返回错误事件")

                    # 兼容 Chat Completions 风格流式
                    choices = event.get("choices")
                    if isinstance(choices, list) and choices:
                        c0 = choices[0] if isinstance(choices[0], dict) else {}
                        delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
                        maybe_content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(maybe_content, str):
                            text_parts.append(maybe_content)
                        elif isinstance(maybe_content, list):
                            for part in maybe_content:
                                if isinstance(part, dict):
                                    t = part.get("text")
                                    if t is not None:
                                        text_parts.append(str(t))
                                elif part is not None:
                                    text_parts.append(str(part))

        merged = normalize_text("".join(text_parts))
        if merged:
            return status_code, merged
        if last_response_obj:
            recovered = _setup_extract_response_text_openai(last_response_obj)
            if recovered:
                return status_code, recovered
        raise RuntimeError("stream 成功但未返回文本")

    started = time.perf_counter()
    errors: list[str] = []

    try:
        # OpenAI 兼容协议族（含 DMXAPI/唯AI/Jina）
        if endpoint_type in {"openai", "openai_response", "openai_image", "dmxapi", "weiyi_ai", "jina"}:
            if endpoint_type == "dmxapi" and not normalize_text(str(body.get("base_url", ""))):
                base_url = "https://www.dmxapi.com/v1"
            elif endpoint_type == "weiyi_ai" and not normalize_text(str(body.get("base_url", ""))):
                base_url = "https://api.vveai.com/v1"
            elif endpoint_type == "jina" and not normalize_text(str(body.get("base_url", ""))):
                base_url = "https://api.jina.ai/v1"

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            for base in _setup_candidate_base_urls(base_url, prefer_v1=True):
                try:
                    if endpoint_type == "openai_response":
                        payload = {
                            "model": model,
                            "input": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": "ping"},
                                    ],
                                }
                            ],
                            "max_output_tokens": 24,
                            "temperature": 0,
                        }
                        try:
                            status_code, data = await _post_json(f"{base}/responses", headers, payload)
                            content = _setup_extract_response_text_openai(data)
                        except Exception as exc:
                            # 兼容 SkiAPI 等要求 Responses 必须 stream=true 的网关
                            message = normalize_text(str(exc)).lower()
                            if "stream must be set to true" not in message and "stream must be true" not in message:
                                raise
                            stream_payload = dict(payload)
                            stream_payload["stream"] = True
                            status_code, content = await _post_sse_collect(f"{base}/responses", headers, stream_payload)
                        if not content:
                            raise RuntimeError("responses 成功但未返回文本")
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        return {
                            "ok": True,
                            "message": f"连接成功（Responses）",
                            "latency_ms": elapsed_ms,
                            "status_code": status_code,
                        }

                    if endpoint_type == "openai_image":
                        payload = {
                            "model": model,
                            "prompt": "API connectivity check image",
                            "size": "256x256",
                        }
                        status_code, data = await _post_json(f"{base}/images/generations", headers, payload)
                        items = data.get("data")
                        if not isinstance(items, list) or not items:
                            raise RuntimeError("images 接口成功但未返回 data")
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        return {
                            "ok": True,
                            "message": f"连接成功（Image Generation）",
                            "latency_ms": elapsed_ms,
                            "status_code": status_code,
                        }

                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 24,
                        "temperature": 0,
                    }
                    status_code, data = await _post_json(f"{base}/chat/completions", headers, payload)
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        raise RuntimeError("chat/completions 成功但无 choices")
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return {
                        "ok": True,
                        "message": "连接成功（Chat Completions）",
                        "latency_ms": elapsed_ms,
                        "status_code": status_code,
                    }
                except Exception as exc:
                    errors.append(f"{base} -> {exc}")

        elif endpoint_type == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
                "max_tokens": 24,
                "temperature": 0,
            }
            for base in _setup_candidate_base_urls(base_url, prefer_v1=True):
                try:
                    status_code, data = await _post_json(f"{base}/messages", headers, payload)
                    content = data.get("content")
                    if not isinstance(content, list) or not content:
                        raise RuntimeError("Anthropic 成功但无 content")
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return {
                        "ok": True,
                        "message": "连接成功（Anthropic Messages）",
                        "latency_ms": elapsed_ms,
                        "status_code": status_code,
                    }
                except Exception as exc:
                    errors.append(f"{base} -> {exc}")

        elif endpoint_type == "gemini":
            base_root = _setup_strip_api_version_suffix(base_url)
            headers = {"Content-Type": "application/json"}
            model_escaped = model.replace("/", "%2F")
            payload = {
                "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 24},
            }
            for version in ("v1beta", "v1"):
                url = f"{base_root}/{version}/models/{model_escaped}:generateContent?key={api_key}"
                try:
                    status_code, data = await _post_json(url, headers, payload)
                    candidates = data.get("candidates")
                    if not isinstance(candidates, list) or not candidates:
                        raise RuntimeError("Gemini 成功但无 candidates")
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return {
                        "ok": True,
                        "message": f"连接成功（Gemini {version}）",
                        "latency_ms": elapsed_ms,
                        "status_code": status_code,
                    }
                except Exception as exc:
                    errors.append(f"{url} -> {exc}")

        else:
            return {"ok": False, "message": f"不支持的端点类型: {endpoint_type}"}

    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    tail = " | ".join(errors[-2:]) if errors else "未知错误"
    return {
        "ok": False,
        "message": f"连通性检测失败: {tail}",
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


@setup_router.post("/test-image-gen")
async def setup_test_image_gen(request: Request):
    """测试图片生成配置。"""
    body = await request.json()
    provider = normalize_text(str(body.get("provider", "openai"))).lower() or "openai"
    model = normalize_text(str(body.get("model", ""))) or "dall-e-3"
    api_key = normalize_text(str(body.get("api_key", "")))
    base_url = normalize_text(str(body.get("base_url", "")))
    size = normalize_text(str(body.get("size", ""))) or "1024x1024"

    # 使用与主 API 配置相同的默认值
    if not base_url:
        # 如果 API key 是 SKIAPI 的（sk-O 开头），强制使用 SKIAPI 域名
        if api_key and api_key.startswith("sk-O"):
            base_url = "https://skiapi.dev/v1"
        else:
            provider_defaults = _SETUP_PROVIDER_DEFAULTS.get(provider, _SETUP_PROVIDER_DEFAULTS.get("skiapi", {}))
            base_url = provider_defaults.get("base_url", "https://skiapi.dev")
            # 确保有 /v1 后缀（除非已经有了）
            if not base_url.endswith("/v1"):
                base_url = f"{base_url}/v1"

    # 解析 API Key
    if not api_key:
        env_map = {
            "openai": "OPENAI_API_KEY",
            "xai": "XAI_API_KEY",
        }
        env_var = env_map.get(provider, "SKIAPI_KEY")
        api_key = os.environ.get(env_var, "")
        if not api_key:
            return {"ok": False, "message": f"API Key 为空（可设置环境变量 {env_var}）"}

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 根据模型构建不同的 payload（Grok Imagine 不支持 size）
        if "grok-imagine" in model.lower():
            payload = {
                "model": model,
                "prompt": "A cute anime catgirl with pink hair, wearing a maid outfit, smiling happily, high quality, detailed",
                "n": 1,
            }
        else:
            # OpenAI / DALL-E 标准格式
            payload = {
                "model": model,
                "prompt": "A cute anime catgirl with pink hair, wearing a maid outfit, smiling happily, high quality, detailed",
                "n": 1,
                "size": size,
            }

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            response = await client.post(f"{base_url}/images/generations", headers=headers, json=payload)

        if response.status_code >= 400:
            detail = ""
            try:
                data = response.json()
                if isinstance(data, dict):
                    err = data.get("error")
                    if isinstance(err, dict):
                        detail = normalize_text(str(err.get("message", "")))
                    elif isinstance(err, str):
                        detail = normalize_text(err)
                    if not detail:
                        detail = normalize_text(str(data.get("message", "")))
                    if not detail:
                        detail = normalize_text(str(data.get("code", "")))
            except Exception:
                detail = ""
            if not detail:
                detail = normalize_text((response.text or "")[:220])
            return {"ok": False, "message": f"HTTP {response.status_code}: {detail or '请求失败'}"}

        try:
            data = response.json()
        except Exception:
            return {"ok": False, "message": f"返回非 JSON: {(response.text or '')[:180]}"}

        if not isinstance(data, dict):
            return {"ok": False, "message": "返回格式异常"}

        # 提取图片 URL
        image_data = data.get("data")
        if not isinstance(image_data, list) or not image_data:
            return {"ok": False, "message": "返回数据中没有图片"}

        image_url = image_data[0].get("url")
        if not image_url:
            return {"ok": False, "message": "图片 URL 为空"}

        return {
            "ok": True,
            "message": "生成成功",
            "image_url": image_url,
        }

    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _join_cookie_pairs(cookies: dict[str, str], important: list[str] | None = None) -> str:
    """Join cookie dict into cookie string, pinning important keys first."""
    if not cookies:
        return ""
    parts: list[str] = []
    pinned = set(important or [])
    for key in important or []:
        val = str(cookies.get(key, "") or "")
        if val:
            parts.append(f"{key}={val}")
    for key, val in cookies.items():
        if key in pinned:
            continue
        value = str(val or "")
        if value:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _format_platform_cookie_payload(
    platform: str,
    raw_by_domain: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """Format extracted cookies into the payload expected by the frontend."""
    if platform == "bilibili":
        bili = raw_by_domain.get(".bilibili.com", {})
        sessdata = str(bili.get("SESSDATA", "") or "")
        if not sessdata:
            return None
        return {
            "sessdata": sessdata,
            "bili_jct": str(bili.get("bili_jct", "") or ""),
        }

    if platform == "douyin":
        dy = raw_by_domain.get(".douyin.com", {})
        cookie = _join_cookie_pairs(dy, _SETUP_COOKIE_IMPORTANT_KEYS.get("douyin", []))
        return {"cookie": cookie} if cookie else None

    if platform == "kuaishou":
        ks = raw_by_domain.get(".kuaishou.com", {})
        cookie = _join_cookie_pairs(ks)
        return {"cookie": cookie} if cookie else None

    if platform == "qzone":
        qq = raw_by_domain.get(".qq.com", {})
        qz = raw_by_domain.get(".qzone.qq.com", {})
        iqq = raw_by_domain.get(".i.qq.com", {})
        merged = {**qq, **iqq, **qz}
        if not str(merged.get("p_skey", "") or merged.get("skey", "") or ""):
            return None
        cookie = _join_cookie_pairs(merged, _SETUP_COOKIE_IMPORTANT_KEYS.get("qzone", []))
        return {"cookie": cookie} if cookie else None

    return None


def _not_found_message(
    *,
    platform: str,
    running: bool,
    allow_close: bool,
    sources: dict[str, str],
) -> str:
    site = _SETUP_COOKIE_PLATFORM_SITES.get(platform, platform)
    source_used = ",".join(sorted({str(v) for v in sources.values() if str(v)})) or "none"

    qzone_hint = ""
    if platform == "qzone":
        qzone_hint = (
            "\n\n[QQ Space Cookie Guide]\n"
            "1. Visit https://qzone.qq.com and login with YOUR QQ account\n"
            "2. After login, it redirects to https://user.qzone.qq.com/yourQQ\n"
            "3. Make sure page fully loaded with your posts/albums visible\n"
            "4. Do NOT just visit others' space, MUST login to YOUR OWN space\n"
            "5. Chrome/Edge v130+ may need admin privileges"
        )

    if running and not allow_close:
        return (
            f"Not found {site} Cookie. Tried no-close extraction (source={source_used}) failed. "
            f"Please confirm browser logged in to {site}; "
            f"if still fails, enable auto-close retry or run as admin."
            f"{qzone_hint}"
        )

    if running and allow_close:
        return (
            f"Not found {site} Cookie. Tried auto-close retry (source={source_used}) failed. "
            f"Please confirm account logged in current browser profile."
            f"{qzone_hint}"
        )

    return (
        f"Not found {site} Cookie (source={source_used}). "
        f"Please login {site} in browser first; "
        f"If Chromium v130+, recommend running as admin."
        f"{qzone_hint}"
    )


@setup_router.post("/extract-cookie")
async def setup_extract_cookie(request: Request):
    """Extract cookies from browser for a specific platform."""
    import asyncio
    body = await request.json()
    platform = str(body.get("platform", ""))
    browser = str(body.get("browser", "edge"))
    allow_close = bool(body.get("allow_close", False))
    if platform not in _SETUP_COOKIE_PLATFORM_DOMAINS:
        raise HTTPException(400, f"Unknown platform: {platform}")

    loop = asyncio.get_event_loop()
    domains = _SETUP_COOKIE_PLATFORM_DOMAINS[platform]

    try:
        def _extract_platform():
            from core.cookie_auth import extract_browser_cookies_with_source, is_browser_running

            raw: dict[str, dict[str, str]] = {}
            sources: dict[str, str] = {}
            for domain in domains:
                cookies, source = extract_browser_cookies_with_source(
                    browser=browser,
                    domain=domain,
                    auto_close=allow_close,
                )
                sources[domain] = source
                if cookies:
                    raw[domain] = cookies
            running = bool(is_browser_running(browser))
            return raw, sources, running

        raw_by_domain, sources, running = await loop.run_in_executor(None, _extract_platform)
        payload = _format_platform_cookie_payload(platform, raw_by_domain)

        _log.info(
            "setup_extract_cookie | platform=%s | browser=%s | allow_close=%s | sources=%s | ok=%s",
            platform, browser, allow_close, sources, bool(payload),
        )

        if payload:
            return {
                "ok": True,
                "data": payload,
                "meta": {"browser": browser, "sources": sources, "running": running},
            }
        return {
            "ok": False,
            "message": _not_found_message(
                platform=platform, running=running,
                allow_close=allow_close, sources=sources,
            ),
            "meta": {"browser": browser, "sources": sources, "running": running, "allow_close": allow_close},
        }
    except ImportError as exc:
        return {"ok": False, "message": f"Missing dependency: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# Smart extract state
_smart_extract_result: dict | None = None
_smart_extract_meta: dict | None = None
_smart_extract_status: str = "idle"  # idle | running | done | error
_smart_extract_error: str = ""


@setup_router.post("/smart-extract")
async def setup_smart_extract(request: Request):
    """Smart extract all platform cookies (default: no browser restart)."""
    import asyncio
    global _smart_extract_result, _smart_extract_meta, _smart_extract_status, _smart_extract_error

    if _smart_extract_status == "running":
        return {"ok": False, "message": "Extraction in progress, please wait..."}

    body = await request.json()
    browser = str(body.get("browser", "edge"))
    allow_close = bool(body.get("allow_close", False))
    setup_url = str(request.base_url).rstrip("/") + "/webui/setup"

    _smart_extract_status = "running"
    _smart_extract_result = None
    _smart_extract_meta = None
    _smart_extract_error = ""

    loop = asyncio.get_event_loop()

    async def _do_extract():
        global _smart_extract_result, _smart_extract_meta, _smart_extract_status, _smart_extract_error
        try:
            def _sync_extract():
                from core.cookie_auth import smart_extract_all_cookies, smart_extract_all_cookies_no_restart
                mode = "no_restart"
                restart_attempted = False
                raw, meta = smart_extract_all_cookies_no_restart(
                    browser=browser, include_meta=True,
                )
                # If allow_close and some platforms missing, try restart approach
                if allow_close:
                    missing = [d for d in [".bilibili.com", ".douyin.com", ".kuaishou.com", ".qq.com", ".qzone.qq.com"]
                               if d not in raw or not raw[d]]
                    if missing:
                        restart_attempted = True
                        restarted = smart_extract_all_cookies(
                            browser=browser, setup_url=setup_url,
                            domains=missing,
                        )
                        if restarted:
                            mode = "restart"
                        for d, cookies in restarted.items():
                            if cookies and (d not in raw or len(cookies) > len(raw.get(d, {}))):
                                raw[d] = cookies
                return raw, meta, mode, restart_attempted

            raw, meta, mode, restart_attempted = await loop.run_in_executor(None, _sync_extract)

            # Format per-platform
            result = {}
            platform_counts: dict[str, int] = {}
            for platform in ["bilibili", "douyin", "kuaishou", "qzone"]:
                payload = _format_platform_cookie_payload(platform, raw)
                if payload:
                    result[platform] = payload
                    platform_counts[platform] = len(payload)
                else:
                    platform_counts[platform] = 0

            _smart_extract_result = result
            _smart_extract_meta = {
                "browser": browser,
                "sources": meta.get("sources", {}),
                "warnings": meta.get("warnings", []),
                "mode": mode,
                "restart_attempted": restart_attempted,
                "platform_counts": platform_counts,
                "found_platforms": sorted(result.keys()),
            }
            _smart_extract_status = "done"
        except Exception as exc:
            _smart_extract_status = "error"
            _smart_extract_error = str(exc)

    asyncio.create_task(_do_extract())
    return {"ok": True, "status": "running"}


@setup_router.get("/smart-extract-result")
async def setup_smart_extract_result():
    """Get smart extraction result."""
    if _smart_extract_status == "idle":
        return {"status": "idle"}
    elif _smart_extract_status == "running":
        return {"status": "running"}
    elif _smart_extract_status == "error":
        return {"status": "error", "message": _smart_extract_error}
    else:
        return {"status": "done", "data": _smart_extract_result, "meta": _smart_extract_meta or {}}


@setup_router.post("/save")
async def setup_save(request: Request):
    """保存 Setup 配置。"""
    body = await request.json()
    config_data = body.get("config")
    if not isinstance(config_data, dict):
        config_data = _setup_build_config_from_legacy_payload(body if isinstance(body, dict) else {})

    if not isinstance(config_data, dict):
        raise HTTPException(400, "config 必须是对象")

    # 加密敏感字段
    try:
        from core.crypto import SecretManager
        sm = SecretManager(_ROOT_DIR / "storage" / ".secret_key")

        # 加密 API key
        api_cfg = config_data.get("api", {})
        if "api_key" in api_cfg and api_cfg["api_key"]:
            api_key = str(api_cfg["api_key"])
            if not (api_key.startswith("${") and api_key.endswith("}")):
                api_cfg["api_key"] = sm.encrypt(api_key)

        # 加密 cookie
        video_cfg = config_data.get("video_analysis", {})
        if "bilibili" in video_cfg:
            bili = video_cfg["bilibili"]
            if "sessdata" in bili and bili["sessdata"]:
                bili["sessdata"] = sm.encrypt(str(bili["sessdata"]))
            if "bili_jct" in bili and bili["bili_jct"]:
                bili["bili_jct"] = sm.encrypt(str(bili["bili_jct"]))

        for platform in ["douyin", "kuaishou", "qzone"]:
            if platform in video_cfg and "cookie" in video_cfg[platform]:
                cookie = video_cfg[platform]["cookie"]
                if cookie:
                    video_cfg[platform]["cookie"] = sm.encrypt(str(cookie))

        # 加密 image_gen 模型 API key
        image_gen_cfg = config_data.get("image_gen", {})
        models = image_gen_cfg.get("models", []) if isinstance(image_gen_cfg, dict) else []
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

    except Exception as e:
        _log.warning(f"加密失败，使用明文存储: {e}")

    # 合并到模板
    template = load_config_template()
    merged = _deep_merge_template(template, _strip_deprecated_local_paths_config(config_data))

    # 写入配置文件
    config_file = _ROOT_DIR / "config" / "config.yml"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    header = (
        "# YuKiKo Bot 配置文件\n"
        "# 由 WebUI Setup 自动生成\n"
        "# 修改后发送 /yukibot 或 /yukiko 即可热重载\n\n"
    )

    with open(config_file, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(merged, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 确保 prompts.yml 存在
    _ensure_prompts_file_from_template(_PROMPTS_FILE)

    _log.info("Setup 配置已保存")

    # 停止 setup server
    global _setup_uvicorn_server
    if _setup_uvicorn_server:
        _setup_uvicorn_server.should_exit = True

    return {"ok": True, "message": "配置已保存，Setup 完成"}


def _make_spa_app(dist_dir: Path, api_router: APIRouter):
    """创建 SPA 应用（用于 Setup 模式）。"""
    from fastapi import FastAPI
    from starlette.staticfiles import StaticFiles
    from starlette.responses import FileResponse, Response

    app = FastAPI()
    app.include_router(api_router)

    if dist_dir.exists():
        index_file = dist_dir / "index.html"
        assets_dir = dist_dir / "assets"

        if assets_dir.exists():
            app.mount("/webui/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/webui/{path:path}")
        async def spa_handler(path: str):
            if path.lower().startswith("setup"):
                if index_file.exists():
                    return FileResponse(index_file)
            file_path = dist_dir / path
            if file_path.is_file() and ".." not in path:
                return FileResponse(file_path)
            if index_file.exists():
                return FileResponse(index_file)
            return Response("Not found", status_code=404)

        @app.get("/webui")
        async def spa_root():
            if index_file.exists():
                return FileResponse(index_file)
            return Response("Not found", status_code=404)

        @app.get("/")
        async def root_redirect():
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/webui/setup", status_code=307)

    return app


def run_setup_server(host: str = "127.0.0.1", port: int = 8081):
    """运行 Setup 服务器。"""
    import uvicorn

    # 构建 SPA 应用
    webui_dist = Path(__file__).resolve().parents[1] / "webui" / "dist"
    app = _make_spa_app(webui_dist, api_router=setup_router)

    print(f"\n  YuKiKo 首次运行配置向导")
    print(f"  请在浏览器打开: http://{host}:{port}/webui/setup\n")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    global _setup_uvicorn_server
    _setup_uvicorn_server = server

    try:
        server.run()
    finally:
        _setup_uvicorn_server = None
