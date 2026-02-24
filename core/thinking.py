from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.personality import PersonalityEngine
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


@dataclass(slots=True)
class ThinkingDecision:
    action: str
    reason: str
    reply_style: str = "short"


class ThinkingEngine:
    """只负责生成回复正文，不承担本地关键词判定。"""

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

    _VERBOSITY_MAX_TOKENS = {
        "verbose": 8192,
        "medium": 4096,
        "brief": 2048,
        "minimal": 1024,
    }

    _VERBOSITY_INSTRUCTIONS = {
        "verbose": "回复可以详细展开，给出完整的分析和解释。",
        "medium": "",  # 默认，不加额外指令
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
    ) -> str:
        _ = (interest_keywords, conflict_keywords)
        text = normalize_text(user_text)
        if not text:
            return ""

        scene_tag = normalize_text(scene_hint) or "chat"
        style = reply_style if reply_style in {"short", "casual", "serious", "long"} else "short"

        if self.allow_thinking and self.model_client.enabled:
            try:
                system_prompt = self.personality.system_instruction(bot_name=self.bot_name, language=self.language)
                style_instruction = self.personality.style_instruction(style)
                scene_instruction = self.personality.scene_instruction(scene_tag)
                extra_rules = SystemPromptRelay.thinking_extra_rules()
                verbosity_instruction = self._VERBOSITY_INSTRUCTIONS.get(verbosity, "")
                if verbosity_instruction:
                    extra_rules += f"\n输出详细度要求: {verbosity_instruction}"
                verbosity_max_tokens = self._VERBOSITY_MAX_TOKENS.get(verbosity, 4096)
                messages = [
                    {
                        "role": "system",
                        "content": f"{system_prompt}\n{style_instruction}\n{scene_instruction}\n{extra_rules}",
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
                        ),
                    },
                ]
                output = normalize_text(await self.model_client.chat_text(messages, max_tokens=verbosity_max_tokens))
                if output:
                    return output
            except Exception:
                pass

        return self._fallback_reply(
            user_text=text,
            style=style,
            scene_tag=scene_tag,
            search_summary=search_summary,
            trigger_reason=trigger_reason,
        )

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
    ) -> str:
        blocks = [f"用户消息:\n{user_text}", f"场景: {scene_tag}"]
        if trigger_reason:
            blocks.append(f"触发信息: {trigger_reason}")
        if user_profile_summary:
            profile_block = (
                "用户画像（仅当前发言用户，禁止混淆到其他群成员）:\n"
                f"{clip_text(user_profile_summary, 400)}"
            )
            # 根据画像生成自适应回复风格提示
            style_guide = self._adaptive_style_hint(user_profile_summary)
            if style_guide:
                profile_block += f"\n回复风格建议: {style_guide}"
            blocks.append(profile_block)
        if memory_context:
            rows = [f"- {clip_text(normalize_text(item), 80)}" for item in memory_context[-8:] if normalize_text(item)]
            if rows:
                blocks.append("最近对话:\n" + "\n".join(rows))
        if related_memories:
            rows = [f"- {clip_text(normalize_text(item), 80)}" for item in related_memories[:5] if normalize_text(item)]
            if rows:
                blocks.append("相关长期记忆:\n" + "\n".join(rows))
        if search_summary:
            # 视频分析结果通常更长，给更大的上下文窗口
            is_video = "关键帧内容描述:" in search_summary or "弹幕热词:" in search_summary
            is_rich_search = search_summary.count("标题:") >= 2 or search_summary.count("摘要:") >= 2
            if is_video:
                max_summary = 2400
                label = "工具结果(视频分析)"
            elif is_rich_search:
                max_summary = 1600
                label = "工具结果(搜索 — 请从以下多条结果中筛选最相关的信息综合回答)"
            else:
                max_summary = 1200
                label = "工具结果(搜索)"
            blocks.append(f"{label}:\n{clip_text(search_summary, max_summary)}")
        if sensitive_context:
            blocks.append(f"风险上下文:\n{clip_text(sensitive_context, 300)}")
        blocks.append("请只输出最终回复正文，不要输出 JSON。")
        return "\n\n".join(blocks)

    @staticmethod
    def _adaptive_style_hint(profile_summary: str) -> str:
        """根据用户画像生成自适应回复风格提示。"""
        hints: list[str] = []
        lower = profile_summary.lower()

        # 语言风格适配
        if "网络用语多" in lower:
            hints.append("可以用网络梗和缩写回复，语气轻松活泼")
        elif "表达偏正式" in lower:
            hints.append("回复语气稍正式，避免过多网络用语")

        # 消息长度适配
        if "偏短句" in lower:
            hints.append("回复尽量简短精炼")
        elif "描述偏详细" in lower:
            hints.append("可以适当展开回复")

        # 话题适配
        if "技术" in lower:
            hints.append("对方可能懂技术，可以用专业术语")
        if "游戏" in lower:
            hints.append("对方是游戏玩家，可以聊游戏相关话题")
        if "动漫" in lower or "二次元" in lower:
            hints.append("对方喜欢二次元，可以用相关梗")

        # 情绪适配
        if "情绪偏消极" in lower or "情绪偏焦虑" in lower:
            hints.append("注意语气温和，多给鼓励")

        return "；".join(hints) if hints else ""

    def _fallback_reply(
        self,
        user_text: str,
        style: str,
        scene_tag: str,
        search_summary: str,
        trigger_reason: str,
    ) -> str:
        _ = (user_text, trigger_reason)
        if search_summary:
            return clip_text(search_summary, 220)

        if scene_tag == "conflict_mediation":
            return "先冷静一下，把分歧点说清楚，我帮你们拆开看。"
        if scene_tag == "emotion_support":
            return "我在，你可以先说最卡你的那一点。"
        if style == "short":
            return "我在，你继续说。"
        if style == "serious":
            return "先给我目标、环境和报错信息，我按步骤帮你定位。"
        return "收到，我在听。"
