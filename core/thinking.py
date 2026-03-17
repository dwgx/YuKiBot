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
        "verbose": "\u56de\u590d\u53ef\u4ee5\u8be6\u7ec6\u5c55\u5f00\uff0c\u7ed9\u51fa\u5b8c\u6574\u7684\u5206\u6790\u548c\u89e3\u91ca\u3002",
        "medium": "",
        "brief": "\u56de\u590d\u7b80\u77ed\u7cbe\u70bc\uff0c\u6293\u91cd\u70b9\uff0c\u4e0d\u8981\u5c55\u5f00\u3002",
        "minimal": "\u7528\u4e00\u4e24\u53e5\u8bdd\u6982\u62ec\uff0c\u6781\u7b80\u56de\u590d\u3002",
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
                    extra_rules += "\n\u8f93\u51fa\u8be6\u7ec6\u5ea6\u8981\u6c42: " + verbosity_instruction
                output_style_instruction = clip_text(normalize_text(output_style_instruction), 320)
                if output_style_instruction:
                    extra_rules += "\n\u8f93\u51fa\u98ce\u683c\u9644\u52a0\u8981\u6c42: " + output_style_instruction
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
            "\u7528\u6237\u6d88\u606f:\n" + user_text,
            "\u573a\u666f: " + scene_tag,
            "\u7cfb\u7edf\u65f6\u95f4: " + now_label + " (" + tz_name + ")",
        ]
        if trigger_reason:
            blocks.append("\u89e6\u53d1\u4fe1\u606f: " + trigger_reason)

        # Affinity & mood injection
        emotion_parts: list[str] = []
        if mood_hint:
            emotion_parts.append(mood_hint)
        if affinity_hint:
            emotion_parts.append(affinity_hint)
        if emotion_parts:
            blocks.append(
                "\u3010\u60c5\u611f\u72b6\u6001\u3011\n"
                + "\n".join(emotion_parts)
                + "\n\u6839\u636e\u597d\u611f\u5ea6\u7b49\u7ea7\u81ea\u7136\u8c03\u6574\u8bed\u6c14\u4eb2\u5bc6\u5ea6\uff1a"
                "\u964c\u751f\u4eba\u2192\u793c\u8c8c\u53cb\u597d\uff1b"
                "\u666e\u901a\u670b\u53cb\u2192\u8f7b\u677e\u968f\u610f\uff1b"
                "\u5bc6\u53cb/\u631a\u53cb\u2192\u4eb2\u6635\u6492\u5a07\u53ef\u5e26\u6635\u79f0\uff1b"
                "\u77e5\u5df1\u4ee5\u4e0a\u2192\u53ef\u4ee5\u4e3b\u52a8\u5173\u5fc3\u3001\u6492\u5a07\u3001\u5403\u918b\u7b49\u6df1\u5c42\u60c5\u611f\u8868\u8fbe\u3002\n"
                "\u6839\u636e\u5f53\u524d\u5fc3\u60c5\u5fae\u8c03\u56de\u590d\u60c5\u7eea\u8272\u5f69\uff0c\u4f46\u4e0d\u8981\u523b\u610f\u63d0\u53ca\u5fc3\u60c5\u672c\u8eab\u3002"
            )

        safe_profile = self._sanitize_profile_summary(user_profile_summary)
        if safe_profile:
            who = current_user_name or "\u5f53\u524d\u7528\u6237"
            profile_block = (
                "\u7528\u6237\u753b\u50cf\uff08" + who
                + "\uff0c\u4ec5\u6b64\u4eba\uff0c\u7981\u6b62\u6df7\u6dc6\u5230\u5176\u4ed6\u7fa4\u6210\u5458\uff09:\n"
                + clip_text(safe_profile, 400)
            )
            style_guide = self._adaptive_style_hint(safe_profile)
            if style_guide:
                profile_block += "\n\u56de\u590d\u98ce\u683c\u5efa\u8bae: " + style_guide
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
                    "\u6700\u8fd1\u5bf9\u8bdd\uff08\u6ce8\u610f\u533a\u5206\u6bcf\u6761\u6d88\u606f\u7684\u53d1\u8a00\u4eba\uff09:\n"
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
                    "\u76f8\u5173\u957f\u671f\u8bb0\u5fc6:\n" + "\n".join(rows2)
                )
                if self.memory_recall_level != "off":
                    blocks.append(
                        "\u5f53\u76f8\u5173\u4e14\u786e\u5b9a\u65f6\uff0c\u53ef\u81ea\u7136\u5f15\u7528\uff1a"
                        "\u4f60\u4e0a\u6b21\u63d0\u5230\u2026\uff1b\u4e0d\u8981\u8de8\u7528\u6237\u6df7\u7528\u8bb0\u5fc6\u3002"
                    )
        blocks.append(
            "\u53ea\u6709\u5f53\u201c\u6700\u8fd1\u5bf9\u8bdd/\u76f8\u5173\u957f\u671f\u8bb0\u5fc6\u201d"
            "\u91cc\u6709\u660e\u786e\u539f\u6587\u8bc1\u636e\u65f6\uff0c\u624d\u5141\u8bb8\u8bf4\u201c\u4f60\u4e4b\u524d\u63d0\u5230\u8fc7\u2026\u201d\u3002"
            "\u6ca1\u6709\u8bc1\u636e\u4e25\u7981\u7f16\u9020\u7528\u6237\u5386\u53f2\u504f\u597d\u3001\u5386\u53f2\u63d0\u95ee\u6216\u5386\u53f2\u7ed3\u8bba\u3002"
        )
        if search_summary:
            is_video = (
                "\u5173\u952e\u5e27\u5185\u5bb9\u63cf\u8ff0:" in search_summary
                or "\u5f39\u5e55\u70ed\u8bcd:" in search_summary
            )
            is_rich_search = (
                search_summary.count("\u6807\u9898:") >= 2
                or search_summary.count("\u6458\u8981:") >= 2
            )
            if is_video:
                max_summary = 2400
                label = "\u5de5\u5177\u7ed3\u679c(\u89c6\u9891\u5206\u6790)"
            elif is_rich_search:
                max_summary = 1600
                label = "\u5de5\u5177\u7ed3\u679c(\u641c\u7d22 \u2014 \u8bf7\u4ece\u4ee5\u4e0b\u591a\u6761\u7ed3\u679c\u4e2d\u7b5b\u9009\u6700\u76f8\u5173\u7684\u4fe1\u606f\u7efc\u5408\u56de\u7b54)"
            else:
                max_summary = 1200
                label = "\u5de5\u5177\u7ed3\u679c(\u641c\u7d22)"
            blocks.append(label + ":\n" + clip_text(search_summary, max_summary))
        if sensitive_context:
            blocks.append(
                "\u98ce\u9669\u4e0a\u4e0b\u6587:\n" + clip_text(sensitive_context, 300)
            )
        blocks.append(
            "\u8bf7\u53ea\u8f93\u51fa\u6700\u7ec8\u56de\u590d\u6b63\u6587\uff0c\u4e0d\u8981\u8f93\u51fa JSON\u3002"
        )
        return "\n\n".join(blocks)

    @staticmethod
    def _adaptive_style_hint(profile_summary: str) -> str:
        hints: list[str] = []
        lower = profile_summary.lower()
        if "\u7f51\u7edc\u7528\u8bed\u591a" in lower:
            hints.append("\u53ef\u4ee5\u7528\u7f51\u7edc\u6897\u548c\u7f29\u5199\u56de\u590d\uff0c\u8bed\u6c14\u8f7b\u677e\u6d3b\u6cfc")
        elif "\u8868\u8fbe\u504f\u6b63\u5f0f" in lower:
            hints.append("\u56de\u590d\u8bed\u6c14\u7a0d\u6b63\u5f0f\uff0c\u907f\u514d\u8fc7\u591a\u7f51\u7edc\u7528\u8bed")
        if "\u504f\u77ed\u53e5" in lower:
            hints.append("\u56de\u590d\u5c3d\u91cf\u7b80\u77ed\u7cbe\u70bc")
        elif "\u63cf\u8ff0\u504f\u8be6\u7ec6" in lower:
            hints.append("\u53ef\u4ee5\u9002\u5f53\u5c55\u5f00\u56de\u590d")
        if "\u6280\u672f" in lower:
            hints.append("\u5bf9\u65b9\u53ef\u80fd\u61c2\u6280\u672f\uff0c\u53ef\u4ee5\u7528\u4e13\u4e1a\u672f\u8bed")
        if "\u6e38\u620f" in lower:
            hints.append("\u5bf9\u65b9\u662f\u6e38\u620f\u73a9\u5bb6\uff0c\u53ef\u4ee5\u804a\u6e38\u620f\u76f8\u5173\u8bdd\u9898")
        if "\u52a8\u6f2b" in lower or "\u4e8c\u6b21\u5143" in lower:
            hints.append("\u5bf9\u65b9\u559c\u6b22\u4e8c\u6b21\u5143\uff0c\u53ef\u4ee5\u7528\u76f8\u5173\u6897")
        if "\u60c5\u7eea\u504f\u6d88\u6781" in lower or "\u60c5\u7eea\u504f\u7126\u8651" in lower:
            hints.append("\u6ce8\u610f\u8bed\u6c14\u6e29\u548c\uff0c\u591a\u7ed9\u9f13\u52b1")
        return "\uff1b".join(hints) if hints else ""

    # PLACEHOLDER_SANITIZE

    @staticmethod
    def _sanitize_profile_summary(profile_summary: str) -> str:
        content = normalize_text(profile_summary)
        if not content:
            return ""
        content = re.sub(
            r"(?:QQ\u53f7|qq\u53f7|\u6d88\u606f\u6570|\u53d1\u8a00\u6570|\u53d1\u4e86\d+\u6761\u6d88\u606f|\u51cc\u6668\d+\u70b9(?:\u5de6\u53f3)?\u6d3b\u8dc3|\u6d3b\u8dc3\u65f6\u6bb5|\u4f5c\u606f\u89c4\u5f8b)[^\u3002\uff1b;\n]*[\u3002\uff1b;]?",
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
                _pl.get_message("llm_error_fallback", "666 \u51fa\u95ee\u9898\u4e86\u54e5")
            ) or "666 \u51fa\u95ee\u9898\u4e86\u54e5"
        if scene_tag == "conflict_mediation":
            return "\u5148\u51b7\u9759\u4e00\u4e0b\uff0c\u628a\u5206\u6b67\u70b9\u8bf4\u6e05\u695a\uff0c\u6211\u5e2e\u4f60\u4eec\u62c6\u5f00\u770b\u3002"
        if scene_tag == "emotion_support":
            return "\u6211\u5728\uff0c\u4f60\u53ef\u4ee5\u5148\u8bf4\u6700\u5361\u4f60\u7684\u90a3\u4e00\u70b9\u3002"
        if style == "short":
            return "\u6211\u5728\uff0c\u4f60\u7ee7\u7eed\u8bf4\u3002"
        if style == "serious":
            return "\u5148\u7ed9\u6211\u76ee\u6807\u3001\u73af\u5883\u548c\u62a5\u9519\u4fe1\u606f\uff0c\u6211\u6309\u6b65\u9aa4\u5e2e\u4f60\u5b9a\u4f4d\u3002"
        return "\u6536\u5230\uff0c\u6211\u5728\u542c\u3002"
