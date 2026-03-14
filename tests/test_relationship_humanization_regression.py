from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.context_compat import CompatContextInput, build_context_compat_block
from core.engine import EngineMessage, YukikoEngine


class _AffinityStub:
    def __init__(self, *, level: int, interactions: int) -> None:
        self._level = level
        self._interactions = interactions

    def get_user(self, user_id: str):
        _ = user_id
        return SimpleNamespace(level=self._level, total_interactions=self._interactions)


class RelationshipHumanizationRegressionTests(unittest.TestCase):
    @staticmethod
    def _build_engine(*, level: int, interactions: int) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.kaomoji_enable = True
        engine.kaomoji_allowlist = ["QWQ", "AWA"]
        engine.default_kaomoji = "QWQ"
        engine.relationship_progressive_enable = True
        engine.relationship_hard_boundary_enabled = True
        engine.relationship_commitment_min_level = 8
        engine.relationship_commitment_min_interactions = 30
        engine.relationship_commitment_private_only = True
        engine.relationship_boundary_reply_template = "这件事我会认真对待，我们先慢慢来。"
        engine.relationship_commitment_terms = ("结婚", "家庭", "老婆", "老公")
        engine.humanization_profile = {
            "warmth": 0.8,
            "initiative": 0.6,
            "empathy": 0.85,
            "jealousy": 0.2,
            "vulnerability": 0.5,
            "humor": 0.62,
            "tsundere": 0.35,
            "intimacy_pace": 0.4,
        }
        engine.affinity = _AffinityStub(level=level, interactions=interactions)
        engine.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
        return engine

    def test_kaomoji_toggle_can_disable_qwq_awa(self) -> None:
        engine = self._build_engine(level=9, interactions=100)
        engine.kaomoji_enable = False
        text = engine._apply_tone_guard("我想你了 QWQ AWA")
        self.assertNotIn("QWQ", text.upper())
        self.assertNotIn("AWA", text.upper())

    def test_relationship_commitment_guard_blocks_when_threshold_not_met(self) -> None:
        engine = self._build_engine(level=4, interactions=6)
        message = EngineMessage(
            conversation_id="group:1",
            user_id="10001",
            text="我们结婚吧",
            is_private=False,
        )
        guarded = engine._apply_relationship_commitment_guard(
            reply_text="好呀，那我们明天就结婚。",
            user_text="我们结婚吧",
            message=message,
        )
        self.assertEqual(guarded, "这件事我会认真对待，我们先慢慢来。")

    def test_relationship_commitment_guard_allows_when_ready(self) -> None:
        engine = self._build_engine(level=9, interactions=120)
        message = EngineMessage(
            conversation_id="private:1",
            user_id="10001",
            text="和我结婚",
            is_private=True,
        )
        guarded = engine._apply_relationship_commitment_guard(
            reply_text="我会认真考虑，我们一起慢慢走下去。",
            user_text="和我结婚",
            message=message,
        )
        self.assertEqual(guarded, "我会认真考虑，我们一起慢慢走下去。")

    def test_relationship_guard_does_not_hijack_definition_question(self) -> None:
        engine = self._build_engine(level=3, interactions=4)
        message = EngineMessage(
            conversation_id="group:1",
            user_id="10001",
            text="结婚是什么意思",
            is_private=False,
        )
        guarded = engine._apply_relationship_commitment_guard(
            reply_text="结婚通常指法律与社会关系上的伴侣承诺。",
            user_text="结婚是什么意思",
            message=message,
        )
        self.assertEqual(guarded, "结婚通常指法律与社会关系上的伴侣承诺。")

    def test_relationship_prompt_hint_contains_threshold_and_stage(self) -> None:
        engine = self._build_engine(level=6, interactions=15)
        message = EngineMessage(
            conversation_id="group:1",
            user_id="10001",
            text="你好",
            is_private=False,
        )
        hint = engine._build_relationship_prompt_hint(message)
        self.assertIn("当前关系阶段", hint)
        self.assertIn("承诺关系门槛", hint)
        self.assertIn("不要直接答应家庭/结婚等承诺关系", hint)

    def test_context_compat_block_includes_humanization_and_relationship(self) -> None:
        block = build_context_compat_block(
            CompatContextInput(
                conversation_id="group:1",
                user_id="10001",
                user_name="测试用户",
                relationship_summary="当前关系阶段: 熟络朋友（Lv.4 / 互动12次）",
                humanization_summary="温度=0.80 / 共情=0.85 / 亲密推进速度=0.40",
                kaomoji_summary="颜文字开关: 开启（允许: QWQ,AWA）",
            )
        )
        self.assertIn("关系策略:", block)
        self.assertIn("拟人参数:", block)
        self.assertIn("颜文字开关", block)


if __name__ == "__main__":
    unittest.main()
