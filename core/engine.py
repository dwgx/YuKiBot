from __future__ import annotations





import asyncio


import contextlib


import importlib.util


import inspect


import json


import logging


import re


import sys


from collections import OrderedDict, defaultdict, deque


from dataclasses import dataclass, field


from datetime import datetime, timezone


from pathlib import Path


from types import SimpleNamespace


from typing import Any, Awaitable, Callable


from urllib.parse import urlparse





from core import prompt_loader as _pl


from core.admin import AdminEngine


from core.agent import AgentContext, AgentLoop, AgentResult


from core.agent_tools import AgentToolRegistry, register_builtin_tools, register_sticker_tools


from core.affinity import AffinityEngine


from core.config_manager import ConfigManager


from core.crawlers import CrawlerHub


from core.emotion import EmotionEngine


from core.image import ImageEngine


from core.knowledge import KnowledgeBase


from core.knowledge_updater import KnowledgeUpdater


from core.markdown import MarkdownRenderer


from core.memory import MemoryEngine


from core.paths import PathResolver


from core.personality import PersonalityEngine


from core.router import RouterDecision, RouterEngine, RouterInput


from core.safety import SafetyEngine


from core.search import SearchEngine


from core.sticker import StickerManager


from core.thinking import ThinkingEngine


from core.tools import ToolExecutor


from core.trigger import TriggerEngine, TriggerInput


from services.logger import get_logger


from services.model_client import ModelClient


from utils.text import (


    clip_text,


    normalize_kaomoji_style,


    normalize_text,


    remove_markdown,


    replace_emoji_with_kaomoji,


    tokenize,


)








@dataclass(slots=True)


class EngineMessage:


    conversation_id: str


    user_id: str


    text: str


    user_name: str = ""


    message_id: str = ""


    seq: int = 0


    raw_segments: list[dict[str, Any]] = field(default_factory=list)


    queue_depth: int = 0


    mentioned: bool = False


    is_private: bool = False


    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


    group_id: int = 0


    bot_id: str = ""


    at_other_user_only: bool = False


    at_other_user_ids: list[str] = field(default_factory=list)


    reply_to_message_id: str = ""


    reply_to_user_id: str = ""


    reply_to_user_name: str = ""


    reply_to_text: str = ""


    reply_media_segments: list[dict[str, Any]] = field(default_factory=list)


    api_call: Callable[..., Awaitable[Any]] | None = None


    trace_id: str = ""


    sender_role: str = ""  # "owner" / "admin" / "member" — 来自 OneBot sender.role








@dataclass(slots=True)


class EngineResponse:


    action: str


    reason: str


    reply_text: str = ""


    image_url: str = ""


    image_urls: list[str] = field(default_factory=list)


    video_url: str = ""


    cover_url: str = ""


    record_b64: str = ""


    audio_file: str = ""


    pre_ack: str = ""


    meta: dict[str, Any] = field(default_factory=dict)








@dataclass


class PluginSetupContext:


    """Dependencies injected into plugins during setup()."""


    model_client: Any = None


    config: dict[str, Any] = field(default_factory=dict)


    logger: Any = None


    storage_dir: Path | None = None


    agent_tool_registry: Any = None








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


                    self.logger.info("插件 %s 需要首次配置，启动向导...", file.stem)


                    try:


                        interactive_fn()


                    except Exception as exc:


                        self.logger.warning("插件 %s 配置向导失败: %s", file.stem, exc)





                plugin = plugin_cls()





                name = normalize_text(str(getattr(plugin, "name", file.stem))) or file.stem


                description = normalize_text(str(getattr(plugin, "description", "")))


                intent_examples = getattr(plugin, "intent_examples", [])


                args_schema = getattr(plugin, "args_schema", {})


                rules_raw = getattr(plugin, "rules", [])


                if not isinstance(intent_examples, list):


                    intent_examples = []


                if not isinstance(args_schema, dict):


                    args_schema = {}


                rules: list[str] = []


                if isinstance(rules_raw, str):


                    item = normalize_text(rules_raw)


                    if item:


                        rules.append(item)


                elif isinstance(rules_raw, list):


                    rules = [normalize_text(str(item)) for item in rules_raw if normalize_text(str(item))]


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


                        "intent_examples": [normalize_text(str(item)) for item in intent_examples if str(item).strip()],


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


                        self.logger.info("统一插件配置来自 %s | keys=%s", path, list(data.keys()))


                        return data


                except Exception as exc:


                    self.logger.warning("读取统一插件配置失败 %s: %s", path, exc)


        return {}





    def _load_plugin_config(self, name: str, fallback: dict[str, Any]) -> dict[str, Any]:


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


                self.logger.warning("读取插件配置失败 %s: %s，回退到主配置", yml_file, exc)





        # 优先级 3: config.yml → plugins.<name>


        return fallback.get(name, {}) or {}





    def _has_local_plugin_config(self, name: str) -> bool:


        plugin_cfg_dir = getattr(self, "_plugin_config_dir", self.plugins_dir / "config")


        return (plugin_cfg_dir / f"{name}.yml").is_file()





    def _normalize_plugin_guide(self, raw: Any) -> list[str]:


        if isinstance(raw, str):


            item = normalize_text(raw)


            return [item] if item else []


        if not isinstance(raw, list):


            return []


        return [normalize_text(str(item)) for item in raw if normalize_text(str(item))]





    def _extract_plugin_editable_keys(self, plugin: Any, config: dict[str, Any]) -> list[str]:


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


        config_guide = self._normalize_plugin_guide(getattr(plugin, "config_guide", None))


        config_target = self._resolve_plugin_config_target(


            name,


            supports_interactive_setup=supports_interactive_setup,


        )


        configurable = bool(editable_keys or config_guide or supports_interactive_setup or config)


        if not config_guide and configurable:


            config_guide = [f"配置入口: {config_target}"]


            if editable_keys:


                preview = "、".join(editable_keys[:4])


                config_guide.append(f"常用字段: {preview}")


            if supports_interactive_setup:


                config_guide.append("首次启动支持交互向导，之后也可以直接手改 YAML。")


        setup_mode = "wizard" if supports_interactive_setup else "manual" if configurable else "none"


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


        key_preview = "、".join(str(item) for item in editable_keys[:4]) if editable_keys else "-"


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


            s for s in self.schemas


            if not getattr(self.plugins.get(s.get("name", "")), "internal_only", False)


        ]








class YukikoEngine:


    def __init__(self, project_root: Path):


        self.project_root = project_root


        self.path_resolver = PathResolver(project_root=project_root)


        self.config_dir = project_root / "config"


        self.storage_dir = self.path_resolver.data()


        self.plugins_dir = project_root / "plugins"





        # ── 配置中心（替代原有 _load_yaml + _resolve_env_vars）──


        self.config_manager = ConfigManager(self.config_dir, self.storage_dir)


        self.config = self.config_manager.raw





        bot_config = self.config.get("bot", {})


        debug = bool(bot_config.get("debug", False))


        self.logger = get_logger("yukiko", self.storage_dir / "logs", debug=debug)





        # ── 管理员系统 ──


        self.admin = AdminEngine(self.config, self.storage_dir)





        self._init_from_config()





        self.model_client = ModelClient(self.config.get("api", {}))


        self.personality = PersonalityEngine.from_file(self.config_dir / "personality.yml", config=self.config)


        self.memory = MemoryEngine(


            self.config.get("memory", {}),


            self.storage_dir / "memory",


            global_config=self.config,


        )


        self.safety = SafetyEngine(self.config.get("safety", {}))


        self.trigger = TriggerEngine(


            trigger_config=self.config.get("trigger", {}),


            bot_config=self.config.get("bot", {}),


        )


        self.emotion = EmotionEngine(self.config.get("emotion", {}))


        self.search = SearchEngine(self.config.get("search", {}))


        self.image = ImageEngine(self.config.get("image", {}), self.model_client)


        self.markdown = MarkdownRenderer(


            config=self.config.get("markdown", {}),


            enabled=bool(self.config.get("bot", {}).get("allow_markdown", True)),


        )


        self.thinking = ThinkingEngine(


            config=self.config,


            personality=self.personality,


            model_client=self.model_client,


        )


        self.router = RouterEngine(


            config=self.config,


            personality=self.personality,


            model_client=self.model_client,


        )





        self.plugins = PluginRegistry(self.plugins_dir, self.logger, config_dir=self.config_dir)


        self.plugins.load(global_config=self.config)





        self.tools = ToolExecutor(


            search_engine=self.search,


            image_engine=self.image,


            plugin_runner=self._run_plugin,


            config=self.config,


        )





        # ── Agent 系统 ──


        self.agent_tool_registry = AgentToolRegistry()


        self.agent_tool_registry.set_intent_keyword_routing_enabled(False)


        register_builtin_tools(


            registry=self.agent_tool_registry,


            search_engine=self.search,


            image_engine=self.image,


            model_client=self.model_client,


            config=self.config,


        )





        # ── 增强功能: 好感度 + 卡片 + 图片生成 ──


        self.affinity = AffinityEngine(


            storage_dir=str(self.storage_dir / "affinity"),


        )


        try:


            from core.image_gen import ImageGenEngine


            from core.enhanced_tools import register_enhanced_tools


            self.image_gen = ImageGenEngine(config=self.config, model_client=self.model_client)


            register_enhanced_tools(


                registry=self.agent_tool_registry,


                affinity=self.affinity,


                image_gen=self.image_gen,


                config=self.config,


            )


            self.logger.info("enhanced_tools_registered | affinity + card_builder + image_gen + napcat_ext")


        except Exception:


            self.logger.warning("enhanced_tools_register_failed", exc_info=True)





        self.agent = AgentLoop(


            model_client=self.model_client,


            tool_registry=self.agent_tool_registry,


            config=self.config,


        )





        # ── 爬虫 + 知识库 ──


        self.crawler_hub = CrawlerHub(self.config)


        self.knowledge_base = KnowledgeBase(


            db_path=str(self.storage_dir / "knowledge" / "knowledge.db"),


        )


        self.knowledge_updater = KnowledgeUpdater(


            knowledge_base=self.knowledge_base,


            config=self.config,


            logger=self.logger,


            model_client=self.model_client,


        )





        # ── 表情系统 ──


        sticker_cfg = self.config.get("sticker", {})


        self.sticker = StickerManager(


            storage_dir=self.storage_dir / "sticker",


            config=sticker_cfg,


        )


        if sticker_cfg.get("enabled", True):


            qq_path = sticker_cfg.get("qq_data_path")


            scan_result = self.sticker.scan(


                qq_data_path=qq_path if qq_path and qq_path != "auto" else None,


            )


            register_sticker_tools(self.agent_tool_registry, model_client=self.model_client)


            self.logger.info(


                "sticker_init | faces=%d emojis=%d registered=%d unregistered=%d",


                scan_result["faces"], scan_result["emojis"],


                self.sticker.registered_count, len(self.sticker.get_unregistered()),


            )





        self._last_reply_state: dict[str, dict[str, Any]] = {}


        self._pending_fragments: dict[str, dict[str, Any]] = {}


        self._recent_directed_hints: dict[str, datetime] = {}


        self._recent_search_cache: dict[str, dict[str, Any]] = {}


        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()


        self._seen_message_ids_max = 200


        self._runtime_group_chat_cache: dict[str, deque[str]] = defaultdict(


            lambda: deque(maxlen=self.runtime_group_cache_max_messages)


        )


        self._agent_conversation_locks: dict[str, asyncio.Lock] = {}


        # 媒体 artifact 索引: message_id -> [{"type": "...", "url": str, "file_id": str, "data_uri": str}]


        self._media_artifact_index: OrderedDict[str, list[dict[str, str]]] = OrderedDict()


        self._media_artifact_index_max = 500
        memory_cfg = self.config.get("memory", {}) if isinstance(self.config, dict) else {}
        if not isinstance(memory_cfg, dict):
            memory_cfg = {}
        self._memory_media_capture_enable = bool(memory_cfg.get("media_memory_enable", True))
        try:
            max_images_per_message = int(memory_cfg.get("media_memory_max_images_per_message", 4))
        except (TypeError, ValueError):
            max_images_per_message = 4
        self._memory_media_max_images_per_message = max(1, min(8, max_images_per_message))
        try:
            capture_timeout = float(memory_cfg.get("media_memory_capture_timeout_seconds", 6.0))
        except (TypeError, ValueError):
            capture_timeout = 6.0
        self._memory_media_capture_timeout_seconds = max(1.0, min(15.0, capture_timeout))


        self._async_init_done = False


        self._async_init_lock = asyncio.Lock()


        self._reload_lock = asyncio.Lock()





    async def async_init(self) -> None:


        """Async initialization: plugin setup etc. Call once after __init__."""


        if self._async_init_done:


            return


        async with self._async_init_lock:


            if self._async_init_done:


                return


            self._async_init_done = True


            setup_ctx = PluginSetupContext(


                model_client=self.model_client,


                config=self.config,


                logger=self.logger,


                storage_dir=self.storage_dir,


                agent_tool_registry=self.agent_tool_registry,


            )


            await self.plugins.setup_all(setup_ctx)


            self.plugins.filter_internal()





            # 表情包自动注册 (后台任务，不阻塞启动)


            sticker_cfg = self.config.get("sticker", {})


            if (hasattr(self, "sticker")


                    and sticker_cfg.get("enabled", True)


                    and sticker_cfg.get("auto_register", True)


                    and self.sticker.get_unregistered()):


                self._sticker_auto_task = asyncio.create_task(


                    self._auto_register_stickers()


                )





            # 热搜预热 + 知识库清理 (后台任务)


            if hasattr(self, "crawler_hub") and hasattr(self, "knowledge_base"):


                # 检查是否启用热搜抓取
                knowledge_cfg = self.config.get("knowledge_update", {})
                trend_fetch_enable = bool(knowledge_cfg.get("trend_fetch_enable", False))

                if trend_fetch_enable:
                    self._trend_task = asyncio.create_task(


                        self._background_trend_fetch()


                    )
                else:
                    self.logger.info("trend_fetch_disabled | 热搜抓取已禁用")





    async def _auto_register_stickers(self) -> None:


        """后台自动注册未识别的表情包 (不阻塞主流程)。"""


        await asyncio.sleep(5)  # 等启动稳定


        try:


            async def _llm_call(messages: list) -> str:


                return await self.model_client.chat_text(messages=messages, max_tokens=200)





            batch = self.config.get("sticker", {}).get("auto_register_batch", 10)


            result = await self.sticker.auto_register(llm_call=_llm_call, batch_size=batch)


            self.logger.info("sticker_auto_register | %s", result)


        except Exception as e:


            self.logger.warning("sticker_auto_register_fail | %s", e, exc_info=True)





    async def _background_trend_fetch(self) -> None:


        """后台定时拉取热搜并写入知识库。启动后立即拉一次，之后每30分钟刷新。"""


        await asyncio.sleep(8)  # 等启动稳定


        interval = 1800  # 30分钟


        while True:


            try:


                trends = await self.crawler_hub.get_trends_cached(max_age=60)


                total = 0


                for platform, items in trends.items():


                    for item in items:


                        self.knowledge_base.add(


                            category="trend", title=item.title,


                            content=item.snippet or "",


                            source=platform,


                            tags=[platform],


                            extra={"heat": item.heat, "url": item.url},


                        )


                        total += 1


                self.knowledge_base.cleanup_expired()


                stats = self.knowledge_base.stats()


                self.logger.info(


                    "trend_fetch_done | items=%d | kb_stats=%s",


                    total, stats,


                )


            except Exception as e:


                self.logger.warning("trend_fetch_error | %s", e)


            await asyncio.sleep(interval)





    def _init_from_config(self) -> None:


        """从 config 读取阈值/参数，热重载时也会调用。"""


        bot_config = self.config.get("bot", {})


        self.mention_only_reply_template = normalize_text(


            str(bot_config.get("mention_only_reply_template", ""))


        )


        self.mention_only_reply_template_with_name = normalize_text(


            str(bot_config.get("mention_only_reply_template_with_name", ""))


        )


        self.mention_only_reply_mode = normalize_text(


            str(bot_config.get("mention_only_reply_mode", "ai"))


        ).lower() or "ai"


        if self.mention_only_reply_mode not in {"template", "ai", "hybrid"}:


            self.mention_only_reply_mode = "ai"


        self.mention_only_ai_prompt = normalize_text(


            str(bot_config.get("mention_only_ai_prompt", ""))


        )


        self.mention_only_ai_system_prompt = normalize_text(


            str(bot_config.get("mention_only_ai_system_prompt", ""))


        )


        self.short_ping_require_directed = bool(bot_config.get("short_ping_require_directed", True))


        short_ping_raw = bot_config.get("short_ping_phrases", [])


        if not isinstance(short_ping_raw, list):


            short_ping_raw = []


        short_ping_phrases: list[str] = []


        for item in short_ping_raw:


            norm = self._normalize_short_ping_phrase(str(item))


            if norm:


                short_ping_phrases.append(norm)


        self.short_ping_phrases = tuple(dict.fromkeys(short_ping_phrases))


        sanitize_phrases_raw = bot_config.get("sanitize_banned_phrases", [])


        if not isinstance(sanitize_phrases_raw, list):


            sanitize_phrases_raw = []


        sanitize_phrases = [normalize_text(str(item)) for item in sanitize_phrases_raw if normalize_text(str(item))]


        self.sanitize_banned_phrases = tuple(dict.fromkeys(sanitize_phrases))


        self.reply_privacy_guard_enable = bool(bot_config.get("reply_privacy_guard_enable", True))


        self.reply_redact_qq_numbers = bool(bot_config.get("reply_redact_qq_numbers", True))


        self.reply_block_profile_claims = bool(bot_config.get("reply_block_profile_claims", True))


        self.max_reply_chars = max(60, int(bot_config.get("max_reply_chars", 220)))


        self.max_reply_chars_proactive = max(40, int(bot_config.get("max_reply_chars_proactive", 120)))


        self.min_reply_chars = max(8, int(bot_config.get("min_reply_chars", 16)))


        kaomoji_raw = bot_config.get("kaomoji_allowlist", ["QWQ", "AWA"])


        if not isinstance(kaomoji_raw, list):


            kaomoji_raw = ["QWQ", "AWA"]


        kaomoji_allowlist = [normalize_text(str(item)) for item in kaomoji_raw if normalize_text(str(item))]


        if not kaomoji_allowlist:


            kaomoji_allowlist = ["QWQ", "AWA"]


        self.kaomoji_allowlist = kaomoji_allowlist


        self.default_kaomoji = self.kaomoji_allowlist[0]





        routing_cfg = self.config.get("routing", {})


        self.router_timeout_seconds = max(1, int(routing_cfg.get("router_timeout_seconds", 18)))


        self.router_min_confidence = max(0.0, min(1.0, float(routing_cfg.get("min_confidence", 0.55))))


        self.followup_min_confidence = max(0.0, min(1.0, float(routing_cfg.get("followup_min_confidence", 0.75))))


        self.non_directed_min_confidence = max(0.0, min(1.0, float(routing_cfg.get("non_directed_min_confidence", 0.72))))


        self.ai_gate_min_confidence = max(0.0, min(1.0, float(routing_cfg.get("ai_gate_min_confidence", 0.66))))


        self.routing_zero_disables_undirected = bool(routing_cfg.get("zero_threshold_disables_undirected", True))


        self.failover_mode = str(routing_cfg.get("failover_mode", "mention_or_private_only"))


        self.fragment_join_enable = bool(routing_cfg.get("fragment_join_enable", True))


        self.fragment_join_window_seconds = max(3, int(routing_cfg.get("fragment_join_window_seconds", 12)))


        self.fragment_timeout_fallback_seconds = max(


            self.fragment_join_window_seconds + 1,


            int(routing_cfg.get("fragment_timeout_fallback_seconds", 30)),


        )


        self.fragment_hold_max_chars = max(4, int(routing_cfg.get("fragment_hold_max_chars", 24)))


        self.directed_grace_seconds = max(6, int(routing_cfg.get("directed_grace_seconds", 18)))


        self.followup_consume_on_send = bool(routing_cfg.get("followup_consume_on_send", True))


        self.runtime_group_cache_max_messages = max(


            20,


            int(routing_cfg.get("runtime_group_cache_max_messages", 180)),


        )


        self.runtime_group_cache_context_limit = max(


            4,


            int(routing_cfg.get("runtime_group_cache_context_limit", 12)),


        )


        queue_cfg = self.config.get("queue", {})


        if not isinstance(queue_cfg, dict):


            queue_cfg = {}


        # 与 queue 的 single_inflight 配置保持一致，避免“队列允许并行但 Agent 仍串行”等待。


        self.agent_single_inflight_per_conversation = bool(


            queue_cfg.get("single_inflight_per_conversation", True)


        )


        self.smart_interrupt_enable = bool(queue_cfg.get("smart_interrupt_enable", True))


        self.smart_interrupt_cross_user_enable = bool(


            queue_cfg.get("smart_interrupt_cross_user_enable", True)


        )


        self.smart_interrupt_same_user_enable = bool(


            queue_cfg.get("smart_interrupt_same_user_enable", False)


        )


        self.smart_interrupt_require_directed = bool(


            queue_cfg.get("smart_interrupt_require_directed", True)


        )


        self.smart_interrupt_min_pending = max(


            1,


            int(queue_cfg.get("smart_interrupt_min_pending", 1)),


        )


        self_check_cfg = self.config.get("self_check", {})


        if not isinstance(self_check_cfg, dict):


            self_check_cfg = {}


        self.self_check_enable = bool(self_check_cfg.get("enable", True))


        self.self_check_block_at_other = bool(self_check_cfg.get("block_at_other", True))


        self.self_check_listen_probe_min_confidence = max(


            0.5,


            min(1.0, float(self_check_cfg.get("listen_probe_min_confidence", 0.78))),


        )


        self.self_check_non_direct_reply_min_confidence = max(0.0, min(1.0, float(self_check_cfg.get("non_direct_reply_min_confidence", 0.82))))


        self.self_check_cross_user_guard_seconds = max(


            8,


            int(self_check_cfg.get("cross_user_guard_seconds", 45)),


        )





        # ── 总控面板映射（少量高层参数） ──


        control_cfg = self.config.get("control", {})


        if not isinstance(control_cfg, dict):


            control_cfg = {}


        if hasattr(self, "agent_tool_registry"):


            self.agent_tool_registry.set_intent_keyword_routing_enabled(False)


        self.control_chat_mode = normalize_text(str(control_cfg.get("chat_mode", "balanced"))).lower() or "balanced"


        self.control_undirected_policy = normalize_text(


            str(control_cfg.get("undirected_policy", "mention_only"))


        ).lower() or "mention_only"


        self.control_knowledge_learning = normalize_text(


            str(control_cfg.get("knowledge_learning", "aggressive"))


        ).lower() or "aggressive"


        self.control_memory_recall_level = normalize_text(


            str(control_cfg.get("memory_recall_level", "light"))


        ).lower() or "light"


        self.control_emoji_level = normalize_text(str(control_cfg.get("emoji_level", "medium"))).lower() or "medium"


        self.control_split_mode = normalize_text(str(control_cfg.get("split_mode", "semantic"))).lower() or "semantic"


        self.control_send_rate_profile = normalize_text(


            str(control_cfg.get("send_rate_profile", "safe_qq_group"))


        ).lower() or "safe_qq_group"





        # chat_mode 只调整非指向场景阈值，明确@/私聊不受影响。


        mode_bias = 0.0


        if self.control_chat_mode == "quiet":


            mode_bias = 0.12


        elif self.control_chat_mode == "active":


            mode_bias = -0.08


        self.non_directed_min_confidence = max(0.0, min(1.0, self.non_directed_min_confidence + mode_bias))


        self.ai_gate_min_confidence = max(0.0, min(1.0, self.ai_gate_min_confidence + mode_bias))


        self.self_check_non_direct_reply_min_confidence = max(


            0.0,


            min(1.0, self.self_check_non_direct_reply_min_confidence + mode_bias),


        )





        # 用户选择的策略: 非@仅高置信、且阈值 0 时必须不自动接话。


        self.non_directed_high_confidence_only = self.control_undirected_policy == "high_confidence_only"





        default_overload_notice = "你们等等呀，我回复不过来了。请 @我 或叫我的名字（雪 / yukiko），我会优先回你。"


        self.overload_notice_text = (


            normalize_text(str(self.config.get("queue", {}).get("overload_notice_text", default_overload_notice)))


            or default_overload_notice


        )





        # 搜索候选追问：用于“第2个 / 就这个 / 再发一次”类轻量 follow-up。


        self.search_followup_cache_enable = False


        self.search_followup_cache_ttl_seconds = 30 * 60


        self.search_followup_number_choice_enable = False


        self.search_followup_rotate_choice_enable = False


        self.search_followup_resend_enable = False


        self.search_followup_resend_media_cues = ()


        self.search_followup_max_choices = 10


        followup_cfg = self.config.get("search_followup", {}) or {}


        if isinstance(followup_cfg, dict):


            ttl_minutes = max(1, min(24 * 60, int(followup_cfg.get("ttl_minutes", 30) or 30)))


            self.search_followup_cache_enable = bool(followup_cfg.get("enable", False))


            self.search_followup_cache_ttl_seconds = ttl_minutes * 60


            self.search_followup_number_choice_enable = bool(followup_cfg.get("number_choice_enable", False))


            self.search_followup_rotate_choice_enable = bool(followup_cfg.get("rotate_choice_enable", False))


            self.search_followup_resend_enable = bool(followup_cfg.get("resend_enable", False))


            resend_cues_raw = followup_cfg.get("resend_media_cues", [])


            if isinstance(resend_cues_raw, list):


                self.search_followup_resend_media_cues = tuple(


                    dict.fromkeys(


                        normalize_text(str(item)).lower()


                        for item in resend_cues_raw


                        if normalize_text(str(item))


                    )


                )


            self.search_followup_max_choices = max(1, min(20, int(followup_cfg.get("max_choices", 10) or 10)))


        if hasattr(self, "_recent_search_cache"):


            self._recent_search_cache.clear()





        # 输出风格


        output_cfg = self.config.get("output", {}) or {}


        self.verbosity = str(output_cfg.get("verbosity", "medium")).lower()


        self.token_saving = bool(output_cfg.get("token_saving", False))


        self._verbosity_group_overrides: dict[str, str] = {}


        raw_overrides = output_cfg.get("group_overrides", {})


        if isinstance(raw_overrides, dict):


            for k, v in raw_overrides.items():


                self._verbosity_group_overrides[str(k)] = str(v).lower()


        self.output_style_instruction = normalize_text(


            str(output_cfg.get("style_instruction", output_cfg.get("prompt_instruction", "")))


        )


        self._group_output_style_overrides: dict[str, str] = {}


        raw_style_overrides = output_cfg.get("group_style_overrides")


        if raw_style_overrides is None:


            raw_style_overrides = output_cfg.get("group_prompt_overrides", {})


        if isinstance(raw_style_overrides, dict):


            for k, v in raw_style_overrides.items():


                gid = normalize_text(str(k))


                style_text = normalize_text(str(v))


                if gid and style_text:


                    self._group_output_style_overrides[gid] = style_text





        # 热重载场景下同步 trigger 配置（避免重建对象）。


        if hasattr(self, "trigger"):


            trigger_cfg = self.config.get("trigger", {})


            if isinstance(trigger_cfg, dict):


                self.trigger.ai_listen_enable = bool(trigger_cfg.get("ai_listen_enable", self.trigger.ai_listen_enable))


                self.trigger.delegate_undirected_to_ai = bool(


                    trigger_cfg.get("delegate_undirected_to_ai", self.trigger.delegate_undirected_to_ai)


                )





        # 热重载时刷新自动学习配置。


        if hasattr(self, "knowledge_updater"):


            self.knowledge_updater = KnowledgeUpdater(


                knowledge_base=self.knowledge_base if hasattr(self, "knowledge_base") else None,


                config=self.config,


                logger=self.logger,


                model_client=self.model_client if hasattr(self, "model_client") else None,


            )





    def get_verbosity(self, group_id: int | str = 0) -> str:


        """获取指定群的输出详细度。"""


        return self._verbosity_group_overrides.get(str(group_id), self.verbosity)





    def get_output_style_instruction(self, group_id: int | str = 0) -> str:


        """获取指定群的输出风格附加指令。"""


        group_hint = self._group_output_style_overrides.get(str(group_id), "")


        if group_hint:


            return group_hint


        return self.output_style_instruction





    def reload_config(self) -> tuple[bool, str]:


        """热重载配置（不重建 ModelClient / Memory 等重量级组件）。





        使用 _reload_lock 防止并发重载导致状态不一致。


        """


        import threading


        if not hasattr(self, "_reload_sync_lock"):


            self._reload_sync_lock = threading.Lock()


        if not self._reload_sync_lock.acquire(blocking=False):


            return False, "reload_already_in_progress"


        try:


            ok, msg = self.config_manager.reload()


            if ok:


                _pl.reload()


                self.config = self.config_manager.raw


                self._init_from_config()


                self.admin = AdminEngine(self.config, self.storage_dir)


                self.safety = SafetyEngine(self.config.get("safety", {}))


                self.emotion = EmotionEngine(self.config.get("emotion", {}))


                self.personality = PersonalityEngine.from_file(self.config_dir / "personality.yml", config=self.config)


                self.trigger = TriggerEngine(


                    trigger_config=self.config.get("trigger", {}),


                    bot_config=self.config.get("bot", {}),


                )


                self.thinking = ThinkingEngine(


                    config=self.config,


                    personality=self.personality,


                    model_client=self.model_client,


                )


                self.router = RouterEngine(


                    config=self.config,


                    personality=self.personality,


                    model_client=self.model_client,


                )


                self.tools = ToolExecutor(


                    search_engine=self.search,


                    image_engine=self.image,


                    plugin_runner=self._run_plugin,


                    config=self.config,


                )


                self.plugins.load(global_config=self.config)


                if hasattr(self, "agent"):


                    self.agent.refresh_runtime_config(self.config)


                self._async_init_done = False  # re-run async_init on next message


                self.logger.info("配置热重载完成")


            return ok, msg


        finally:


            self._reload_sync_lock.release()





    @staticmethod


    def _deep_merge_plain(base: Any, patch: Any) -> Any:


        if not isinstance(base, dict) or not isinstance(patch, dict):


            return patch


        out: dict[str, Any] = {}


        for key, value in base.items():


            out[key] = value


        for key, value in patch.items():


            if key in out and isinstance(out[key], dict) and isinstance(value, dict):


                out[key] = YukikoEngine._deep_merge_plain(out[key], value)


            else:


                out[key] = value


        return out





    def apply_config_patch(


        self,


        patch: dict[str, Any],


        actor_user_id: str = "",


        source: str = "runtime",


        reason: str = "",


        dry_run: bool = False,


    ) -> tuple[bool, str, dict[str, Any]]:


        """把补丁写入 config.yml 并热重载，供 Agent/WebUI/管理命令统一复用。"""


        if not isinstance(patch, dict) or not patch:


            return False, "invalid_patch", {}





        config_path = self.config_manager._config_file


        try:


            import yaml


            if config_path.exists():


                with open(config_path, "r", encoding="utf-8") as f:


                    raw_yaml = yaml.safe_load(f) or {}


            else:


                raw_yaml = {}


        except Exception as exc:


            return False, f"read_config_failed:{exc}", {}


        if not isinstance(raw_yaml, dict):


            raw_yaml = {}





        merged = self._deep_merge_plain(raw_yaml, patch)


        if not isinstance(merged, dict):


            return False, "merged_config_invalid", {}





        admin_cfg = merged.get("admin")


        if isinstance(admin_cfg, dict):


            super_admin_qq = normalize_text(str(admin_cfg.get("super_admin_qq", "")))


            super_users_raw = admin_cfg.get("super_users", [])


            super_users: list[str] = []


            if isinstance(super_users_raw, list):


                for item in super_users_raw:


                    uid = normalize_text(str(item))


                    if uid and uid not in super_users:


                        super_users.append(uid)


            if super_admin_qq and super_admin_qq not in super_users:


                super_users.insert(0, super_admin_qq)


            if super_users:


                admin_cfg["super_users"] = super_users


                admin_cfg["super_admin_qq"] = super_admin_qq or super_users[0]





            white_rows = admin_cfg.get("whitelist_groups", [])


            white_groups: list[int] = []


            if isinstance(white_rows, list):


                for item in white_rows:


                    try:


                        gid = int(item)


                    except Exception:


                        continue


                    if gid not in white_groups:


                        white_groups.append(gid)


            admin_cfg["whitelist_groups"] = sorted(white_groups)





        if dry_run:


            return True, "dry_run_ok", merged





        try:


            import yaml


            with open(config_path, "w", encoding="utf-8") as f:


                yaml.safe_dump(merged, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


        except Exception as exc:


            return False, f"write_config_failed:{exc}", {}





        try:


            admin_cfg_saved = merged.get("admin", {})


            if isinstance(admin_cfg_saved, dict):


                white_rows = admin_cfg_saved.get("whitelist_groups", [])


                groups: list[int] = []


                if isinstance(white_rows, list):


                    for item in white_rows:


                        try:


                            gid = int(item)


                        except Exception:


                            continue


                        if gid not in groups:


                            groups.append(gid)


                white_path = self.storage_dir / "whitelist_groups.json"


                white_path.parent.mkdir(parents=True, exist_ok=True)


                white_path.write_text(


                    json.dumps({"groups": sorted(groups)}, ensure_ascii=False, indent=2),


                    encoding="utf-8",


                )


        except Exception as exc:


            self.logger.warning("config_patch_whitelist_sync_fail | %s", exc)





        ok, msg = self.reload_config()


        if ok:


            self.logger.info(


                "config_patch_applied | source=%s | actor=%s | reason=%s | keys=%s",


                source or "runtime",


                actor_user_id or "-",


                clip_text(reason, 120) if reason else "-",


                ",".join(sorted(str(k) for k in patch.keys()))[:160],


            )


            return True, "配置已更新并生效", merged


        return False, msg, {}





    @classmethod


    def from_default_paths(cls, project_root: Path | None = None) -> "YukikoEngine":


        root = project_root or Path(__file__).resolve().parents[1]


        return cls(project_root=root)





    async def handle_message(self, message: EngineMessage) -> EngineResponse:


        if not self._async_init_done:


            await self.async_init()


        self.admin.increment_message_count()





        # ── 消息去重（NapCat 偶尔重复推送同一条消息）──


        if message.message_id:


            if message.message_id in self._seen_message_ids:


                return EngineResponse(action="ignore", reason="duplicate_message")


            self._seen_message_ids[message.message_id] = message.timestamp.timestamp()


            while len(self._seen_message_ids) > self._seen_message_ids_max:


                self._seen_message_ids.popitem(last=False)





        text = normalize_text(message.text)


        if not text:


            return EngineResponse(action="ignore", reason="empty_message")





        # ── 白名单检查（非私聊 + 权限系统启用时）──


        if not message.is_private and self.admin.enabled:


            if not self.admin.is_group_whitelisted(message.group_id):


                if self.admin.non_whitelist_mode == "silent":


                    return EngineResponse(action="ignore", reason="group_not_whitelisted")


                if not message.mentioned:


                    return EngineResponse(action="ignore", reason="group_not_whitelisted_not_mentioned")





        # Keep recent media even when this turn is ignored, so "先发图后问" can still work.


        self.tools.remember_incoming_media(message.conversation_id, message.raw_segments)


        if message.reply_media_segments:


            self.tools.remember_incoming_media(message.conversation_id, message.reply_media_segments)


        # 建立媒体 artifact 索引（message_id -> media refs）


        if message.message_id:


            self._index_message_media(message.message_id, message.raw_segments)


        if message.reply_to_message_id and message.reply_media_segments:


            self._index_message_media(message.reply_to_message_id, message.reply_media_segments)


        self._record_runtime_group_chat(message=message, text=text)





        text, fragment_state, fragment_mentioned = self._merge_fragmented_user_message(message, text)


        if fragment_state == "hold":


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                "fragment_waiting_followup",


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason="fragment_waiting_followup")


        if fragment_state == "merged":


            self.logger.info(


                "断句补回 | 会话=%s | 用户=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                clip_text(text, 120),


            )


        if fragment_state == "timeout_fallback":


            self.logger.info(


                "断句超时回补 | 会话=%s | 用户=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                clip_text(text, 120),


            )


        if fragment_mentioned and not message.mentioned:


            message.mentioned = True





        self._track_directed_hint(message, text)





        if self._is_explicitly_replying_other_user(message) and not self._allow_at_other_target_dialog(message, text):


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                "at_other_not_for_bot_hard",


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason="at_other_not_for_bot_hard")





        short_ping_call = self._is_short_ping_message(text) and (


            message.is_private or message.mentioned or (not self.short_ping_require_directed)


        )


        if text == "__mention_only__" or self._is_bot_alias_only_message(text) or short_ping_call:


            quick_reply = await self._build_mention_only_reply_auto(message)


            quick_reply = self._apply_tone_guard(quick_reply)


            quick_reply = self._limit_reply_text(quick_reply, "short", proactive=False)


            rendered = self.markdown.render(quick_reply)


            if text == "__mention_only__":


                quick_reason = "mention_only"


            elif self._is_bot_alias_only_message(text):


                quick_reason = "alias_only_call"


            else:


                quick_reason = "short_ping_call"


            self.logger.info(


                "消息已处理 | 会话=%s | 用户=%s | 动作=%s | 原因=%s | 回复长度=%d",


                message.conversation_id,


                message.user_id,


                "reply",


                quick_reason,


                len(rendered),


            )


            await self._after_reply(message, rendered, proactive=False, action="reply", open_followup=True)


            self._record_intent(message, action="reply", reason=quick_reason, text=text)


            return EngineResponse(action="reply", reason=quick_reason, reply_text=rendered)





        allow_memory = bool(self.config.get("bot", {}).get("allow_memory", True))





        safety = self.safety.evaluate(


            conversation_id=message.conversation_id,


            user_id=message.user_id,


            text=text,


            now=message.timestamp,


        )


        if safety.action == "silence":


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                safety.reason,


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason=safety.reason)





        if safety.action == "moderate" and safety.should_reply:


            reply = self._limit_reply_text(safety.reply_text, "short", proactive=False)


            rendered = self.markdown.render(reply)


            await self._after_reply(message, rendered, proactive=False, action="moderate", open_followup=False)


            self._record_intent(message, action="moderate", reason=safety.reason, text=text)


            return EngineResponse(action="moderate", reason=safety.reason, reply_text=rendered)





        trigger = self.trigger.evaluate(


            TriggerInput(


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                text=text,


                mentioned=message.mentioned,


                is_private=message.is_private,


                timestamp=message.timestamp,


            ),


            recent_messages=[],


        )





        if trigger.reason == "overload_notice":


            notice = self.markdown.render(self._limit_reply_text(self.overload_notice_text, "short", proactive=True))


            await self._after_reply(message, notice, proactive=True, action="overload_notice", open_followup=False)


            self._record_intent(message, action="reply", reason="overload_notice", text=text)


            return EngineResponse(action="reply", reason="overload_notice", reply_text=notice)





        trigger_candidate = normalize_text(str(trigger.reason)).lower() == "ai_router_candidate"


        if not trigger.should_handle and not trigger_candidate:


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                trigger.reason,


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason=trigger.reason)


        if trigger_candidate:


            # 仅作为候选进入 router/self_check，不代表可直接回复。


            trigger.should_handle = True





        alias_call_hint = ""


        text, alias_token = self._strip_edge_bot_alias_tokens(text)


        if alias_token:


            hint_template = normalize_text(_pl.get_message("alias_call_hint", ""))


            if hint_template:


                alias_call_hint = hint_template.replace("{alias}", alias_token)





        # 仅在明确对 bot 说话或 followup 窗口内写入用户消息，避免群聊旁路噪声污染记忆。


        if allow_memory and (message.mentioned or message.is_private or bool(trigger.followup_candidate)):
            await self._remember_message_media_memory(message)


            self.memory.add_message(


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                user_name=message.user_name,


                role="user",


                content=text,


                timestamp=message.timestamp,


            )





        recent_messages = self.memory.get_recent_messages(message.conversation_id, limit=25) if allow_memory else []


        # 群聊里优先使用“当前用户上下文”，避免把其他人的历史误注入当前会话。


        if allow_memory and not message.is_private:


            memory_context = self._build_recent_user_lines_by_user_id(


                recent_messages=recent_messages,


                user_id=message.user_id,


                limit=10,


            )


            reply_target_uid = normalize_text(str(message.reply_to_user_id))


            bot_uid = normalize_text(str(message.bot_id))


            current_uid = normalize_text(str(message.user_id))


            if reply_target_uid and reply_target_uid not in {bot_uid, current_uid}:


                reply_target_recent = self._build_recent_user_lines_by_user_id(


                    recent_messages=recent_messages,


                    user_id=reply_target_uid,


                    limit=4,


                )


                if reply_target_recent:


                    memory_context = (memory_context + [f"[引用对象近期]{item}" for item in reply_target_recent])[-16:]

            if message.reply_to_message_id and hasattr(self.memory, "get_message_media_artifacts"):
                reply_media_items = self.memory.get_message_media_artifacts(
                    message_id=message.reply_to_message_id,
                    conversation_id=message.conversation_id,
                    media_type="image",
                    limit=3,
                )
                if reply_media_items:
                    reply_media_lines = self._build_reply_media_memory_lines(reply_media_items, limit=2)
                    if reply_media_lines:
                        memory_context = (memory_context + reply_media_lines)[-22:]


            if not memory_context:


                memory_context = self.memory.get_recent_texts(message.conversation_id, limit=8)


        else:


            memory_context = self.memory.get_recent_texts(message.conversation_id, limit=16) if allow_memory else []


        current_user_recent = (


            self._build_recent_user_lines_by_user_id(


                recent_messages=recent_messages,


                user_id=message.user_id,


                limit=6,


            )


            if allow_memory


            else []


        )


        if current_user_recent:


            memory_context = (memory_context + [f"[当前用户近期]{item}" for item in current_user_recent])[-22:]


        related_memories = (


            self.memory.search_related(


                message.conversation_id,


                text,


                roles=("user",),


                user_id=message.user_id,


            )


            if allow_memory


            else []


        )


        user_profile_summary = self.memory.get_user_profile_summary(message.user_id) if allow_memory else ""


        preferred_name = self.memory.get_preferred_name(message.user_id) if allow_memory else ""


        recent_speakers = (


            self.memory.get_recent_speakers(message.conversation_id, limit=12)


            if allow_memory and hasattr(self.memory, "get_recent_speakers")


            else []


        )


        user_policies = (


            self.memory.get_agent_policies(message.user_id)


            if allow_memory and hasattr(self.memory, "get_agent_policies")


            else {}


        )


        user_directives = (


            self.memory.get_agent_directives(message.user_id)


            if allow_memory and hasattr(self.memory, "get_agent_directives")


            else []


        )


        thread_state = self.memory.get_thread_state(message.conversation_id) if allow_memory else {}


        learned_keywords = self.memory.get_conversation_keyword_hints(message.conversation_id, limit=10) if allow_memory else []


        runtime_group_context = self._build_runtime_group_context(


            message.conversation_id,


            limit=self.runtime_group_cache_context_limit,


        )


        # 群聊 @bot / 私聊时，避免额外注入群里其他人的短期缓存，降低跨用户污染概率。


        allow_group_context = bool(message.is_private or not message.mentioned)


        if runtime_group_context and allow_group_context:


            memory_context = (memory_context + [f"[群聊缓存]{item}" for item in runtime_group_context])[-18:]


        if alias_call_hint:


            memory_context = (memory_context + [f"[调用提示]{alias_call_hint}"])[-20:]


        memory_context, related_memories = self._prune_memory_context_for_current_turn(


            message=message,


            current_text=text,


            memory_context=memory_context,


            related_memories=related_memories,


        )





        # 显式记忆优先命中：对“你让我记住的事实”走确定性回复，降低 LLM 跑偏概率。


        explicit_fact_match = None


        if (


            allow_memory


            and "记住" not in text


            and hasattr(self.memory, "match_explicit_fact_query")


            and callable(getattr(self.memory, "match_explicit_fact_query"))


        ):


            try:


                explicit_fact_match = self.memory.match_explicit_fact_query(message.user_id, text)


            except Exception:


                explicit_fact_match = None


        if isinstance(explicit_fact_match, dict) and explicit_fact_match:


            lhs = normalize_text(str(explicit_fact_match.get("lhs", "")))


            rhs = normalize_text(str(explicit_fact_match.get("rhs", "")))


            if lhs and rhs:


                template = normalize_text(


                    _pl.get_message(


                        "explicit_fact_recall_reply",


                        "你之前让我记住的是：{lhs}={rhs}。",


                    )


                )


                reply_text = template or "你之前让我记住的是：{lhs}={rhs}。"


                if "{lhs}" in reply_text or "{rhs}" in reply_text:


                    reply_text = reply_text.replace("{lhs}", lhs).replace("{rhs}", rhs)


                else:


                    reply_text = f"{reply_text} {lhs}={rhs}"


                rendered = self.markdown.render(


                    self._limit_reply_text(self._apply_tone_guard(reply_text), "short", proactive=False),


                )


                await self._after_reply(


                    message=message,


                    reply_text=rendered,


                    proactive=False,


                    action="reply",


                    open_followup=True,


                    user_text=text,


                )


                self._record_intent(message, action="reply", reason="explicit_fact_recall", text=text)


                return EngineResponse(action="reply", reason="explicit_fact_recall", reply_text=rendered)





        # 最近结果追问：命中后优先走本地缓存选择，不必重新让 Agent 理解“第2个/再发一次”。





        # ── 音乐快速通道：可配置地跳过 Agent/Router，直接执行 ──


        agent_cfg = self.config.get("agent", {}) if isinstance(self.config, dict) else {}


        music_fast_path_enable = False


        if isinstance(agent_cfg, dict):


            music_fast_path_enable = bool(agent_cfg.get("music_fast_path_enable", False))


        if music_fast_path_enable and (message.mentioned or message.is_private) and self._looks_like_music_request(text):


            keyword = self._extract_music_keyword(text)


            if keyword:


                action = "music_search" if self._looks_like_music_search_request(text) else "music_play"


                tool_args = {"keyword": keyword, "limit": 8} if action == "music_search" else {"keyword": keyword}


                self.logger.info(


                    "music_fast_path | trace=%s | action=%s | keyword=%s",


                    message.trace_id, action, clip_text(keyword, 60),


                )


                tool_result = await self.tools.execute(


                    action=action,


                    tool_name=action,


                    tool_args=tool_args,


                    message_text=text,


                    conversation_id=message.conversation_id,


                    user_id=message.user_id,


                    user_name=message.user_name,


                    group_id=message.group_id,


                    api_call=message.api_call,


                    raw_segments=message.raw_segments,


                    bot_id=message.bot_id,


                    trace_id=message.trace_id,


                )


                reply_text = ""


                record_b64 = ""


                audio_file = ""


                video_url = ""


                if tool_result is not None:


                    reply_text = normalize_text(str(tool_result.payload.get("text", "")))


                    record_b64 = normalize_text(str(tool_result.payload.get("record_b64", "")))


                    audio_file = normalize_text(str(tool_result.payload.get("audio_file", "")))


                if not reply_text:


                    reply_text = await self._ai_error_reply(


                        user_text=text,


                        error_context=f"用户想点歌，但播放失败了。用户原文：{text}。请用简短自然的语气告诉用户这首歌没播出来，可以换个关键词再试。",


                        memory_context=memory_context,


                        scene_hint="music_error",


                    )


                rendered = self.markdown.render(reply_text)


                await self._after_reply(message, rendered, proactive=False, action=action, open_followup=True)


                self._record_intent(message, action=action, reason="music_fast_path", text=text)


                return EngineResponse(


                    action=action,


                    reason="music_fast_path",


                    reply_text=rendered,


                    video_url=video_url,


                    record_b64=record_b64,


                    audio_file=audio_file,


                )





        # ── Agent 模式：优先走 Agent 循环 ──


        if self.agent.enable and self.model_client.enabled:


            agent_result = await self._try_agent_path(


                message=message,


                text=text,


                trigger=trigger,


                memory_context=memory_context,


                related_memories=related_memories,


                user_profile_summary=user_profile_summary,


                preferred_name=preferred_name,


                recent_speakers=recent_speakers,


                user_policies=user_policies,


                user_directives=user_directives,


            )


            if agent_result is not None:


                return agent_result





        router_input = RouterInput(


            text=text,


            conversation_id=message.conversation_id,


            user_id=message.user_id,


            user_name=message.user_name,


            trace_id=message.trace_id,


            mentioned=message.mentioned,


            is_private=message.is_private,


            at_other_user_only=message.at_other_user_only,


            at_other_user_ids=message.at_other_user_ids,


            reply_to_message_id=message.reply_to_message_id,


            reply_to_user_id=message.reply_to_user_id,


            reply_to_user_name=message.reply_to_user_name,


            reply_to_text=message.reply_to_text,


            raw_segments=message.raw_segments,


            media_summary=self._build_media_summary(message.raw_segments),


            recent_messages=self._build_recent_user_lines(recent_messages),


            recent_bot_replies=self._build_recent_bot_reply_lines(recent_messages),


            user_profile_summary=user_profile_summary,


            thread_state=thread_state,


            queue_depth=max(0, int(message.queue_depth)),


            busy_messages=int(getattr(trigger, "busy_messages", 0) or 0),


            busy_users=int(getattr(trigger, "busy_users", 0) or 0),


            overload_active=trigger.overload_active,


            active_session=trigger.active_session,


            followup_candidate=trigger.followup_candidate,


            listen_probe=trigger.listen_probe,


            risk_level=safety.risk_level,


            learned_keywords=learned_keywords,


            runtime_group_context=runtime_group_context,


        )





        decision, route_fail_reason = await self._route_with_failover(router_input)


        if decision is None:


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                route_fail_reason,


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason=route_fail_reason)





        decision = self._normalize_decision_with_tool_policy(


            message=message,


            trigger=trigger,


            decision=decision,


            text=text,


        )


        self_check_reason = self._self_check_decision(message=message, trigger=trigger, decision=decision)


        if self_check_reason:


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                self_check_reason,


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason=self_check_reason)





        directed_like_call = (


            message.mentioned


            or message.is_private


            or self._looks_like_bot_call(text)


            or self._has_recent_directed_hint(message)


            or normalize_text(str(trigger.reason)).lower() in {


                "directed",


                "name_call",


                "followup_window",


                "explicit_memory_fact",


            }


        )





        effective_min_confidence = self.router_min_confidence


        threshold_source = "routing.min_confidence"


        if not directed_like_call:


            if trigger.followup_candidate or trigger.active_session:


                effective_min_confidence = self.followup_min_confidence


                threshold_source = "routing.followup_min_confidence"


            elif normalize_text(str(trigger.reason)).lower() in {"ai_router_gate", "ai_router_candidate"}:


                effective_min_confidence = self.ai_gate_min_confidence


                threshold_source = "routing.ai_gate_min_confidence"


            else:


                effective_min_confidence = self.non_directed_min_confidence


                threshold_source = "routing.non_directed_min_confidence"





        self.logger.info(


            "effective_threshold_trace | trace=%s | conversation=%s | user=%s | directed=%s | trigger_reason=%s | threshold_source=%s | threshold=%.4f | confidence=%.4f | action=%s",


            message.trace_id,


            message.conversation_id,


            message.user_id,


            directed_like_call,


            normalize_text(str(trigger.reason)),


            threshold_source,


            float(effective_min_confidence),


            float(decision.confidence),


            normalize_text(str(decision.action)),


        )





        # 非指向阈值=0 时视为关闭自动接话。


        if (


            self.routing_zero_disables_undirected


            and self.non_directed_high_confidence_only


            and not directed_like_call


            and normalize_text(str(trigger.reason)).lower() in {"ai_router_candidate", "ai_router_gate", "not_directed"}


            and effective_min_confidence <= 0.0


        ):


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                "non_directed_threshold_disabled",


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason="non_directed_threshold_disabled")





        if (


            decision.confidence < effective_min_confidence


            and not directed_like_call


            and decision.action not in {"moderate", "ignore", "search"}


            and not (


                int(getattr(trigger, "busy_users", 0) or 0) <= 1


                and self._looks_like_explicit_request(text)


            )


        ):


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                "router_low_confidence",


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason="router_low_confidence")





        if not decision.should_handle or decision.action == "ignore":


            short_reason = f"router:{clip_text(normalize_text(decision.reason), 96)}"


            self.logger.info(


                "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                message.conversation_id,


                message.user_id,


                short_reason,


                clip_text(text, 80),


            )


            return EngineResponse(action="ignore", reason=short_reason)





        if decision.action == "moderate":


            reply = self._limit_reply_text(self.safety.high_risk_reply, "short", proactive=False)


            rendered = self.markdown.render(reply)


            await self._after_reply(message, rendered, proactive=False, action="moderate", open_followup=False)


            short_reason = f"router:{clip_text(normalize_text(decision.reason), 96)}"


            self._record_intent(message, action="moderate", reason=short_reason, text=text)


            return EngineResponse(action="moderate", reason=short_reason, reply_text=rendered)





        emotion_response = await self._maybe_emotion_gate(


            message=message,


            trigger=trigger,


            decision=decision,


            text=text,


        )


        if emotion_response is not None:


            return emotion_response





        tool_result = None


        if decision.action in {


            "search",


            "music_search",


            "music_play",


            "generate_image",


            "get_group_member_count",


            "get_group_member_names",


            "plugin_call",


        }:


            dispatch_args = decision.tool_args if isinstance(decision.tool_args, dict) else {}


            self.logger.info(


                "tool_dispatch | trace=%s | 会话=%s | 用户=%s | action=%s | method=%s | mode=%s",


                message.trace_id,


                message.conversation_id,


                message.user_id,


                decision.action,


                normalize_text(str(dispatch_args.get("method", ""))),


                normalize_text(str(dispatch_args.get("mode", ""))),


            )


            tool_result = await self.tools.execute(


                action=decision.action,


                tool_name=decision.tool_name,


                tool_args=decision.tool_args,


                message_text=text,


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                user_name=message.user_name,


                group_id=message.group_id,


                api_call=message.api_call,


                raw_segments=message.raw_segments,


                bot_id=message.bot_id,


                trace_id=message.trace_id,


            )


            self.logger.info(


                "tool_result | trace=%s | 会话=%s | 用户=%s | ok=%s | tool=%s | error=%s",


                message.trace_id,


                message.conversation_id,


                message.user_id,


                bool(getattr(tool_result, "ok", False)),


                normalize_text(str(getattr(tool_result, "tool_name", ""))),


                normalize_text(str(getattr(tool_result, "error", ""))),


            )


            if not tool_result.ok:


                tool_result = await self._retry_tool_after_failure(


                    message=message,


                    decision=decision,


                    tool_result=tool_result,


                    user_text=text,


                )


            if not tool_result.ok:


                if bool((tool_result.payload or {}).get("silent_ignore")):


                    reason = normalize_text(tool_result.error) or "tool_silent_ignore"


                    self.logger.info(


                        "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",


                        message.conversation_id,


                        message.user_id,


                        reason,


                        clip_text(text, 80),


                    )


                    return EngineResponse(action="ignore", reason=reason)


                self.logger.warning(


                    "tool_exec_error | trace=%s | 会话=%s | 用户=%s | 工具=%s | 错误=%s",


                    message.trace_id,


                    message.conversation_id,


                    message.user_id,


                    tool_result.tool_name,


                    tool_result.error,


                )





        action = decision.action


        reason = f"router:{clip_text(normalize_text(decision.reason), 96)}"


        verbosity = self.get_verbosity(message.group_id)


        output_style_instruction = self.get_output_style_instruction(message.group_id)


        reply_text = ""


        image_url = ""


        image_urls: list[str] = []


        video_url = ""


        cover_url = ""


        record_b64 = ""


        audio_file = ""


        search_summary_text = ""


        force_structured_reply = False


        pre_ack = ""





        if action == "reply":


            if self._looks_like_summary_followup(text):


                quick_summary = self._compose_preferred_summary(message=message, recent_messages=recent_messages)


                if quick_summary:


                    reply_text = quick_summary


                    force_structured_reply = True


                else:


                    reply_text = await self._ai_error_reply(


                        user_text=text,


                        error_context="用户想让你总结之前的内容，但没有找到可总结的上下文。请简短自然地问用户想总结什么。",


                        memory_context=memory_context,


                        scene_hint="summary_miss",


                    )


                    force_structured_reply = True


            else:


                reply_text = await self.thinking.generate_reply(


                    user_text=text,


                    memory_context=memory_context,


                    related_memories=related_memories,


                    reply_style=decision.reply_style,


                    search_summary="",


                    sensitive_context="",


                    user_profile_summary=user_profile_summary,


                    trigger_reason=trigger.reason,


                    scene_hint=trigger.scene_hint,


                    verbosity=verbosity,


                    output_style_instruction=output_style_instruction,


                    current_user_id=message.user_id,


                    current_user_name=preferred_name or message.user_name,


                    recent_speakers=recent_speakers,


                )


        elif action == "search":


            search_text = ""


            if tool_result is not None:


                search_text = normalize_text(str(tool_result.payload.get("text", "")))


                image_url = normalize_text(str(tool_result.payload.get("image_url", "")))


                raw_image_urls = (tool_result.payload or {}).get("image_urls", [])


                if isinstance(raw_image_urls, list):


                    image_urls = [


                        normalize_text(str(item))


                        for item in raw_image_urls


                        if normalize_text(str(item))


                    ]


                if image_url and image_url not in image_urls:


                    image_urls.insert(0, image_url)


                if image_urls and not image_url:


                    image_url = image_urls[0]


                video_url = normalize_text(str(tool_result.payload.get("video_url", "")))


                cover_url = normalize_text(str(tool_result.payload.get("cover_url", "")))


                record_b64 = normalize_text(str(tool_result.payload.get("record_b64", "")))


                audio_file = normalize_text(str(tool_result.payload.get("audio_file", "")))


            if video_url and self._looks_like_video_text_only_intent(text):


                video_url = ""


                cover_url = ""


            search_summary_text = search_text


            cached_query = text


            if tool_result is not None:


                payload = getattr(tool_result, "payload", {}) or {}


                payload_query = normalize_text(str(payload.get("query", "")))


                if payload_query:


                    cached_query = payload_query


            self._remember_search_cache(


                message=message,


                query=cached_query,


                tool_result=tool_result,


                search_text=search_text,


            )





            if image_url or image_urls or video_url:


                # 视频分析请求：把结构化分析结果交给 AI 生成有深度的回复


                is_video_analysis = bool((tool_result.payload or {}).get("video_analysis"))


                analysis_strict = bool((tool_result.payload or {}).get("analysis_strict"))


                if is_video_analysis:


                    pre_ack = "OK，我现在去深度分析这个视频（关键帧识别+元数据解析），稍等。"


                if is_video_analysis and search_text and analysis_strict:


                    reply_text = search_text


                elif is_video_analysis and search_text:


                    reply_text = await self.thinking.generate_reply(


                        user_text=text,


                        memory_context=memory_context,


                        related_memories=related_memories,


                        reply_style="long",


                        search_summary=search_text,


                        sensitive_context="",


                        user_profile_summary=user_profile_summary,


                        trigger_reason=trigger.reason,


                        scene_hint="video_analysis",


                        verbosity=verbosity,


                        output_style_instruction=output_style_instruction,


                        current_user_id=message.user_id,


                        current_user_name=preferred_name or message.user_name,


                        recent_speakers=recent_speakers,


                    )


                    if not normalize_text(reply_text):


                        reply_text = search_text


                elif video_url and search_text:


                    # 普通"解析并发视频"场景优先直出工具文本，避免模型二次改写成矛盾拒绝话术。


                    reply_text = search_text


                else:


                    reply_text = search_text


                    if not normalize_text(reply_text):


                        reply_text = await self._ai_error_reply(


                            user_text=text,


                            error_context="媒体已经解析完成，但没有生成可展示的说明文本。请简短自然地告诉用户已处理好并继续发送媒体。",


                            memory_context=memory_context,


                            scene_hint="media_ack",


                        )


            elif search_text:


                # 搜索有文本结果：交给 AI 综合分析并生成高质量回复


                reply_text = await self.thinking.generate_reply(


                    user_text=text,


                    memory_context=memory_context,


                    related_memories=related_memories,


                    reply_style=decision.reply_style or "casual",


                    search_summary=search_text,


                    sensitive_context="",


                    user_profile_summary=user_profile_summary,


                    trigger_reason=trigger.reason,


                    scene_hint="search_synthesis",


                    verbosity=verbosity,


                    output_style_instruction=output_style_instruction,


                    current_user_id=message.user_id,


                    current_user_name=preferred_name or message.user_name,


                    recent_speakers=recent_speakers,


                )


                if not normalize_text(reply_text):


                    reply_text = search_text


            else:


                reply_text = await self.thinking.generate_reply(


                    user_text=text,


                    memory_context=memory_context,


                    related_memories=related_memories,


                    reply_style="serious",


                    search_summary=search_text,


                    sensitive_context="",


                    user_profile_summary=user_profile_summary,


                    trigger_reason=trigger.reason,


                    scene_hint="tech_support",


                    verbosity=verbosity,


                    output_style_instruction=output_style_instruction,


                    current_user_id=message.user_id,


                    current_user_name=preferred_name or message.user_name,


                    recent_speakers=recent_speakers,


                )


                if not normalize_text(reply_text):


                    reply_text = search_text


        elif action in {"music_search", "music_play"}:


            if tool_result is not None:


                reply_text = normalize_text(str(tool_result.payload.get("text", "")))


                record_b64 = normalize_text(str(tool_result.payload.get("record_b64", "")))


                audio_file = normalize_text(str(tool_result.payload.get("audio_file", "")))


            if not reply_text:


                _music_hint = "用户搜歌但没找到结果" if action == "music_search" else "用户点歌但播放失败了"


                reply_text = await self._ai_error_reply(


                    user_text=text,


                    error_context=f"{_music_hint}。用户原文：{text}。请简短自然地告诉用户，可以换个关键词再试。",


                    memory_context=memory_context,


                    scene_hint="music_error",


                )


        elif action == "generate_image":


            if tool_result is not None:


                reply_text = normalize_text(str(tool_result.payload.get("text", "")))


                image_url = normalize_text(str(tool_result.payload.get("image_url", "")))


                if image_url:


                    image_urls = [image_url]


            if not reply_text:


                reply_text = await self._ai_error_reply(


                    user_text=text,


                    error_context=f"用户要求生成图片但失败了。用户原文：{text}。请简短自然地告诉用户生成失败，可以再试一次或换个描述。",


                    memory_context=memory_context,


                    scene_hint="image_gen_error",


                )


        elif action in {"get_group_member_count", "get_group_member_names", "plugin_call"}:


            if tool_result is not None:


                reply_text = normalize_text(str(tool_result.payload.get("text", "")))


            if not reply_text:


                reply_text = await self._ai_error_reply(


                    user_text=text,


                    error_context=f"用户的请求执行失败了。用户原文：{text}。请简短自然地告诉用户执行失败，可以再试。",


                    memory_context=memory_context,


                    scene_hint="tool_error",


                )


        elif action == "send_segment":


            # 消息段已在 tools 层直接发送，这里只处理回复文本


            if tool_result is not None and tool_result.ok:


                return EngineResponse(action="ignore", reason="segment_sent_directly")


            reply_text = normalize_text(str(getattr(tool_result, "error", ""))) if tool_result else ""


            if not reply_text:


                reply_text = await self._ai_error_reply(


                    user_text=text,


                    error_context="消息片段发送失败，没有拿到明确错误信息。请简短自然地让用户稍后重试。",


                    memory_context=memory_context,


                    scene_hint="segment_send_error",


                )


        else:


            return EngineResponse(action="ignore", reason="router_unknown_action")





        if action in {"reply", "search"}:


            reply_text = self._guard_unverified_memory_claims(


                reply_text=reply_text,


                user_text=text,


                current_user_recent=current_user_recent,


                related_memories=related_memories,


            )





        reply_text = self._sanitize_reply_output(reply_text, action=action)


        reply_text = self._enforce_identity_claim(reply_text)


        reply_text = self._apply_tone_guard(reply_text)


        reply_text = self.safety.filter_output(reply_text)


        reply_text = self._apply_privacy_output_guard(reply_text, action=action)


        if reply_text:


            reply_text = self._inject_user_name(


                reply_text=reply_text,


                user_name=message.user_name,


                should_address=(


                    message.mentioned


                    or message.is_private


                    or trigger.followup_candidate


                    or trigger.reason in {"directed", "name_call", "followup_window", "explicit_memory_fact"}


                ),


            )


            if action == "search":


                reply_text = clip_text(reply_text, max(480, self.max_reply_chars * 2))


            else:


                if force_structured_reply:


                    reply_text = clip_text(reply_text, max(320, self.max_reply_chars * 2))


                else:


                    reply_text = self._limit_reply_text(reply_text, decision.reply_style, proactive=False)





        if reply_text:


            if action == "search" or force_structured_reply:


                rendered = self.markdown.render(


                    reply_text,


                    max_len=max(self.markdown.max_output_chars, 480),


                    max_lines=max(self.markdown.max_output_lines, 6),


                )


            else:


                rendered = self.markdown.render(reply_text)


        else:


            rendered = ""





        rendered = self._ensure_min_reply_text(


            rendered=rendered,


            action=action,


            user_text=text,


            search_summary=search_summary_text,


            message=message,


            recent_messages=recent_messages,


        )





        if not rendered and not image_url and not image_urls and not video_url and not record_b64 and not audio_file:


            return EngineResponse(action="ignore", reason="empty_reply")





        self.logger.info(


            "消息已处理 | trace=%s | 会话=%s | 用户=%s | 动作=%s | 原因=%s | 回复长度=%d",


            message.trace_id,


            message.conversation_id,


            message.user_id,


            action,


            reason,


            len(rendered),


        )





        await self._after_reply(


            message,


            rendered,


            proactive=False,


            action=action,


            open_followup=action not in {"moderate", "overload_notice"},


        )


        self._record_intent(message, action=action, reason=reason, text=text)





        return EngineResponse(


            action=action,


            reason=reason,


            reply_text=rendered,


            image_url=image_url,


            image_urls=image_urls,


            video_url=video_url,


            cover_url=cover_url,


            record_b64=record_b64,


            audio_file=audio_file,


            pre_ack=pre_ack,


            meta={


                "trace_id": message.trace_id,


                "confidence": decision.confidence,


                "tool": decision.tool_name,


                "reason_code": getattr(decision, "reason_code", ""),


                "target_user_id": getattr(decision, "target_user_id", ""),


            },


        )





    async def _try_agent_path(


        self,


        message: EngineMessage,


        text: str,


        trigger: Any,


        memory_context: list[str],


        related_memories: list[str],


        user_profile_summary: str,


        preferred_name: str,


        recent_speakers: list[tuple[str, str, str]],


        user_policies: dict[str, Any] | None = None,


        user_directives: list[str] | None = None,


    ) -> EngineResponse | None:


        """尝试走 Agent 循环处理消息。成功返回 EngineResponse，失败返回 None 回退旧管线。"""


        try:


            media_summary = self._build_media_summary(message.raw_segments)


            reply_media_summary = self._build_media_summary(message.reply_media_segments)


            # 构建 admin_handler 闭包


            _engine_ref = self


            async def _admin_handler_for_agent(text: str, user_id: str, group_id: int) -> str | None:


                return await _engine_ref.admin.handle_command(


                    text=text, user_id=user_id, group_id=group_id,


                    engine=_engine_ref, api_call=message.api_call,


                )


            async def _config_patch_handler_for_agent(


                patch: dict[str, Any],


                actor_user_id: str,


                reason: str = "",


                dry_run: bool = False,


            ) -> tuple[bool, str, dict[str, Any]]:


                return _engine_ref.apply_config_patch(


                    patch=patch,


                    actor_user_id=actor_user_id,


                    source="agent",


                    reason=reason,


                    dry_run=dry_run,


                )





            ctx = AgentContext(


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                user_name=message.user_name,


                group_id=message.group_id,


                bot_id=message.bot_id,


                is_private=message.is_private,


                mentioned=message.mentioned,


                message_text=text,


                message_id=message.message_id,


                reply_to_message_id=message.reply_to_message_id,


                raw_segments=message.raw_segments,


                reply_media_segments=message.reply_media_segments,


                reply_to_user_id=message.reply_to_user_id,


                reply_to_user_name=message.reply_to_user_name,


                reply_to_text=message.reply_to_text,


                api_call=message.api_call,


                admin_handler=_admin_handler_for_agent,


                config_patch_handler=_config_patch_handler_for_agent,


                sticker_manager=self.sticker if hasattr(self, "sticker") else None,


                tool_executor=self.tools if hasattr(self, "tools") else None,


                crawler_hub=self.crawler_hub if hasattr(self, "crawler_hub") else None,


                knowledge_base=self.knowledge_base if hasattr(self, "knowledge_base") else None,


                memory_engine=self.memory if hasattr(self, "memory") else None,


                trace_id=message.trace_id,


                memory_context=memory_context,


                related_memories=related_memories,


                user_profile_summary=user_profile_summary,


                preferred_name=preferred_name,


                recent_speakers=recent_speakers[:8],


                user_policies=user_policies or {},


                user_directives=user_directives or [],


                media_summary=media_summary,


                reply_media_summary=reply_media_summary,


                at_other_user_ids=message.at_other_user_ids or [],


                at_other_user_names=await self._resolve_at_user_names(


                    message.at_other_user_ids or [], message.api_call,


                ),


                verbosity=self.get_verbosity(message.group_id),


                output_style_instruction=self.get_output_style_instruction(message.group_id),


                sender_role=message.sender_role or "",


                is_whitelisted_group=self.admin.is_group_whitelisted(message.group_id) if not message.is_private else False,


                stream_callback=self._get_stream_callback(message.conversation_id),

            )





            # 含媒体或"文本里明确是媒体重任务"时，放宽超时预算


            merged_media_text = normalize_text(


                f"{self._extract_multimodal_user_text(message.text)}\n{text}"


            )


            forced_method, _forced_args, forced_reason = self._infer_forced_tool_plan(


                message=message,


                text=merged_media_text or text,


            )


            # 明确可由本地工具直接处理的媒体任务，跳过 Agent 循环，避免慢思考超时。


            # 注意：browser.resolve_video 不再跳过 Agent，因为 Agent 的 parse_video


            # 有更完善的抖音视频/图文判断逻辑和 douyin_share 回退。


            if forced_method in {


                "video.analyze",


                "media.analyze_image",


                "media.pick_video_from_message",


            }:


                self.logger.info(


                    "agent_bypass_local_tool | trace=%s | method=%s | reason=%s",


                    message.trace_id,


                    forced_method,


                    forced_reason or "local_force",


                )


                return None


            media_like_text = bool(


                self._looks_like_video_request(merged_media_text)


                or self._looks_like_video_analysis_intent(merged_media_text)


                or self._looks_like_video_resolve_intent(merged_media_text)


                or self._looks_like_image_analyze_intent(merged_media_text)


                or self._extract_first_video_url_from_text(merged_media_text)


                or self._extract_first_image_url_from_text(merged_media_text)


            )


            download_like_text = self._looks_like_download_task_intent(merged_media_text or text)


            has_media = bool(media_summary) or bool(reply_media_summary) or media_like_text


            agent_timeout = max(90, self.router_timeout_seconds * 5) if has_media else max(45, self.router_timeout_seconds * 3)


            if download_like_text:


                # 下载/安装包链路通常包含「搜索+抓取+下载+上传」，给更高超时预算，减少中途超时导致的重复执行。


                agent_timeout = max(agent_timeout, max(120, self.router_timeout_seconds * 8))


            # 对齐 Agent 内部预算，避免“工具已拿到结果但外层 wait_for 先超时”导致回退旧管线偏题。


            try:


                inner_budget = float(self.agent.estimate_total_timeout_seconds(ctx, has_media))


            except Exception:


                inner_budget = 0.0


            if inner_budget > 0:


                agent_timeout = max(agent_timeout, min(300.0, inner_budget + 6.0))


            self.logger.info(


                "agent_timeout_budget | trace=%s | outer=%.1fs | inner=%.1fs | has_media=%s | download_like=%s",


                message.trace_id,


                float(agent_timeout),


                float(inner_budget),


                has_media,


                download_like_text,


            )





            if self.agent_single_inflight_per_conversation:


                lock = self._agent_conversation_locks.get(message.conversation_id)


                if lock is None:


                    lock = asyncio.Lock()


                    self._agent_conversation_locks[message.conversation_id] = lock


                if lock.locked():


                    self.logger.info(


                        "agent_inflight_wait | trace=%s | 会话=%s",


                        message.trace_id,


                        message.conversation_id,


                    )


                async with lock:


                    agent_result = await asyncio.wait_for(


                        self.agent.run(ctx),


                        timeout=agent_timeout,


                    )


            else:


                agent_result = await asyncio.wait_for(


                    self.agent.run(ctx),


                    timeout=agent_timeout,


                )





            self.logger.info(


                "agent_done | trace=%s | 会话=%s | 用户=%s | steps=%d | tools=%d | time=%dms | reason=%s",


                message.trace_id,


                message.conversation_id,


                message.user_id,


                len(agent_result.steps),


                agent_result.tool_calls_made,


                agent_result.total_time_ms,


                agent_result.reason,


            )


            # Agent 路径也写入 followup 候选缓存，确保候选结果可被后续追问稳定复用。


            self._remember_agent_followup_cache(message=message, agent_result=agent_result)


            if self._should_block_ambiguous_link_recall_result(


                message=message,


                current_text=text,


                agent_result=agent_result,


            ):


                self.logger.info(


                    "agent_link_recall_guard_block | trace=%s | 会话=%s | 用户=%s | text=%s",


                    message.trace_id,


                    message.conversation_id,


                    message.user_id,


                    clip_text(text, 100),


                )


                agent_result.reply_text = "你说的链接我这边有多个历史记录。请补一个关键词（例如 ONDALOOP）或直接贴链接，我再精准给你。"


                agent_result.image_url = ""


                agent_result.image_urls = []


                agent_result.video_url = ""





            reply_text = normalize_text(agent_result.reply_text)


            if not reply_text and not agent_result.image_url and not agent_result.video_url and not agent_result.audio_file:


                # Agent 明确用 final_answer 返回空文本 = 主动选择不回复，尊重这个决定


                # 或者 LLM 挂了但消息不是对 bot 说的 → 静默


                if agent_result.reason in ("agent_final_answer", "agent_llm_error_silent"):


                    user_core = normalize_text(self._extract_multimodal_user_text(message.text) or text)


                    is_true_mention_only = (


                        user_core == "__mention_only__"


                        or self._is_bot_alias_only_message(user_core)


                    )


                    if (message.mentioned or message.is_private) and is_true_mention_only:


                        fallback_text = self._build_mention_only_reply(message.user_name)


                        rendered = self.markdown.render(fallback_text) if fallback_text else ""


                        if rendered:


                            await self._after_reply(


                                message,


                                rendered,


                                proactive=False,


                                action="reply",


                                open_followup=True,


                            )


                            return EngineResponse(


                                action="reply",


                                reason="agent_empty_final_fallback",


                                reply_text=rendered,


                                meta={"trace_id": message.trace_id},


                            )


                    if message.mentioned or message.is_private:


                        ai_fix = await self._ai_error_reply(


                            user_text=user_core or text,


                            error_context="用户在和你对话，但你上一轮生成了空结果。请直接给一句自然有效回复。",


                            memory_context=memory_context,


                            user_profile_summary=user_profile_summary,


                            trigger_reason=trigger.reason,


                            scene_hint="agent_empty_repair",


                        )


                        ai_fix = self._sanitize_reply_output(ai_fix, action="reply")


                        ai_fix = self._apply_tone_guard(ai_fix)


                        ai_fix = self.safety.filter_output(ai_fix)


                        ai_fix = self._apply_privacy_output_guard(ai_fix, action="reply")


                        if ai_fix:


                            rendered = self.markdown.render(ai_fix)


                            await self._after_reply(


                                message,


                                rendered,


                                proactive=False,


                                action="reply",


                                open_followup=True,


                            )


                            return EngineResponse(


                                action="reply",


                                reason="agent_empty_repair",


                                reply_text=rendered,


                                meta={"trace_id": message.trace_id},


                            )


                    self.logger.debug(


                        "agent_intentional_silence | trace=%s | 会话=%s",


                        message.trace_id, message.conversation_id,


                    )


                    return EngineResponse(action="ignore", reason="agent_silence")


                # 其他原因（超时/解析失败等）才回退旧管线


                return None





            # 后处理


            reply_text = self._sanitize_reply_output(reply_text, action=agent_result.action)


            reply_text = self._enforce_identity_claim(reply_text)


            reply_text = self._apply_tone_guard(reply_text)


            reply_text = self.safety.filter_output(reply_text)


            reply_text = self._apply_privacy_output_guard(reply_text, action=agent_result.action)


            if reply_text:


                reply_text = self._inject_user_name(


                    reply_text=reply_text,


                    user_name=message.user_name,


                    should_address=(message.mentioned or message.is_private),


                )


                reply_text = clip_text(reply_text, max(480, self.max_reply_chars * 2))


                if self._looks_like_choice_prompt_text(reply_text):


                    if agent_result.image_url or agent_result.image_urls or agent_result.video_url or agent_result.audio_file:


                        self.logger.info(


                            "agent_choice_prompt_strip_media | trace=%s | conversation=%s | image=%s | images=%d | video=%s | audio=%s",


                            message.trace_id,


                            message.conversation_id,


                            bool(agent_result.image_url),


                            len(agent_result.image_urls or []),


                            bool(agent_result.video_url),


                            bool(agent_result.audio_file),


                        )


                    # 编号选择链路已下线：此分支通常不会命中，保留兼容兜底。


                    agent_result.image_url = ""


                    agent_result.image_urls = []


                    agent_result.video_url = ""


                    agent_result.audio_file = ""





            rendered = self.markdown.render(reply_text) if reply_text else ""


            rendered = self._ensure_min_reply_text(


                rendered=rendered,


                action=agent_result.action,


                user_text=text,


                search_summary="",


                message=message,


                recent_messages=[],


            )





            if not rendered and not agent_result.image_url and not agent_result.video_url and not agent_result.audio_file:


                return None





            # ── 把 agent 工具调用的副作用动作写入记忆 ──


            # 这样 bot 下次能记住自己发过表情包、图片等


            self._record_agent_side_effects(message, agent_result)





            await self._after_reply(


                message, rendered, proactive=False,


                action=agent_result.action,


                open_followup=True,


            )


            self._record_intent(message, action=agent_result.action, reason=agent_result.reason, text=text)





            return EngineResponse(


                action=agent_result.action,


                reason=agent_result.reason,


                reply_text=rendered,


                image_url=agent_result.image_url,


                image_urls=agent_result.image_urls,


                video_url=agent_result.video_url,


                audio_file=agent_result.audio_file,


                meta={


                    "trace_id": message.trace_id,


                    "agent_steps": len(agent_result.steps),


                    "agent_tool_calls": agent_result.tool_calls_made,


                    "agent_time_ms": agent_result.total_time_ms,


                },


            )


        except TimeoutError:


            self.logger.warning("agent_timeout | trace=%s | 回退旧管线", message.trace_id)


            # 自动回退旧管线继续处理，避免要求用户“再发一次”。


            return None


        except Exception as exc:


            self.logger.warning("agent_error | trace=%s | %s | 回退旧管线", message.trace_id, exc, exc_info=True)


            return None





    async def _route_with_failover(self, payload: RouterInput) -> tuple[RouterDecision | None, str]:


        try:


            decision = await asyncio.wait_for(


                self.router.route(payload, self.plugins.schemas, self.tools.get_ai_method_schemas()),


                timeout=self.router_timeout_seconds,


            )


            return decision, "ok"


        except TimeoutError:


            self.logger.warning(


                "router_timeout | 会话=%s | 用户=%s | 文本=%s",


                payload.conversation_id,


                payload.user_id,


                clip_text(payload.text, 80),


            )


            return self._failover_decision(payload, "router_timeout"), "router_timeout"


        except Exception as exc:


            self.logger.warning(


                "router_parse_error | 会话=%s | 用户=%s | 错误=%s",


                payload.conversation_id,


                payload.user_id,


                repr(exc),


            )


            return self._failover_decision(payload, "router_parse_error"), "router_parse_error"





    def _failover_decision(self, payload: RouterInput, reason: str) -> RouterDecision | None:


        is_media = self._looks_like_media_request(payload.text)


        is_video = self._looks_like_video_request(payload.text) if is_media else False


        is_music = self._looks_like_music_request(payload.text)


        self.logger.info(


            "failover_check | reason=%s | is_media=%s | is_video=%s | is_music=%s | text=%s",


            reason, is_media, is_video, is_music, clip_text(payload.text, 60),


        )


        if reason in {"router_timeout", "router_parse_error"} and is_music:


            keyword = self._extract_music_keyword(payload.text)


            action = "music_search" if self._looks_like_music_search_request(payload.text) else "music_play"


            return RouterDecision(


                should_handle=True,


                action=action,


                reason=f"{reason}_music_fallback",


                confidence=0.74,


                reply_style="short",


                tool_args={"keyword": keyword},


            )


        if reason in {"router_timeout", "router_parse_error"} and is_media:


            mode = "video" if is_video else "image"


            query = payload.text


            # BV/av 号自动补全为完整 URL


            bv_match = re.search(r"(BV\w{10})", query, flags=re.IGNORECASE)


            if bv_match and "bilibili.com" not in query.lower():


                query = f"https://www.bilibili.com/video/{bv_match.group(1)}"


                mode = "video"


            return RouterDecision(


                should_handle=True,


                action="search",


                reason=f"{reason}_media_fallback",


                confidence=0.72,


                reply_style="short",


                tool_args={"mode": mode, "query": query},


            )





        if self.failover_mode == "mention_or_private_only":


            if payload.mentioned or payload.is_private or self._looks_like_bot_call(payload.text):


                return RouterDecision(


                    should_handle=True,


                    action="reply",


                    reason=reason,


                    confidence=0.4,


                    reply_style="short",


                )


            # followup/active_session 中的消息也应该处理


            if payload.active_session or payload.followup_candidate:


                if self._is_passive_multimodal_text(payload.text):


                    return RouterDecision(


                        should_handle=True,


                        action="search",


                        reason=f"{reason}_active_session_multimodal",


                        confidence=0.65,


                        reply_style="casual",


                        tool_args={"method": "media.analyze_image", "method_args": {}},


                    )


                return RouterDecision(


                    should_handle=True,


                    action="reply",


                    reason=f"{reason}_active_session_fallback",


                    confidence=0.55,


                    reply_style="short",


                )


            return None


        if self.failover_mode == "always_ignore":


            return None


        return RouterDecision(


            should_handle=True,


            action="reply",


            reason=reason,


            confidence=0.3,


            reply_style="short",


        )





    def _normalize_decision_with_tool_policy(


        self,


        message: EngineMessage,


        trigger: Any,


        decision: RouterDecision,


        text: str,


    ) -> RouterDecision:


        _ = trigger


        action = normalize_text(str(decision.action)).lower()


        tool_args = decision.tool_args if isinstance(decision.tool_args, dict) else {}


        merged_text = normalize_text(f"{self._extract_multimodal_user_text(message.text)}\n{text}")


        changed = False


        new_tool_args = dict(tool_args)





        # 搜索动作至少补齐 query，避免空参数导致工具无法执行。


        if action == "search":


            if not normalize_text(str(new_tool_args.get("query", ""))) and not normalize_text(


                str(new_tool_args.get("method", ""))


            ):


                new_tool_args["query"] = merged_text or text


                changed = True





        forced_method, forced_method_args, forced_reason = self._infer_forced_tool_plan(


            message=message,


            text=merged_text or text,


        )


        if forced_method:


            current_method = normalize_text(str(new_tool_args.get("method", ""))).lower()


            if action != "search" or current_method != forced_method:


                next_args = dict(new_tool_args)


                next_args["method"] = forced_method


                next_args["method_args"] = forced_method_args


                if not normalize_text(str(next_args.get("query", ""))):


                    next_args["query"] = merged_text or text


                self.logger.info(


                    "decision_tool_override | trace=%s | 会话=%s | 用户=%s | from=%s | method=%s | reason=%s",


                    message.trace_id,


                    message.conversation_id,


                    message.user_id,


                    action or "unknown",


                    forced_method,


                    forced_reason,


                )


                return RouterDecision(


                    should_handle=True,


                    action="search",


                    reason=f"{normalize_text(decision.reason)}|{forced_reason}",


                    reason_code=getattr(decision, "reason_code", "") or forced_reason,


                    confidence=max(0.78, float(getattr(decision, "confidence", 0.0) or 0.0)),


                    reply_style=decision.reply_style,


                    tool_name=decision.tool_name,


                    tool_args=next_args,


                    target_user_id=decision.target_user_id,


                )





        if changed:


            return RouterDecision(


                should_handle=decision.should_handle,


                action=decision.action,


                reason=decision.reason,


                reason_code=getattr(decision, "reason_code", ""),


                confidence=decision.confidence,


                reply_style=decision.reply_style,


                tool_name=decision.tool_name,


                tool_args=new_tool_args,


                target_user_id=decision.target_user_id,


            )


        return decision





    def _infer_forced_tool_plan(self, message: EngineMessage, text: str) -> tuple[str, dict[str, Any], str]:


        _ = (message, text)


        return "", {}, ""





    async def _retry_tool_after_failure(


        self,


        message: EngineMessage,


        decision: RouterDecision,


        tool_result: Any,


        user_text: str,


    ) -> Any:


        if tool_result is None or bool(getattr(tool_result, "ok", False)):


            return tool_result


        if normalize_text(str(decision.action)).lower() != "search":


            return tool_result





        tool_args = decision.tool_args if isinstance(decision.tool_args, dict) else {}


        mode = normalize_text(str(tool_args.get("mode", ""))).lower()


        method_name = normalize_text(str(tool_args.get("method", ""))).lower()


        error = normalize_text(str(getattr(tool_result, "error", ""))).lower()


        merged_text = normalize_text(f"{self._extract_multimodal_user_text(message.text)}\n{user_text}")





        def _error_like(*patterns: str) -> bool:


            if not error:


                return False


            return any(error == item or error.startswith(f"{item}:") for item in patterns)





        attempts: list[tuple[str, dict[str, Any]]] = []





        if method_name == "browser.resolve_video" and _error_like(


            "video_resolve_failed",


            "video_detail_url_required",


            "unsupported_video_platform",


            "resolve_timeout",


        ):


            method_args = tool_args.get("method_args", {}) if isinstance(tool_args, dict) else {}


            if not isinstance(method_args, dict):


                method_args = {}


            explicit_url = normalize_text(str(method_args.get("url", ""))) or self._extract_first_video_url_from_text(


                merged_text


            )


            # 对"给定具体链接解析"的场景，不做跨平台搜索回退，避免发错视频来源。


            if not explicit_url:


                attempts.append(("fallback_video_search", {"mode": "video", "query": merged_text or user_text}))





        if method_name == "media.pick_video_from_message" and _error_like("message_video_not_found"):


            preferred_platform = "douyin.com" if re.search(r"(抖音|douyin)", merged_text, re.IGNORECASE) else ""


            cached_video_url = self._pick_recent_video_source_url(


                message=message,


                preferred_platform=preferred_platform,


            )


            if cached_video_url:


                attempts.append(


                    (


                        "fallback_resolve_video_from_cache",


                        {


                            "query": merged_text or user_text,


                            "method": "browser.resolve_video",


                            "method_args": {"url": cached_video_url},


                        },


                    )


                )





        if mode in {"video", "movie", "clip"} and _error_like(


            "video_result_unavailable",


            "video_result_duration_filtered",


            "video_resolve_failed",


        ):


            video_url = self._extract_first_video_url_from_text(merged_text)


            if video_url:


                attempts.append(


                    (


                        "fallback_resolve_video",


                        {


                            "query": merged_text or user_text,


                            "method": "browser.resolve_video",


                            "method_args": {"url": video_url},


                        },


                    )


                )


            has_video_segment = any(


                normalize_text(str((seg or {}).get("type", ""))).lower() == "video"


                for seg in (message.raw_segments or [])


                if isinstance(seg, dict)


            )


            if has_video_segment:


                attempts.append(


                    (


                        "fallback_pick_video_from_message",


                        {


                            "query": merged_text or user_text,


                            "method": "media.pick_video_from_message",


                            "method_args": {},


                        },


                    )


                )





        if method_name == "media.analyze_image" and _error_like(


            "image_not_found",


            "vision_analyze_failed",


            "vision_low_confidence",


        ):


            image_url = self._extract_first_image_url_from_text(merged_text)


            if image_url:


                attempts.append(


                    (


                        "fallback_analyze_image_url",


                        {


                            "query": merged_text or user_text,


                            "method": "media.analyze_image",


                            "method_args": {"url": image_url},


                        },


                    )


                )


            if _error_like("vision_analyze_failed", "vision_low_confidence"):


                search_query = self._build_vision_search_fallback_query(


                    merged_text=merged_text,


                    user_text=user_text,


                )


                if search_query:


                    attempts.append(


                        (


                            "fallback_web_search_after_vision_uncertain",


                            {


                                "query": search_query,


                                "mode": "text",


                            },


                        )


                    )





        if method_name == "browser.github_readme" and _error_like("github_repo_required", "github_repo_not_found"):


            attempts.append(


                (


                    "fallback_github_search",


                    {


                        "query": merged_text or user_text,


                        "method": "browser.github_search",


                        "method_args": {"query": merged_text or user_text},


                    },


                )


            )


        if method_name == "browser.github_search" and _error_like("github_search_failed"):


            repo = self._extract_github_repo_from_text(merged_text)


            if repo:


                attempts.append(


                    (


                        "fallback_github_readme",


                        {


                            "query": merged_text or user_text,


                            "method": "browser.github_readme",


                            "method_args": {"repo": repo},


                        },


                    )


                )





        if not attempts:


            return tool_result





        base_args_sig = normalize_text(repr(tool_args))


        for tag, attempt_args in attempts:


            if normalize_text(repr(attempt_args)) == base_args_sig:


                continue


            self.logger.info(


                "tool_retry_try | trace=%s | 会话=%s | 用户=%s | from=%s | to=%s | error=%s",


                message.trace_id,


                message.conversation_id,


                message.user_id,


                method_name or mode or "search",


                tag,


                error,


            )


            retry_result = await self.tools.execute(


                action="search",


                tool_name=decision.tool_name,


                tool_args=attempt_args,


                message_text=user_text,


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                user_name=message.user_name,


                group_id=message.group_id,


                api_call=message.api_call,


                raw_segments=message.raw_segments,


                bot_id=message.bot_id,


                trace_id=message.trace_id,


            )


            if retry_result is not None and bool(getattr(retry_result, "ok", False)):


                self.logger.info(


                    "tool_retry_ok | trace=%s | 会话=%s | 用户=%s | path=%s",


                    message.trace_id,


                    message.conversation_id,


                    message.user_id,


                    tag,


                )


                return retry_result


        return tool_result





    @staticmethod


    def _build_vision_search_fallback_query(merged_text: str, user_text: str) -> str:


        merged_clean = YukikoEngine._extract_multimodal_user_text(merged_text)


        user_clean = YukikoEngine._extract_multimodal_user_text(user_text)


        candidate = normalize_text(merged_clean) or normalize_text(user_clean)


        if not candidate:


            return ""


        if candidate.lower().startswith(("multimodal_event", "user sent multimodal")):


            return ""


        candidate = re.sub(r"https?://\S+", " ", candidate, flags=re.IGNORECASE)


        candidate = normalize_text(candidate)


        if not candidate:


            return ""


        has_explicit_lookup = YukikoEngine._has_control_token(candidate, "/lookup", "/search", "mode=lookup", "mode=search")


        has_question = "?" in candidate or "?" in candidate


        has_structured_reference = bool(YukikoEngine._extract_structured_reference_spans(candidate, max_terms=2))


        if not (has_explicit_lookup or has_question or has_structured_reference):


            return ""


        if re.fullmatch(r"[A-Za-z]{1,8}", candidate):


            return ""


        return candidate





    def _pick_recent_video_source_url(self, message: EngineMessage, preferred_platform: str = "") -> str:


        key = f"{message.conversation_id}:{message.user_id}"


        cached = self._recent_search_cache.get(key, {})


        if not isinstance(cached, dict):


            cached = {}


        evidence = cached.get("evidence", [])


        if not isinstance(evidence, list):


            evidence = []


        platform_hint = normalize_text(preferred_platform).lower()





        def _is_match(url: str) -> bool:


            target = normalize_text(url)


            if not target or not re.match(r"^https?://", target, flags=re.IGNORECASE):


                return False


            # 只接受看起来是视频详情/直链的 URL，避免把普通网页误当成视频来源。


            if not self._extract_first_video_url_from_text(target):


                return False


            if platform_hint:


                host = normalize_text(urlparse(target).netloc).lower()


                if platform_hint not in host:


                    return False


            return True





        for item in evidence:


            if not isinstance(item, dict):


                continue


            source = normalize_text(str(item.get("source", "")))


            if _is_match(source):


                return source





        full_text = normalize_text(str(cached.get("full_text", "")))


        if full_text:


            for found in re.findall(r"https?://\S+", full_text, flags=re.IGNORECASE):


                if _is_match(found):


                    return found





        # 兜底1: 同会话其他用户的最近 search 缓存


        conv_prefix = f"{message.conversation_id}:"


        for cache_key, cache_value in reversed(list(self._recent_search_cache.items())):


            if cache_key == key or not cache_key.startswith(conv_prefix):


                continue


            if not isinstance(cache_value, dict):


                continue


            rows = cache_value.get("evidence", [])


            if isinstance(rows, list):


                for item in rows:


                    if not isinstance(item, dict):


                        continue


                    source = normalize_text(str(item.get("source", "")))


                    if _is_match(source):


                        return source


            cache_text = normalize_text(str(cache_value.get("full_text", "")))


            if cache_text:


                for found in re.findall(r"https?://\S+", cache_text, flags=re.IGNORECASE):


                    if _is_match(found):


                        return found





        # 兜底2: 会话最近消息里提到过的视频 URL


        with contextlib.suppress(Exception):


            recent_messages = self.memory.get_recent_messages(message.conversation_id, limit=28)


            for row in reversed(recent_messages):


                content = normalize_text(str(getattr(row, "content", "")))


                if not content:


                    continue


                picked = self._extract_first_video_url_from_text(content)


                if picked and _is_match(picked):


                    return picked


        return ""





    def _self_check_decision(self, message: EngineMessage, trigger: Any, decision: RouterDecision) -> str:


        """本地自检：在 AI 判定后做一致性约束，降低误回与越界风险。"""


        if not self.self_check_enable:


            return ""





        action = normalize_text(str(decision.action)).lower()


        text_norm = normalize_text(message.text)


        confidence = float(decision.confidence)


        followup_active = bool(getattr(trigger, "followup_candidate", False)) or bool(


            getattr(trigger, "active_session", False)


        )


        all_segments: list[dict[str, Any]] = []


        for segment_group in (message.raw_segments, message.reply_media_segments):


            if not isinstance(segment_group, list):


                continue


            all_segments.extend(seg for seg in segment_group if isinstance(seg, dict))


        has_image_signal = any(


            normalize_text(str((seg or {}).get("type", ""))).lower() == "image"


            for seg in all_segments


        ) or bool(self._extract_first_image_url_from_text(text_norm))


        has_video_signal = any(


            normalize_text(str((seg or {}).get("type", ""))).lower() == "video"


            for seg in all_segments


        ) or bool(self._extract_first_video_url_from_text(text_norm))


        image_reference = bool(re.search(r"(这张图|历史图片|图里|图中|截图|照片)", text_norm))


        if action in {"ignore"}:


            return ""


        if normalize_text(str(getattr(decision, "reason_code", ""))).lower() == "followup_multimodal_fast_path":


            return ""





        # 明确工具型诉求不允许走纯 reply，防止"会说不会做"。


        if (


            action == "reply"


            and (


                (self._looks_like_image_analyze_intent(text_norm) and (has_image_signal or image_reference))


                or (self._looks_like_video_resolve_intent(text_norm) and has_video_signal)


                or (


                    self._looks_like_local_file_request(text_norm)


                    and bool(self._pick_local_path_candidate(text_norm))


                )


                or (


                    self._looks_like_github_request(text_norm)


                    and (self._looks_like_repo_readme_request(text_norm) or self._looks_like_explicit_request(text_norm))


                )


            )


        ):


            return "self_check:tool_required_for_request"





        if (


            action in {"reply", "search", "generate_image", "plugin_call"}


            and self._is_passive_multimodal_text(message.text)


            and not message.mentioned


            and not message.is_private


            and not self._has_recent_directed_hint(message)


            and not self._looks_like_bot_call(text_norm)


            and not self._looks_like_media_instruction(self._extract_multimodal_user_text(message.text))


        ):


            return "self_check:passive_multimodal_not_directed"





        # 多用户群聊中，若机器人刚回复过 A，B 在短时间内的非指向消息不能"接续 A 的上下文"。


        if (


            action in {"reply", "search", "generate_image", "plugin_call"}


            and self._is_cross_user_context_collision(message=message, trigger=trigger, text=text_norm)


        ):


            return "self_check:cross_user_context_isolated"





        # 群聊 followup 窗口内，非明确请求的闲聊不自动接话，避免连续"嗯嗯/确实"刷屏。


        if (


            action == "reply"


            and not message.mentioned


            and not message.is_private


            and (followup_active or bool(getattr(trigger, "active_session", False)))


            and int(getattr(trigger, "busy_users", 0) or 0) > 1


            and not self._looks_like_bot_call(text_norm)


            and not self._has_recent_directed_hint(message)


            and not self._looks_like_explicit_request(text_norm)


            and not self._looks_like_media_instruction(self._extract_multimodal_user_text(message.text))


            and len(text_norm) <= 36


        ):


            return "self_check:group_followup_chitchat"





        # 非指向群聊中的低信息短句（如“??/牛逼/笑死”）默认不接话，避免乱回复。


        if (


            action == "reply"


            and not message.mentioned


            and not message.is_private


            and not self._looks_like_bot_call(text_norm)


            and not self._has_recent_directed_hint(message)


            and not self._looks_like_explicit_request(text_norm)


            and not self._looks_like_media_instruction(self._extract_multimodal_user_text(message.text))


            and self._looks_like_low_info_group_chitchat(text_norm)


        ):


            return "self_check:undirected_group_chitchat"





        if self.self_check_block_at_other and message.at_other_user_only and not message.mentioned:


            if not self._allow_at_other_target_dialog(message, normalize_text(message.text)):


                return "self_check:at_other_not_for_bot"





        # 群聊非指向消息在多人场景默认更保守：


        # 未@、非私聊、非followup 且没有“明确叫bot”时，必须先通过 listen_probe 才可继续。


        if (


            action in {"reply", "search", "generate_image", "plugin_call"}


            and not message.mentioned


            and not message.is_private


            and not bool(getattr(trigger, "followup_candidate", False))


            and not bool(getattr(trigger, "active_session", False))


            and not self._looks_like_bot_call(text_norm)


            and not self._has_recent_directed_hint(message)


            and int(getattr(trigger, "busy_users", 0) or 0) > 1


            and not bool(getattr(trigger, "listen_probe", False))


        ):


            return "self_check:undirected_requires_listen_probe"





        # 监听探测阶段更保守：除非高置信，不主动介入。


        if (


            bool(getattr(trigger, "listen_probe", False))


            and not message.mentioned


            and not message.is_private


            and action in {"reply", "search", "generate_image", "plugin_call"}


            and int(getattr(trigger, "busy_users", 0) or 0) > 1


            and not self._looks_like_explicit_request(normalize_text(message.text))


            and confidence < self.self_check_listen_probe_min_confidence


        ):


            return "self_check:listen_probe_low_confidence"





        # 非指向场景默认不回，除非监听探测且达到更高置信阈值。


        if (


            action == "reply"


            and not message.mentioned


            and not message.is_private


            and not bool(getattr(trigger, "followup_candidate", False))


            and not bool(getattr(trigger, "active_session", False))


            and not self._looks_like_bot_call(text_norm)


            and not self._has_recent_directed_hint(message)


        ):


            listen_probe = bool(getattr(trigger, "listen_probe", False))


            # 阈值=0 表示关闭非指向自动接话（仅对白名单指向消息放行）。


            if (


                self.routing_zero_disables_undirected


                and self.non_directed_high_confidence_only


                and self.self_check_non_direct_reply_min_confidence <= 0.0


            ):


                return "self_check:not_directed_reply_threshold_disabled"


            if (not listen_probe) or confidence < self.self_check_non_direct_reply_min_confidence:


                return "self_check:not_directed_reply"





        # 非指向场景的普通回复必须更高置信，避免"偷摸插话"。


        if (


            action == "reply"


            and not message.mentioned


            and not message.is_private


            and not bool(getattr(trigger, "followup_candidate", False))


            and not bool(getattr(trigger, "active_session", False))


            and not self._has_recent_directed_hint(message)


            and float(decision.confidence) < self.self_check_non_direct_reply_min_confidence


        ):


            return "self_check:non_direct_reply_low_confidence"





        # 非指向场景的"工具型动作"更容易误接话：在多人群聊里要求更高置信或明确指向。


        if (


            action in {"search", "generate_image", "plugin_call"}


            and not message.mentioned


            and not message.is_private


            and not bool(getattr(trigger, "followup_candidate", False))


            and not bool(getattr(trigger, "active_session", False))


            and not self._has_recent_directed_hint(message)


            and int(getattr(trigger, "busy_users", 0) or 0) > 1


        ):


            if confidence < self.self_check_non_direct_reply_min_confidence:


                return "self_check:not_directed_action_low_confidence"





        # 搜索动作至少要有可执行线索（query 或 method）。


        if action == "search":


            tool_args = decision.tool_args if isinstance(decision.tool_args, dict) else {}


            query = normalize_text(str(tool_args.get("query", "")))


            method_name = normalize_text(str(tool_args.get("method", "")))


            if not query and not method_name and len(normalize_text(message.text)) <= 10:


                return "self_check:search_without_query"





        return ""





    def _looks_like_bot_call(self, text: str) -> bool:

        content = normalize_text(text).lower()

        if not content:

            return False

        aliases = self._get_bot_aliases()

        return any(alias in content for alias in aliases)



    def _is_bot_alias_only_message(self, text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        aliases = self._get_bot_aliases()


        if not aliases:


            return False


        cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", content)


        tokens = [tok for tok in cleaned.split() if tok]


        if not tokens:


            compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)


            return bool(compact) and compact in aliases


        return all(tok in aliases for tok in tokens)





    def _strip_edge_bot_alias_tokens(self, text: str) -> tuple[str, str]:


        """剥离消息首尾的机器人别名 token（用于内容语义，不影响触发判定）。"""


        content = normalize_text(text)


        if not content or self._is_bot_alias_only_message(content):


            return content, ""


        aliases = self._get_bot_aliases()


        if not aliases:


            return content, ""


        tokens = [tok for tok in re.split(r"[\s,，。!！?？:：;；、~`\"'|/\\<>@#]+", content) if tok]


        if len(tokens) <= 1:


            return content, ""





        lowered = [tok.lower() for tok in tokens]


        alias_token = ""


        changed = False





        while len(tokens) > 1 and lowered and lowered[0] in aliases:


            alias_token = lowered[0]


            tokens.pop(0)


            lowered.pop(0)


            changed = True


        while len(tokens) > 1 and lowered and lowered[-1] in aliases:


            alias_token = lowered[-1]


            tokens.pop()


            lowered.pop()


            changed = True





        if not changed:


            return content, ""


        stripped = normalize_text(" ".join(tokens))


        if not stripped:


            return content, ""


        return stripped, alias_token





    @staticmethod


    def _normalize_short_ping_phrase(text: str) -> str:


        content = normalize_text(text).lower()


        if not content:


            return ""


        content = re.sub(r"\s+", "", content)


        content = re.sub(r"[。！？!?，,、~…]+$", "", content)


        return content





    def _is_short_ping_message(self, text: str) -> bool:


        if not self.short_ping_phrases:


            return False


        normalized = self._normalize_short_ping_phrase(text)


        if not normalized:


            return False


        return normalized in self.short_ping_phrases





    def _get_bot_aliases(self) -> set[str]:


        aliases = {


            normalize_text(str(self.config.get("bot", {}).get("name", ""))).lower(),


        }


        for item in self.config.get("bot", {}).get("nicknames", []) or []:


            aliases.add(normalize_text(str(item)).lower())


        # 常用默认别名兜底，避免配置缺省时喊不醒。


        aliases.update({"yuki", "yukiko", "雪"})


        aliases.discard("")


        return aliases





    def _allow_at_other_target_dialog(self, message: EngineMessage, text: str) -> bool:


        """允许 @他人但仍在和机器人聊该人 的场景通过前置拦截。"""


        if message.mentioned or message.is_private:


            return True


        # 如果消息是明确回复另一个用户的（reply 引用），不放行


        # 这种情况用户大概率在跟那个人说话，不是跟 bot 说话


        reply_uid = str(message.reply_to_user_id or "").strip()


        bot_id = str(message.bot_id or "").strip()


        if reply_uid and reply_uid != bot_id:


            return False


        if self._looks_like_bot_call(text):


            return True


        # 最近刚回过同一用户，视为对话连续期，可容忍其 @某人后继续问机器人。


        if self._has_recent_reply_to_user(message, within_seconds=150):


            return True


        return False





    @staticmethod


    def _looks_like_explicit_request(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if "?" in content or "？" in content:


            return True


        if re.match(r"^[!/][a-z0-9_.:-]+", content, flags=re.IGNORECASE):


            return True


        return False





    @staticmethod


    def _has_control_token(text: str, *tokens: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        for token in tokens:


            token_norm = normalize_text(token).lower()


            if not token_norm:


                continue


            if re.search(rf"(?<![a-z0-9_]){re.escape(token_norm)}(?![a-z0-9_])", content):


                return True


        return False





    def _has_structural_media_locator(self, text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if self._is_passive_multimodal_text(text):


            return True


        if re.search(r"https?://[^\s]+", content):


            return True


        if re.search(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", content, flags=re.IGNORECASE):


            return True


        if re.search(r"\.(?:png|jpe?g|gif|webp|bmp|mp4|webm|mov|m4v|mp3|wav|flac|ogg)\b", content, flags=re.IGNORECASE):


            return True


        return self._has_control_token(content, "/image", "/img", "/video", "/music", "/audio", "/avatar")





    def _looks_like_media_instruction(self, text: str) -> bool:


        return self._has_structural_media_locator(text)





    def _has_recent_reply_to_user(self, message: EngineMessage, within_seconds: int = 120) -> bool:


        state = self._last_reply_state.get(message.conversation_id, {})


        if not isinstance(state, dict):


            return False


        last_uid = str(state.get("user_id", ""))


        if last_uid != str(message.user_id):


            return False


        ts = state.get("timestamp")


        if not isinstance(ts, datetime):


            return False


        try:


            return (message.timestamp - ts).total_seconds() <= max(10, int(within_seconds))


        except Exception:


            return False





    def _is_cross_user_context_collision(self, message: EngineMessage, trigger: Any, text: str) -> bool:


        if message.is_private or message.mentioned:


            return False


        if bool(getattr(trigger, "followup_candidate", False)) or bool(getattr(trigger, "active_session", False)):


            return False


        if self._looks_like_bot_call(text) or self._has_recent_directed_hint(message):


            return False





        state = self._last_reply_state.get(message.conversation_id, {})


        if not isinstance(state, dict):


            return False


        last_uid = str(state.get("user_id", ""))


        if not last_uid or last_uid == str(message.user_id):


            return False


        last_ts = state.get("timestamp")


        if not isinstance(last_ts, datetime):


            return False





        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        try:


            age_seconds = (now - last_ts).total_seconds()


        except Exception:


            return False


        if age_seconds > float(self.self_check_cross_user_guard_seconds):


            return False





        # 跨用户隔离窗口内，仅允许明显"在叫机器人"的句子继续进入。


        return True





    def _track_directed_hint(self, message: EngineMessage, text: str) -> None:


        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        self._cleanup_directed_hints(now)


        if message.mentioned or message.is_private or self._looks_like_bot_call(text):


            key = f"{message.conversation_id}:{message.user_id}"


            self._recent_directed_hints[key] = now





    def _has_recent_directed_hint(self, message: EngineMessage) -> bool:


        key = f"{message.conversation_id}:{message.user_id}"


        ts = self._recent_directed_hints.get(key)


        if not isinstance(ts, datetime):


            return False


        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        try:


            return (now - ts).total_seconds() <= self.directed_grace_seconds


        except Exception:


            return False





    def _cleanup_directed_hints(self, now: datetime) -> None:


        if not self._recent_directed_hints:


            return


        expire_seconds = max(10, self.directed_grace_seconds * 2)


        stale: list[str] = []


        for key, ts in self._recent_directed_hints.items():


            if not isinstance(ts, datetime):


                stale.append(key)


                continue


            try:


                age = (now - ts).total_seconds()


            except Exception:


                age = expire_seconds + 1


            if age > expire_seconds:


                stale.append(key)


        for key in stale:


            self._recent_directed_hints.pop(key, None)





        # 顺便清理其他无上限缓存


        if len(self._last_reply_state) > 200:


            # 保留最近 100 个


            keys = list(self._last_reply_state.keys())


            for k in keys[:-100]:


                self._last_reply_state.pop(k, None)


        if len(self._recent_search_cache) > 100:


            self._recent_search_cache.clear()





    def should_interrupt_previous_task(


        self,


        *,


        message: EngineMessage,


        previous_user_id: str = "",


        previous_text: str = "",


        pending_count: int = 0,


        high_priority: bool = False,


        reply_to_bot: bool = False,


    ) -> tuple[bool, str]:


        if not self.smart_interrupt_enable:


            return False, "smart_interrupt_disabled"


        if int(pending_count or 0) < self.smart_interrupt_min_pending:


            return False, "pending_below_threshold"





        content = normalize_text(message.text)


        if not content:


            return False, "empty_text"


        prev_uid = normalize_text(previous_user_id)


        if not prev_uid:


            return False, "no_previous_context"





        # 消息明确在和其他人对话时，不打断队列中的任务。


        if message.at_other_user_only and not message.mentioned and not reply_to_bot:


            return False, "at_other_user_context"





        directed = bool(


            high_priority


            or message.mentioned


            or message.is_private


            or reply_to_bot


            or self._looks_like_bot_call(content)


            or self._has_recent_directed_hint(message)


        )


        task_like = bool(


            self._looks_like_explicit_request(content)


            or self._looks_like_music_request(content)


            or self._looks_like_download_task_intent(content)


            or self._looks_like_video_request(content)


            or self._looks_like_media_request(content)


        )


        low_info = self._looks_like_low_info_group_chitchat(content)


        same_user = prev_uid == str(message.user_id)





        if same_user:


            if not self.smart_interrupt_same_user_enable:


                return False, "same_user_interrupt_disabled"


            if low_info and not directed:


                return False, "same_user_low_info"


            if task_like or directed:


                if normalize_text(previous_text) == content:


                    return False, "same_user_duplicate_task"


                return True, "same_user_new_task"


            return False, "same_user_non_task"





        if not self.smart_interrupt_cross_user_enable:


            return False, "cross_user_interrupt_disabled"


        if self.smart_interrupt_require_directed and not directed:


            return False, "cross_user_not_directed"


        if low_info and not high_priority:


            return False, "cross_user_low_info"


        if not task_like and not high_priority:


            return False, "cross_user_not_task_like"


        return True, "cross_user_task_interrupt"





    @staticmethod


    def _looks_like_media_request(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if re.search(r"https?://[^\s]+", content):


            return True


        # BV/av 号识别


        if re.search(r"(?:bv|av)\w{6,}", content, flags=re.IGNORECASE):


            return True


        return False





    def _looks_like_video_request(self, text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if re.search(r"https?://[^\s]+", content):


            return True


        if re.search(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", content, flags=re.IGNORECASE):


            return True


        return bool(re.search(r"\.(mp4|webm|mov|m4v)\b", content))





    def _looks_like_image_analyze_intent(self, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        return self._has_structural_media_locator(text) and (


            self._has_control_token(text, "/analyze", "mode=analyze", "ocr=true")


            or "?" in content


            or "？" in content


        )





    def _looks_like_video_resolve_intent(self, text: str) -> bool:


        return self._looks_like_video_request(text) and self._has_control_token(


            text,


            "/send",


            "/resolve",


            "mode=send",


            "mode=resolve",


        )





    def _looks_like_video_analysis_intent(self, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        return self._looks_like_video_request(text) and (


            self._has_control_token(text, "/analyze", "mode=analyze", "output=text", "mode=text")


            or "?" in content


            or "？" in content


        )





    @staticmethod


    def _looks_like_low_info_group_chitchat(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return True


        compact = re.sub(r"\s+", "", content)


        if not compact:


            return True


        if re.fullmatch(r"[?？!！。./\\,，:：;；~～\-_=+*'\"`·…]{1,12}", compact):


            return True


        return len(compact) <= 2





    @staticmethod


    def _looks_like_video_text_only_intent(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "output=text", "mode=text", "text-only", "/text")





    @staticmethod


    def _looks_like_download_task_intent(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        return bool(re.search(r"\.(exe|apk|ipa|msi|zip|rar|7z)\b", content))





    @staticmethod


    def _looks_like_music_request(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if re.search(r"https?://[^\s]+", content):


            return True


        if re.search(r"\.(?:mp3|wav|flac|ogg|m4a|aac)\b", content, flags=re.IGNORECASE):


            return True


        return YukikoEngine._has_control_token(text, "/music", "/song", "mode=music")





    @staticmethod


    def _looks_like_music_search_request(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if not YukikoEngine._looks_like_music_request(content):


            return False


        return YukikoEngine._has_control_token(text, "/search", "mode=search")





    @staticmethod


    def _extract_music_keyword(text: str) -> str:


        content = normalize_text(text)


        if not content:


            return ""


        content = re.sub(r"^@\S+\s*", "", content)


        content = re.sub(r"(?i)(?<!\S)/(?:music|song|search)\b", " ", content)


        content = re.sub(r"(?i)\b(?:mode|type|platform|source|output|target|title|artist|id)=[^\s]+", " ", content)


        content = re.sub(r"\s+", " ", content).strip("`\"'[](){}<>.,;:!?\uFF0C\u3002\uFF1F\uFF01\uFF1A")


        return content





    @staticmethod


    def _build_music_match_tokens(keyword: str) -> list[str]:


        content = normalize_text(keyword).lower()


        if not content:


            return []





        out: list[str] = []


        seen: set[str] = set()


        for token in tokenize(content):


            value = normalize_text(token).lower().strip()


            if not value or value.startswith("/") or "=" in value:


                continue


            if re.search(r"[a-z0-9]", value):


                compact = re.sub(r"[^a-z0-9_.-]+", "", value)


                if len(compact) < 2:


                    continue


                value = compact


            elif re.fullmatch(r"[\u4e00-\u9fff]+", value):


                if len(value) < 2:


                    continue


            else:


                continue


            if value in seen:


                continue


            seen.add(value)


            out.append(value)


            if len(out) >= 6:


                break


        return out





    @classmethod


    def _is_music_fallback_relevant(cls, keyword: str, payload: dict[str, Any]) -> bool:


        tokens = cls._build_music_match_tokens(keyword)


        if not tokens:


            return False


        if not isinstance(payload, dict):


            return False


        corpus_parts: list[str] = [


            normalize_text(str(payload.get("video_url", ""))),


            normalize_text(str(payload.get("text", ""))),


        ]


        rows = payload.get("results", [])


        if isinstance(rows, list) and rows:


            first = rows[0]


            if isinstance(first, dict):


                corpus_parts.extend(


                    [


                        normalize_text(str(first.get("title", ""))),


                        normalize_text(str(first.get("snippet", ""))),


                        normalize_text(str(first.get("url", ""))),


                    ]


                )


        corpus = normalize_text("\n".join(corpus_parts)).lower()


        if not corpus:


            return False


        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", corpus)


        hit = 0


        for token in tokens:


            t = normalize_text(token).lower()


            if not t:


                continue


            if t in corpus:


                hit += 1


                continue


            compact_t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t)


            if compact_t and compact_t in compact:


                hit += 1


        required = len(tokens) if len(tokens) <= 2 else 2


        return hit >= required





    def _looks_like_github_request(self, text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        if "github.com/" in content:


            return True


        if self._has_control_token(text, "/github", "platform=github", "source=github"):


            return True


        return bool(re.search(r"\b[a-z0-9_.-]+/[a-z0-9_.-]+\b", content))





    def _looks_like_repo_readme_request(self, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if re.search(r"\bREADME(?:\.md)?\b", content, flags=re.IGNORECASE):


            return True


        return self._has_control_token(text, "/readme", "type=readme", "target=readme")





    @staticmethod


    def _extract_github_repo_from_text(text: str) -> str:


        content = normalize_text(text)


        if not content:


            return ""


        match = re.search(


            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",


            content,


            flags=re.IGNORECASE,


        )


        if not match:


            return ""


        owner = match.group(1)


        repo = re.sub(r"\.git$", "", match.group(2), flags=re.IGNORECASE)


        return f"{owner}/{repo}"





    def _looks_like_qq_avatar_intent(self, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if not self._has_control_token(text, "/avatar", "/qqavatar", "type=avatar"):


            return False


        return bool(re.search(r"(?<!\d)[1-9]\d{5,11}(?!\d)", content) or self._has_control_token(text, "target=self", "/me"))





    def _looks_like_qq_profile_intent(self, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if not self._has_control_token(text, "/qqprofile", "/profile", "type=qq-profile"):


            return False


        return bool(re.search(r"(?<!\d)[1-9]\d{5,11}(?!\d)", content) or self._has_control_token(text, "target=self", "/me"))





    @staticmethod


    def _extract_candidate_qq_target(message: EngineMessage, text: str) -> str:


        bot_id = normalize_text(str(message.bot_id))


        for seg in (message.raw_segments or []):


            if not isinstance(seg, dict):


                continue


            if normalize_text(str(seg.get("type", ""))).lower() != "at":


                continue


            data = seg.get("data", {}) or {}


            qq = normalize_text(str(data.get("qq", "")))


            if qq and qq != bot_id and re.fullmatch(r"[1-9]\d{5,11}", qq):


                return qq





        reply_uid = normalize_text(str(message.reply_to_user_id))


        if reply_uid and reply_uid != bot_id and re.fullmatch(r"[1-9]\d{5,11}", reply_uid):


            return reply_uid





        for uid in (message.at_other_user_ids or []):


            qq = normalize_text(str(uid))


            if qq and qq != bot_id and re.fullmatch(r"[1-9]\d{5,11}", qq):


                return qq





        match = re.search(r"(?<!\d)([1-9]\d{5,11})(?!\d)", normalize_text(text))


        if match:


            return match.group(1)


        return ""





    @staticmethod


    def _extract_local_path_candidates(text: str) -> list[str]:


        content = normalize_text(text)


        if not content:


            return []


        patterns = (


            r"[A-Za-z]:\\[^\s\"'<>|?*]+",


            r"(?:\./|\.\./|/)[^\s\"'<>|?*]+",


            r"(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}",


            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",


        )


        out: list[str] = []


        seen: set[str] = set()


        for pattern in patterns:


            for raw in re.findall(pattern, content):


                candidate = normalize_text(str(raw)).strip().rstrip("，。！？!?,.;:)]}")


                if not candidate:


                    continue


                lower = candidate.lower()


                if lower.startswith("http://") or lower.startswith("https://"):


                    continue


                if candidate in seen:


                    continue


                seen.add(candidate)


                out.append(candidate)


        return out





    @classmethod


    def _pick_local_path_candidate(cls, text: str) -> str:


        rows = cls._extract_local_path_candidates(text)


        if not rows:


            return ""


        scored: list[tuple[int, str]] = []


        for item in rows:


            score = 0


            if re.search(r"\.[A-Za-z0-9]{1,10}$", item):


                score += 4


            if any(


                cue in item


                for cue in ("core/", "core\\", "docs/", "docs\\", "config/", "config\\", "storage/", "storage\\")


            ):


                score += 2


            if item.startswith(("./", "../", "/", "core/", "docs/", "config/", "storage/")):


                score += 1


            if re.match(r"^[A-Za-z]:\\", item):


                score += 2


            if item.startswith("/") and any(other != item and other.endswith(item) for other in rows):


                score -= 3


            scored.append((score, item))


        scored.sort(key=lambda it: it[0], reverse=True)


        return scored[0][1] if scored else ""





    @staticmethod


    @staticmethod

    @staticmethod
    def _looks_like_local_file_request(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        has_local_path = bool(re.search(r"(?:[A-Za-z]:\\|\\\\|(?:^|\s)(?:\./|\.\./|/))", content))
        has_file_ext = bool(
            re.search(r"\.(?:zip|7z|rar|exe|apk|ipa|msi|pdf|docx?|xlsx?|pptx?|txt|mp3|mp4)\b", content, flags=re.IGNORECASE)
        )
        has_control = YukikoEngine._has_control_token(
            text,
            "/upload",
            "/download",
            "/file",
            "mode=file",
            "mode=upload",
            "mode=download",
            "output=file",
        )
        return has_control and (has_local_path or has_file_ext)

    def _looks_like_local_media_request(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        has_local_path = bool(re.search(r"(?:[A-Za-z]:\\|\\\\|/)", content))


        has_media_ext = bool(re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|webm|mov|m4v|mp3|wav|flac|ogg)\b", content, flags=re.IGNORECASE))


        return has_local_path and has_media_ext





    @staticmethod


    def _looks_like_local_media_path(path: str) -> bool:


        value = normalize_text(path).lower()


        if not value:


            return False


        return bool(re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|webm|mov|m4v)$", value))





    @staticmethod


    def _extract_urls_from_text(text: str) -> list[str]:


        content = normalize_text(text)


        if not content:


            return []


        urls = re.findall(


            r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",


            content,


            flags=re.IGNORECASE,


        )


        out: list[str] = []


        seen: set[str] = set()


        for item in urls:


            value = normalize_text(item).rstrip("，。！？!?,.;:)")


            if not value or value in seen:


                continue


            seen.add(value)


            out.append(value)


        return out





    @staticmethod


    def _extract_first_image_url_from_text(text: str) -> str:


        urls = YukikoEngine._extract_urls_from_text(text)


        for url in urls:


            lower = url.lower()


            if re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", lower):


                return url


            if "multimedia.nt.qq.com.cn" in lower:


                return url


        return ""





    @staticmethod


    def _extract_first_video_url_from_text(text: str) -> str:


        content = normalize_text(text)


        urls = YukikoEngine._extract_urls_from_text(content)


        for url in urls:


            lower = url.lower()


            if re.search(r"\.(?:mp4|webm|mov|m4v)(?:\?|$)", lower):


                return url


            if any(host in lower for host in ("bilibili.com/video/", "b23.tv/", "douyin.com/", "kuaishou.com/", "acfun.cn/v/ac")):


                return url


        bv_match = re.search(r"\b(BV[0-9A-Za-z]{10})\b", content, flags=re.IGNORECASE)


        if bv_match:


            return f"https://www.bilibili.com/video/{bv_match.group(1)}"


        return ""





    @staticmethod


    def _is_passive_multimodal_text(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if re.fullmatch(


            r"(?:\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]\s*)+",


            content,


            flags=re.IGNORECASE,


        ):


            return True


        return (


            content.startswith("MULTIMODAL_EVENT")


            or content.startswith("用户发送多模态消息：")


            or content.startswith("用户@了你并发送多模态消息：")


            or content.lower().startswith("user sent multimodal message:")


            or content.lower().startswith("user mentioned bot and sent multimodal message:")


        )





    @staticmethod


    def _extract_multimodal_user_text(text: str) -> str:


        content = normalize_text(text)


        if not content:


            return ""


        content = re.sub(r"\bMULTIMODAL_EVENT(?:_AT)?\b", " ", content, flags=re.IGNORECASE)


        content = content.replace("用户发送多模态消息：", " ").replace("用户@了你并发送多模态消息：", " ")


        content = content.replace("user sent multimodal message:", " ").replace(


            "user mentioned bot and sent multimodal message:",


            " ",


        )


        content = re.sub(


            r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]",


            " ",


            content,


            flags=re.IGNORECASE,


        )


        content = re.sub(r"\b(?:image|video|record|audio|forward)\s*:\s*\S+", " ", content, flags=re.IGNORECASE)


        content = normalize_text(content)


        parts = content.split()


        while parts and not re.search(r"[A-Za-z0-9一-龥]", parts[0]):


            parts.pop(0)


        return normalize_text(" ".join(parts))





    async def _run_plugin(self, name: str, message: str, context: dict[str, Any]) -> str:


        return await self.plugins.call(name, message, context)





    @staticmethod


    def _is_explicitly_replying_other_user(message: EngineMessage) -> bool:


        bot_id = str(message.bot_id or "").strip()


        if not bot_id:


            return bool(message.at_other_user_only)





        reply_uid = str(message.reply_to_user_id or "").strip()


        if reply_uid and reply_uid != bot_id:


            return True





        for seg in message.raw_segments or []:


            if not isinstance(seg, dict):


                continue


            seg_type = str(seg.get("type", "")).strip().lower()


            if seg_type != "at":


                continue


            data = seg.get("data", {}) or {}


            qq = str(data.get("qq") or data.get("user_id") or data.get("uid") or "").strip()


            if qq and qq not in {bot_id, "all"}:


                return True


        return bool(message.at_other_user_only)





    @staticmethod


    def _build_recent_user_lines(recent_messages: list[Any], limit: int = 12) -> list[str]:


        lines: list[str] = []


        for item in recent_messages[-max(1, limit) :]:


            if str(getattr(item, "role", "")) != "user":


                continue


            content = normalize_text(str(getattr(item, "content", "")))


            if not content:


                continue


            user_name = normalize_text(str(getattr(item, "user_name", "")))


            user_id = str(getattr(item, "user_id", ""))


            lines.append(f"{user_name or user_id or '用户'}: {clip_text(content, 80)}")


        return lines





    @staticmethod


    def _build_recent_bot_reply_lines(recent_messages: list[Any], limit: int = 2) -> list[str]:


        lines: list[str] = []


        for item in reversed(recent_messages):


            if str(getattr(item, "role", "")) != "assistant":


                continue


            content = normalize_text(str(getattr(item, "content", "")))


            if not content:


                continue


            lines.append(clip_text(content, 120))


            if len(lines) >= max(1, limit):


                break


        lines.reverse()


        return lines





    @staticmethod


    def _build_recent_user_lines_by_user_id(recent_messages: list[Any], user_id: str, limit: int = 6) -> list[str]:


        uid = normalize_text(str(user_id))


        if not uid:


            return []


        lines: list[str] = []


        for item in reversed(recent_messages):


            if str(getattr(item, "role", "")) != "user":


                continue


            row_uid = normalize_text(str(getattr(item, "user_id", "")))


            if row_uid != uid:


                continue


            content = normalize_text(str(getattr(item, "content", "")))


            if not content:


                continue


            user_name = normalize_text(str(getattr(item, "user_name", "")))


            lines.append(f"{user_name or row_uid}: {clip_text(content, 80)}")


            if len(lines) >= max(1, limit):


                break


        lines.reverse()


        return lines





    @staticmethod


    @staticmethod


    def _build_media_summary(raw_segments: list[dict[str, Any]], limit: int = 8) -> list[str]:


        items: list[str] = []


        for seg in raw_segments or []:


            if not isinstance(seg, dict):


                continue


            seg_type = normalize_text(str(seg.get("type", ""))).lower()


            if not seg_type:


                continue


            data = seg.get("data", {}) or {}


            if seg_type in {"text", "at", "reply"}:


                continue


            if seg_type == "image":


                url = normalize_text(str(data.get("url", "")))
                data_uri = normalize_text(str(data.get("memory_data_uri", "")))


                if data_uri.startswith("data:image"):
                    items.append(f"image:base64:{clip_text(data_uri, 80)}")
                else:
                    items.append(f"image:{clip_text(url or 'no_url', 80)}")


            elif seg_type == "video":


                url = normalize_text(str(data.get("url", "")))


                items.append(f"video:{clip_text(url or 'no_url', 80)}")


            elif seg_type in {"record", "audio"}:


                url = normalize_text(str(data.get("url", "")))


                items.append(f"audio:{clip_text(url or 'no_url', 80)}")


            elif seg_type == "forward":


                items.append("forward:message")


            else:


                items.append(seg_type)


            if len(items) >= max(1, limit):


                break


        return items





    async def _resolve_at_user_names(


        self,


        user_ids: list[str],


        api_call: Callable[..., Awaitable[Any]] | None,


    ) -> dict[str, str]:


        """解析 @提及用户的昵称，返回 {qq_id: name} 映射。"""


        names: dict[str, str] = {}


        if not user_ids:


            return names


        for uid in user_ids[:5]:


            # 优先从 memory 中获取


            display = self.memory.get_display_name(uid) if hasattr(self.memory, "get_display_name") else ""


            if display:


                names[uid] = display


                continue


            # 回退到 API 查询


            if api_call:


                try:


                    info = await api_call("get_stranger_info", user_id=int(uid))


                    if isinstance(info, dict):


                        nick = normalize_text(str(info.get("nickname", "")))


                        if nick:


                            names[uid] = nick


                except Exception:


                    pass


        return names





    def _get_stream_callback(self, conversation_id: str) -> Any:
        """获取当前会话的流式回调队列（如果存在）。"""
        bridge = getattr(self, "_runtime_webui_bridge", None) or {}
        callbacks = bridge.get("stream_callbacks", {})
        return callbacks.get(conversation_id)

    def _index_message_media(self, message_id: str, raw_segments: list[dict[str, Any]]) -> None:


        """将消息中的媒体建立 artifact 索引: message_id -> [media_refs]。"""


        if not message_id:


            return


        refs: list[dict[str, str]] = []


        for seg in raw_segments or []:


            if not isinstance(seg, dict):


                continue


            seg_type = normalize_text(str(seg.get("type", ""))).lower()


            if seg_type not in ("image", "video", "record", "audio"):


                continue


            data = seg.get("data", {}) or {}


            url = normalize_text(str(data.get("url", "")))
            data_uri = normalize_text(str(data.get("memory_data_uri", "")))


            file_id = normalize_text(str(data.get("file", "") or data.get("file_id", "")))


            refs.append({"type": seg_type, "url": url, "file_id": file_id, "data_uri": data_uri})


        if refs:


            self._media_artifact_index[message_id] = refs


            # 限制大小


            while len(self._media_artifact_index) > self._media_artifact_index_max:


                self._media_artifact_index.popitem(last=False)





    def _get_message_media_refs(self, message_id: str) -> list[dict[str, str]]:


        """查询消息的媒体 artifact 列表。"""


        return self._media_artifact_index.get(message_id, [])

    async def _resolve_memory_image_data_uri(
        self,
        seg_data: dict[str, Any],
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> str:
        existing = normalize_text(str(seg_data.get("memory_data_uri", "")))
        if existing.startswith("data:image"):
            return existing
        tools = getattr(self, "tools", None)
        if tools is None:
            return ""

        file_id = normalize_text(str(seg_data.get("file", "") or seg_data.get("file_id", "")))
        if file_id and api_call and hasattr(tools, "_data_uri_from_onebot_image_file"):
            try:
                data_uri = await asyncio.wait_for(
                    tools._data_uri_from_onebot_image_file(image_file=file_id, api_call=api_call),
                    timeout=self._memory_media_capture_timeout_seconds,
                )
            except Exception:
                data_uri = ""
            if normalize_text(data_uri).startswith("data:image"):
                return normalize_text(data_uri)

        url = normalize_text(str(seg_data.get("url", "")))
        if url and hasattr(tools, "_prepare_vision_image_ref"):
            try:
                prepared = await asyncio.wait_for(
                    tools._prepare_vision_image_ref(url),
                    timeout=self._memory_media_capture_timeout_seconds,
                )
            except Exception:
                prepared = ""
            prepared_norm = normalize_text(str(prepared))
            if prepared_norm.startswith("data:image"):
                return prepared_norm
        return ""

    async def _capture_media_memory_for_segments(
        self,
        *,
        conversation_id: str,
        message_id: str,
        user_id: str,
        source: str,
        segments: list[dict[str, Any]],
        api_call: Callable[..., Awaitable[Any]] | None,
        timestamp: datetime,
    ) -> int:
        if not self._memory_media_capture_enable:
            return 0
        if not segments:
            return 0
        media_rows: list[dict[str, Any]] = []
        image_count = 0
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            if seg_type not in {"image", "video", "record", "audio"}:
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            url = normalize_text(str(data.get("url", "")))
            file_id = normalize_text(str(data.get("file", "") or data.get("file_id", "")))
            data_uri = normalize_text(str(data.get("memory_data_uri", "")))
            if seg_type == "image":
                if image_count >= self._memory_media_max_images_per_message:
                    continue
                image_count += 1
                if not data_uri:
                    data_uri = await self._resolve_memory_image_data_uri(seg_data=data, api_call=api_call)
                    if data_uri:
                        data["memory_data_uri"] = data_uri
                        seg["data"] = data
            if not data_uri and not url and not file_id:
                continue
            media_rows.append(
                {
                    "type": seg_type,
                    "data_uri": data_uri,
                    "url": url,
                    "file_id": file_id,
                }
            )
        if not media_rows:
            return 0
        if hasattr(self.memory, "add_media_artifacts"):
            self.memory.add_media_artifacts(
                conversation_id=conversation_id,
                message_id=message_id,
                user_id=user_id,
                source=source,
                media_items=media_rows,
                timestamp=timestamp,
            )
        return len(media_rows)

    async def _remember_message_media_memory(self, message: EngineMessage) -> None:
        if not self._memory_media_capture_enable:
            return
        if not hasattr(self.memory, "add_media_artifacts"):
            return
        captured = 0
        if message.message_id and message.raw_segments:
            added = await self._capture_media_memory_for_segments(
                conversation_id=message.conversation_id,
                message_id=message.message_id,
                user_id=message.user_id,
                source="message",
                segments=message.raw_segments,
                api_call=message.api_call,
                timestamp=message.timestamp,
            )
            captured += added
            if added > 0:
                self._index_message_media(message.message_id, message.raw_segments)
        if message.reply_to_message_id and message.reply_media_segments:
            added = await self._capture_media_memory_for_segments(
                conversation_id=message.conversation_id,
                message_id=message.reply_to_message_id,
                user_id=message.reply_to_user_id or message.user_id,
                source="reply",
                segments=message.reply_media_segments,
                api_call=message.api_call,
                timestamp=message.timestamp,
            )
            captured += added
            if added > 0:
                self._index_message_media(message.reply_to_message_id, message.reply_media_segments)
        if captured > 0:
            self.logger.info(
                "memory_media_captured | conversation=%s | message=%s | reply=%s | records=%d",
                message.conversation_id,
                message.message_id or "-",
                message.reply_to_message_id or "-",
                captured,
            )

    @staticmethod
    def _build_reply_media_memory_lines(media_items: list[dict[str, str]], limit: int = 2) -> list[str]:
        rows = media_items[-max(1, limit) :]
        if not rows:
            return []
        lines: list[str] = [f"[引用图片记忆] 已缓存 {len(media_items)} 张图片(base64)"]
        for idx, item in enumerate(rows, start=1):
            data_uri = normalize_text(str(item.get("data_uri", "")))
            url = normalize_text(str(item.get("url", "")))
            if data_uri.startswith("data:image"):
                lines.append(f"[引用图片base64#{idx}] {clip_text(data_uri, 96)}")
            elif url:
                lines.append(f"[引用图片URL#{idx}] {clip_text(url, 96)}")
        return lines





    def _record_runtime_group_chat(self, message: EngineMessage, text: str) -> None:


        if message.is_private:


            return


        if self.admin.enabled and not self.admin.is_group_whitelisted(message.group_id):


            return


        content = normalize_text(text)


        if not content:


            return


        line = f"{message.user_name or message.user_id}: {clip_text(content, 88)}"


        cache = self._runtime_group_chat_cache[message.conversation_id]


        cache.append(line)





    def _build_runtime_group_context(self, conversation_id: str, limit: int = 10) -> list[str]:


        cache = self._runtime_group_chat_cache.get(conversation_id)


        if not cache:


            return []


        rows = [normalize_text(item) for item in list(cache)[-max(1, limit):]]


        return [item for item in rows if item]





    async def _ai_error_reply(


        self,


        user_text: str,


        error_context: str,


        memory_context: list[str] | None = None,


        user_profile_summary: str = "",


        trigger_reason: str = "",


        scene_hint: str = "error_recovery",


    ) -> str:


        """用 AI 生成错误/失败场景的回复，而不是硬编码文案。





        把错误上下文作为 search_summary 传给 thinking，让 AI 结合用户原文


        和错误信息生成自然的回复。LLM 也挂了就返回空字符串。


        """


        if not self.model_client.enabled:


            return ""


        try:


            reply = await asyncio.wait_for(


                self.thinking.generate_reply(


                    user_text=user_text,


                    memory_context=memory_context or [],


                    related_memories=[],


                    reply_style="short",


                    search_summary=error_context,


                    sensitive_context="",


                    user_profile_summary=user_profile_summary,


                    trigger_reason=trigger_reason,


                    scene_hint=scene_hint,


                    verbosity="brief",


                ),


                timeout=15,


            )


            return normalize_text(reply)


        except Exception as exc:


            self.logger.debug("ai_error_reply_fail | %s", exc)


            return ""





    def _sanitize_reply_output(self, text: str, action: str = "") -> str:


        try:


            content = str(text or "")


            leaked_call_markup = bool(


                re.search(r"</?\s*function_calls?\b|<\s*invoke\b|<\s*parameter\b", content, flags=re.IGNORECASE)


            )





            # 剥离 <thinking>...</thinking> 块（LLM 内部思考，不应发给用户）


            content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL | re.IGNORECASE)


            content = re.sub(r"</?thinking>", "", content, flags=re.IGNORECASE)





            # 剥离 <tool_call>...</tool_call> 块


            content = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL | re.IGNORECASE)


            content = re.sub(r"</?tool_call>", "", content, flags=re.IGNORECASE)





            content = re.sub(r"</?search_web>", "", content, flags=re.IGNORECASE)


            content = re.sub(r"<\s*tool[^>]*>", "", content, flags=re.IGNORECASE)


            content = re.sub(r"</\s*tool\s*>", "", content, flags=re.IGNORECASE)


            # 剥离 XML 风格函数调用块（某些模型会错误地把函数调用文本泄漏到最终回复）


            content = re.sub(r"<function_calls?>.*?</function_calls?>", "", content, flags=re.DOTALL | re.IGNORECASE)


            content = re.sub(r"<invoke\b[^>]*>.*?</invoke>", "", content, flags=re.DOTALL | re.IGNORECASE)


            content = re.sub(r"<parameter\b[^>]*>.*?</parameter>", "", content, flags=re.DOTALL | re.IGNORECASE)


            content = re.sub(r"</?\s*(function_calls?|invoke|parameter)\b[^>]*>", "", content, flags=re.IGNORECASE)


            # 兜底：若输出了半截函数标签，直接从标签处截断。


            content = re.sub(r"<\s*(function_calls?|invoke|parameter)\b[\s\S]*$", "", content, flags=re.IGNORECASE)





            # 剥离 ```json {"name":"tool_name",...} ``` 格式的内嵌 tool call


            content = re.sub(


                r"```(?:json)?\s*\{[^}]*\"name\"\s*:\s*\"(?:think|final_answer|analyze_image|search_web|fetch_url)[^}]*\}[^`]*```",


                "", content, flags=re.DOTALL,


            )


            # 剥离裸露的 {"name":"tool_name",...} JSON tool call（非 code block）


            content = re.sub(


                r'\{\s*"name"\s*:\s*"(?:think|final_answer|analyze_image|search_web|fetch_url)"[^}]*\}',


                "", content, flags=re.DOTALL,


            )





            content = re.sub(r"\n{3,}", "\n\n", content)


            # 去掉尾部不完整标签（如 "<p"）


            content = re.sub(r"<[^>\n]*$", "", content)


            content = content.strip()





            # 仅按配置移除禁用短语，不再内置硬编码口头禅过滤规则。


            removed_template = False


            for phrase in self.sanitize_banned_phrases:


                if phrase and phrase in content:


                    removed_template = True


                    content = content.replace(phrase, "")


            content = normalize_text(content)


            # 兜底清理：若 final_answer 的 JSON 片段尾巴误混入正文，截断后续字段键。


            content = re.sub(


                r'"\s*,\s*"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack)"\s*:\s*$',


                "",


                content,


                flags=re.IGNORECASE,


            )


            content = re.sub(


                r',\s*"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack)"\s*:\s*$',


                "",


                content,


                flags=re.IGNORECASE,


            )


            if removed_template and not content and action == "reply":


                return self._build_mention_only_reply("")





            # 防止 Agent 内部 JSON tool_call 泄漏到回复中


            if content.startswith("{") and content.endswith("}"):


                try:


                    maybe_tool = json.loads(content)


                    if isinstance(maybe_tool, dict) and ("tool" in maybe_tool or "name" in maybe_tool):


                        _log_sanitize = logging.getLogger("yukiko.sanitize")


                        _log_sanitize.warning("sanitize_leaked_tool_call | tool=%s", maybe_tool.get("tool") or maybe_tool.get("name"))


                        return ""


                except (json.JSONDecodeError, ValueError):


                    pass


            if re.search(r"</?\s*(function_calls?|invoke|parameter)\b", content, flags=re.IGNORECASE):


                _log_sanitize = logging.getLogger("yukiko.sanitize")


                _log_sanitize.warning("sanitize_leaked_xml_tool_call")


                return ""


            if leaked_call_markup:


                # 避免“我需要搜索一下……”这类内部执行说明直接外发。


                content = re.sub(


                    r"(我需要|我先|我会|让我|先帮你)\s*(搜索|查|调用|执行|联网|检索)[^。！？\n]*[。！？]?",


                    "",


                    content,


                )


                content = normalize_text(content)


            lower_content = content.lower()


            english_refusal_cues = (


                "i can't discuss",


                "i cannot discuss",


                "i'm an ai assistant",


                "i am an ai assistant",


                "built to help developers",


            )


            if any(cue in lower_content for cue in english_refusal_cues):


                content = _pl.get_message("refusal", "")





            # 输出隐私保护（QQ 号脱敏 + 画像宣称裁剪）。


            content = self._apply_privacy_output_guard(content, action=action)





            # 音乐结果经常包含英文歌名/艺人名，不做英文占比兜底替换。


            if action in {"music_search", "music_play"}:


                return content





            # 英文兜底：如果回复几乎全是英文（中文字符占比极低），保留原文。


            if content and len(content) >= 20:


                cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")


                total_alpha = sum(1 for ch in content if ch.isalpha())


                if total_alpha > 0 and cjk_count / max(total_alpha, 1) < 0.1:


                    pass


            return content


        except Exception as exc:


            logging.getLogger("yukiko.sanitize").warning("sanitize_output_error | %s", exc)


            fallback = normalize_text(str(text or ""))


            fallback = re.sub(r"</?\s*(function_calls?|invoke|parameter|thinking|tool_call)\b[^>]*>", "", fallback, flags=re.IGNORECASE)


            return self._apply_privacy_output_guard(fallback, action=action)





    @staticmethod


    def _mask_numeric_id(value: str) -> str:


        raw = normalize_text(value)


        if not raw:


            return ""


        if len(raw) <= 4:


            return "*" * len(raw)


        keep_tail = 3 if len(raw) >= 7 else 2


        return f"{'*' * (len(raw) - keep_tail)}{raw[-keep_tail:]}"





    def _apply_privacy_output_guard(self, text: str, action: str = "") -> str:


        content = normalize_text(text)


        if not content:


            return ""


        if not self.reply_privacy_guard_enable:


            return content





        guarded = content


        if self.reply_redact_qq_numbers:


            guarded = re.sub(


                r"((?:QQ|qq|企鹅号|用户ID|uid|UID|账号|群号)\s*(?:号|ID|号码)?\s*(?:是|为|:|：)?\s*)(\d{5,12})",


                lambda m: f"{m.group(1)}{self._mask_numeric_id(m.group(2))}",


                guarded,


            )





        if self.reply_block_profile_claims and action in {"reply", "search"}:


            # 禁止在普通问答里抛“画像统计”类信息，避免误导与隐私风险。


            guarded = re.sub(


                r"[^。！？\n]*(?:发了\d+条消息|消息\d+条|凌晨\d+点(?:左右)?活跃|活跃(?:时间|时段|规律)|用户画像|画像来看|作息规律)[^。！？\n]*[。！？]?",


                "",


                guarded,


            )


            guarded = re.sub(r"\s{2,}", " ", guarded).strip()





        return normalize_text(guarded)





    @staticmethod


    def _enforce_identity_claim(text: str) -> str:


        content = normalize_text(text)


        if not content:


            return ""


        # 清理模型常见越权身份拒答话术，统一身份口径


        strips = (


            r"我注意到这个请求不在我的能力范围内[^。！？]*[。！？]?",


            r"我是\s*SKIAPI[^。！？]*[。！？]?",


            r"我专注于帮助开发者[^。！？]*[。！？]?",


            r"不能扮演[^。！？]*[。！？]?",


            r"这里的对话似乎是在模拟[^。！？]*[。！？]?",


        )


        for pat in strips:


            content = re.sub(pat, "", content, flags=re.IGNORECASE)


        content = normalize_text(content)


        lower = content.lower()


        vendor_hint = bool(re.search(r"\b(openai|chatgpt|anthropic|claude|gemini|kiro|deepseek)\b", lower))


        assistant_claim = bool(


            re.search(r"(?i)\b(i am|i'm)\b.{0,32}\b(ai|assistant|model|bot|ide)\b", content)


            or re.search(r"(我是|我叫).{0,32}(ai|助手|模型|机器人|ide)", content, flags=re.IGNORECASE)


        )


        if vendor_hint and assistant_claim:


            return "我是 YuKiKo，在 SKIAPI 上班。"


        if "基于 skiapi 的助手" in lower:


            return content.replace("基于 SKIAPI 的助手", "YuKiKo，在 SKIAPI 上班").strip("（）() ")


        if not content:


            return "我是 YuKiKo，在 SKIAPI 上班。"


        return content





    @staticmethod


    def _contains_video_send_negative_claim(text: str) -> bool:


        _ = text


        # 已移除本地负面话术猜测，统一交给模型或显式状态判断。


        return False





    @staticmethod


    def _inject_user_name(reply_text: str, user_name: str, should_address: bool) -> str:


        if not should_address:


            return reply_text


        name = normalize_text(user_name)


        if not name or len(name) > 24:


            return reply_text


        content = normalize_text(reply_text)


        if not content:


            return reply_text


        if name.lower() in content.lower():


            return reply_text


        if content.startswith(("```", "- ", "* ", "1.", "1、")):


            return reply_text


        return f"{name}，{reply_text}"





    def _apply_tone_guard(self, text: str) -> str:


        content = replace_emoji_with_kaomoji(text, kaomoji=self.default_kaomoji)


        content = normalize_kaomoji_style(content, default=self.default_kaomoji)


        content = self._enforce_kaomoji_allowlist(content)


        content = re.sub(r"\n{3,}", "\n\n", content)


        return content.strip()





    def _enforce_kaomoji_allowlist(self, text: str) -> str:


        content = str(text or "")


        if not content:


            return ""


        allowed = {normalize_text(item).lower() for item in self.kaomoji_allowlist if normalize_text(item)}


        if not allowed:


            return content





        known = ("QWQ", "AWA", "OwO", "UwU", "QAQ", ">_<", "TAT", "XD")


        for token in known:


            token_key = token.lower()


            if token_key in allowed:


                continue


            if re.fullmatch(r"[A-Za-z0-9_]+", token):


                pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"


            else:


                pattern = re.escape(token)


            content = re.sub(pattern, " ", content, flags=re.IGNORECASE)





        # 至多保留一个允许的颜文字


        kept = ""


        for token in self.kaomoji_allowlist:


            pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"


            found = re.search(pattern, content, flags=re.IGNORECASE)


            if found:


                kept = token


                content = re.sub(pattern, " ", content, flags=re.IGNORECASE)


                break





        content = re.sub(r"[ \t]{2,}", " ", content).strip()


        if kept:


            return f"{content} {kept}".strip()


        return content





    def _build_mention_only_reply(self, user_name: str) -> str:


        name = normalize_text(user_name)


        template = normalize_text(self.mention_only_reply_template)


        template_with_name = normalize_text(self.mention_only_reply_template_with_name)


        fallback = normalize_text(_pl.get_message("mention_only_fallback", ""))


        fallback_with_name = normalize_text(_pl.get_message("mention_only_fallback_with_name", ""))


        if name:


            rendered = ""


            if template_with_name:


                rendered = template_with_name.replace("{name}", name) if "{name}" in template_with_name else f"{name}，{template_with_name}"


            elif fallback_with_name:


                rendered = fallback_with_name.replace("{name}", name) if "{name}" in fallback_with_name else f"{name}，{fallback_with_name}"


            elif template:


                rendered = f"{name}，{template}"


            elif fallback:


                rendered = f"{name}，{fallback}"


            return normalize_text(rendered) or name


        bot_name = normalize_text(str(self.config.get("bot", {}).get("name", "")))


        return template or fallback or bot_name





    async def _build_mention_only_reply_auto(self, message: EngineMessage) -> str:


        """mention-only 回复支持模板与 AI 模式。"""


        template_reply = self._build_mention_only_reply(message.user_name)


        mode = self.mention_only_reply_mode


        if mode == "template":


            return template_reply


        if not self.model_client.enabled:


            return template_reply





        user_name = normalize_text(message.user_name) or "对方"


        bot_name = str(self.config.get("bot", {}).get("name", "YuKiKo"))


        ai_prompt = self.mention_only_ai_prompt.replace("{name}", user_name).replace("{bot_name}", bot_name)


        ai_system_prompt = self.mention_only_ai_system_prompt.replace("{name}", user_name).replace("{bot_name}", bot_name)


        if not ai_prompt:


            return template_reply


        messages: list[dict[str, str]] = []


        if ai_system_prompt:


            messages.append({"role": "system", "content": ai_system_prompt})


        messages.append({"role": "user", "content": ai_prompt})


        try:


            ai_text = await asyncio.wait_for(


                self.model_client.chat_text(messages=messages, max_tokens=96),


                timeout=8,


            )


            ai_text = normalize_text(ai_text)


            ai_text = self._sanitize_reply_output(ai_text, action="reply")


            if ai_text:


                return ai_text


        except Exception as exc:


            self.logger.debug("mention_only_ai_reply_fail | %s", exc)





        # mode=ai/hybrid 任一失败都回退模板，避免空回复。


        return template_reply





    def _limit_reply_text(self, text: str, reply_style: str, proactive: bool) -> str:


        if not normalize_text(text):


            return ""


        if "```" in text:


            return text





        limit = self.max_reply_chars_proactive if proactive else self.max_reply_chars


        if reply_style == "short":


            limit = min(limit, 72)


        elif reply_style == "casual":


            limit = min(limit, 110)


        elif reply_style == "serious":


            limit = min(limit, 180)


        elif reply_style == "long":


            limit = int(limit * 1.5)





        plain = remove_markdown(text)


        if len(plain) <= limit:


            return text





        parts = [item.strip() for item in re.split(r"(?<=[。！？!?])\s*", text) if item.strip()]


        if not parts:


            return clip_text(text, limit)





        selected: list[str] = []


        max_sentences = 1 if reply_style == "short" else 2 if reply_style == "casual" else 4


        for part in parts:


            if len(selected) >= max_sentences:


                break


            candidate = "".join(selected + [part])


            if len(remove_markdown(candidate)) > limit:


                break


            selected.append(part)





        if not selected:


            return clip_text(text, limit)





        short = "".join(selected).strip()


        if short.endswith(("。", "！", "？", "!", "?")):


            short = short[:-1]


        return short + "..."





    async def _after_reply(


        self,


        message: EngineMessage,


        reply_text: str,


        proactive: bool = False,


        action: str = "reply",


        open_followup: bool = True,


        user_text: str = "",


    ) -> None:


        # 记录好感度互动


        if hasattr(self, "affinity") and message.user_id:


            try:


                self.affinity.record_interaction(message.user_id, quality=1.0)


            except Exception:


                pass





        self.trigger.activate_session(


            conversation_id=message.conversation_id,


            user_id=message.user_id,


            is_private=message.is_private,


            now=message.timestamp,


        )





        if open_followup:


            if self.followup_consume_on_send:


                # 延迟到传输层成功回调再创建 followup，避免发送失败时误开窗口。


                pass


            else:


                self.trigger.mark_reply_target(message.conversation_id, message.user_id, message.timestamp)





        if proactive:


            self.trigger.mark_proactive_reply(message.conversation_id, message.timestamp)





        self._last_reply_state[message.conversation_id] = {


            "user_id": message.user_id,


            "timestamp": message.timestamp,


            "action": action,


        }





        # 把 bot 回复也记录到 runtime group chat cache，保持上下文完整


        if reply_text and not message.is_private:


            bot_name = self.config.get("bot", {}).get("name", "yukiko")


            cache = self._runtime_group_chat_cache.get(message.conversation_id)


            if cache is not None:


                cache.append(f"{bot_name}: {clip_text(normalize_text(reply_text), 88)}")





        if bool(self.config.get("bot", {}).get("allow_memory", True)) and reply_text:


            self.memory.add_message(


                conversation_id=message.conversation_id,


                user_id=self.config.get("bot", {}).get("name", "yukiko"),


                user_name=self.config.get("bot", {}).get("name", "yukiko"),


                role="assistant",


                content=reply_text,


                timestamp=datetime.now(timezone.utc),


            )


            # write_daily_snapshot 是同步 I/O，放到线程池避免阻塞事件循环


            loop = asyncio.get_running_loop()


            loop.run_in_executor(None, self.memory.write_daily_snapshot)





        # 激进自动学习：异步提取候选事实并入知识库，冲突时自动版本化更新。


        updater = getattr(self, "knowledge_updater", None)


        if updater is not None and reply_text:


            try:


                source_text = normalize_text(user_text) or normalize_text(message.text)


                if hasattr(updater, "update_from_turn_async"):


                    async def _run_knowledge_update() -> None:


                        try:


                            await updater.update_from_turn_async(


                                message.conversation_id,


                                message.user_id,


                                source_text,


                                normalize_text(reply_text),


                                message.timestamp,


                            )


                        except Exception as inner_exc:


                            self.logger.debug("knowledge_auto_update_async_fail | %s", inner_exc)





                    asyncio.create_task(_run_knowledge_update())


                else:


                    updater.update_from_turn(


                        message.conversation_id,


                        message.user_id,


                        source_text,


                        normalize_text(reply_text),


                        message.timestamp,


                    )


            except Exception as exc:


                self.logger.debug("knowledge_auto_update_schedule_fail | %s", exc)





    def on_delivery_success(


        self,


        conversation_id: str,


        user_id: str,


        action: str,


        now: datetime | None = None,


    ) -> None:


        """由传输层在消息实际发出后调用。"""


        if not self.followup_consume_on_send:


            return


        if action in {"ignore", "moderate", "overload_notice"}:


            return


        self.trigger.mark_reply_target(


            conversation_id=conversation_id,


            user_id=user_id,


            now=now or datetime.now(timezone.utc),


        )





    async def _maybe_emotion_gate(


        self,


        message: EngineMessage,


        trigger: Any,


        decision: RouterDecision,


        text: str,


    ) -> EngineResponse | None:


        engine = getattr(self, "emotion", None)


        if engine is None or not bool(getattr(engine, "enable", False)):


            return None


        if not decision.should_handle:


            return None





        action = normalize_text(str(decision.action)).lower()


        if action in {"ignore", "moderate"}:


            return None





        # @机器人 或明确请求时，不触发 emotion gate — 用户主动找你就该干活


        if message.mentioned or message.is_private:


            return None


        if self._looks_like_explicit_request(text) or self._looks_like_media_instruction(text):


            return None





        decision_row = engine.evaluate(


            conversation_id=message.conversation_id,


            user_id=message.user_id,


            now=message.timestamp,


            action=action,


            queue_depth=max(0, int(message.queue_depth)),


            busy_users=max(0, int(getattr(trigger, "busy_users", 0) or 0)),


            is_private=bool(message.is_private),


            mentioned=bool(message.mentioned),


            explicit_request=(


                self._looks_like_explicit_request(text) or self._looks_like_media_instruction(text)


            ),


        )


        if normalize_text(decision_row.state) not in {"warn", "strike"}:


            return None





        reply = normalize_text(decision_row.reply_text)


        if not reply:


            return None


        reply = self._apply_tone_guard(reply)


        reply = self._limit_reply_text(reply, "short", proactive=False)


        rendered = self.markdown.render(reply)


        reason = f"emotion:{normalize_text(decision_row.reason) or decision_row.state}"





        self.logger.info(


            "emotion_gate | trace=%s | 会话=%s | 用户=%s | state=%s | score=%.2f | action=%s",


            message.trace_id,


            message.conversation_id,


            message.user_id,


            decision_row.state,


            float(decision_row.score),


            action,


        )





        await self._after_reply(


            message,


            rendered,


            proactive=False,


            action="emotion_strike" if decision_row.state == "strike" else "emotion_warn",


            open_followup=False,


        )


        self._record_intent(message, action="reply", reason=reason, text=text)





        return EngineResponse(


            action="reply",


            reason=reason,


            reply_text=rendered,


            meta={


                "trace_id": message.trace_id,


                "emotion_state": decision_row.state,


                "emotion_score": decision_row.score,


            },


        )





    def _record_intent(self, message: EngineMessage, action: str, reason: str, text: str) -> None:


        if not hasattr(self.memory, "record_decision"):


            return


        try:


            self.memory.record_decision(


                conversation_id=message.conversation_id,


                user_id=message.user_id,


                action=action,


                reason=reason,


                text=text,


                timestamp=message.timestamp,


            )


        except Exception:


            return





    # ── Agent 副作用记忆 ──────────────────────────────────────


    _SIDE_EFFECT_TOOLS = frozenset({


        "send_face", "send_emoji", "send_group_message",


        "send_private_message", "send_group_forward_msg",


        "send_group_ai_record", "upload_group_file",


        "upload_private_file", "set_msg_emoji_like",


        "generate_image", "web_search",


    })





    def _record_agent_side_effects(self, message: "EngineMessage", agent_result: Any) -> None:


        """把 agent 工具调用中的副作用动作写入记忆，让 bot 记住自己做过什么。"""


        if not bool(self.config.get("bot", {}).get("allow_memory", True)):


            return


        bot_name = self.config.get("bot", {}).get("name", "yukiko")


        parts: list[str] = []


        for step in agent_result.steps:


            tool = step.get("tool", "")


            if tool in self._SIDE_EFFECT_TOOLS:


                result_text = str(step.get("result", "")).strip()


                display = str(step.get("display", result_text)).strip()


                if tool in ("send_face", "send_emoji"):


                    parts.append(f"[发送了表情包] {display}" if display else "[发送了表情包]")


                elif tool == "generate_image":


                    parts.append(f"[生成了图片] {display}" if display else "[生成了图片]")


                elif tool == "web_search":


                    parts.append(f"[进行了搜索] {display[:120]}" if display else "[进行了搜索]")


                elif tool in ("send_group_message", "send_private_message"):


                    parts.append(f"[发送了消息] {display[:120]}" if display else "[发送了消息]")


                elif tool == "send_group_ai_record":


                    parts.append(f"[发送了AI语音]")


                elif tool == "set_msg_emoji_like":


                    parts.append(f"[对消息做了表情回应]")


                else:


                    parts.append(f"[执行了{tool}] {display[:80]}" if display else f"[执行了{tool}]")


        if not parts:


            return


        action_summary = " | ".join(parts)


        try:


            self.memory.add_message(


                conversation_id=message.conversation_id,


                user_id=bot_name,


                user_name=bot_name,


                role="assistant",


                content=f"[bot动作] {action_summary}",


                timestamp=datetime.now(timezone.utc),


            )


        except Exception:


            pass





    @staticmethod


    def _looks_like_summary_followup(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/summary", "/summarize", "mode=summary", "output=summary")





    @staticmethod


    def _looks_like_resend_followup(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/resend", "/retry", "mode=resend")





    def _compose_cached_full_reply(self, message: EngineMessage) -> str:


        if not bool(getattr(self, "search_followup_cache_enable", True)):


            return ""


        key = f"{message.conversation_id}:{message.user_id}"


        cached = self._recent_search_cache.get(key, {})


        if not isinstance(cached, dict):


            return ""


        cached_ts = cached.get("timestamp")


        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        if isinstance(cached_ts, datetime):


            try:


                if (now - cached_ts).total_seconds() > float(getattr(self, "search_followup_cache_ttl_seconds", 1800)):


                    return ""


            except Exception:


                return ""


        full_text = str(cached.get("full_text", "") or "").strip()


        if not full_text:


            full_text = normalize_text(str(cached.get("summary", "")))


        if not full_text:


            return ""


        return f"上一条结果：\n{clip_text(full_text, 3200)}"





    def _resolve_cached_search_followup(self, message: EngineMessage, text: str) -> dict[str, Any] | None:


        if not bool(getattr(self, "search_followup_cache_enable", True)):


            return None


        content = normalize_text(text)


        if not content:


            return None


        # 新任务（尤其是表情包/图片指令）不应该被旧的搜索候选缓存抢答。


        if self._looks_like_sticker_request(content):


            return None


        if any(normalize_text(str(seg.get("type", ""))).lower() == "image" for seg in (message.raw_segments or [])):


            return None


        # 用户在明确回复他人时，不抢答。


        if self._is_explicitly_replying_other_user(message):


            return None





        key = f"{message.conversation_id}:{message.user_id}"


        cached = self._recent_search_cache.get(key, {})


        if not isinstance(cached, dict):


            return None


        cached_ts = cached.get("timestamp")


        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        if isinstance(cached_ts, datetime):


            try:


                if (now - cached_ts).total_seconds() > float(getattr(self, "search_followup_cache_ttl_seconds", 1800)):


                    return None


            except Exception:


                return None





        choices = cached.get("choices", [])


        if not isinstance(choices, list) or not choices:


            return None





        source_followup = self._build_cached_source_trace_result(cached, content)


        if source_followup is not None:


            return source_followup





        if bool(getattr(self, "search_followup_number_choice_enable", True)):


            picked = self._extract_choice_index(content)


            if picked is not None:


                if not self._allow_number_choice_followup(message=message, cached=cached):


                    return None


                return self._build_cached_choice_result(cached, picked)





        if bool(getattr(self, "search_followup_rotate_choice_enable", True)) and self._looks_like_choice_next(content):


            return self._rotate_cached_choice(cached, direction=1, image_only=True)


        if bool(getattr(self, "search_followup_rotate_choice_enable", True)) and self._looks_like_choice_prev(content):


            return self._rotate_cached_choice(cached, direction=-1, image_only=True)


        if bool(getattr(self, "search_followup_resend_enable", True)) and self._looks_like_resend_media_followup(content):


            cursor = cached.get("cursor", 0)


            try:


                cursor_idx = int(cursor)


            except Exception:


                cursor_idx = 0


            return self._build_cached_choice_result(cached, cursor_idx + 1)


        return None





    @staticmethod


    def _extract_choice_index(text: str) -> int | None:


        content = normalize_text(text)


        if not content:


            return None





        compact = re.sub(r"\s+", "", content)


        unit_pattern = r"(?:\u4e2a|\u500b|\u5f20|\u5f35|\u6761|\u689d|\u53f7|\u865f)"





        direct_match = re.fullmatch(rf"([1-9]\d?){unit_pattern}?", compact)


        if direct_match:


            try:


                return int(direct_match.group(1))


            except Exception:


                return None





        ordinal_match = re.fullmatch(rf"\u7b2c([1-9]\d?){unit_pattern}", compact)


        if ordinal_match:


            try:


                return int(ordinal_match.group(1))


            except Exception:


                return None





        zh_ordinal = re.fullmatch(


            rf"\u7b2c([\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{{1,3}}){unit_pattern}",


            compact,


        )


        if zh_ordinal:


            n = YukikoEngine._parse_zh_number(zh_ordinal.group(1))


            if n is not None and n > 0:


                return n


        return None





    @staticmethod


    def _parse_zh_number(token: str) -> int | None:


        value = normalize_text(token)


        if not value:


            return None


        mapping = {


            "一": 1,


            "二": 2,


            "两": 2,


            "三": 3,


            "四": 4,


            "五": 5,


            "六": 6,


            "七": 7,


            "八": 8,


            "九": 9,


        }


        if value == "十":


            return 10


        if "十" not in value:


            return mapping.get(value)


        # 十一 / 二十 / 二十三


        parts = value.split("十")


        if len(parts) != 2:


            return None


        left = parts[0].strip()


        right = parts[1].strip()


        tens = 1 if left == "" else mapping.get(left, 0)


        ones = 0 if right == "" else mapping.get(right, 0)


        if tens <= 0:


            return None


        return tens * 10 + ones





    @staticmethod


    def _looks_like_choice_next(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/next", "page=next")





    @staticmethod


    def _looks_like_choice_prev(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/prev", "page=prev")





    def _looks_like_resend_media_followup(self, text: str) -> bool:


        return self._looks_like_resend_followup(text) and self._has_structural_media_locator(text)





    @staticmethod


    def _looks_like_source_trace_followup(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/source", "/sources", "mode=sources")





    def _build_cached_source_trace_result(self, cached: dict[str, Any], text: str) -> dict[str, Any] | None:


        if not isinstance(cached, dict):


            return None


        if not self._looks_like_source_trace_followup(text):


            return None





        query = normalize_text(str(cached.get("query", "")))


        choices = cached.get("choices", [])


        evidence = cached.get("evidence", [])


        urls: list[tuple[str, str]] = []





        if isinstance(choices, list):


            for item in choices:


                if not isinstance(item, dict):


                    continue


                title = normalize_text(str(item.get("title", ""))) or "候选来源"


                url = normalize_text(str(item.get("url", "")))


                if not url:


                    url = normalize_text(str(item.get("source", "")))


                if not url:


                    continue


                if any(existing_url == url for _, existing_url in urls):


                    continue


                urls.append((title, url))


                if len(urls) >= 3:


                    break





        if len(urls) < 2 and isinstance(evidence, list):


            for item in evidence:


                if not isinstance(item, dict):


                    continue


                title = normalize_text(str(item.get("title", ""))) or "来源"


                url = normalize_text(str(item.get("source", "")))


                if not url:


                    continue


                if any(existing_url == url for _, existing_url in urls):


                    continue


                urls.append((title, url))


                if len(urls) >= 3:


                    break





        if not urls:


            return None





        lines: list[str] = []


        if query:


            lines.append(f"我刚才用的是这个检索词：{query}")


        lines.append("我实际点进去的是这些链接：")


        for idx, (title, url) in enumerate(urls, start=1):


            lines.append(f"{idx}. {title}：{url}")


        lines.append("如果你要我只走官方站，我现在就按“仅官方来源”重跑。")


        return {"text": "\n".join(lines)}





    @staticmethod


    def _looks_like_sticker_request(text: str) -> bool:


        return YukikoEngine._has_control_token(text, "/sticker", "/emoji", "/meme")





    @staticmethod


    def _looks_like_choice_prompt_text(text: str) -> bool:


        _ = text


        # “回复数字/选第几个”链路已下线。


        return False





    @staticmethod


    def _contains_choice_numbered_list(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        has_1 = bool(re.search(r"(?:^|\n)\s*1\s*[\.、\)]", content))


        has_2 = bool(re.search(r"(?:^|\n)\s*2\s*[\.、\)]", content))


        return has_1 and has_2





    def _allow_number_choice_followup(self, message: EngineMessage, cached: dict[str, Any]) -> bool:


        if not isinstance(cached, dict):


            return False


        choices = cached.get("choices", [])


        if not isinstance(choices, list) or len(choices) < 2:


            return False





        reply_text = normalize_text(message.reply_to_text)


        if reply_text:


            if self._looks_like_choice_prompt_text(reply_text):


                return True


            if self._contains_choice_numbered_list(reply_text):


                return True





        cached_full_text = normalize_text(str(cached.get("full_text", "")))


        if cached_full_text:


            if self._looks_like_choice_prompt_text(cached_full_text):


                return True


            if self._contains_choice_numbered_list(cached_full_text):


                return True





        return False





    @staticmethod


    def _looks_like_image_url(url: str) -> bool:


        value = normalize_text(url).lower()


        if not value:


            return False


        if "multimedia.nt.qq.com.cn" in value:


            return True


        return bool(re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", value))





    @staticmethod


    def _looks_like_video_url(url: str) -> bool:


        value = normalize_text(url).lower()


        if not value:


            return False


        return bool(


            re.search(r"\.(?:mp4|mov|webm|m4v)(?:\?|$)", value)


            or any(host in value for host in ("bilibili.com/video/", "b23.tv/", "douyin.com/", "kuaishou.com/", "acfun.cn/v/ac"))


        )





    @staticmethod


    def _looks_like_direct_video_url(url: str) -> bool:


        value = normalize_text(url).lower()


        if not value:


            return False


        if value.startswith("file://"):


            return True


        return bool(re.search(r"\.(?:mp4|mov|webm|m4v|flv|mkv)(?:\?|$)", value))





    def _rotate_cached_choice(


        self,


        cached: dict[str, Any],


        direction: int,


        image_only: bool = False,


    ) -> dict[str, Any] | None:


        choices = cached.get("choices", [])


        if not isinstance(choices, list) or not choices:


            return None


        valid_indices: list[int] = []


        for idx, item in enumerate(choices):


            if not isinstance(item, dict):


                continue


            if not image_only:


                valid_indices.append(idx)


                continue


            image_url = normalize_text(str(item.get("image_url", "")))


            thumbnail_url = normalize_text(str(item.get("thumbnail_url", "")))


            url = normalize_text(str(item.get("url", "")))


            if image_url or thumbnail_url or self._looks_like_image_url(url):


                valid_indices.append(idx)


        if not valid_indices:


            return None


        cursor = cached.get("cursor", -1)


        try:


            cursor_idx = int(cursor)


        except Exception:


            cursor_idx = -1


        if cursor_idx not in valid_indices:


            next_idx = valid_indices[0] if direction >= 0 else valid_indices[-1]


        else:


            pos = valid_indices.index(cursor_idx)


            next_idx = valid_indices[(pos + (1 if direction >= 0 else -1)) % len(valid_indices)]


        return self._build_cached_choice_result(cached, next_idx + 1)





    def _build_cached_choice_result(self, cached: dict[str, Any], one_based_index: int) -> dict[str, Any] | None:


        choices = cached.get("choices", [])


        if not isinstance(choices, list) or not choices:


            return None


        try:


            idx = max(1, int(one_based_index)) - 1


        except (TypeError, ValueError):


            return None


        if idx < 0 or idx >= len(choices):


            return None


        raw_item = choices[idx]


        if not isinstance(raw_item, dict):


            return None





        title = normalize_text(str(raw_item.get("title", ""))) or f"第{idx + 1}项"


        image_url = normalize_text(str(raw_item.get("image_url", "")))


        thumbnail_url = normalize_text(str(raw_item.get("thumbnail_url", "")))


        video_url = normalize_text(str(raw_item.get("video_url", "")))


        source_url = normalize_text(str(raw_item.get("url", "")))


        image_urls_raw = raw_item.get("image_urls", [])


        image_urls: list[str] = []


        if isinstance(image_urls_raw, list):


            image_urls = [normalize_text(str(item)) for item in image_urls_raw if normalize_text(str(item))]





        if not image_url and image_urls:


            image_url = image_urls[0]


        if not image_url and thumbnail_url:


            image_url = thumbnail_url


        if not image_url and source_url and self._looks_like_image_url(source_url):


            image_url = source_url


        if not video_url and source_url and self._looks_like_direct_video_url(source_url):


            video_url = source_url


        if image_url and image_url not in image_urls:


            image_urls.insert(0, image_url)





        selected_idx = idx


        fallback_note = ""


        if not image_url and not video_url:


            for alt_idx, alt_item in enumerate(choices):


                if not isinstance(alt_item, dict):


                    continue


                alt_image = normalize_text(str(alt_item.get("image_url", "")))


                alt_thumb = normalize_text(str(alt_item.get("thumbnail_url", "")))


                alt_source = normalize_text(str(alt_item.get("url", "")))


                if not alt_image and alt_thumb:


                    alt_image = alt_thumb


                if not alt_image and alt_source and self._looks_like_image_url(alt_source):


                    alt_image = alt_source


                if not alt_image:


                    continue


                image_url = alt_image


                image_urls = [alt_image]


                if alt_source:


                    source_url = alt_source


                selected_idx = alt_idx


                if alt_idx != idx:


                    fallback_note = f"（第{idx + 1}项没有直链，先发第{alt_idx + 1}项可发送图片）"


                break





        if image_url:


            text_out = f"给你第{selected_idx + 1}个：{title}{fallback_note}"


            if source_url and source_url != image_url:


                text_out += f"\n出处：{source_url}"


        elif video_url:


            text_out = f"给你第{selected_idx + 1}个视频：{title}"


            if source_url and source_url != video_url:


                text_out += f"\n出处：{source_url}"


        elif source_url:


            text_out = f"第{selected_idx + 1}个来源：{title}\n{source_url}"


        else:


            # 空壳候选（无媒体、无来源）不应命中序号选择。


            return None





        cached["cursor"] = selected_idx


        return {


            "text": text_out,


            "image_url": image_url,


            "image_urls": image_urls,


            "video_url": video_url,


        }





    @staticmethod


    def _compose_recent_summary(recent_messages: list[Any]) -> str:


        if not recent_messages:


            return ""





        recent_bot = YukikoEngine._build_recent_bot_reply_lines(recent_messages, limit=3)


        if not recent_bot:


            return ""


        latest = normalize_text(recent_bot[-1])


        if not latest:


            return ""





        lines = [normalize_text(line) for line in latest.splitlines() if normalize_text(line)]


        items: list[str] = []


        for line in lines:


            match = re.match(r"^\s*\d+\.\s*(.+)$", line)


            if not match:


                continue


            title = normalize_text(match.group(1))


            title = re.sub(r"https?://\S+", "", title).strip()


            title = title.split(" - ")[0].strip()


            if title.endswith("-"):


                title = title[:-1].strip()


            title = title.rstrip("：:")


            if not title:


                continue


            items.append(clip_text(title, 36))


            if len(items) >= 3:


                break





        # 兼容 memory 归一化后"1. xxx 2. yyy 3. zzz"被挤成一行的场景。


        if not items:


            inline = re.findall(r"(?:^|\s)\d+\.\s*(.+?)(?=(?:\s\d+\.\s)|$)", latest)


            for chunk in inline:


                title = normalize_text(chunk)


                title = re.sub(r"https?://\S+", "", title).strip()


                title = title.split(" - ")[0].strip()


                if title.endswith("-"):


                    title = title[:-1].strip()


                title = title.rstrip("：:")


                if not title:


                    continue


                items.append(clip_text(title, 36))


                if len(items) >= 3:


                    break





        if items:


            return f"简短总结：目前可重点看 { '、'.join(items) }。"





        plain = re.sub(r"https?://\S+", "", latest)


        plain = clip_text(normalize_text(plain), 160)


        if not plain:


            return ""


        return f"简短总结：{plain}"





    def _compose_preferred_summary(self, message: EngineMessage, recent_messages: list[Any]) -> str:


        cache_key = f"{message.conversation_id}:{message.user_id}"


        cached = self._recent_search_cache.get(cache_key, {})


        if isinstance(cached, dict):


            cached_ts = cached.get("timestamp")


            now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


            if isinstance(cached_ts, datetime):


                try:


                    if (now - cached_ts).total_seconds() <= float(getattr(self, "search_followup_cache_ttl_seconds", 1800)):


                        cached_summary = normalize_text(str(cached.get("summary", "")))


                        evidence = cached.get("evidence", [])


                        if not isinstance(evidence, list):


                            evidence = []


                        if cached_summary:


                            parts: list[str] = [f"简短总结：{clip_text(cached_summary, 140)}"]


                            evidence_lines: list[str] = []


                            for item in evidence[:3]:


                                if not isinstance(item, dict):


                                    continue


                                title = normalize_text(str(item.get("title", "")))


                                point = normalize_text(str(item.get("point", "")))


                                if title and point:


                                    evidence_lines.append(f"- {clip_text(title, 26)}：{clip_text(point, 56)}")


                                elif point:


                                    evidence_lines.append(f"- {clip_text(point, 68)}")


                            if evidence_lines:


                                parts.append("\n".join(evidence_lines))


                            return "\n".join(parts)


                except Exception:


                    pass


        return self._compose_recent_summary(recent_messages)





    def _remember_search_cache(


        self,


        message: EngineMessage,


        query: str,


        tool_result: Any,


        search_text: str,


    ) -> None:


        if not bool(getattr(self, "search_followup_cache_enable", True)):


            return


        if tool_result is None and not search_text:


            return


        key = f"{message.conversation_id}:{message.user_id}"


        evidence: list[dict[str, Any]] = []


        choices: list[dict[str, Any]] = []


        payload: dict[str, Any] = {}


        if tool_result is not None:


            payload = getattr(tool_result, "payload", {}) or {}


            raw_evidence = getattr(tool_result, "evidence", None)


            if isinstance(raw_evidence, list):


                evidence = [item for item in raw_evidence if isinstance(item, dict)]


            if not evidence:


                payload_evidence = payload.get("evidence", [])


                if isinstance(payload_evidence, list):


                    evidence = [item for item in payload_evidence if isinstance(item, dict)]


                if not evidence:


                    payload_results = payload.get("results", [])


                    if isinstance(payload_results, list):


                        for item in payload_results[:3]:


                            if not isinstance(item, dict):


                                continue


                            title = normalize_text(str(item.get("title", "")))


                            snippet = normalize_text(str(item.get("snippet", "")))


                            url = normalize_text(str(item.get("url", "")))


                            if title or snippet:


                                evidence.append(


                                    {


                                        "title": title or "来源",


                                        "point": clip_text(snippet or title, 90),


                                        "source": url,


                                    }


                                )


            payload_results = payload.get("results", [])


            if not isinstance(payload_results, list):


                payload_results = []


            payload_items = payload.get("items", [])


            if isinstance(payload_items, list) and payload_items:


                payload_results = list(payload_results) + payload_items


            if isinstance(payload_results, list):


                for item in payload_results[:10]:


                    if not isinstance(item, dict):


                        continue


                    title = normalize_text(str(item.get("title", ""))) or "候选项"


                    url = normalize_text(str(item.get("url", ""))) or normalize_text(str(item.get("source_url", "")))


                    image_url = normalize_text(str(item.get("image_url", "")))


                    thumbnail_url = normalize_text(str(item.get("thumbnail_url", "")))


                    video_url = normalize_text(str(item.get("video_url", "")))


                    if not (url or image_url or thumbnail_url or video_url):


                        continue


                    choices.append(


                        {


                            "title": title,


                            "url": url,


                            "image_url": image_url,


                            "thumbnail_url": thumbnail_url,


                            "video_url": video_url,


                            "snippet": clip_text(normalize_text(str(item.get("snippet", ""))), 90),


                        }


                    )


            fallback_image = normalize_text(str(payload.get("image_url", ""))) or normalize_text(


                str(payload.get("thumbnail_url", ""))


            )


            fallback_video = normalize_text(str(payload.get("video_url", "")))


            fallback_image_urls = payload.get("image_urls", [])


            image_urls: list[str] = []


            if isinstance(fallback_image_urls, list):


                image_urls = [normalize_text(str(item)) for item in fallback_image_urls if normalize_text(str(item))]


            fallback_source = normalize_text(str(payload.get("url", ""))) or normalize_text(


                str(payload.get("source_url", ""))


            )


            fallback_target = fallback_video or fallback_image or (image_urls[0] if image_urls else "") or fallback_source


            # 仅当存在真实可发送/可追踪目标时才写入候选，避免出现“第1个：最近结果”空壳项。


            if not choices and fallback_target:


                if fallback_image and fallback_image not in image_urls:


                    image_urls.insert(0, fallback_image)


                recent_media_title = normalize_text(


                    _pl.get_message("search_followup_recent_media_title", "最近媒体结果")


                ) or "最近媒体结果"


                recent_result_title = normalize_text(


                    _pl.get_message("search_followup_recent_result_title", "最近结果")


                ) or "最近结果"


                choices.append(


                    {


                        "title": recent_media_title if (fallback_video or fallback_image or image_urls) else recent_result_title,


                        "url": fallback_target,


                        "image_url": fallback_image,


                        "video_url": fallback_video,


                        "image_urls": image_urls,


                        "snippet": clip_text(normalize_text(search_text), 90),


                    }


                )


        if not choices and evidence:


            for item in evidence[:8]:


                if not isinstance(item, dict):


                    continue


                title = normalize_text(str(item.get("title", ""))) or "来源"


                source = normalize_text(str(item.get("source", "")))


                if not source:


                    continue


                choices.append(


                    {


                        "title": title,


                        "url": source,


                        "image_url": source if self._looks_like_image_url(source) else "",


                        "video_url": source if self._looks_like_direct_video_url(source) else "",


                        "snippet": clip_text(normalize_text(str(item.get("point", ""))), 90),


                    }


                )





        summary = normalize_text(search_text)


        if summary:


            summary = re.sub(r'^我查了\u201c[^\u201d]+\u201d，先给你\s*\d+\s*条：', "", summary).strip()


        full_text = str(search_text or "").strip()


        self._recent_search_cache[key] = {


            "timestamp": message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc),


            "query": normalize_text(query),


            "summary": summary,


            "full_text": clip_text(full_text, 4000),


            "evidence": evidence[:6],


            "choices": choices[: int(getattr(self, "search_followup_max_choices", 10))],


            "cursor": 0 if choices else -1,


        }





    def _prune_memory_context_for_current_turn(


        self,


        message: EngineMessage,


        current_text: str,


        memory_context: list[str],


        related_memories: list[str],


    ) -> tuple[list[str], list[str]]:


        mem_rows = [normalize_text(str(row)) for row in (memory_context or []) if normalize_text(str(row))]


        rel_rows = [normalize_text(str(row)) for row in (related_memories or []) if normalize_text(str(row))]


        text = normalize_text(current_text)





        topic_terms = self._extract_topic_terms_for_memory(f"{text} {message.reply_to_text}")


        if topic_terms:


            mem_filtered = self._filter_rows_by_topic_terms(mem_rows, topic_terms, keep_current_user_rows=True)


            rel_filtered = self._filter_rows_by_topic_terms(rel_rows, topic_terms, keep_current_user_rows=False)


            if mem_filtered:


                mem_rows = mem_filtered


            else:


                mem_rows = [row for row in mem_rows if row.startswith("[当前用户近期]")]


            rel_rows = rel_filtered


            return mem_rows[-18:], rel_rows[:8]





        # 群聊短追问 + reply 他人时，收敛到当前用户/引用对象上下文，避免跨用户串台。


        reply_uid = normalize_text(str(message.reply_to_user_id))


        current_uid = normalize_text(str(message.user_id))


        bot_uid = normalize_text(str(message.bot_id))


        if (


            reply_uid


            and reply_uid not in {current_uid, bot_uid}


            and self._looks_like_short_context_sensitive_query(text)


        ):


            focused_rows = [


                row


                for row in mem_rows


                if (
                    row.startswith("[当前用户近期]")
                    or row.startswith("[引用对象近期]")
                    or row.startswith("[引用图片记忆]")
                    or row.startswith("[引用图片base64")
                    or row.startswith("[引用图片URL")
                )


            ]


            if focused_rows:


                mem_rows = focused_rows[-18:]


            rel_rows = []





        # 用户只说“还记得链接吗”这类模糊追问时，不把历史 URL 喂给模型，避免串题误召回。


        if self._looks_like_ambiguous_link_memory_query(text):


            if not self._extract_urls_from_text(message.reply_to_text):


                mem_rows = [row for row in mem_rows if not self._extract_urls_from_text(row)]


                rel_rows = [row for row in rel_rows if not self._extract_urls_from_text(row)]


        return mem_rows[-18:], rel_rows[:8]





    @staticmethod


    def _extract_topic_terms_for_memory(text: str, max_terms: int = 6) -> list[str]:


        content = normalize_text(text)


        if not content or max_terms <= 0:


            return []





        out: list[str] = []


        seen: set[str] = set()


        strip_chars = "`\"'[](){}<>.,;:!?\uFF0C\u3002\uFF1F\uFF01\uFF1A"





        def add_candidate(raw: str) -> None:


            item = normalize_text(str(raw)).strip(strip_chars)


            if not item:


                return


            lower = item.lower()


            if lower in seen:


                return


            if lower.startswith("/") or "=" in lower:


                return


            if re.search(r"https?://", lower, flags=re.IGNORECASE):


                return


            if re.fullmatch(r"[1-9]\d*", item):


                return


            if re.search(r"[a-z0-9]", lower):


                compact = re.sub(r"[^a-z0-9_.-]+", "", lower)


                if len(compact) < 3:


                    return


            elif re.fullmatch(r"[\u4e00-\u9fff]+", item):


                if len(item) < 3:


                    return


            elif len(item) < 3:


                return


            seen.add(lower)


            out.append(item)





        explicit_patterns = (


            r"`([^`]{2,80})`",


            r"\*\*([^*]{2,80})\*\*",


            r"[\u201c\"]([^\u201d\"]{2,80})[\u201d\"]",


            r"\u300a([^\u300b]{2,80})\u300b",


        )


        for pattern in explicit_patterns:


            for raw in re.findall(pattern, content):


                add_candidate(raw)


                if len(out) >= max_terms:


                    return out[:max_terms]





        for token in tokenize(content):


            add_candidate(token)


            if len(out) >= max_terms:


                break


        return out[:max_terms]





    @staticmethod


    def _extract_structured_reference_spans(text: str, max_terms: int = 4) -> list[str]:


        content = normalize_text(text)


        if not content or max_terms <= 0:


            return []





        out: list[str] = []


        seen: set[str] = set()


        patterns = (


            r"`([^`]{2,80})`",


            r"\*\*([^*]{2,80})\*\*",


            r"[\u201c\"]([^\u201d\"]{2,80})[\u201d\"]",


            r"\u300a([^\u300b]{2,80})\u300b",


        )


        for pattern in patterns:


            for raw in re.findall(pattern, content):


                item = normalize_text(raw)


                if not item:


                    continue


                key = item.lower()


                if key in seen:


                    continue


                seen.add(key)


                out.append(item)


                if len(out) >= max_terms:


                    return out


        return out





    @staticmethod


    def _filter_rows_by_topic_terms(rows: list[str], topic_terms: list[str], keep_current_user_rows: bool) -> list[str]:


        if not rows or not topic_terms:


            return rows


        matched: list[str] = []


        fallback_current_user: list[str] = []


        for row in rows:


            text = normalize_text(row)


            if not text:


                continue


            lower = text.lower()


            if any(term in lower for term in topic_terms):


                matched.append(text)


            elif keep_current_user_rows and text.startswith("[当前用户近期]"):


                fallback_current_user.append(text)


        result = matched + fallback_current_user


        return result if result else []





    def _should_block_ambiguous_link_recall_result(


        self,


        message: EngineMessage,


        current_text: str,


        agent_result: AgentResult,


    ) -> bool:


        text = normalize_text(current_text)


        if not self._looks_like_ambiguous_link_memory_query(text):


            return False


        if self._extract_urls_from_text(text):


            return False


        # 用户明确引用的那条消息里带链接，说明目标明确，不拦截。


        if self._extract_urls_from_text(message.reply_to_text):


            return False


        has_result_url = bool(


            self._extract_urls_from_text(agent_result.reply_text)


            or self._extract_urls_from_text(agent_result.image_url)


            or self._extract_urls_from_text(agent_result.video_url)


            or any(self._extract_urls_from_text(url) for url in (agent_result.image_urls or []))


        )


        return has_result_url





    @staticmethod


    def _looks_like_ambiguous_link_memory_query(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if re.search(r"https?://", content, flags=re.IGNORECASE):


            return False


        if not YukikoEngine._has_control_token(text, "/link", "/url", "type=link", "type=url", "mode=url"):


            return False


        cleaned = re.sub(r"(?i)(?<!\S)/(?:link|url)\b", " ", content)


        cleaned = re.sub(r"(?i)\b(?:type|mode)\s*=\s*(?:link|url)\b", " ", cleaned)


        return not YukikoEngine._extract_topic_terms_for_memory(cleaned, max_terms=3)





    @staticmethod


    def _looks_like_short_context_sensitive_query(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if re.search(r"https?://", content, flags=re.IGNORECASE):


            return False


        if re.fullmatch(r"[?!.,\uFF1F\uFF01\uFF0C\u3002]+", content):


            return True


        if len(content) > 24:


            return False


        if YukikoEngine._extract_topic_terms_for_memory(content, max_terms=2):


            return False


        compact = re.sub(r"\s+", "", content)


        if len(compact) <= 8:


            return True


        tokens = [normalize_text(str(token)) for token in tokenize(content)]


        tokens = [token for token in tokens if token]


        return len(tokens) <= 2





    def _remember_agent_followup_cache(self, message: EngineMessage, agent_result: AgentResult) -> None:


        """将 Agent 的候选/媒体结果写入会话缓存。"""


        if not bool(getattr(self, "search_followup_cache_enable", True)):


            return


        if not isinstance(agent_result, AgentResult):


            return





        payload: dict[str, Any] = {}


        evidence: list[dict[str, Any]] = []


        query = normalize_text(message.text)


        search_text = normalize_text(agent_result.reply_text)





        # 优先复用最近一次成功工具步骤的数据（agent.py 已把 compact_data 写入 steps）。


        for step in reversed(agent_result.steps or []):


            if not isinstance(step, dict):


                continue


            if not bool(step.get("ok")):


                continue


            step_data = step.get("data", {})


            if not isinstance(step_data, dict) or not step_data:


                continue


            tool_name = normalize_text(str(step.get("tool", ""))).lower()


            if tool_name not in {


                "search_web_media",


                "web_search",


                "search_zhihu",


                "lookup_wiki",


                "search_knowledge",


                "github_search",


                "douyin_search",


                "fetch_webpage",


                "search_download_resources",


                "smart_download",


                "download_file",


            }:


                continue


            payload = dict(step_data)


            raw_evidence = step_data.get("evidence", [])


            if isinstance(raw_evidence, list):


                evidence = [item for item in raw_evidence if isinstance(item, dict)]


            if tool_name in {"smart_download", "download_file"}:


                source_url = normalize_text(str(step_data.get("source_url", "")))


                download_url = normalize_text(str(step_data.get("download_url", "")))


                if source_url:


                    evidence.append(


                        {"title": "下载来源页", "point": "触发下载的页面链接", "source": source_url}


                    )


                if download_url:


                    evidence.append(


                        {"title": "最终下载链接", "point": "实际下载的文件直链", "source": download_url}


                    )


                if source_url or download_url:


                    payload.setdefault("results", [])


                    if isinstance(payload["results"], list):


                        if source_url:


                            payload["results"].append(


                                {"title": "下载来源页", "url": source_url, "snippet": "触发下载的页面链接"}


                            )


                        if download_url:


                            payload["results"].append(


                                {"title": "最终下载链接", "url": download_url, "snippet": "实际下载文件直链"}


                            )


            step_query = normalize_text(str(step_data.get("query", "")))


            if step_query:


                query = step_query


            break





        # 回填 final_answer 携带的媒体字段（即便工具步骤缺 data，也能稳定复用输出）。


        image_urls = [


            normalize_text(str(item))


            for item in (agent_result.image_urls or [])


            if normalize_text(str(item))


        ]


        image_url = normalize_text(agent_result.image_url)


        video_url = normalize_text(agent_result.video_url)


        if image_url and image_url not in image_urls:


            image_urls.insert(0, image_url)


        if image_urls and not image_url:


            image_url = image_urls[0]


        if image_url:


            payload["image_url"] = image_url


        if image_urls:


            payload["image_urls"] = image_urls


        if video_url:


            payload["video_url"] = video_url





        # 没有结构化结果时，根据图片列表合成编号候选。


        if image_urls and not isinstance(payload.get("results"), list):


            payload["results"] = [


                {


                    "title": f"候选图 {idx}",


                    "url": url,


                    "image_url": url,


                    "snippet": "",


                }


                for idx, url in enumerate(image_urls[: int(getattr(self, "search_followup_max_choices", 10))], start=1)


            ]





        if not payload and not search_text:


            return





        # 清理占位链接，避免把 example.com 之类脏数据写入复用缓存。


        payload = self._sanitize_cached_media_payload(payload)


        if not payload and not search_text:


            return





        fake_result = SimpleNamespace(payload=payload, evidence=evidence)


        self._remember_search_cache(


            message=message,


            query=query or normalize_text(message.text),


            tool_result=fake_result,


            search_text=search_text,


        )





    @staticmethod


    def _is_placeholder_media_url(url: str) -> bool:


        value = normalize_text(url).lower()


        if not value:


            return False


        if not (value.startswith("http://") or value.startswith("https://")):


            return False


        blocked_tokens = (


            "example.com",


            "example.org",


            "example.net",


            "localhost",


            "127.0.0.1",


            "0.0.0.0",


            ".invalid/",


        )


        return any(token in value for token in blocked_tokens)





    def _sanitize_cached_media_payload(self, payload: dict[str, Any]) -> dict[str, Any]:


        if not isinstance(payload, dict):


            return {}


        out = dict(payload)


        for key in ("image_url", "thumbnail_url", "video_url", "url"):


            value = normalize_text(str(out.get(key, "")))


            if self._is_placeholder_media_url(value):


                out[key] = ""





        raw_image_urls = out.get("image_urls", [])


        image_urls: list[str] = []


        if isinstance(raw_image_urls, list):


            for item in raw_image_urls:


                value = normalize_text(str(item))


                if not value or self._is_placeholder_media_url(value):


                    continue


                image_urls.append(value)


        out["image_urls"] = image_urls


        if image_urls and not normalize_text(str(out.get("image_url", ""))):


            out["image_url"] = image_urls[0]





        raw_results = out.get("results", [])


        clean_results: list[dict[str, Any]] = []


        if isinstance(raw_results, list):


            for item in raw_results:


                if not isinstance(item, dict):


                    continue


                row = dict(item)


                row_url = normalize_text(str(row.get("url", "")))


                row_img = normalize_text(str(row.get("image_url", "")))


                row_thumb = normalize_text(str(row.get("thumbnail_url", "")))


                row_video = normalize_text(str(row.get("video_url", "")))


                if self._is_placeholder_media_url(row_url):


                    row["url"] = ""


                if self._is_placeholder_media_url(row_img):


                    row["image_url"] = ""


                if self._is_placeholder_media_url(row_thumb):


                    row["thumbnail_url"] = ""


                if self._is_placeholder_media_url(row_video):


                    row["video_url"] = ""


                if not (


                    normalize_text(str(row.get("url", "")))


                    or normalize_text(str(row.get("image_url", "")))


                    or normalize_text(str(row.get("thumbnail_url", "")))


                    or normalize_text(str(row.get("video_url", "")))


                ):


                    continue


                clean_results.append(row)


        out["results"] = clean_results





        raw_items = out.get("items", [])


        clean_items: list[dict[str, Any]] = []


        if isinstance(raw_items, list):


            for item in raw_items:


                if not isinstance(item, dict):


                    continue


                row = dict(item)


                row_url = normalize_text(str(row.get("url", "")))


                row_img = normalize_text(str(row.get("image_url", "")))


                row_thumb = normalize_text(str(row.get("thumbnail_url", "")))


                row_video = normalize_text(str(row.get("video_url", "")))


                if self._is_placeholder_media_url(row_url):


                    row["url"] = ""


                if self._is_placeholder_media_url(row_img):


                    row["image_url"] = ""


                if self._is_placeholder_media_url(row_thumb):


                    row["thumbnail_url"] = ""


                if self._is_placeholder_media_url(row_video):


                    row["video_url"] = ""


                if not (


                    normalize_text(str(row.get("url", "")))


                    or normalize_text(str(row.get("image_url", "")))


                    or normalize_text(str(row.get("thumbnail_url", "")))


                    or normalize_text(str(row.get("video_url", "")))


                ):


                    continue


                clean_items.append(row)


        out["items"] = clean_items


        return out





    def _ensure_min_reply_text(


        self,


        rendered: str,


        action: str,


        user_text: str,


        search_summary: str,


        message: EngineMessage,


        recent_messages: list[Any],


    ) -> str:


        content = normalize_text(rendered)


        if not content:


            return ""


        if action in {"moderate", "overload_notice", "music_search", "music_play"}:


            return rendered





        plain = normalize_text(remove_markdown(content))


        if len(plain) >= self.min_reply_chars:


            return rendered





        if self._looks_like_summary_followup(user_text):


            summary = self._compose_preferred_summary(message=message, recent_messages=recent_messages)


            summary = normalize_text(summary)


            if summary and len(normalize_text(remove_markdown(summary))) >= self.min_reply_chars:


                return self.markdown.render(


                    summary,


                    max_len=max(self.markdown.max_output_chars, 360),


                    max_lines=max(self.markdown.max_output_lines, 5),


                )





        fallback = normalize_text(search_summary)


        if action == "search" and fallback:


            fallback = clip_text(fallback, 220)


            if len(normalize_text(remove_markdown(fallback))) >= self.min_reply_chars:


                return self.markdown.render(


                    fallback,


                    max_len=max(self.markdown.max_output_chars, 360),


                    max_lines=max(self.markdown.max_output_lines, 5),


                )





        # 低质量短句直接保留，不再拼接“继续说具体要我做什么”之类机械话术。


        if plain.endswith("...") or plain in {"QWQ", "AWA", "OwO"} or len(plain) <= 6:


            return rendered





        return rendered





    def _guard_unverified_memory_claims(


        self,


        reply_text: str,


        user_text: str,


        current_user_recent: list[str],


        related_memories: list[str],


    ) -> str:


        _ = user_text


        text = normalize_text(reply_text)


        if not text:


            return reply_text





        evidence_lines: list[str] = []


        for row in (current_user_recent or []):


            line = normalize_text(row)


            if line:


                evidence_lines.append(line)


        for row in (related_memories or []):


            line = normalize_text(row)


            if line:


                evidence_lines.append(line)


        if not evidence_lines:


            return reply_text





        evidence_text = "\n".join(evidence_lines).lower()


        for span in self._extract_structured_reference_spans(text):


            if normalize_text(span).lower() not in evidence_text:


                return "\u6211\u521a\u624d\u90a3\u53e5\u5386\u53f2\u5f15\u7528\u4e0d\u51c6\u786e\uff0c\u5ffd\u7565\u5b83\u3002\u4f60\u73b0\u5728\u76f4\u63a5\u544a\u8bc9\u6211\u9700\u6c42\uff0c\u6211\u6309\u4f60\u8fd9\u6761\u6765\u3002"


        return reply_text





    def _merge_fragmented_user_message(self, message: EngineMessage, text: str) -> tuple[str, str, bool]:


        """


        处理"断句连发"：


        例：@bot facd12   -> 下一条 是谁  => 合并为 facd12 是谁


        返回：(new_text, state)，state in {"none", "hold", "merged", "timeout_fallback"}。


        """


        content = normalize_text(text)


        if not self.fragment_join_enable or not content:


            return content, "none", False





        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)


        self._cleanup_pending_fragments(now)





        key = f"{message.conversation_id}:{message.user_id}"


        pending = self._pending_fragments.get(key)


        if pending:


            pending_text = normalize_text(str(pending.get("text", "")))


            pending_ts = pending.get("timestamp")


            try:


                age_seconds = (now - pending_ts).total_seconds() if isinstance(pending_ts, datetime) else 10_000


            except Exception:


                age_seconds = 10_000





            if pending_text and age_seconds <= self.fragment_join_window_seconds:


                if self._is_fragment_continuation(content):


                    merged = normalize_text(f"{pending_text} {content}")


                    pending_mentioned = bool(pending.get("mentioned", False))


                    self._pending_fragments.pop(key, None)


                    return merged, "merged", pending_mentioned


            elif (


                pending_text


                and age_seconds <= self.fragment_timeout_fallback_seconds


                and self._is_fragment_timeout_nudge(content)


            ):


                pending_mentioned = bool(pending.get("mentioned", False))


                self._pending_fragments.pop(key, None)


                return pending_text, "timeout_fallback", pending_mentioned


            self._pending_fragments.pop(key, None)





        if self._should_hold_as_fragment(message=message, text=content):


            self._pending_fragments[key] = {


                "text": content,


                "timestamp": now,


                "mentioned": bool(message.mentioned or message.is_private or self._looks_like_bot_call(content)),


            }


            return content, "hold", False





        return content, "none", False





    def _cleanup_pending_fragments(self, now: datetime) -> None:


        if not self._pending_fragments:


            return


        expire_seconds = max(6, self.fragment_join_window_seconds * 2, self.fragment_timeout_fallback_seconds + 2)


        stale: list[str] = []


        for key, state in self._pending_fragments.items():


            ts = state.get("timestamp") if isinstance(state, dict) else None


            if not isinstance(ts, datetime):


                stale.append(key)


                continue


            try:


                age_seconds = (now - ts).total_seconds()


            except Exception:


                age_seconds = expire_seconds + 1


            if age_seconds > expire_seconds:


                stale.append(key)


        for key in stale:


            self._pending_fragments.pop(key, None)





    def _should_hold_as_fragment(self, message: EngineMessage, text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if len(content) > self.fragment_hold_max_chars:


            return False


        if re.search(r"https?://", content, flags=re.IGNORECASE):


            return False


        if re.search(r"[。！？!?]", content):


            return False


        if self._looks_like_explicit_request(content):


            return False


        if self._is_passive_multimodal_text(content):


            return False





        # @机器人的消息一律不 hold，交给 router LLM 判断意图


        if message.mentioned:


            return False





        # 只保留结构化判定：群聊里非 @mention 的短 token / ID 可以暂存，


        # 自然语言短句一律交给 router/LLM，不再做本地词表分类。


        if re.fullmatch(r"[A-Za-z0-9_\-.]{2,32}", content):


            return True


        return False





    @staticmethod


    def _is_fragment_continuation(text: str) -> bool:


        content = normalize_text(text)


        if not content:


            return False


        if len(content) > 42:


            return False


        return bool(re.fullmatch(r"[?？!！~～…,.，]{1,6}", content))





    @staticmethod


    def _is_fragment_timeout_nudge(text: str) -> bool:


        content = normalize_text(text).lower()


        if not content:


            return False


        return bool(re.fullmatch(r"[?？!！~～…,.，]{1,8}", content))

