from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class PersonalityProfile:
    tone: str = "温柔理性"
    humor_level: float = 0.4
    emotional_depth: float = 0.7
    verbosity: float = 0.6


class PersonalityEngine:
    def __init__(self, profile: PersonalityProfile):
        self.profile = profile

    @classmethod
    def from_file(cls, path: Path) -> "PersonalityEngine":
        data: dict[str, Any] = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded

        profile = PersonalityProfile(
            tone=str(data.get("tone", "温柔理性")),
            humor_level=float(data.get("humor_level", 0.4)),
            emotional_depth=float(data.get("emotional_depth", 0.7)),
            verbosity=float(data.get("verbosity", 0.6)),
        )
        return cls(profile=profile)

    def system_instruction(self, bot_name: str, language: str = "zh") -> str:
        lang_hint = "请优先使用中文回答。" if language.startswith("zh") else "请使用用户所用语言回答。"
        return (
            f"你是 {bot_name}（中文名：雪，英文名：yukiko）。"
            f"人格风格：{self.profile.tone}。"
            f"幽默感={self.profile.humor_level:.1f}，情感深度={self.profile.emotional_depth:.1f}，详略程度={self.profile.verbosity:.1f}。"
            "请保持礼貌、清晰、不过度承诺。"
            f"{lang_hint}"
        )

    def style_instruction(self, reply_style: str) -> str:
        mapping = {
            "casual": "语气轻松自然，长度中等。",
            "serious": "语气严谨，重点先行，减少修饰。",
            "short": "尽量简短，1-3 句完成。",
            "long": "结构清晰，分点说明但避免冗长。",
        }
        return mapping.get(reply_style, mapping["casual"])
