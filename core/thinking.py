from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from core import prompt_loader as _pl
from core.personality import PersonalityEngine
from core.prompt_policy import PromptPolicy
from core.system_prompts import SystemPromptRelay
from services.model_client import ModelClient
from utils.text import clip_text, normalize_text


@dataclass(slots=True)
class ThinkingInput:
    text: str
    trigger_reason: str = ""
    scene_hint: str = "chat"
    memory_context: list[str] | None = None
    related_memories: list[str] | None = None
    search_summary: str = ""
    user_profile_summary: str = ""
    affinity_hint: str = ""
    mood_hint: str = ""
    recent_speakers: list[tuple[str, str, str]] | None = None


@dataclass(slots=True)
class ThinkingDecision:
    action: str
    reason: str
    reply_style: str = "short"


class ThinkingEngine:
    """Only generates reply text; no local keyword routing."""

    def __init__(
        self,
        config: dict[str, Any],
        personality: PersonalityEngine,
        model_client: ModelClient,
    ):
        bot_cfg = config.get("bot", {}) if isinstance(config, dict) else {}
        search_cfg = config.get("search", {}) if isinstance(config, dict) else {}
        self.bot_name = str(bot_cfg.get("name", "yukiko"))
        self.language = str(bot_cfg.get("language", "zh"))
        self.allow_thinking = bool(bot_cfg.get("allow_thinking", True))
        self.default_source_links = max(1, min(3, int(search_cfg.get("default_source_links", 3))))
        self.personality = personality
        self.model_client = model_client
        self.prompt_policy = PromptPolicy.from_config(config)
        control_cfg = config.get("control", {}) if isinstance(config, dict) else {}
        if not isinstance(control_cfg, dict):
            control_cfg = {}
        self.memory_recall_level = normalize_text(str(control_cfg.get("memory_recall_level", "light"))).lower() or "light"

    _VERBOSITY_MAX_TOKENS = {
        "verbose": 8192,
        "medium": 4096,
        "brief": 2048,
        "minimal": 1024,
    }

    _VERBOSITY_INSTRUCTIONS = {
        "verbose": "回复可以详细展开，给出完整的分析和解释。",
        "medium": "",
        "brief": "回复简短精炼，抓重点，不要展开。",
        "minimal": "用一两句话概括，极简回复。",
    }

    async def generate_reply(
        self,
        user_text: str,
        memory_context: list[str],
        related_memories: list[str],
        reply_style: str,
        search_summary: str = "",
        sensitive_context: str = "",
        user_profile_summary: str = "",
        trigger_reason: str = "",
        scene_hint: str = "chat",
        interest_keywords: tuple[str, ...] = (),
        conflict_keywords: tuple[str, ...] = (),
        verbosity: str = "medium",
        output_style_instruction: str = "",
        current_user_id: str = "",
        current_user_name: str = "",
        recent_speakers: list[tuple[str, str, str]] | None = None,
        compat_context: str = "",
        affinity_hint: str = "",
        mood_hint: str = "",
    ) -> str:
        _ = (interest_keywords, conflict_keywords)
        text = normalize_text(user_text)
        if not text:
            return ""

        scene_tag = normalize_text(scene_hint) or "chat"
        style = reply_style if reply_style in {"short", "casual", "serious", "long"} else "short"
        llm_failed = False

        if self.allow_thinking and self.model_client.enabled:
            try:
                system_prompt = self.personality.system_instruction(
                    bot_name=self.bot_name,
                    language=self.language,
                    current_user_id=current_user_id,
                    current_user_name=current_user_name,
                    recent_speakers=recent_speakers,
                )
                system_prompt = self.prompt_policy.compose_prompt(channel="thinking", base_prompt=system_prompt)
                style_instruction = self.personality.style_instruction(style)
                scene_instruction = self.personality.scene_instruction(scene_tag)
                extra_rules = SystemPromptRelay.thinking_extra_rules()
                verbosity_instruction = self._VERBOSITY_INSTRUCTIONS.get(verbosity, "")
                if verbosity_instruction:
                    extra_rules += "\n输出详细度要求: " + verbosity_instruction
                output_style_instruction = clip_text(normalize_text(output_style_instruction), 320)
                if output_style_instruction:
                    extra_rules += "\n输出风格附加要求: " + output_style_instruction
                verbosity_max_tokens = self._VERBOSITY_MAX_TOKENS.get(verbosity, 4096)
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt + "\n" + style_instruction + "\n" + scene_instruction + "\n" + extra_rules,
                    },
                    {
                        "role": "user",
                        "content": self._build_payload(
                            user_text=text,
                            trigger_reason=trigger_reason,
                            memory_context=memory_context,
                            related_memories=related_memories,
                            search_summary=search_summary,
                            sensitive_context=sensitive_context,
                            user_profile_summary=user_profile_summary,
                            scene_tag=scene_tag,
                            compat_context=compat_context,
                            affinity_hint=affinity_hint,
                            mood_hint=mood_hint,
                            current_user_name=current_user_name,
                            recent_speakers=recent_speakers,
                        ),
                    },
                ]
                output = normalize_text(await self.model_client.chat_text(messages, max_tokens=verbosity_max_tokens))
                if output:
                    return output
            except Exception as exc:
                import logging as _logging
                _logging.getLogger("yukiko.thinking").error(
                    "LLM generate_reply failed: %s", exc, exc_info=True,
                )
                llm_failed = True

        return self._fallback_reply(
            user_text=text,
            style=style,
            scene_tag=scene_tag,
            search_summary=search_summary,
            trigger_reason=trigger_reason,
            llm_failed=llm_failed,
        )

    # PLACEHOLDER_BUILD_PAYLOAD

    def _build_payload(
        self,
        user_text: str,
        trigger_reason: str,
        memory_context: list[str],
        related_memories: list[str],
        search_summary: str,
        sensitive_context: str,
        user_profile_summary: str,
        scene_tag: str,
        compat_context: str,
        affinity_hint: str = "",
        mood_hint: str = "",
        current_user_name: str = "",
        recent_speakers: list[tuple[str, str, str]] | None = None,
    ) -> str:
        now_local = datetime.now().astimezone()
        now_label = now_local.strftime("%Y-%m-%d %H:%M:%S %z")
        tz_name = now_local.tzname() or "local"
        blocks: list[str] = [
            "用户消息:\n" + user_text,
            "场景: " + scene_tag,
            "系统时间: " + now_label + " (" + tz_name + ")",
        ]
        if trigger_reason:
            blocks.append("触发信息: " + trigger_reason)

        # Affinity & mood injection
        emotion_parts: list[str] = []
        if mood_hint:
            emotion_parts.append(mood_hint)
        if affinity_hint:
            emotion_parts.append(affinity_hint)
        if emotion_parts:
            blocks.append(
                "【情感状态】\n"
                + "\n".join(emotion_parts)
                + "\n根据好感度等级自然调整语气亲密度："
                "陌生人→礼貌友好；"
                "普通朋友→轻松随意；"
                "密友/挚友→亲昵撒娇可带昵称；"
                "知己以上→可以主动关心、撒娇、吃醋等深层情感表达。\n"
                "根据当前心情微调回复情绪色彩，但不要刻意提及心情本身。"
            )

        safe_profile = self._sanitize_profile_summary(user_profile_summary)
        if safe_profile:
            who = current_user_name or "当前用户"
            profile_block = (
                "用户画像（" + who
                + "，仅此人，禁止混淆到其他群成员）:\n"
                + clip_text(safe_profile, 400)
            )
            style_guide = self._adaptive_style_hint(safe_profile)
            if style_guide:
                profile_block += "\n回复风格建议: " + style_guide
            blocks.append(profile_block)
        compat_block = normalize_text(compat_context)
        if compat_block:
            blocks.append(clip_text(compat_block, 900))

        if recent_speakers:
            speaker_rows: list[str] = []
            for speaker_id, speaker_name, speaker_text in recent_speakers[:6]:
                sid = normalize_text(speaker_id)
                if not sid:
                    continue
                label = normalize_text(speaker_name) or sid
                said = clip_text(normalize_text(speaker_text), 100)
                speaker_rows.append(
                    f"- {label}(QQ:{sid})" + (f": {said}" if said else "")
                )
            if speaker_rows:
                blocks.append(
                    "最近活跃用户（仅用于群聊指代消解，禁止混淆当前用户）:\n"
                    + "\n".join(speaker_rows)
                )

        # Recent conversation with user attribution
        if memory_context:
            rows: list[str] = []
            for item in memory_context[-12:]:
                cleaned = normalize_text(item)
                if not cleaned:
                    continue
                rows.append("- " + clip_text(cleaned, 100))
            if rows:
                blocks.append(
                    "最近对话（注意区分每条消息的发言人）:\n"
                    + "\n".join(rows)
                )

        if related_memories:
            rows2 = [
                "- " + clip_text(normalize_text(item), 80)
                for item in related_memories[:5]
                if normalize_text(item)
            ]
            if rows2:
                blocks.append(
                    "相关长期记忆:\n" + "\n".join(rows2)
                )
                if self.memory_recall_level != "off":
                    blocks.append(
                        "当相关且确定时，可自然引用："
                        "你上次提到…；不要跨用户混用记忆。"
                    )
        blocks.append(
            "只有当“最近对话/相关长期记忆”"
            "里有明确原文证据时，才允许说“你之前提到过…”。"
            "没有证据严禁编造用户历史偏好、历史提问或历史结论。"
        )
        if search_summary:
            is_video = (
                "关键帧内容描述:" in search_summary
                or "弹幕热词:" in search_summary
            )
            is_rich_search = (
                search_summary.count("标题:") >= 2
                or search_summary.count("摘要:") >= 2
            )
            if is_video:
                max_summary = 2400
                label = "工具结果(视频分析)"
            elif is_rich_search:
                max_summary = 1600
                label = "工具结果(搜索 — 请从以下多条结果中筛选最相关的信息综合回答)"
            else:
                max_summary = 1200
                label = "工具结果(搜索)"
            blocks.append(label + ":\n" + clip_text(search_summary, max_summary))
        if sensitive_context:
            blocks.append(
                "风险上下文:\n" + clip_text(sensitive_context, 300)
            )
        blocks.append(
            "请只输出最终回复正文，不要输出 JSON。"
        )
        return "\n\n".join(blocks)

    @staticmethod
    def _adaptive_style_hint(profile_summary: str) -> str:
        hints: list[str] = []
        lower = profile_summary.lower()
        if "网络用语多" in lower:
            hints.append("可以用网络梗和缩写回复，语气轻松活泼")
        elif "表达偏正式" in lower:
            hints.append("回复语气稍正式，避免过多网络用语")
        if "偏短句" in lower:
            hints.append("回复尽量简短精炼")
        elif "描述偏详细" in lower:
            hints.append("可以适当展开回复")
        if "技术" in lower:
            hints.append("对方可能懂技术，可以用专业术语")
        if "游戏" in lower:
            hints.append("对方是游戏玩家，可以聊游戏相关话题")
        if "动漫" in lower or "二次元" in lower:
            hints.append("对方喜欢二次元，可以用相关梗")
        if "情绪偏消极" in lower or "情绪偏焦虑" in lower:
            hints.append("注意语气温和，多给鼓励")
        return "；".join(hints) if hints else ""

    # PLACEHOLDER_SANITIZE

    @staticmethod
    def _sanitize_profile_summary(profile_summary: str) -> str:
        content = normalize_text(profile_summary)
        if not content:
            return ""
        content = re.sub(
            r"(?:QQ号|qq号|消息数|发言数|发了\d+条消息|凌晨\d+点(?:左右)?活跃|活跃时段|作息规律)[^。；;\n]*[。；;]?",
            "",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(r"\s{2,}", " ", content).strip()
        return content

    def _fallback_reply(
        self,
        user_text: str,
        style: str,
        scene_tag: str,
        search_summary: str,
        trigger_reason: str,
        llm_failed: bool = False,
    ) -> str:
        _ = (user_text, trigger_reason)
        if search_summary:
            return clip_text(search_summary, 220)
        if llm_failed:
            return normalize_text(
                _pl.get_message("llm_error_fallback", "666 出问题了哥")
            ) or "666 出问题了哥"
        if scene_tag == "conflict_mediation":
            return "先冷静一下，把分歧点说清楚，我帮你们拆开看。"
        if scene_tag == "emotion_support":
            return "我在，你可以先说最卡你的那一点。"
        if style == "short":
            return "我在，你继续说。"
        if style == "serious":
            return "先给我目标、环境和报错信息，我按步骤帮你定位。"
        return "收到，我在听。"
