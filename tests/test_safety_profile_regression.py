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


if __name__ == "__main__":
    unittest.main()

