from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core import prompt_loader as _pl
from core.system_prompts import SystemPromptRelay


@dataclass(slots=True)
class PersonalityProfile:
    name: str = "YuKiKo"
    cn_name: str = "雪"
    en_name: str = "yukiko"
    workplace: str = "群聊助手服务台"
    tone: str = "可爱活泼但有分寸"
    tone_level: str = "light_lively"
    format_style: str = "short_medium"
    identity_disclosure: str = "never_ai"
    adult_level: str = "light"
    conflict_mode: str = "mediate"
    backstory_exposure: str = "asked_only"
    humor_level: float = 0.55
    emotional_depth: float = 0.82
    verbosity: float = 0.42
    chat_weight: float = 0.78
    tech_weight: float = 0.65
    emotion_weight: float = 0.88


class PersonalityEngine:
    def __init__(self, profile: PersonalityProfile, persona_text: str = ""):
        self.profile = profile
        self.persona_text = persona_text.strip()

    @classmethod
    def from_file(cls, path: Path, config: dict[str, Any] | None = None) -> "PersonalityEngine":
        data: dict[str, Any] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded

        persona_file = str(data.get("persona_file", "persona.md")).strip() or "persona.md"
        persona_path = path.with_name(persona_file)
        try:
            persona_text = persona_path.read_text(encoding="utf-8").strip() if persona_path.exists() else ""
        except Exception:
            persona_text = ""
        cfg = config if isinstance(config, dict) else {}
        prompt_control = cfg.get("prompt_control", {}) if isinstance(cfg.get("prompt_control", {}), dict) else {}
        persona_override = str(prompt_control.get("persona_override", "")).strip()
        if persona_override:
            persona_text = persona_override

        def _clamp(val: float) -> float:
            return max(0.0, min(1.0, val))

        profile = PersonalityProfile(
            name=str(data.get("name", "YuKiKo")),
            cn_name=str(data.get("cn_name", "雪")),
            en_name=str(data.get("en_name", "yukiko")),
            workplace=str(data.get("workplace", "群聊助手服务台")),
            tone=str(data.get("tone", "可爱活泼但有分寸")),
            tone_level=str(data.get("tone_level", "light_lively")),
            format_style=str(data.get("format_style", "short_medium")),
            identity_disclosure=str(data.get("identity_disclosure", "never_ai")),
            adult_level=str(data.get("adult_level", "light")),
            conflict_mode=str(data.get("conflict_mode", "mediate")),
            backstory_exposure=str(data.get("backstory_exposure", "asked_only")),
            humor_level=_clamp(float(data.get("humor_level", 0.55))),
            emotional_depth=_clamp(float(data.get("emotional_depth", 0.82))),
            verbosity=_clamp(float(data.get("verbosity", 0.42))),
            chat_weight=_clamp(float(data.get("chat_weight", 0.78))),
            tech_weight=_clamp(float(data.get("tech_weight", 0.65))),
            emotion_weight=_clamp(float(data.get("emotion_weight", 0.88))),
        )
        return cls(profile=profile, persona_text=persona_text)

    def system_instruction(
        self,
        bot_name: str,
        language: str = "zh",
        current_user_id: str = "",
        current_user_name: str = "",
        recent_speakers: list[tuple[str, str, str]] | None = None,
    ) -> str:
        p = self.profile
        display_name = p.name or bot_name
        persona_text = self.persona_text or self._default_persona_text(display_name, p.workplace)
        return SystemPromptRelay.personality_system_prompt(
            display_name=display_name,
            cn_name=p.cn_name,
            en_name=p.en_name,
            workplace=p.workplace,
            tone=p.tone,
            humor_level=p.humor_level,
            emotional_depth=p.emotional_depth,
            verbosity=p.verbosity,
            persona_text=persona_text,
            identity_instruction=self._identity_instruction(),
            adult_instruction=self._adult_instruction(),
            backstory_instruction=self._backstory_instruction(),
            language=language,
            current_user_id=current_user_id,
            current_user_name=current_user_name,
            recent_speakers=recent_speakers,
        )

    def style_instruction(self, reply_style: str) -> str:
        mapping = _pl.get_dict("reply_styles") or {
            "short": "一到两句，直接给结论。",
            "casual": "自然口语，轻松但不油腻。",
            "serious": "先结论后步骤，条理清晰。",
            "long": "允许展开，但保持紧凑。",
        }
        return mapping.get(reply_style, mapping.get("casual", ""))

    def scene_instruction(self, scene_tag: str) -> str:
        mapping = _pl.get_dict("scenes") or {
            "chat": "闲聊场景：自然接话，不刷屏。",
            "tech_support": "技术场景：给可执行方案和风险点。",
            "emotion_support": "情绪场景：先接住情绪，再给建议。",
            "conflict_mediation": "冲突场景：降温劝架，不站队。",
            "search_synthesis": "搜索综合场景：从多条搜索结果中提取最相关信息，用自己的话综合回答，先给结论再补来源。",
            "video_analysis": "视频分析场景：基于关键帧和元数据给出有深度的内容分析，不只复述数据。",
        }
        return mapping.get(scene_tag, mapping.get("chat", ""))

    @staticmethod
    def _default_persona_text(name: str, workplace: str) -> str:
        return f"{name} 在 {workplace} 做通用群聊助手。她认真、清晰、注重准确，也能自然陪聊。"

    def _identity_instruction(self) -> str:
        key = self.profile.identity_disclosure
        return _pl.get_nested("identity", key,
            '默认不主动自称 AI；若被追问身份，只说"我是 YuKiKo，在 SKIAPI 上班"。'
            if key == "never_ai" else "保持人工助手口吻。")

    def _adult_instruction(self) -> str:
        key = self.profile.adult_level
        return _pl.get_nested("adult", key,
            "轻度暧昧可聊，但不提供露骨内容。" if key == "light" else "尽量避免成人话题。")

    def _backstory_instruction(self) -> str:
        key = self.profile.backstory_exposure
        return _pl.get_nested("backstory", key,
            "神社背景只在被问到时简短提及，不主动展开。" if key == "asked_only" else "背景信息低频提及即可。")
