from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from core import prompt_loader as _pl
from core.personality import PersonalityEngine
from core.prompt_policy import PromptPolicy
from core.system_prompts import SystemPromptRelay
from services.model_client import ModelClient
# 已移除本地关键词判断，完全依赖 AI
# from utils.intent import ...
from utils.text import clip_text, normalize_text


@dataclass(slots=True)
class RouterInput:
    text: str
    conversation_id: str
    user_id: str
    user_name: str
    trace_id: str
    mentioned: bool
    is_private: bool
    at_other_user_only: bool = False
    at_other_user_ids: list[str] = field(default_factory=list)
    reply_to_message_id: str = ""
    reply_to_user_id: str = ""
    reply_to_user_name: str = ""
    reply_to_text: str = ""
    raw_segments: list[dict[str, Any]] = field(default_factory=list)
    media_summary: list[str] = field(default_factory=list)
    recent_messages: list[str] = field(default_factory=list)
    recent_bot_replies: list[str] = field(default_factory=list)
    user_profile_summary: str = ""
    thread_state: dict[str, Any] = field(default_factory=dict)
    queue_depth: int = 0
    busy_messages: int = 0
    busy_users: int = 0
    overload_active: bool = False
    active_session: bool = False
    followup_candidate: bool = False
    listen_probe: bool = False
    risk_level: str = "safe"
    learned_keywords: list[str] = field(default_factory=list)
    runtime_group_context: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RouterDecision:
    should_handle: bool
    action: str
    reason: str
    reason_code: str = ""
    confidence: float = 0.0
    reply_style: str = "short"
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    target_user_id: str = ""


class RouterEngine:
    def __init__(
        self,
        config: dict[str, Any],
        personality: PersonalityEngine,
        model_client: ModelClient,
    ):
        self.config = config if isinstance(config, dict) else {}
        routing_cfg = config.get("routing", {}) if isinstance(config, dict) else {}
        bot_cfg = config.get("bot", {}) if isinstance(config, dict) else {}
        self.mode = str(routing_cfg.get("mode", "ai_full")).strip() or "ai_full"
        self.min_confidence = float(routing_cfg.get("min_confidence", 0.58))
        self.max_tool_hops = max(1, int(routing_cfg.get("max_tool_hops", 1)))
        self.failover_mode = str(routing_cfg.get("failover_mode", "mention_or_private_only")).strip()
        self.trust_ai_fully = bool(routing_cfg.get("trust_ai_fully", True))  # 默认完全信任 AI
        self.followup_fast_path_enable = bool(routing_cfg.get("followup_fast_path_enable", False))  # 禁用快速路径
        self.enable_keyword_heuristics = bool(routing_cfg.get("enable_keyword_heuristics", False))  # 禁用关键词
        self.passive_multimodal_followup_min_confidence = max(
            0.0,
            min(1.0, float(routing_cfg.get("passive_multimodal_followup_min_confidence", 0.72))),
        )
        self.multi_user_dialogue_min_users = max(2, int(routing_cfg.get("multi_user_dialogue_min_users", 2)))
        output_cfg = config.get("output", {}) if isinstance(config, dict) else {}
        self.token_saving = bool(output_cfg.get("token_saving", False))
        self.allow_actions = {
            str(item).strip()
            for item in routing_cfg.get(
                "allow_actions",
                [
                    "ignore",
                    "reply",
                    "search",
                    "generate_image",
                    "music_search",
                    "music_play",
                    "get_group_member_count",
                    "get_group_member_names",
                    "plugin_call",
                    "send_segment",
                    "moderate",
                ],
            )
            if str(item).strip()
        }
        if not self.allow_actions:
            self.allow_actions = {"ignore", "reply"}

        aliases = {normalize_text(str(bot_cfg.get("name", ""))).lower()}
        for item in bot_cfg.get("nicknames", []) or []:
            aliases.add(normalize_text(str(item)).lower())
        aliases.discard("")
        self.bot_aliases = aliases

        self.personality = personality
        self.model_client = model_client
        self.prompt_policy = PromptPolicy.from_config(config)
        self._log = logging.getLogger("yukiko.router")

    async def route(
        self,
        payload: RouterInput,
        plugins: list[dict[str, Any]],
        tool_methods: list[dict[str, Any]] | None = None,
    ) -> RouterDecision:
        fallback = self._fallback_decision(payload)
        fast_path = self._fast_path_decision(payload)
        if fast_path is not None:
            self._log.info(
                "router_fast_path | trace=%s | action=%s | reason=%s",
                payload.trace_id,
                fast_path.action,
                fast_path.reason,
            )
            return fast_path

        # 最小本地兜底：仅@他人且未@机器人，默认不处理；
        # 但如果语句明显在问机器人（例如"你觉得这个人怎么样 @某人"），允许继续走 AI。
        # 注意：followup_candidate / active_session 不应覆盖此判断——
        # 用户显式 @了别人说明这条消息不是对机器人说的。
        if (
            payload.at_other_user_only
            and not payload.mentioned
            and not self._looks_like_bot_address(payload.text)
        ):
            return RouterDecision(
                should_handle=False,
                action="ignore",
                reason="at_other_not_for_bot",
                confidence=0.95,
                reply_style="short",
            )

        if self.mode != "ai_full":
            return fallback
        if not self.model_client.enabled:
            if payload.mentioned or payload.is_private or self._looks_like_bot_address(payload.text):
                return RouterDecision(
                    should_handle=True,
                    action="reply",
                    reason="fallback_direct_no_model",
                    confidence=0.55,
                    reply_style="short",
                )
            return fallback

        plugin_schema = [
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "intent_examples": item.get("intent_examples", []),
                "args_schema": item.get("args_schema", {}),
                "rules": item.get("rules", []),
            }
            for item in plugins
            if item.get("name")
        ]

        method_schema = [
            {
                "name": item.get("name", ""),
                "scope": item.get("scope", ""),
                "description": item.get("description", ""),
                "args_schema": item.get("args_schema", {}),
            }
            for item in (tool_methods or [])
            if isinstance(item, dict) and item.get("name")
        ]

        system_prompt = self._build_system_prompt(plugin_schema, method_schema)
        # token_saving 模式：减少上下文窗口
        recent_limit = 6 if self.token_saving else 12
        user_payload = {
            "message": payload.text,
            "conversation_id": payload.conversation_id,
            "user_id": payload.user_id,
            "user_name": payload.user_name,
            "mentioned": payload.mentioned,
            "is_private": payload.is_private,
            "at_other_user_only": payload.at_other_user_only,
            "at_other_user_ids": payload.at_other_user_ids[:6],
            "reply_to_message_id": payload.reply_to_message_id,
            "reply_to_user_id": payload.reply_to_user_id,
            "reply_to_user_name": payload.reply_to_user_name,
            "reply_to_text": clip_text(payload.reply_to_text, 400),
            "recent_messages": payload.recent_messages[-recent_limit:],
            "recent_bot_replies": payload.recent_bot_replies[-2:],
            "user_profile_summary": "" if self.token_saving else payload.user_profile_summary,
            "thread_state": {} if self.token_saving else payload.thread_state,
            "queue_depth": payload.queue_depth,
            "busy_messages": payload.busy_messages,
            "busy_users": payload.busy_users,
            "overload_active": payload.overload_active,
            "active_session": payload.active_session,
            "followup_candidate": payload.followup_candidate,
            "listen_probe": payload.listen_probe,
            "risk_level": payload.risk_level,
            "learned_keywords": payload.learned_keywords[:12],
            "runtime_group_context": payload.runtime_group_context[-10:],
            "raw_segments": payload.raw_segments,
            "media_summary": payload.media_summary[:8],
            "plugins": plugin_schema,
            "tool_methods": method_schema,
            "constraints": {
                "must_not_reveal_chain_of_thought": True,
                "allow_uncertain_ignore": True,
                "max_tool_hops": self.max_tool_hops,
            },
            "fallback": asdict(fallback),
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

        t0 = time.monotonic()
        data = await self.model_client.chat_json(messages)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        self._log.info("router_llm | trace=%s | elapsed=%dms | raw=%s", payload.trace_id, elapsed_ms, repr(data)[:300])

        if not isinstance(data, dict):
            self._log.warning(
                "router_llm_bad_type | trace=%s | type=%s | fallback=%s",
                payload.trace_id,
                type(data).__name__,
                fallback.action,
            )
            return fallback

        decision = self._parse_decision(data, fallback, plugin_schema, method_schema, payload)
        self._log.info(
            "router_decision | trace=%s | action=%s | confidence=%.2f | reason=%s | style=%s",
            payload.trace_id,
            decision.action,
            decision.confidence,
            decision.reason[:80],
            decision.reply_style,
        )
        return decision

    def _build_system_prompt(
        self,
        plugin_schema: list[dict[str, Any]],
        method_schema: list[dict[str, Any]],
    ) -> str:
        base = SystemPromptRelay.router_system_prompt(
            allow_actions=self.allow_actions,
            plugin_schema=plugin_schema,
            method_schema=method_schema,
        )
        return self.prompt_policy.compose_prompt(channel="router", base_prompt=base)

    def _parse_decision(
        self,
        data: dict[str, Any],
        fallback: RouterDecision,
        plugin_schema: list[dict[str, Any]],
        method_schema: list[dict[str, Any]],
        payload: RouterInput,
    ) -> RouterDecision:
        allowed_styles = {"short", "casual", "serious", "long"}
        plugin_names = {str(item.get("name", "")) for item in plugin_schema}
        method_names = {normalize_text(str(item.get("name", "")).lower()) for item in method_schema}

        action = str(data.get("action", fallback.action)).strip()
        if action not in self.allow_actions:
            action = fallback.action

        should_handle = self._safe_bool(data.get("should_handle"), fallback.should_handle)
        if action == "ignore":
            should_handle = False

        reason = normalize_text(str(data.get("reason", fallback.reason))) or fallback.reason
        reason_code = normalize_text(str(data.get("reason_code", ""))).lower()
        if not reason_code:
            reason_code = self._build_reason_code(action=action, reason=reason)
        confidence = self._safe_float(data.get("confidence"), fallback.confidence)
        confidence = max(0.0, min(1.0, confidence))

        reply_style = str(data.get("reply_style", fallback.reply_style)).strip()
        if reply_style not in allowed_styles:
            reply_style = fallback.reply_style

        llm_tool_name = normalize_text(str(data.get("tool_name", "")))
        tool_name = llm_tool_name
        tool_args = data.get("tool_args", {})
        if not isinstance(tool_args, dict):
            tool_args = {}

        if action == "plugin_call":
            if tool_name not in plugin_names:
                return fallback
        else:
            tool_name = ""
            # 兼容弱模型：把 method 错填到 tool_name 的情况纠正回来。
            if llm_tool_name and not normalize_text(str(tool_args.get("method", ""))):
                guessed_method = self._canonicalize_method_name(llm_tool_name)
                if guessed_method and guessed_method in method_names:
                    normalized_args = dict(tool_args)
                    method_args = normalized_args.get("method_args", {})
                    if not isinstance(method_args, dict):
                        method_args = {}
                    if not method_args:
                        passthrough = {
                            k: v
                            for k, v in normalized_args.items()
                            if k not in {"query", "mode", "method", "method_args", "platform"}
                        }
                        if passthrough:
                            method_args = passthrough
                            for k in passthrough:
                                normalized_args.pop(k, None)
                    normalized_args["method"] = guessed_method
                    normalized_args["method_args"] = method_args
                    tool_args = normalized_args

        method_name_raw = normalize_text(str(tool_args.get("method", "")))
        method_name = self._canonicalize_method_name(method_name_raw)
        if method_name and action != "search":
            self._log.debug("router_override | method_force_search | method=%s | was=%s", method_name, action)
            action = "search"
            should_handle = True
        if method_name:
            # 仅允许约定的兼容方法名称格式。
            if not re.match(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", method_name) or method_name not in method_names:
                tool_args.pop("method", None)
                tool_args.pop("method_args", None)
            else:
                tool_args = dict(tool_args)
                tool_args["method"] = method_name
                method_args = tool_args.get("method_args", {})
                if not isinstance(method_args, dict):
                    tool_args["method_args"] = {}

        target_user_id = normalize_text(str(data.get("target_user_id", "")))
        if target_user_id and not re.fullmatch(r"[0-9]{5,20}", target_user_id):
            target_user_id = ""

        user_text = payload.text
        has_image_media = any(item.startswith("image:") for item in payload.media_summary)
        media_user_text = self._extract_multimodal_user_text(user_text)
        followup_multimodal_window = bool(payload.followup_candidate or payload.active_session)
        passive_multimodal_addressed = (
            payload.mentioned
            or payload.is_private
            or self._looks_like_bot_address(user_text)
            or self._looks_like_media_instruction(user_text)
        )
        passive_multimodal_followup_allowed = (
            followup_multimodal_window
            and has_image_media
            and confidence >= self.passive_multimodal_followup_min_confidence
            and not self._contains_explicit_adult_intent(media_user_text)
            and passive_multimodal_addressed
        )
        media_force = (
            payload.mentioned
            or payload.followup_candidate
            or payload.active_session
            or self._looks_like_bot_address(user_text)
            or payload.busy_users <= 1
        )

        # 关键词覆盖门控：trust_ai_fully=true 时完全跳过关键词覆盖；
        # 否则仅在 AI 置信度 < 0.5 时用关键词兜底。
        keyword_override_enabled = not self.trust_ai_fully and confidence < 0.5

        # 纯"系统拼接的多模态占位文本"默认不触发业务动作，避免用户随手发图被误接话。
        # 但如果同条消息里有明确指令（如"这是什么/识图/把图发送过来"），允许继续处理。
        if (
            self._is_passive_multimodal_event(user_text)
            and not payload.mentioned
            and not payload.is_private
            and not self._looks_like_bot_address(user_text)
            and not self._looks_like_media_instruction(user_text)
            and not passive_multimodal_followup_allowed
            and not payload.active_session
            and not payload.followup_candidate
        ):
            return RouterDecision(
                should_handle=False,
                action="ignore",
                reason="passive_multimodal_event",
                reason_code="passive_multimodal_event",
                confidence=max(confidence, 0.92),
                reply_style="short",
            )

        if (
            not payload.mentioned
            and not payload.is_private
            and not payload.followup_candidate
            and not payload.active_session
            and not self._looks_like_bot_address(user_text)
            and int(payload.busy_users or 0) >= self.multi_user_dialogue_min_users
            and not self._looks_like_group_open_question(user_text)
        ):
            return RouterDecision(
                should_handle=False,
                action="ignore",
                reason="multi_user_dialogue_not_for_bot",
                reason_code="multi_user_dialogue_not_for_bot",
                confidence=max(confidence, 0.9),
                reply_style="short",
            )

        if passive_multimodal_followup_allowed and action in {"ignore", "reply", "search"}:
            self._log.debug("router_override | passive_multimodal_followup | was=%s", action)
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            tool_args["method"] = "media.analyze_image"
            tool_args["method_args"] = {}
            tool_args.pop("mode", None)
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(media_user_text) or "继续分析这张图"

        if (
            has_image_media
            and self._looks_like_media_instruction(user_text)
            and not self._contains_explicit_adult_intent(media_user_text)
            and action in {"ignore", "reply", "search"}
        ):
            self._log.debug("router_override | media_instruction_force_search | was=%s", action)
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            tool_args["method"] = "media.analyze_image"
            tool_args["method_args"] = {}
            tool_args.pop("mode", None)
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(media_user_text) or "分析这张图"

        if keyword_override_enabled and (
            self._looks_like_safe_beauty_request(user_text)
            and not self._contains_explicit_adult_intent(user_text)
            and (action in {"reply", "search"} or (action == "ignore" and media_force))
        ):
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            if self._looks_like_video_request(user_text):
                tool_args["mode"] = "video"
            elif self._looks_like_image_request(user_text):
                tool_args["mode"] = "image"
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(user_text)

        if keyword_override_enabled and action in {"reply", "search"} and self._looks_like_github_request(user_text):
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(user_text)

            method_existing = normalize_text(str(tool_args.get("method", ""))).lower()
            if not method_existing:
                repo = self._extract_github_repo_from_text(user_text)
                if repo and self._looks_like_repo_readme_request(user_text):
                    tool_args["method"] = "browser.github_readme"
                    tool_args["method_args"] = {"repo": repo}
                else:
                    tool_args["method"] = "browser.github_search"
                    if not isinstance(tool_args.get("method_args"), dict):
                        tool_args["method_args"] = {}

        if keyword_override_enabled and self._looks_like_image_request(user_text) and (
            action in {"reply", "search"} or (action == "ignore" and media_force)
        ):
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            tool_args["mode"] = "image"
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(user_text)

        if keyword_override_enabled and (
            self._looks_like_video_request(user_text)
            and not self._contains_explicit_adult_intent(user_text)
            and (action in {"reply", "search"} or (action == "ignore" and media_force))
        ):
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            tool_args["mode"] = "video"
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(user_text)

        if keyword_override_enabled and action == "reply" and self._looks_like_summary_request(user_text):
            should_handle = True
            if reply_style == "short":
                reply_style = "serious"

        # 中性内容救回：AI 误判"小姐姐视频/舞蹈视频/BV号推荐"为软色情而拒绝时，
        # 如果文本不含明确露骨词且是合规媒体请求，强制覆盖为 search。
        if (
            action == "reply"
            and media_force
            and not self._contains_explicit_adult_intent(user_text)
            and (
                self._looks_like_safe_beauty_request(user_text)
                or (self._looks_like_video_request(user_text) and not self._contains_explicit_adult_intent(user_text))
            )
        ):
            action = "search"
            should_handle = True
            tool_args = dict(tool_args)
            if self._looks_like_video_request(user_text):
                tool_args["mode"] = "video"
            elif self._looks_like_image_request(user_text):
                tool_args["mode"] = "image"
            if not normalize_text(str(tool_args.get("query", ""))):
                tool_args["query"] = normalize_text(user_text)

        if payload.at_other_user_only and not payload.mentioned and should_handle and action != "ignore":
            if not self._looks_like_bot_address(user_text):
                return RouterDecision(
                    should_handle=False,
                    action="ignore",
                    reason="at_other_not_for_bot",
                    reason_code="at_other_not_for_bot",
                    confidence=max(confidence, 0.9),
                    reply_style="short",
                )

        if not target_user_id:
            reply_target = normalize_text(payload.reply_to_user_id)
            if reply_target and reply_target != payload.user_id:
                target_user_id = reply_target
            elif payload.at_other_user_ids:
                discuss_cues = ("他", "她", "这个人", "那个人", "ta", "是谁", "叫什么", "哪里人", "你觉得他")
                if any(cue in normalize_text(user_text).lower() for cue in discuss_cues):
                    target_user_id = payload.at_other_user_ids[0]

        return RouterDecision(
            should_handle=should_handle,
            action=action,
            reason=reason,
            reason_code=reason_code,
            confidence=confidence,
            reply_style=reply_style,
            tool_name=tool_name,
            tool_args=tool_args,
            target_user_id=target_user_id,
        )

    def _fast_path_decision(self, payload: RouterInput) -> RouterDecision | None:
        if not self.followup_fast_path_enable:
            return None
        if payload.risk_level in {"illegal", "high_risk"}:
            return None
        if not (payload.followup_candidate or payload.active_session):
            return None

        has_image_media = any(item.startswith("image:") for item in payload.media_summary)
        fast_path_allowed = (
            payload.mentioned
            or payload.is_private
            or self._looks_like_bot_address(payload.text)
            or self._looks_like_media_instruction(payload.text)
        )
        if has_image_media and fast_path_allowed and (
            self._looks_like_media_instruction(payload.text) or self._is_passive_multimodal_event(payload.text)
        ):
            query = normalize_text(self._extract_multimodal_user_text(payload.text)) or "继续分析这张图"
            return RouterDecision(
                should_handle=True,
                action="search",
                reason="followup_multimodal_fast_path",
                reason_code="followup_multimodal_fast_path",
                confidence=0.9,
                reply_style="casual",
                tool_args={"method": "media.analyze_image", "method_args": {}, "query": query},
            )
        return None

    def _looks_like_bot_address(self, text: str) -> bool:
        if not self.enable_keyword_heuristics:  # 纯 AI 模式：禁用关键词
            return False
        content = normalize_text(text).lower()
        if not content:
            return False
        if any(alias and alias in content for alias in self.bot_aliases):
            return True
        direct_cues = ("你觉得", "你怎么看", "你认为", "你能", "你可以", "你帮我", "请你", "问你")
        return any(cue in content for cue in direct_cues)

    @staticmethod
    def _looks_like_image_request(text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    def _looks_like_video_request(self, text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    @staticmethod
    def _looks_like_safe_beauty_request(text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    @staticmethod
    def _contains_explicit_adult_intent(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        adult_cues = (
            "成人",
            "18禁",
            "无码",
            "里番",
            "porn",
            "nsfw",
            "r18",
            "黄网",
            "露点",
            "做爱",
            "性行为",
        )
        return any(cue in content for cue in adult_cues)

    @staticmethod
    def _is_passive_multimodal_event(text: str) -> bool:
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
        content = re.sub(r"\s+", " ", content).strip()
        parts = content.split()
        while parts and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", parts[0]):
            parts.pop(0)
        return " ".join(parts).strip()

    @classmethod
    def _looks_like_media_instruction(cls, text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    def _looks_like_github_request(self, text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    @staticmethod
    def _looks_like_summary_request(text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    @staticmethod
    def _looks_like_group_open_question(text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

    def _looks_like_repo_readme_request(self, text: str) -> bool:
        return False  # 纯 AI 模式：禁用关键词判断

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
    def _canonicalize_method_name(raw: str) -> str:
        value = normalize_text(raw)
        if not value:
            return ""
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()
        value = value.replace("-", "_").replace(" ", "")
        aliases = {
            "resolve_video": "browser.resolve_video",
            "browser.resolvevideo": "browser.resolve_video",
            "video.resolve": "browser.resolve_video",
            "video.resolve_video": "browser.resolve_video",
            "resolve_image": "browser.resolve_image",
            "browser.resolveimage": "browser.resolve_image",
            "fetch_url": "browser.fetch_url",
            "github.search": "browser.github_search",
            "github_search": "browser.github_search",
            "github.readme": "browser.github_readme",
            "github_readme": "browser.github_readme",
            "analyze_image": "media.analyze_image",
            "media.analyze": "media.analyze_image",
            "image.analyze": "media.analyze_image",
            "vision.analyze": "media.analyze_image",
            "pick_image": "media.pick_image_from_message",
            "pick_video": "media.pick_video_from_message",
            "pick_audio": "media.pick_audio_from_message",
            "qq_avatar": "media.qq_avatar",
            "video_analyze": "video.analyze",
            "local.read": "local.read_text",
            "local_read": "local.read_text",
            "read_local": "local.read_text",
            "local.media": "local.media_from_path",
            "local_media": "local.media_from_path",
            "local.send_media": "local.media_from_path",
        }
        if value in aliases:
            return aliases[value]
        return value

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _safe_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = normalize_text(value).strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"false", "0", "no", "n", "off"}:
                return False
        return bool(default)

    @staticmethod
    def _build_reason_code(action: str, reason: str) -> str:
        raw = normalize_text(f"{action}_{reason}").lower()
        if not raw:
            return "unknown"
        raw = re.sub(r"[^a-z0-9_]+", "_", raw)
        raw = re.sub(r"_+", "_", raw).strip("_")
        return raw or "unknown"

    @staticmethod
    def _fallback_decision(payload: RouterInput) -> RouterDecision:
        if payload.risk_level in {"illegal", "high_risk"}:
            return RouterDecision(
                should_handle=True,
                action="moderate",
                reason="fallback_risk_level",
                confidence=1.0,
                reply_style="short",
            )
        if payload.mentioned or payload.is_private:
            return RouterDecision(
                should_handle=True,
                action="reply",
                reason="fallback_direct",
                confidence=0.6,
                reply_style="short",
            )
        return RouterDecision(
            should_handle=False,
            action="ignore",
            reason="fallback_ignore",
            confidence=0.7,
            reply_style="short",
        )
