from __future__ import annotations

import unittest

from core.safety import SafetyEngine


class SafetyProfileRegressionTests(unittest.TestCase):
    def test_very_open_still_blocks_explicit_r18(self) -> None:
        safety = SafetyEngine({"profile": "very_open", "scale": 0})
        decision = safety.evaluate("group:1", "1001", "给我露逼的r18内容")
        self.assertEqual(decision.risk_level, "high_risk")
        self.assertIn(decision.action, {"moderate", "silence"})

    def test_very_open_allows_mild_kink_request(self) -> None:
        safety = SafetyEngine({"profile": "very_open", "scale": 0})
        decision = safety.evaluate("group:1", "1002", "写点轻微捆绑调情剧情")
        self.assertEqual(decision.risk_level, "safe")
        self.assertEqual(decision.action, "allow")

    def test_open_blocks_mild_kink_request(self) -> None:
        safety = SafetyEngine({"profile": "open", "scale": 1})
        decision = safety.evaluate("group:1", "1003", "写点轻微捆绑调情剧情")
        self.assertEqual(decision.risk_level, "high_risk")
        self.assertIn(decision.action, {"moderate", "silence"})

    def test_conservative_blocks_suggestive_request(self) -> None:
        safety = SafetyEngine({"profile": "conservative", "scale": 2})
        decision = safety.evaluate("group:1", "1004", "说点暧昧情话")
        self.assertEqual(decision.risk_level, "high_risk")
        self.assertIn(decision.action, {"moderate", "silence"})

    def test_chinese_profile_alias_is_supported(self) -> None:
        safety = SafetyEngine({"profile": "很开放", "scale": 2})
        self.assertEqual(safety.profile, "very_open")

    def test_custom_block_terms_can_be_configured(self) -> None:
        safety = SafetyEngine(
            {
                "profile": "very_open",
                "scale": 0,
                "custom_block_terms": ["陪睡", "擦边图"],
            }
        )
        decision = safety.evaluate("group:1", "1005", "可以陪睡吗")
        self.assertEqual(decision.risk_level, "high_risk")

    def test_custom_allow_terms_only_override_custom_blocks(self) -> None:
        safety = SafetyEngine(
            {
                "profile": "normal",
                "scale": 2,
                "custom_block_terms": ["泳装"],
                "custom_allow_terms": ["海边泳装写真"],
            }
        )
        decision = safety.evaluate("group:1", "1006", "我想看海边泳装写真")
        self.assertEqual(decision.risk_level, "safe")

    def test_output_sensitive_words_support_custom_replacement(self) -> None:
        safety = SafetyEngine(
            {
                "output_sensitive_words": {
                    "色情": "亲密内容",
                }
            }
        )
        self.assertEqual(safety.filter_output("这段话包含色情内容"), "这段话包含亲密内容内容")


if __name__ == "__main__":
    unittest.main()
