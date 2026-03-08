"""WebUI 管理 API — 挂载到 nonebot FastAPI 上的管理接口。

提供配置编辑、提示词编辑、日志查看、状态查询等 REST + WebSocket 端点。
认证方式：Bearer token，配置在 .env WEBUI_TOKEN。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from fastapi.responses import JSONResponse
from starlette.requests import Request

from core.config_templates import (
    deep_merge_dict as _deep_merge_template,
    ensure_prompts_file as _ensure_prompts_file_from_template,
    load_config_template,
    load_prompts_template,
)
from core import prompt_loader as _pl
from utils.text import normalize_text

_log = logging.getLogger("yukiko.webui")
router = APIRouter(prefix="/api/webui", tags=["webui"])

_engine: Any = None
_start_time: float = time.time()
_LOG_SPLIT_RE = re.compile(r'(?=(?:\d{4}-\d{2}-\d{2}|(?<!\d{4}-)\d{2}-\d{2}) \d{2}:\d{2}:\d{2} (?:\||\[))')
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

    payload: list[dict[str, Any]] = []
    for name in sorted(plugin_map.keys(), key=lambda x: normalize_text(str(x)).lower()):
        plugin_obj = plugin_map.get(name)
        schema = schema_map.get(name, {})

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
        "yaml_text": text,
        "parsed": parsed,
        "generated_default": generated,
    }


@router.put("/prompts", dependencies=[Depends(_check_auth)])
async def put_prompts(request: Request):
    """完整替换提示词配置。"""
    body = await request.json()
    yaml_text = body.get("yaml_text", "")

    if not yaml_text.strip():
        raise HTTPException(400, "yaml_text 不能为空")

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

    return {"ok": True, "message": "提示词已保存并重载"}


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

    return {"ok": True, "message": "提示词已更新并重载"}


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

    current_config["image_gen"].update(image_gen_cfg)

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

    except Exception as e:
        _log.error(f"WebSocket 日志流错误: {e}")
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


# ============================================================================
# Cookie 管理 API
# ============================================================================

@router.post("/cookies/extract", dependencies=[Depends(_check_auth)])
async def extract_cookie(request: Request):
    """提取平台 Cookie。"""
    try:
        body = await request.json()
        platform = body.get("platform", "bilibili")

        from core.cookie_auth import (
            extract_bilibili_cookies,
            extract_douyin_cookie,
            extract_kuaishou_cookie,
        )

        if platform == "bilibili":
            result = await extract_bilibili_cookies()
            if result and isinstance(result, dict):
                sessdata = result.get("SESSDATA", "")
                bili_jct = result.get("bili_jct", "")
                if sessdata:
                    return JSONResponse({
                        "cookie": json.dumps(result),
                        "message": "B站登录成功",
                        "sessdata": sessdata,
                        "bili_jct": bili_jct,
                    })
            return JSONResponse({"error": "B站登录失败"}, status_code=400)

        elif platform == "douyin":
            cookie = await extract_douyin_cookie()
            if cookie:
                return JSONResponse({"cookie": cookie, "message": "抖音 Cookie 提取成功"})
            return JSONResponse({"error": "抖音 Cookie 提取失败"}, status_code=400)

        elif platform == "kuaishou":
            cookie = await extract_kuaishou_cookie()
            if cookie:
                return JSONResponse({"cookie": cookie, "message": "快手 Cookie 提取成功"})
            return JSONResponse({"error": "快手 Cookie 提取失败"}, status_code=400)

        else:
            return JSONResponse({"error": "不支持的平台"}, status_code=400)

    except Exception as e:
        _log.error(f"Cookie 提取失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/cookies/save", dependencies=[Depends(_check_auth)])
async def save_cookie(request: Request):
    """保存 Cookie 到配置文件。"""
    try:
        body = await request.json()
        platform = body.get("platform", "bilibili")
        cookie = body.get("cookie", "")

        if not cookie:
            return JSONResponse({"error": "Cookie 不能为空"}, status_code=400)

        config_file = _ROOT_DIR / "config" / "config.yml"
        if not config_file.exists():
            return JSONResponse({"error": "配置文件不存在"}, status_code=404)

        config = yaml.safe_load(config_file.read_text(encoding="utf-8"))

        if platform == "bilibili":
            # B站需要解析 JSON 格式的 cookie
            try:
                cookie_dict = json.loads(cookie) if isinstance(cookie, str) else cookie
                sessdata = cookie_dict.get("SESSDATA", "")
                bili_jct = cookie_dict.get("bili_jct", "")

                if "video_analysis" not in config:
                    config["video_analysis"] = {}
                if "bilibili" not in config["video_analysis"]:
                    config["video_analysis"]["bilibili"] = {}

                config["video_analysis"]["bilibili"]["sessdata"] = sessdata
                config["video_analysis"]["bilibili"]["bili_jct"] = bili_jct
            except:
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

setup_router = APIRouter(prefix="/api/webui/setup", tags=["setup"])

_SETUP_COOKIE_PLATFORM_DOMAINS: dict[str, list[str]] = {
    "bilibili": [".bilibili.com"],
    "douyin": [".douyin.com"],
    "kuaishou": [".kuaishou.com"],
    "qzone": ["qzone.qq.com"],
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
    image_gen_provider = normalize_text(str(body.get("image_gen_provider", ""))) or "openai"
    image_gen_api_key = normalize_text(str(body.get("image_gen_api_key", "")))
    image_gen_base_url = normalize_text(str(body.get("image_gen_base_url", "")))
    image_gen_model = normalize_text(str(body.get("image_gen_model", ""))) or "dall-e-3"
    image_gen_size = normalize_text(str(body.get("image_gen_size", ""))) or "1024x1024"

    # 构建图片生成模型配置
    image_gen_models = []
    if image_gen_api_key or image_gen_base_url:
        model_config: dict[str, Any] = {
            "name": image_gen_provider,
            "model": image_gen_model,
            "default_size": image_gen_size,
        }
        if image_gen_base_url:
            model_config["api_base"] = image_gen_base_url
        if image_gen_api_key:
            model_config["api_key"] = image_gen_api_key
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


_SETUP_COOKIE_PLATFORM_SITES: dict[str, str] = {
    "bilibili": "bilibili.com",
    "douyin": "douyin.com",
    "kuaishou": "kuaishou.com",
    "qzone": "qzone.qq.com",
}


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

    except Exception as e:
        _log.warning(f"加密失败，使用明文存储: {e}")

    # 合并到模板
    template = load_config_template()
    merged = _deep_merge_template(template, config_data)

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
