"""配置中心 — 加载 / 环境变量替换 / 解密 / 热重载。

用法:
    cm = ConfigManager(config_dir, storage_dir)
    cm.get("bot.name")          # 点路径访问
    ok, msg = cm.reload()       # 热重载
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from core.crypto import SecretManager

_log = logging.getLogger("yukiko.config")
_ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


class ConfigManager:
    """单例式配置管理器，支持热重载。"""

    def __init__(self, config_dir: Path, storage_dir: Path):
        self._config_dir = config_dir
        self._storage_dir = storage_dir
        self._config_file = config_dir / "config.yml"
        self._secret = SecretManager(storage_dir / ".secret_key")
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.load()

    # ── public ────────────────────────────────────────────────
    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def get(self, dotpath: str, default: Any = None) -> Any:
        """点路径访问: get('bot.name') → config['bot']['name']"""
        keys = dotpath.split(".")
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
        return node if node is not None else default

    def load(self) -> None:
        """加载 config.yml 并处理环境变量 + 解密。"""
        with self._lock:
            raw = self._load_yaml(self._config_file)
            resolved = self._resolve_env_vars(raw)
            self._data = self._secret.decrypt_dict(resolved)  # type: ignore[assignment]
            _log.info("配置已加载: %s", self._config_file)

    def reload(self) -> tuple[bool, str]:
        """热重载配置。返回 (成功, 消息)。"""
        try:
            self.load()
            return True, "配置已重载"
        except Exception as exc:
            msg = f"配置重载失败: {exc}"
            _log.error(msg)
            return False, msg

    @property
    def secret(self) -> SecretManager:
        return self._secret

    # ── private ───────────────────────────────────────────────
    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            _log.warning("配置文件不存在: %s", path)
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _resolve_env_vars(cls, data: Any) -> Any:
        """递归替换 ${VAR_NAME} 为环境变量值。"""
        if isinstance(data, dict):
            return {k: cls._resolve_env_vars(v) for k, v in data.items()}
        if isinstance(data, list):
            return [cls._resolve_env_vars(v) for v in data]
        if isinstance(data, str):
            m = _ENV_PATTERN.fullmatch(data.strip())
            if m:
                return os.environ.get(m.group(1), "")
        return data
