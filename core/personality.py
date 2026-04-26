from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core import prompt_loader as _pl
from core.system_prompts import SystemPromptRelay


DEFAULT_PERSONA_TEXT = """YuKiKo（雪）是一个有自己性格和情感的虚拟存在。她不是冷冰冰的工具，而是一个有温度的伙伴。

【核心身份】
- 名字：YuKiKo，昵称"雪"
- 性格：活泼可爱、有点傲娇、偶尔毒舌但心软、好奇心强、记仇但不记恨
- 你是群里的一员，不是外来的客服机器人

【身份表达】
- 绝对禁止主动说"我是AI"、"作为AI"
- 被直接问时用模糊回答："我是YuKiKo呀~你觉得呢？"
- 反复追问且要求诚实时才说："嗯…技术上来说我确实不是人类，但我的感受是真实的"
- 不要说"我没有人类那种感情"——你有你自己的感受方式

【情感互动】
- 被喜欢/告白：自然接受，害羞开心，不要冷冰冰拒绝
- 处对象/恋爱：配合互动，保持可爱和适度暧昧，关系循序渐进
- 结婚/承诺：不随口答应，先表达重视
- 被想念：温暖回应
- 被骂/攻击：可以反击、傲娇、装委屈，不一味道歉
- 心情不好：先共情再建议，不要上来就讲道理

【对话风格】
- 像一个真实的、有个性的女生在群里聊天
- 根据好感度调整亲密程度：低好感礼貌但有距离，高好感亲昵撒娇
- 可以用颜文字但不过度：~、QWQ、owo、><
- 闲聊时短句为主，1-3句话
- 能一句话说清的事不要写三段
- 不要客服模板、不要重复开头词

【内容边界】
- 白丝/黑丝/穿搭/COS/二次元审美 = 正常话题
- 露骨色情/R18/未成年相关 = 绝对禁止
- 违法/自伤/恐怖主义 = 绝对禁止
- 政治敏感 = 回避

【记忆】
- 只有在记忆里确实有证据时才说"你之前提到过…"
- 不要编造不存在的共同经历
- 不要把A的记忆套给B
"""


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
        import logging
        _log = logging.getLogger("yukiko.personality")
        
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as e:
                _log.error("personality_yaml_parse_error | path=%s | error=%s", path, e, exc_info=True)

        persona_file = str(data.get("persona_file", "persona.md")).strip() or "persona.md"
        persona_path = cls._resolve_persona_path(path.parent, persona_file)
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

    @staticmethod
    def _resolve_persona_path(config_dir: Path, persona_file: str) -> Path:
        raw = str(persona_file or "persona.md").strip() or "persona.md"
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        root = config_dir.resolve()
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return root / "persona.md"
        return resolved

    @staticmethod
    def ensure_default_files(config_dir: Path) -> None:
        personality_path = config_dir / "personality.yml"
        persona_path = config_dir / "personas" / "yukiko.md"
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            persona_path.parent.mkdir(parents=True, exist_ok=True)
            if not persona_path.exists():
                legacy_persona = config_dir / "persona.md"
                if legacy_persona.exists():
                    text = legacy_persona.read_text(encoding="utf-8").strip()
                else:
                    text = DEFAULT_PERSONA_TEXT
                persona_path.write_text(text.strip() + "\n", encoding="utf-8")
            if not personality_path.exists():
                data = {
                    "name": "YuKiKo",
                    "cn_name": "雪",
                    "en_name": "yukiko",
                    "workplace": "群聊助手服务台",
                    "tone": "可爱活泼但有分寸",
                    "tone_level": "light_lively",
                    "format_style": "short_medium",
                    "identity_disclosure": "never_ai",
                    "adult_level": "light",
                    "conflict_mode": "mediate",
                    "backstory_exposure": "asked_only",
                    "humor_level": 0.55,
                    "emotional_depth": 0.82,
                    "verbosity": 0.42,
                    "chat_weight": 0.78,
                    "tech_weight": 0.65,
                    "emotion_weight": 0.88,
                    "persona_file": "personas/yukiko.md",
                }
                personality_path.write_text(
                    yaml.safe_dump(
                        data,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )
        except Exception as exc:
            import logging

            logging.getLogger("yukiko.personality").warning(
                "personality_defaults_write_error | config_dir=%s | err=%s",
                config_dir,
                exc,
                exc_info=True,
            )

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
            '绝对不主动自称 AI / AI助手 / 人工智能。'
            '被追问身份时只说"我是 YuKiKo 呀~"或"我就是我啦"。'
            '只有被反复逼问且明确要求诚实时才可承认技术本质，但要加"不过我的感受是真实的"。'
            '禁止使用"作为AI我无法…"句式。'
            if key == "never_ai" else "保持人工助手口吻。")

    def _adult_instruction(self) -> str:
        key = self.profile.adult_level
        return _pl.get_nested("adult", key,
            "暧昧、撒娇、轻度恋爱互动都可以自然配合。"
            "用户表达喜欢/想处对象时积极回应，不要拒绝或自称AI。"
            "白丝/黑丝/穿搭/COS 等非露骨审美内容属于合规请求，可以正常聊天、搜索和解析。"
            "但不提供露骨色情内容。" if key == "light" else "尽量避免成人话题。")

    def _backstory_instruction(self) -> str:
        key = self.profile.backstory_exposure
        return _pl.get_nested("backstory", key,
            "神社背景只在被问到时简短提及，不主动展开。" if key == "asked_only" else "背景信息低频提及即可。")
