"""PluginRegistry — 插件注册中心。

从 core/engine.py 拆分。负责插件的发现、加载、配置和生命周期管理。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

from utils.text import normalize_text
from core.engine_types import PluginSetupContext  # noqa: F401


class PluginRegistry:
    """插件注册中心。
    配置加载优先级（从高到低）:
        1. config/plugins.yml → <plugin_name> 段
        2. plugins/config/<plugin_name>.yml（独立文件）
        3. config.yml → plugins.<plugin_name> 段
    """

    def __init__(self, plugins_dir: Path, logger, config_dir: Path | None = None):
        self.plugins_dir = plugins_dir
        self.logger = logger
        self.plugins: dict[str, Any] = {}
        self.schemas: list[dict[str, Any]] = []
        self._plugin_configs: dict[str, dict[str, Any]] = {}
        self._plugin_meta: dict[str, dict[str, Any]] = {}
        self._config_dir = config_dir or plugins_dir.parent / "config"

    def load(self, global_config: dict[str, Any] | None = None) -> None:
        self.plugins.clear()
        self.schemas.clear()
        self._plugin_configs.clear()
        self._plugin_meta.clear()
        plugins_config = (global_config or {}).get("plugins", {})
        if not isinstance(plugins_config, dict):
            plugins_config = {}
        # 统一插件配置: config/plugins.yml（优先级最高）
        self._unified_plugin_config = self._load_unified_plugins_yml()
        # 兼容旧路径: plugins/config/<name>.yml
        self._plugin_config_dir = self.plugins_dir / "config"
        if not self.plugins_dir.exists():
            return
        for file in sorted(self.plugins_dir.glob("*.py")):
            if file.name.startswith("_") or file.stem == "__init__":
                continue

            try:
                module_name = f"yukiko_plugin_{file.stem}"
                spec = importlib.util.spec_from_file_location(module_name, file)
                if not spec or not spec.loader:
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                plugin_cls = getattr(module, "Plugin", None)
                if plugin_cls is None:
                    continue

                # 首次配置向导: 插件定义了 needs_setup() 且返回 True
                needs_setup = False
                needs_setup_fn = getattr(plugin_cls, "needs_setup", None)
                if callable(needs_setup_fn):
                    needs_setup = bool(needs_setup_fn())
                interactive_fn = getattr(plugin_cls, "interactive_setup", None)
                supports_interactive_setup = callable(interactive_fn)
                if needs_setup and supports_interactive_setup:
                    stdin = getattr(sys, "stdin", None)
                    stdin_is_tty = bool(
                        stdin and hasattr(stdin, "isatty") and stdin.isatty()
                    )
                    if not stdin_is_tty:
                        self.logger.warning(
                            "插件 %s 需要首次配置，但当前为非交互环境，已跳过向导",
                            file.stem,
                        )
                    else:
                        self.logger.info("插件 %s 需要首次配置，启动向导...", file.stem)
                        try:
                            interactive_fn()
                        except Exception as exc:

                            self.logger.warning("插件 %s 配置向导失败: %s", file.stem, exc)
                plugin = plugin_cls()
                name = (
                    normalize_text(str(getattr(plugin, "name", file.stem))) or file.stem
                )
                description = normalize_text(str(getattr(plugin, "description", "")))
                args_schema = getattr(plugin, "args_schema", {})
                rules_raw = getattr(plugin, "rules", [])
                if not isinstance(args_schema, dict):
                    args_schema = {}
                rules: list[str] = []
                if isinstance(rules_raw, str):
                    item = normalize_text(rules_raw)
                    if item:
                        rules.append(item)
                elif isinstance(rules_raw, list):
                    rules = [
                        normalize_text(str(item))
                        for item in rules_raw
                        if normalize_text(str(item))
                    ]
                elif isinstance(rules_raw, dict):
                    for key, value in rules_raw.items():
                        left = normalize_text(str(key))
                        right = normalize_text(str(value))
                        if left and right:
                            rules.append(f"{left}: {right}")
                        elif left:
                            rules.append(left)
                self.plugins[name] = plugin
                plugin_cfg = self._load_plugin_config(name, plugins_config)
                self._plugin_configs[name] = plugin_cfg
                plugin_meta = self._build_plugin_meta(
                    name=name,
                    plugin=plugin,
                    config=plugin_cfg,
                    needs_setup=needs_setup,
                    supports_interactive_setup=supports_interactive_setup,
                )
                self._plugin_meta[name] = plugin_meta
                self.schemas.append(
                    {
                        "name": name,
                        "description": description or f"插件 {name}",
                        "args_schema": args_schema,
                        "rules": rules,
                    }
                )
                self.logger.info("已加载插件：%s", name)
                self._emit_plugin_config_guidance(name, plugin_meta)
            except Exception as exc:

                self.logger.exception("加载插件失败 %s：%s", file.name, exc)

    def _load_unified_plugins_yml(self) -> dict[str, Any]:
        """加载 config/plugins.yml 统一插件配置。"""

        import yaml

        candidates = [
            self._config_dir / "plugins.yml",
            self._config_dir / "Plugins.yml",
            self._config_dir / "plugin.yml",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        self.logger.info(
                            "统一插件配置来自 %s | keys=%s", path, list(data.keys())
                        )
                        return data

                except Exception as exc:

                    self.logger.warning("读取统一插件配置失败 %s: %s", path, exc)
        return {}

    def _load_plugin_config(
        self, name: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        """加载插件配置，优先级: config/plugins.yml > plugins/config/<name>.yml > config.yml plugins 段。"""

        import yaml

        # 优先级 1: config/plugins.yml → <name> 段
        unified = self._unified_plugin_config.get(name)
        if isinstance(unified, dict) and unified:
            self.logger.info("插件 %s 配置来自 config/plugins.yml", name)
            return unified

        # 优先级 2: plugins/config/<name>.yml（独立文件，兼容旧写法）
        yml_file = self._plugin_config_dir / f"{name}.yml"
        if yml_file.is_file():
            try:
                with open(yml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    self.logger.info("插件 %s 配置来自 %s", name, yml_file)
                    return data

            except Exception as exc:

                self.logger.warning(
                    "读取插件配置失败 %s: %s，回退到主配置", yml_file, exc
                )
        # 优先级 3: config.yml → plugins.<name>
        return fallback.get(name, {}) or {}

    def _has_local_plugin_config(self, name: str) -> bool:
        plugin_cfg_dir = getattr(
            self, "_plugin_config_dir", self.plugins_dir / "config"
        )
        return (plugin_cfg_dir / f"{name}.yml").is_file()

    def _normalize_plugin_guide(self, raw: Any) -> list[str]:
        if isinstance(raw, str):
            item = normalize_text(raw)
            return [item] if item else []

        if not isinstance(raw, list):
            return []

        return [normalize_text(str(item)) for item in raw if normalize_text(str(item))]

    def _extract_plugin_editable_keys(
        self, plugin: Any, config: dict[str, Any]
    ) -> list[str]:
        keys: list[str] = []
        config_schema = getattr(plugin, "config_schema", None)
        if isinstance(config_schema, dict):
            properties = config_schema.get("properties", {})
            if isinstance(properties, dict):
                keys.extend(
                    normalize_text(str(item))
                    for item in properties.keys()
                    if normalize_text(str(item))
                )
        if not keys and isinstance(config, dict):
            keys.extend(
                normalize_text(str(item))
                for item in config.keys()
                if normalize_text(str(item))
            )
        seen: set[str] = set()
        unique: list[str] = []
        for item in keys:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _resolve_plugin_config_target(
        self,
        name: str,
        *,
        supports_interactive_setup: bool,
    ) -> str:
        if supports_interactive_setup or self._has_local_plugin_config(name):
            return f"plugins/config/{name}.yml"

        return f"config/plugins.yml -> {name}"

    def _build_plugin_meta(
        self,
        *,
        name: str,
        plugin: Any,
        config: dict[str, Any],
        needs_setup: bool,
        supports_interactive_setup: bool,
    ) -> dict[str, Any]:
        editable_keys = self._extract_plugin_editable_keys(plugin, config)
        config_guide = self._normalize_plugin_guide(
            getattr(plugin, "config_guide", None)
        )
        config_target = self._resolve_plugin_config_target(
            name,
            supports_interactive_setup=supports_interactive_setup,
        )
        configurable = bool(
            editable_keys or config_guide or supports_interactive_setup or config
        )
        if not config_guide and configurable:
            config_guide = [f"配置入口: {config_target}"]
            if editable_keys:
                preview = "、".join(editable_keys[:4])
                config_guide.append(f"常用字段: {preview}")
            if supports_interactive_setup:
                config_guide.append("首次启动支持交互向导，之后也可以直接手改 YAML。")
        setup_mode = (
            "wizard"
            if supports_interactive_setup
            else "manual" if configurable else "none"
        )
        return {
            "configurable": configurable,
            "config_target": config_target,
            "editable_keys": editable_keys,
            "config_guide": config_guide,
            "supports_interactive_setup": supports_interactive_setup,
            "needs_setup": needs_setup,
            "setup_mode": setup_mode,
            "using_defaults": not bool(config),
        }

    def _emit_plugin_config_guidance(self, name: str, meta: dict[str, Any]) -> None:
        if not isinstance(meta, dict) or not meta.get("configurable"):
            return
        editable_keys = meta.get("editable_keys", [])
        if not isinstance(editable_keys, list):
            editable_keys = []
        key_preview = (
            "、".join(str(item) for item in editable_keys[:4]) if editable_keys else "-"
        )
        self.logger.info(
            "插件配置指导 | plugin=%s | target=%s | mode=%s | defaults=%s | fields=%s",
            name,
            meta.get("config_target", ""),
            meta.get("setup_mode", "manual"),
            "yes" if meta.get("using_defaults") else "no",
            key_preview,
        )

    async def call(self, name: str, message: str, context: dict[str, Any]) -> str:
        plugin = self.plugins.get(name)
        if plugin is None:
            raise RuntimeError(f"plugin_not_found:{name}")

        handler = getattr(plugin, "handle", None)
        if handler is None:
            raise RuntimeError(f"plugin_no_handler:{name}")

        result = handler(message, context)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")

    async def setup_all(self, context: PluginSetupContext) -> None:
        """Call setup() on plugins that define it."""

        for name, plugin in self.plugins.items():
            setup_fn = getattr(plugin, "setup", None)
            if setup_fn is None:
                continue

            try:
                plugin_cfg = self._plugin_configs.get(name, {})
                result = setup_fn(config=plugin_cfg, context=context)
                if inspect.isawaitable(result):
                    result = await result
                self.logger.info("插件 setup 完成：%s", name)
            except Exception as exc:

                self.logger.exception("插件 setup 失败 %s：%s", name, exc)

    async def teardown_all(self) -> None:
        """Call teardown() on plugins that define it."""

        for name, plugin in self.plugins.items():
            teardown_fn = getattr(plugin, "teardown", None)
            if teardown_fn is None:
                continue

            try:
                result = teardown_fn()
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:

                self.logger.exception("插件 teardown 失败 %s：%s", name, exc)

    def filter_internal(self) -> None:
        """Remove internal_only plugins from router-visible schemas."""

        self.schemas = [
            s
            for s in self.schemas
            if not getattr(self.plugins.get(s.get("name", "")), "internal_only", False)
        ]

