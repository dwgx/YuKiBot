from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from core.admin import AdminEngine
from core.config_manager import ConfigManager
from core.emotion import EmotionEngine
from core.image import ImageEngine
from core.markdown import MarkdownRenderer
from core.memory import MemoryEngine
from core.personality import PersonalityEngine
from core.router import RouterDecision, RouterEngine, RouterInput
from core.safety import SafetyEngine
from core.search import SearchEngine
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
    api_call: Callable[..., Awaitable[Any]] | None = None
    trace_id: str = ""


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


class PluginRegistry:
    def __init__(self, plugins_dir: Path, logger):
        self.plugins_dir = plugins_dir
        self.logger = logger
        self.plugins: dict[str, Any] = {}
        self.schemas: list[dict[str, Any]] = []

    def load(self) -> None:
        self.plugins.clear()
        self.schemas.clear()
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
            except Exception as exc:
                self.logger.exception("加载插件失败 %s：%s", file.name, exc)

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


class YukikoEngine:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.config_dir = project_root / "config"
        self.storage_dir = project_root / "storage"
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
        self.personality = PersonalityEngine.from_file(self.config_dir / "personality.yml")
        self.memory = MemoryEngine(self.config.get("memory", {}), self.storage_dir / "memory")
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

        self.plugins = PluginRegistry(self.plugins_dir, self.logger)
        self.plugins.load()

        self.tools = ToolExecutor(
            search_engine=self.search,
            image_engine=self.image,
            plugin_runner=self._run_plugin,
            config=self.config,
        )

        self._last_reply_state: dict[str, dict[str, Any]] = {}
        self._pending_fragments: dict[str, dict[str, Any]] = {}
        self._recent_directed_hints: dict[str, datetime] = {}
        self._recent_search_cache: dict[str, dict[str, Any]] = {}
        self._runtime_group_chat_cache: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.runtime_group_cache_max_messages)
        )

    def _init_from_config(self) -> None:
        """从 config 读取阈值/参数，热重载时也会调用。"""
        bot_config = self.config.get("bot", {})
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
        self.followup_min_confidence = max(
            self.router_min_confidence,
            min(1.0, float(routing_cfg.get("followup_min_confidence", 0.75))),
        )
        self.non_directed_min_confidence = max(
            self.router_min_confidence,
            min(1.0, float(routing_cfg.get("non_directed_min_confidence", 0.72))),
        )
        self.ai_gate_min_confidence = max(
            self.router_min_confidence,
            min(1.0, float(routing_cfg.get("ai_gate_min_confidence", 0.66))),
        )
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
        self_check_cfg = self.config.get("self_check", {})
        if not isinstance(self_check_cfg, dict):
            self_check_cfg = {}
        self.self_check_enable = bool(self_check_cfg.get("enable", True))
        self.self_check_block_at_other = bool(self_check_cfg.get("block_at_other", True))
        self.self_check_listen_probe_min_confidence = max(
            self.router_min_confidence,
            min(1.0, float(self_check_cfg.get("listen_probe_min_confidence", 0.86))),
        )
        self.self_check_non_direct_reply_min_confidence = max(
            self.router_min_confidence,
            min(1.0, float(self_check_cfg.get("non_direct_reply_min_confidence", 0.78))),
        )
        self.self_check_cross_user_guard_seconds = max(
            8,
            int(self_check_cfg.get("cross_user_guard_seconds", 45)),
        )

        default_overload_notice = "你们等等呀，我回复不过来了。请 @我 或叫我的名字（雪 / yukiko），我会优先回你。"
        self.overload_notice_text = (
            normalize_text(str(self.config.get("queue", {}).get("overload_notice_text", default_overload_notice)))
            or default_overload_notice
        )

        # 输出风格
        output_cfg = self.config.get("output", {}) or {}
        self.verbosity = str(output_cfg.get("verbosity", "medium")).lower()
        self.token_saving = bool(output_cfg.get("token_saving", False))
        self._verbosity_group_overrides: dict[str, str] = {}
        raw_overrides = output_cfg.get("group_overrides", {})
        if isinstance(raw_overrides, dict):
            for k, v in raw_overrides.items():
                self._verbosity_group_overrides[str(k)] = str(v).lower()

    def get_verbosity(self, group_id: int | str = 0) -> str:
        """获取指定群的输出详细度。"""
        return self._verbosity_group_overrides.get(str(group_id), self.verbosity)

    def reload_config(self) -> tuple[bool, str]:
        """热重载配置（不重建 ModelClient / Memory 等重量级组件）。"""
        ok, msg = self.config_manager.reload()
        if ok:
            self.config = self.config_manager.raw
            self._init_from_config()
            self.admin = AdminEngine(self.config, self.storage_dir)
            self.safety = SafetyEngine(self.config.get("safety", {}))
            self.emotion = EmotionEngine(self.config.get("emotion", {}))
            self.personality = PersonalityEngine.from_file(self.config_dir / "personality.yml")
            self.logger.info("配置热重载完成")
        return ok, msg

    @classmethod
    def from_default_paths(cls, project_root: Path | None = None) -> "YukikoEngine":
        root = project_root or Path(__file__).resolve().parents[1]
        return cls(project_root=root)

    async def handle_message(self, message: EngineMessage) -> EngineResponse:
        self.admin.increment_message_count()
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

        if text == "__mention_only__" or self._is_bot_alias_only_message(text):
            quick_reply = self._build_mention_only_reply(message.user_name)
            quick_reply = self._apply_tone_guard(quick_reply)
            quick_reply = self._limit_reply_text(quick_reply, "short", proactive=False)
            rendered = self.markdown.render(quick_reply)
            quick_reason = "mention_only" if text == "__mention_only__" else "alias_only_call"
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
        if allow_memory:
            self.memory.add_message(
                conversation_id=message.conversation_id,
                user_id=message.user_id,
                user_name=message.user_name,
                role="user",
                content=text,
                timestamp=message.timestamp,
            )

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

        if not trigger.should_handle:
            if trigger.reason == "not_directed" and self.router.mode == "ai_full":
                trigger.should_handle = True
                trigger.reason = "ai_router_gate"
            else:
                self.logger.info(
                    "消息已忽略 | 会话=%s | 用户=%s | 原因=%s | 文本=%s",
                    message.conversation_id,
                    message.user_id,
                    trigger.reason,
                    clip_text(text, 80),
                )
                return EngineResponse(action="ignore", reason=trigger.reason)

        recent_messages = self.memory.get_recent_messages(message.conversation_id, limit=20) if allow_memory else []
        memory_context = self.memory.get_recent_texts(message.conversation_id, limit=12) if allow_memory else []
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
            memory_context = (memory_context + [f"[当前用户近期]{item}" for item in current_user_recent])[-18:]
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
        thread_state = self.memory.get_thread_state(message.conversation_id) if allow_memory else {}
        learned_keywords = self.memory.get_conversation_keyword_hints(message.conversation_id, limit=10) if allow_memory else []
        runtime_group_context = self._build_runtime_group_context(
            message.conversation_id,
            limit=self.runtime_group_cache_context_limit,
        )
        if runtime_group_context:
            memory_context = (memory_context + [f"[群聊缓存]{item}" for item in runtime_group_context])[-18:]

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
        )

        effective_min_confidence = self.router_min_confidence
        if not directed_like_call:
            if trigger.followup_candidate or trigger.active_session:
                effective_min_confidence = self.followup_min_confidence
            elif trigger.reason == "ai_router_gate":
                effective_min_confidence = self.ai_gate_min_confidence
            else:
                effective_min_confidence = self.non_directed_min_confidence

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
            if self._looks_like_resend_followup(text):
                resent = self._compose_cached_full_reply(message=message)
                if resent:
                    reply_text = resent
                    force_structured_reply = True
                else:
                    reply_text = "我这边没有可补发的上一条结果。你可以重新发链接或关键词让我再查一次。"
                    force_structured_reply = True
            elif self._looks_like_summary_followup(text):
                quick_summary = self._compose_preferred_summary(message=message, recent_messages=recent_messages)
                if quick_summary:
                    reply_text = quick_summary
                    force_structured_reply = True
                else:
                    reply_text = '你要我总结哪条内容？回我“总结上一条搜索结果”，或者直接说关键词。'
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
                    )
                    if not normalize_text(reply_text):
                        reply_text = search_text
                elif video_url and search_text:
                    # 普通"解析并发视频"场景优先直出工具文本，避免模型二次改写成矛盾拒绝话术。
                    reply_text = search_text
                else:
                    reply_text = search_text or "给你找到了，发你看。"
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
                )
                if not normalize_text(reply_text):
                    reply_text = search_text
        elif action in {"music_search", "music_play"}:
            if tool_result is not None:
                reply_text = normalize_text(str(tool_result.payload.get("text", "")))
                record_b64 = normalize_text(str(tool_result.payload.get("record_b64", "")))
                audio_file = normalize_text(str(tool_result.payload.get("audio_file", "")))
            if not reply_text:
                if action == "music_search":
                    reply_text = "没找到可播放的歌曲，你可以换个关键词再试。"
                else:
                    reply_text = "这首歌这次没播出来，你换个关键词我继续试。"
        elif action == "generate_image":
            if tool_result is not None:
                reply_text = normalize_text(str(tool_result.payload.get("text", "")))
                image_url = normalize_text(str(tool_result.payload.get("image_url", "")))
                if image_url:
                    image_urls = [image_url]
            if not reply_text:
                reply_text = "这次生成失败了，你稍后再试。"
        elif action in {"get_group_member_count", "get_group_member_names", "plugin_call"}:
            if tool_result is not None:
                reply_text = normalize_text(str(tool_result.payload.get("text", "")))
            if not reply_text:
                reply_text = "这个请求执行失败了，你稍后再试一次。"
        else:
            return EngineResponse(action="ignore", reason="router_unknown_action")

        reply_text = self._sanitize_reply_output(reply_text, action=action)
        reply_text = self._enforce_identity_claim(reply_text)
        reply_text = self._apply_tone_guard(reply_text)
        if reply_text:
            reply_text = self._inject_user_name(
                reply_text=reply_text,
                user_name=message.user_name,
                should_address=(
                    message.mentioned
                    or message.is_private
                    or trigger.followup_candidate
                    or trigger.reason in {"directed", "name_call", "followup_window"}
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

        # 超时/解析失败时，如果文本包含明确搜索意图关键词，仍然执行搜索
        if reason in {"router_timeout", "router_parse_error"}:
            text_lower = normalize_text(payload.text).lower()
            explicit_search_cues = (
                "搜索", "互联网搜索", "网上搜", "帮我查", "帮我搜", "百度",
                "谷歌", "google", "bing", "查一下", "搜一下", "找一下",
                "在网上", "在网络上", "联网搜", "上网搜", "网页搜索",
            )
            if any(cue in text_lower for cue in explicit_search_cues):
                return RouterDecision(
                    should_handle=True,
                    action="search",
                    reason=f"{reason}_explicit_search_fallback",
                    confidence=0.78,
                    reply_style="casual",
                    tool_args={"query": payload.text, "mode": "text"},
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
        content = normalize_text(text)
        if not content:
            return "", {}, ""

        has_image = any(
            normalize_text(str((seg or {}).get("type", ""))).lower() == "image"
            for seg in (message.raw_segments or [])
            if isinstance(seg, dict)
        )
        has_video = any(
            normalize_text(str((seg or {}).get("type", ""))).lower() == "video"
            for seg in (message.raw_segments or [])
            if isinstance(seg, dict)
        )

        if self._looks_like_image_analyze_intent(content) and (has_image or self._extract_first_image_url_from_text(content)):
            image_url = self._extract_first_image_url_from_text(content)
            method_args: dict[str, Any] = {"url": image_url} if image_url else {}
            return "media.analyze_image", method_args, "local_force_image_analyze"

        if self._looks_like_video_resolve_intent(content):
            video_url = self._extract_first_video_url_from_text(content)
            if video_url:
                return "browser.resolve_video", {"url": video_url}, "local_force_video_resolve"
            preferred_platform = "douyin.com" if re.search(r"(抖音|douyin)", content, re.IGNORECASE) else ""
            cached_video_url = self._pick_recent_video_source_url(
                message=message,
                preferred_platform=preferred_platform,
            )
            if cached_video_url:
                return (
                    "browser.resolve_video",
                    {"url": cached_video_url},
                    "local_force_video_resolve_from_cache",
                )
            if has_video:
                return "media.pick_video_from_message", {}, "local_force_pick_video"

        if self._looks_like_video_analysis_intent(content):
            video_url = self._extract_first_video_url_from_text(content)
            if video_url:
                return "video.analyze", {"url": video_url}, "local_force_video_analyze"
            if has_video:
                return "video.analyze", {}, "local_force_video_analyze_from_message"

        local_path = self._pick_local_path_candidate(content)
        # 如果文本包含 URL，local_path 可能是 URL 路径的误提取，跳过
        if local_path and self._looks_like_local_file_request(content) and not re.search(r"https?://", content, re.IGNORECASE):
            if self._looks_like_local_media_request(content) or self._looks_like_local_media_path(local_path):
                return "local.media_from_path", {"path": local_path}, "local_force_local_media"
            return "local.read_text", {"path": local_path}, "local_force_local_read"

        if self._looks_like_github_request(content):
            # 如果文本同时包含其他平台关键词（哔哩哔哩/B站/抖音/快手等），
            # 说明用户有多个意图，不要强制覆盖为 github_search，让 router 决定
            multi_intent_cues = ("哔哩哔哩", "b站", "bilibili", "抖音", "douyin", "快手", "kuaishou", "搜索", "视频")
            has_other_intent = any(cue in content.lower() for cue in multi_intent_cues)
            if has_other_intent:
                return "", {}, ""
            if not (
                self._looks_like_repo_readme_request(content)
                or self._looks_like_explicit_request(content)
                or bool(self._extract_github_repo_from_text(content))
            ):
                return "", {}, ""
            repo = self._extract_github_repo_from_text(content)
            if repo and self._looks_like_repo_readme_request(content):
                return "browser.github_readme", {"repo": repo}, "local_force_github_readme"
            return "browser.github_search", {"query": content}, "local_force_github_search"

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
        # 仅当用户有明确“识别后继续查”的文字问题时才联网兜底，避免图片-only误搜
        if not re.search(
            r"(谁|什么|哪|咋|为何|怎么|是不是|是啥|叫什么|哪个|出处|来源|含义|意思|游戏|人物|角色|品牌|型号|这张|这个|中间)",
            candidate,
            re.IGNORECASE,
        ):
            return ""
        if re.fullmatch(r"[A-Za-z]{1,8}", candidate):
            return ""
        return candidate

    def _pick_recent_video_source_url(self, message: EngineMessage, preferred_platform: str = "") -> str:
        key = f"{message.conversation_id}:{message.user_id}"
        cached = self._recent_search_cache.get(key, {})
        if not isinstance(cached, dict):
            return ""
        evidence = cached.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        platform_hint = normalize_text(preferred_platform).lower()

        def _is_match(url: str) -> bool:
            target = normalize_text(url)
            if not target or not re.match(r"^https?://", target, flags=re.IGNORECASE):
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
        return ""

    def _self_check_decision(self, message: EngineMessage, trigger: Any, decision: RouterDecision) -> str:
        """本地自检：在 AI 判定后做一致性约束，降低误回与越界风险。"""
        if not self.self_check_enable:
            return ""

        action = normalize_text(str(decision.action)).lower()
        text_norm = normalize_text(message.text)
        followup_active = bool(getattr(trigger, "followup_candidate", False)) or bool(
            getattr(trigger, "active_session", False)
        )
        has_image_signal = any(
            normalize_text(str((seg or {}).get("type", ""))).lower() == "image"
            for seg in (message.raw_segments or [])
            if isinstance(seg, dict)
        ) or bool(self._extract_first_image_url_from_text(text_norm))
        has_video_signal = any(
            normalize_text(str((seg or {}).get("type", ""))).lower() == "video"
            for seg in (message.raw_segments or [])
            if isinstance(seg, dict)
        ) or bool(self._extract_first_video_url_from_text(text_norm))
        image_reference = bool(re.search(r"(这张图|上张图|上一张图|历史图片|图里|图中|截图|照片)", text_norm))
        if action in {"ignore"}:
            return ""
        if normalize_text(str(getattr(decision, "reason_code", ""))).lower() == "followup_multimodal_fast_path":
            return ""

        # 明确工具型诉求不允许走纯 reply，防止“会说不会做”。
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
            and not followup_active
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

        if self.self_check_block_at_other and message.at_other_user_only and not message.mentioned:
            if (
                not self._allow_at_other_target_dialog(message, normalize_text(message.text))
                and not bool(getattr(trigger, "followup_candidate", False))
                and not bool(getattr(trigger, "active_session", False))
            ):
                return "self_check:at_other_not_for_bot"

        # 监听探测阶段更保守：除非高置信，不主动介入。
        if (
            bool(getattr(trigger, "listen_probe", False))
            and not message.mentioned
            and not message.is_private
            and action in {"reply", "search", "generate_image", "plugin_call"}
            and int(getattr(trigger, "busy_users", 0) or 0) > 1
            and not self._looks_like_explicit_request(normalize_text(message.text))
            and float(decision.confidence) < self.self_check_listen_probe_min_confidence
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
            confidence = float(decision.confidence)
            if (not listen_probe) or confidence < max(self.self_check_non_direct_reply_min_confidence, 0.92):
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
            explicit = self._looks_like_explicit_request(text_norm) or self._looks_like_media_instruction(
                self._extract_multimodal_user_text(text_norm)
            )
            if not explicit and float(decision.confidence) < max(self.self_check_non_direct_reply_min_confidence, 0.9):
                return "self_check:not_directed_action"

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
        if any(alias in content for alias in aliases):
            return True

        # 即使没叫机器人名字，明显"问机器人"的句式也视作指向机器人。
        direct_cues = (
            "你觉得",
            "你怎么看",
            "你认为",
            "你能",
            "你可以",
            "你帮我",
            "请你",
            "问你",
            "评价",
            "分析",
            "说说",
            "总结",
            "帮我",
        )
        if any(cue in content for cue in direct_cues):
            return True

        if ("?" in content or "？" in content) and "你" in content:
            return True
        return False

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
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "你帮我", "帮我", "给我找", "给我发", "请你", "你能", "你可以",
            "你去", "你来", "你给我", "帮忙", "麻烦你", "能不能",
            "搜索", "搜一下", "查一下", "找一下", "查查", "搜搜",
            "互联网搜索", "网上搜", "帮我查", "帮我搜",
            "是什么", "是谁", "怎么", "如何", "为什么",
            "推荐", "有没有", "有什么",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_media_instruction(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "识图",
            "看图",
            "这是什么",
            "图里",
            "图片里",
            "分析",
            "解释",
            "描述",
            "识别",
            "ocr",
            "发出来",
            "发给我",
            "转发",
            "帮我",
            "给我",
            "找",
            "搜",
            "评价",
            "看看",
        )
        return any(cue in content for cue in cues)

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
        cues = (
            "图片", "图", "头像", "发出来", "发图", "壁纸", "pixiv", "image", "photo",
            "视频", "影片", "video", "clip", "mv", "动画", "番剧", "解析",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_video_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if re.search(r"https?://[^\s]+?\.(?:mp4|webm|mov|m4v)(?:\?|$)", content):
            return True
        # BV/av 号识别
        if re.search(r"(?:bv|av)\w{6,}", content, flags=re.IGNORECASE):
            return True
        cues = (
            "视频",
            "影片",
            "发视频",
            "video",
            "clip",
            "mv",
            ".mp4",
            ".webm",
            "抖音",
            "快手",
            "b站",
            "哔哩",
            "bilibili",
            "acfun",
            "a站",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_image_analyze_intent(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "识图",
            "看图",
            "分析这张图",
            "分析这图",
            "图里是什么",
            "图片里是什么",
            "这是什么",
            "识别文字",
            "ocr",
            "帮我看图",
            "解释这张图",
            "描述这张图",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_video_resolve_intent(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        send_or_resolve_cues = (
            "解析这个视频",
            "解析视频",
            "给我解析",
            "帮我解析",
            "解析一下",
            "解析",
            "发出来",
            "发给我",
            "发视频",
            "转发这个视频",
            "下载这个视频",
            "把视频发我",
        )
        return YukikoEngine._looks_like_video_request(content) and any(cue in content for cue in send_or_resolve_cues)

    @staticmethod
    def _looks_like_video_analysis_intent(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if not YukikoEngine._looks_like_video_request(content):
            return False
        cues = (
            "分析视频",
            "解析视频",
            "解读视频",
            "评价视频",
            "总结视频",
            "视频讲了啥",
            "视频讲了什么",
            "这个视频讲了啥",
            "这个视频讲了什么",
            "文字总结",
            "讲讲这个视频",
            "内容总结",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_video_text_only_intent(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        cues = (
            "不需要本地下载发我",
            "不需要下载发我",
            "不用下载发我",
            "不要发视频",
            "只要总结",
            "只要文字总结",
            "只要文本总结",
            "不需要本地下載發我",
            "不需要下載發我",
            "不要發視頻",
            "只要文字總結",
        )
        if any(cue in plain for cue in cues):
            return True
        patterns = (
            r"不(?:需要|用|要)?(?:本地)?(?:下载|下載).{0,8}(?:发我|發我|给我|給我|发送|發送)",
            r"(?:不要|别|別|不需要|不用).{0,4}(?:发|發|发送|發送).{0,4}(?:视频|視頻|影片)",
            r"(?:只要|只需|仅要|僅要|仅需|僅需).{0,4}(?:文字|文本|总结|總結|结论|結論)",
        )
        return any(re.search(pattern, content) for pattern in patterns)

    @staticmethod
    def _looks_like_music_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "点歌",
            "听歌",
            "放歌",
            "搜歌",
            "来首歌",
            "来一首歌",
            "播放歌曲",
            "播放音乐",
            "music",
            "song",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_music_search_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "搜歌",
            "找歌",
            "查歌",
            "有什么歌",
            "歌曲列表",
            "歌单",
            "music search",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _extract_music_keyword(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        content = re.sub(r"^(?:请|麻烦|帮我|给我|你给我|你帮我)\s*", "", content)
        # 去掉常见发起词，仅保留歌名关键词。
        for prefix in ("点歌", "听歌", "放歌", "搜歌", "来首歌", "来一首歌", "播放歌曲", "播放音乐"):
            if content.startswith(prefix):
                content = content[len(prefix) :].strip()
        content = content.strip("：:，,。.!！?？\"'“”‘’")
        if content in {"点歌", "听歌", "放歌", "搜歌"}:
            return ""
        return content

    @staticmethod
    def _looks_like_github_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if bool(re.search(r"https?://(?:www\.)?github\.com/[^\s]+", content, flags=re.IGNORECASE)):
            return True
        cues = ("github", "git hub", "仓库", "repo", "repository", "开源", "源码")
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_repo_readme_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = ("readme", "文档", "学习", "分析", "怎么用", "怎么跑", "看下这个仓库", "看这个项目")
        return any(cue in content for cue in cues)

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
    def _looks_like_local_file_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "本地",
            "文件",
            "路径",
            "读一下",
            "读取",
            "打开",
            "看看这个文件",
            "分析这个文件",
            "学习这个文件",
            "local",
            "read",
            "path",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_local_media_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = ("发出来", "发给我", "发送", "转发", "播放", "看图", "发图", "发视频")
        return any(cue in content for cue in cues)

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

    @staticmethod
    def _sanitize_reply_output(text: str, action: str = "") -> str:
        content = str(text or "")
        content = re.sub(r"</?search_web>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"<\s*tool[^>]*>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"</\s*tool\s*>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = content.strip()
        lower_content = content.lower()
        english_refusal_cues = (
            "i can't discuss",
            "i cannot discuss",
            "i'm an ai assistant",
            "i am an ai assistant",
            "built to help developers",
        )
        if any(cue in lower_content for cue in english_refusal_cues):
            content = "抱歉，这个问题我暂时回答不了。你可以换个角度问，或者告诉我你具体想了解哪方面。"
        # 音乐结果经常包含英文歌名/艺人名，不做英文占比兜底替换。
        if action in {"music_search", "music_play"}:
            return content

        # 英文兜底：如果回复几乎全是英文（中文字符占比极低），保留原文但追加中文提示。
        # 不再直接替换为无意义的敷衍文本。
        if content and len(content) >= 20:
            cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
            total_alpha = sum(1 for ch in content if ch.isalpha())
            if total_alpha > 0 and cjk_count / max(total_alpha, 1) < 0.1:
                # 几乎全英文 — 保留原始内容，不替换
                pass
        return content

    @staticmethod
    def _enforce_identity_claim(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        blocked_vendor_cues = (
            "openai",
            "chatgpt",
            "anthropic",
            "claude",
            "gemini",
            "google ai",
            "deepseek 官方",
            "kiro",
        )
        lower = content.lower()
        vendor_hit = any(cue in lower for cue in blocked_vendor_cues)
        identity_self_claim = bool(
            re.search(r"(?i)\b(i am|i'm)\b.{0,28}\b(ai|assistant|model|bot)\b", content)
            or re.search(r"(我是|我叫).{0,28}(ai|助手|模型|机器人)", content, flags=re.IGNORECASE)
        )
        if vendor_hit:
            content = re.sub(
                r"(?i)\b(openai|chatgpt|anthropic|claude|gemini|kiro|google ai)\b",
                "SKIAPI",
                content,
            )
        if (vendor_hit or identity_self_claim) and "skiapi" not in content.lower():
            content += "（我是基于 SKIAPI 的助手）"
        return content

    @staticmethod
    def _contains_video_send_negative_claim(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "没法直接下载",
            "不能发视频",
            "无法发送视频",
            "不能直发",
            "没法发出来",
            "无法下载发出",
            "只能给链接",
        )
        return any(cue in content for cue in cues)

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

    @staticmethod
    def _build_mention_only_reply(user_name: str) -> str:
        name = normalize_text(user_name)
        if name:
            return (
                f"{name}，我在。你可以直接说要我做什么：搜资料、找图发图、"
                "发可直发视频链接、写代码或排错。"
            )
        return "我在。你可以直接说要我做什么：搜资料、找图发图、发可直发视频链接、写代码或排错。"

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
    ) -> None:
        self.trigger.activate_session(
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            is_private=message.is_private,
            now=message.timestamp,
        )

        if open_followup:
            if self.followup_consume_on_send:
                # 延迟到传输层成功回调再创建并消费 followup，避免发送失败时误开窗口。
                pass
            else:
                self.trigger.mark_reply_target(message.conversation_id, message.user_id, message.timestamp)
                self.trigger.consume_followup_turn(
                    conversation_id=message.conversation_id,
                    user_id=message.user_id,
                    now=message.timestamp,
                )

        if proactive:
            self.trigger.mark_proactive_reply(message.conversation_id, message.timestamp)

        self._last_reply_state[message.conversation_id] = {
            "user_id": message.user_id,
            "timestamp": message.timestamp,
            "action": action,
        }

        if bool(self.config.get("bot", {}).get("allow_memory", True)) and reply_text:
            self.memory.add_message(
                conversation_id=message.conversation_id,
                user_id=self.config.get("bot", {}).get("name", "yukiko"),
                user_name=self.config.get("bot", {}).get("name", "yukiko"),
                role="assistant",
                content=reply_text,
                timestamp=datetime.now(timezone.utc),
            )
            self.memory.write_daily_snapshot()

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
        self.trigger.consume_followup_turn(
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

    @staticmethod
    def _looks_like_summary_followup(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "总结",
            "概括",
            "简短说",
            "说重点",
            "一句话说",
            "提炼",
            "给我总结",
            "简要总结",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_resend_followup(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "补发",
            "重发",
            "再发一次",
            "重新发",
            "发完整",
            "完整发我",
            "原文发我",
            "继续发",
            "继续补发",
            "补发字幕",
        )
        return any(cue in content for cue in cues)

    def _compose_cached_full_reply(self, message: EngineMessage) -> str:
        key = f"{message.conversation_id}:{message.user_id}"
        cached = self._recent_search_cache.get(key, {})
        if not isinstance(cached, dict):
            return ""
        cached_ts = cached.get("timestamp")
        now = message.timestamp if isinstance(message.timestamp, datetime) else datetime.now(timezone.utc)
        if isinstance(cached_ts, datetime):
            try:
                if (now - cached_ts).total_seconds() > 30 * 60:
                    return ""
            except Exception:
                return ""
        full_text = str(cached.get("full_text", "") or "").strip()
        if not full_text:
            full_text = normalize_text(str(cached.get("summary", "")))
        if not full_text:
            return ""
        return f"补发上一条结果：\n{clip_text(full_text, 3200)}"

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
                    if (now - cached_ts).total_seconds() <= 20 * 60:
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
        if tool_result is None and not search_text:
            return
        key = f"{message.conversation_id}:{message.user_id}"
        evidence: list[dict[str, Any]] = []
        if tool_result is not None:
            raw_evidence = getattr(tool_result, "evidence", None)
            if isinstance(raw_evidence, list):
                evidence = [item for item in raw_evidence if isinstance(item, dict)]
            if not evidence:
                payload = getattr(tool_result, "payload", {}) or {}
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
        }

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

        if plain.endswith("...") or plain in {"QWQ", "AWA", "OwO"} or len(plain) <= 6:
            repaired = f"{plain.rstrip('.。')}，你继续说具体要我做什么，我马上给你结果。"
            return self.markdown.render(
                repaired,
                max_len=max(self.markdown.max_output_chars, 220),
                max_lines=max(self.markdown.max_output_lines, 4),
            )

        return rendered

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
        if "?" in content or "？" in content:
            return False
        if re.search(r"(吗|嘛|么|呢|谁|什么|怎么|为何|为什么|哪[里个儿]|是否|几[点时个号]|多少|多[大长久远高])", content):
            return False
        if len(content) > self.fragment_hold_max_chars:
            return False
        if re.search(r"https?://", content, flags=re.IGNORECASE):
            return False
        if re.search(r"[。！？!?]", content):
            return False
        if re.search(r"(吗|嘛|么|呢|谁|什么|怎么|为何|为什么)$", content):
            return False
        if self._looks_like_explicit_request(content):
            return False
        if self._is_passive_multimodal_text(content):
            return False

        lower = content.lower()
        greetings = {
            "你好",
            "hello",
            "hi",
            "在吗",
            "在嘛",
            "yuki",
            "yukiko",
            "雪",
            "早",
            "早安",
            "晚安",
        }
        if lower in greetings:
            return False

        if content in {"?", "？", "??", "？？", "嗯", "哦", "好", "行", "666", "nb"}:
            return False

        # 负反馈/纠错语句应直接放行，避免被当成碎片吞掉。
        feedback_cues = (
            "错了",
            "不对",
            "你错",
            "瞎猜",
            "胡说",
            "乱说",
            "离谱",
            "扯淡",
            "这不对",
            "补发",
            "重发",
            "继续发",
            "再发一次",
        )
        if any(cue in content for cue in feedback_cues):
            return False

        # @机器人的消息一律不 hold，交给 router LLM 判断意图
        if message.mentioned:
            return False

        # 群聊里非 @mention 的短消息，仅对纯英文关键词/ID 做 fragment hold
        # 中文短句不再 hold — 交给 router LLM 判断是否需要回复
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
        if content in {"?", "？", "??", "？？"}:
            return True

        cues = (
            "是谁",
            "是什么",
            "什么意思",
            "什么来历",
            "来历",
            "谁",
            "哪位",
            "哪来的",
            "怎么说",
            "怎么写",
            "总结",
            "简短说",
            "快点说",
            "详细说",
            "分段说",
            "分析一下",
            "介绍一下",
            "给我总结",
            "上网搜",
            "你去搜",
        )
        if any(cue in content for cue in cues):
            return True

        # "是谁/是什么/怎么..."这类疑问尾句默认视作补句。
        if re.search(r"(谁|什么|哪里|哪位|怎么|为何|为什么|呢|嘛|吗)\s*$", content):
            return True
        return False

    @staticmethod
    def _is_fragment_timeout_nudge(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if content in {"?", "？", "??", "？？", "人呢", "在吗", "快点", "说话", "回复", "还在吗"}:
            return True
        if re.fullmatch(r"[?？!！]+", content):
            return True
        return False
