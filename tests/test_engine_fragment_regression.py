from __future__ import annotations

import unittest

from core.engine import EngineMessage, YukikoEngine


class EngineFragmentRegressionTests(unittest.TestCase):
    def _build_engine(self) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.fragment_hold_max_chars = 24
        engine._looks_like_explicit_request = lambda text: False
        engine._is_passive_multimodal_text = lambda text: False
        return engine

    def test_should_hold_only_plain_token_like_fragments(self) -> None:
        engine = self._build_engine()
        message = EngineMessage(conversation_id="c", user_id="u", text="abc_123")

        self.assertTrue(engine._should_hold_as_fragment(message, "abc_123"))
        self.assertFalse(engine._should_hold_as_fragment(message, "在吗"))
        self.assertFalse(engine._should_hold_as_fragment(message, "为什么"))
        self.assertFalse(engine._should_hold_as_fragment(message, "快点"))

    def test_fragment_continuation_accepts_only_punctuation_nudges(self) -> None:
        self.assertTrue(YukikoEngine._is_fragment_continuation("??"))
        self.assertTrue(YukikoEngine._is_fragment_continuation("？！"))
        self.assertFalse(YukikoEngine._is_fragment_continuation("为什么"))
        self.assertFalse(YukikoEngine._is_fragment_continuation("是谁"))

    def test_fragment_timeout_nudge_accepts_only_punctuation(self) -> None:
        self.assertTrue(YukikoEngine._is_fragment_timeout_nudge("??"))
        self.assertTrue(YukikoEngine._is_fragment_timeout_nudge("!!!"))
        self.assertFalse(YukikoEngine._is_fragment_timeout_nudge("快点"))
        self.assertFalse(YukikoEngine._is_fragment_timeout_nudge("在吗"))


if __name__ == "__main__":
    unittest.main()
