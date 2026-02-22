from __future__ import annotations

import importlib.util
import inspect
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core.image import ImageEngine
from core.markdown import MarkdownRenderer
from core.memory import MemoryEngine
from core.personality import PersonalityEngine
from core.search import SearchEngine
from core.thinking import ThinkingEngine, ThinkingInput
from core.trigger import TriggerEngine, TriggerInput
from services.logger import get_logger
from services.skiapi import SkiAPIClient
from utils.text import normalize_text


@dataclass(slots=True)
class EngineMessage:
    conversation_id: str
    user_id: str
    text: str
    mentioned: bool = False
    is_private: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class EngineResponse:
    action: str
    reason: str
    reply_text: str = ""
    image_url: str = ""


class PluginManager:
    def __init__(self, plugins_dir: Path, logger):
        self.plugins_dir = plugins_dir
        self.logger = logger
        self.plugins: list[Any] = []
        self.command_map: dict[str, Any] = {}

    def load(self) -> None:
        self.plugins.clear()
        self.command_map.clear()
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
                spec.loader.exec_module(module)
                plugin_cls = getattr(module, "Plugin", None)
                if plugin_cls is None:
                    continue
                plugin = plugin_cls()
                self.plugins.append(plugin)
                for command in getattr(plugin, "commands", []):
                    self.command_map[str(command)] = plugin
                self.logger.info("已加载插件：%s", getattr(plugin, "name", file.stem))
            except Exception as exc:
                self.logger.exception("加载插件失败 %s：%s", file.name, exc)

    def match(self, text: str) -> Any | None:
        first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        return self.command_map.get(first)

    async def run(self, plugin: Any, message: str, context: dict[str, Any]) -> str:
        handler = getattr(plugin, "handle", None)
        if handler is None:
            return ""
        result = handler(message, context)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")


class YukikoEngine:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.config_dir = project_root / "config"
        self.storage_dir = project_root / "storage"
        self.plugins_dir = project_root / "plugins"

        self.config = self._load_yaml(self.config_dir / "config.yml")
        self.triggers_config = self._load_yaml(self.config_dir / "triggers.yml")
        self.sensitive_config = self._load_yaml(self.config_dir / "sensitive.yml")
        self.config = self._resolve_env_vars(self.config)

        debug = bool(self.config.get("bot", {}).get("debug", False))
        self.logger = get_logger("yukiko", self.storage_dir / "logs", debug=debug)
        self.skiapi = SkiAPIClient(self.config.get("api", {}))
        self.personality = PersonalityEngine.from_file(self.config_dir / "personality.yml")
        self.memory = MemoryEngine(self.config.get("memory", {}), self.storage_dir / "memory")
        self.trigger = TriggerEngine(
            trigger_config=self.config.get("trigger", {}),
            triggers_file_config=self.triggers_config,
            sensitive_config=self.sensitive_config,
            bot_config=self.config.get("bot", {}),
        )
        self.search = SearchEngine(self.config.get("search", {}))
        self.image = ImageEngine(self.config.get("image", {}), self.skiapi)
        self.markdown = MarkdownRenderer(
            config=self.config.get("markdown", {}),
            enabled=bool(self.config.get("bot", {}).get("allow_markdown", True)),
        )
        self.thinking = ThinkingEngine(
            config=self.config,
            personality=self.personality,
            skiapi=self.skiapi,
        )
        self.plugins = PluginManager(self.plugins_dir, self.logger)
        self.plugins.load()

    @classmethod
    def from_default_paths(cls, project_root: Path | None = None) -> "YukikoEngine":
        root = project_root or Path(__file__).resolve().parents[1]
        return cls(project_root=root)

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _resolve_env_vars(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {k: self._resolve_env_vars(v) for k, v in payload.items()}
        if isinstance(payload, list):
            return [self._resolve_env_vars(v) for v in payload]
        if isinstance(payload, str):
            match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", payload.strip())
            if match:
                return os.getenv(match.group(1), "")
        return payload

    async def handle_message(self, message: EngineMessage) -> EngineResponse:
        text = normalize_text(message.text)
        if not text:
            return EngineResponse(action="ignore", reason="empty_message")

        allow_memory = bool(self.config.get("bot", {}).get("allow_memory", True))
        recent_for_trigger = (
            self.memory.get_recent_texts(
                message.conversation_id, limit=int(self.config.get("trigger", {}).get("sensitive_analysis_window", 6))
            )
            if allow_memory
            else []
        )

        trigger_result = self.trigger.evaluate(
            TriggerInput(
                conversation_id=message.conversation_id,
                text=text,
                mentioned=message.mentioned,
                is_private=message.is_private,
                timestamp=message.timestamp,
            ),
            recent_messages=recent_for_trigger,
        )

        if allow_memory:
            self.memory.add_message(
                conversation_id=message.conversation_id,
                user_id=message.user_id,
                role="user",
                content=text,
                timestamp=message.timestamp,
            )

        if not trigger_result.should_handle:
            return EngineResponse(action="ignore", reason=trigger_result.reason)

        plugin = self.plugins.match(text)
        if plugin is not None:
            try:
                plugin_context = {
                    "conversation_id": message.conversation_id,
                    "user_id": message.user_id,
                    "config": self.config,
                    "timestamp": message.timestamp.isoformat(),
                }
                plugin_reply = (await self.plugins.run(plugin, text, plugin_context)).strip()
                if plugin_reply:
                    rendered = self.markdown.render(plugin_reply)
                    await self._after_reply(message, rendered)
                    return EngineResponse(
                        action="reply",
                        reason=f"plugin:{getattr(plugin, 'name', 'unknown')}",
                        reply_text=rendered,
                    )
            except Exception as exc:
                self.logger.exception("插件执行失败：%s", exc)

        memory_context = self.memory.get_recent_texts(message.conversation_id, limit=12) if allow_memory else []
        related_memories = (
            self.memory.search_related(message.conversation_id, text) if allow_memory else []
        )

        decision = await self.thinking.decide(
            ThinkingInput(
                text=text,
                trigger_reason=trigger_result.reason,
                sensitive_context=trigger_result.sensitive_context,
                memory_context=memory_context,
                related_memories=related_memories,
            )
        )

        if decision.action == "ignore":
            return EngineResponse(action="ignore", reason=decision.reason)

        reply_text = ""
        image_url = ""
        reason = decision.reason
        search_summary = ""

        if decision.action == "search":
            query = decision.query.strip() or text
            try:
                results = await self.search.search(query)
                search_summary = self.search.format_results(query, results)
            except Exception as exc:
                self.logger.exception("联网搜索失败：%s", exc)
                search_summary = f'查询词="{query}"\n搜索失败。'

        if decision.action == "generate_image":
            try:
                image_result = await self.image.generate(prompt=decision.prompt or text)
                reply_text = image_result.message
                image_url = image_result.url
                reason = f"image:{decision.reason}"
            except Exception as exc:
                self.logger.exception("图片生成失败：%s", exc)
                reply_text = "图片生成失败，请稍后再试。"

        if decision.action in {"reply", "search"}:
            try:
                reply_text = await self.thinking.generate_reply(
                    user_text=text,
                    memory_context=memory_context,
                    related_memories=related_memories,
                    reply_style=decision.reply_style,
                    search_summary=search_summary,
                    sensitive_context=trigger_result.sensitive_context,
                )
                reason = f"{decision.action}:{decision.reason}"
            except Exception as exc:
                self.logger.exception("回复生成失败：%s", exc)
                reply_text = "我先记下了。你可以再补充一点信息，我继续帮你。"

        rendered = self.markdown.render(reply_text) if reply_text else ""
        if not rendered and not image_url:
            return EngineResponse(action="ignore", reason="empty_reply")

        await self._after_reply(message, rendered)
        return EngineResponse(action=decision.action, reason=reason, reply_text=rendered, image_url=image_url)

    async def _after_reply(self, message: EngineMessage, reply_text: str) -> None:
        self.trigger.activate_session(message.conversation_id, message.timestamp)
        if bool(self.config.get("bot", {}).get("allow_memory", True)) and reply_text:
            self.memory.add_message(
                conversation_id=message.conversation_id,
                user_id=self.config.get("bot", {}).get("name", "yukiko"),
                role="assistant",
                content=reply_text,
                timestamp=datetime.now(timezone.utc),
            )
            self.memory.write_daily_snapshot()
