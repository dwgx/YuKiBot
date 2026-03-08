"""提示词加载器 — 从 config/prompts.yml 读取可编辑的提示词。

支持热重载：修改 prompts.yml 后发 /yukibot 即可生效。
默认模板来自 config/templates/master.template.yml。
"""
from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from core.config_templates import ensure_prompts_file, load_prompts_template

_log = logging.getLogger("yukiko.prompts")

_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_FILE = _ROOT / "config" / "prompts.yml"

# 全局缓存
_cache: dict[str, Any] = {}
_loaded = False


def _default_prompts() -> dict[str, Any]:
    """读取模板中的 prompts 默认值。"""
    return load_prompts_template()


def _ensure_prompts_file() -> None:
    """确保 prompts.yml 存在，避免运行期一直缺失。"""
    if ensure_prompts_file(_PROMPTS_FILE):
        _log.info("prompts_default_created | path=%s", _PROMPTS_FILE)


def _merge_with_defaults(raw: dict[str, Any], defaults: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """递归回填缺失 key，保留用户已有配置。"""
    merged: dict[str, Any] = dict(raw)
    changed = False
    for key, default_val in defaults.items():
        if key not in merged:
            merged[key] = copy.deepcopy(default_val)
            changed = True
            continue
        current_val = merged.get(key)
        if isinstance(default_val, dict):
            if not isinstance(current_val, dict):
                merged[key] = copy.deepcopy(default_val)
                changed = True
                continue
            child_merged, child_changed = _merge_with_defaults(current_val, default_val)
            if child_changed:
                merged[key] = child_merged
                changed = True
    return merged, changed


def reload() -> None:
    """重新加载 prompts.yml，供热重载调用。"""
    global _cache, _loaded
    _ensure_prompts_file()
    if not _PROMPTS_FILE.exists():
        defaults = _default_prompts()
        _log.warning("prompts_file_missing | path=%s | fallback=template_defaults", _PROMPTS_FILE)
        _cache = defaults if isinstance(defaults, dict) else {}
        _loaded = True
        return
    try:
        raw = yaml.safe_load(_PROMPTS_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raw = {}
        defaults = _default_prompts()
        merged, changed = _merge_with_defaults(raw, defaults)
        _cache = merged
        _loaded = True
        if changed:
            try:
                _PROMPTS_FILE.write_text(
                    yaml.safe_dump(_cache, allow_unicode=True, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )
                _log.info("prompts_backfilled | path=%s", _PROMPTS_FILE)
            except Exception as save_exc:
                _log.warning("prompts_backfill_save_error | %s", save_exc)
        digest = hashlib.sha1(
            yaml.safe_dump(_cache, allow_unicode=True, default_flow_style=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:8]
        _log.info("prompts_loaded | keys=%d | sections=%s | digest=%s", len(_cache), ",".join(sorted(_cache.keys())), digest)
    except Exception as exc:
        _log.warning("prompts_load_error | %s", exc)
        defaults = _default_prompts()
        _cache = defaults if isinstance(defaults, dict) else {}
        _log.warning("prompts_fallback_to_template_defaults | keys=%d", len(_cache))
        _loaded = True


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        reload()


def get(key: str, default: str = "") -> str:
    """获取单层 key 的值。"""
    _ensure_loaded()
    val = _cache.get(key)
    if isinstance(val, str):
        return val.strip()
    return default


def get_nested(section: str, key: str, default: str = "") -> str:
    """获取嵌套 key 的值，如 get_nested('agent', 'identity')。"""
    _ensure_loaded()
    sec = _cache.get(section)
    if isinstance(sec, dict):
        val = sec.get(key)
        if isinstance(val, str):
            return val.strip()
    return default


def get_dict(section: str) -> dict[str, str]:
    """获取整个 section 作为 dict[str, str]。"""
    _ensure_loaded()
    sec = _cache.get(section)
    if isinstance(sec, dict):
        return {str(k): str(v).strip() for k, v in sec.items()}
    return {}


def get_list(key: str) -> list[str]:
    """获取列表类型的值。"""
    _ensure_loaded()
    val = _cache.get(key)
    if isinstance(val, list):
        return [str(v).strip() for v in val if v]
    return []


def get_message(key: str, default: str = "") -> str:
    """获取 messages 段下的消息文本。"""
    return get_nested("messages", key, default)
