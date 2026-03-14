from __future__ import annotations

from datetime import datetime, timezone
import unittest

from core.config_templates import _built_in_config_defaults
from core.sticker import _QQ_DATA_ROOTS
from core.trigger import TriggerEngine, TriggerInput


class ConfigAndTriggerRegressionTests(unittest.TestCase):
    def test_builtin_defaults_are_conservative_for_undirected_messages(self) -> None:
        defaults = _built_in_config_defaults()

        self.assertEqual(defaults["control"]["undirected_policy"], "mention_only")
        self.assertFalse(defaults["bot"]["allow_non_to_me"])
        self.assertFalse(defaults["trigger"]["ai_listen_enable"])
        self.assertFalse(defaults["trigger"]["delegate_undirected_to_ai"])
        self.assertEqual(defaults["trigger"]["delegate_undirected_min_signal"], 1.0)
        self.assertTrue(defaults["bot"]["relationship_progressive_enable"])
        self.assertTrue(defaults["bot"]["kaomoji_enable"])

    def test_trigger_engine_does_not_delegate_undirected_by_default(self) -> None:
        trigger = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})

        self.assertFalse(trigger.ai_listen_enable)
        self.assertFalse(trigger.delegate_undirected_to_ai)

    def test_delegate_undirected_requires_minimum_explicit_signal(self) -> None:
        trigger = TriggerEngine(
            trigger_config={
                "delegate_undirected_to_ai": True,
                "delegate_undirected_min_signal": 1.0,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)

        low_signal = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="随便聊聊",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        low_result = trigger.evaluate(low_signal, recent_messages=[])
        self.assertFalse(low_result.should_handle)
        self.assertEqual(low_result.reason, "not_directed")

        high_signal = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="/help",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        high_result = trigger.evaluate(high_signal, recent_messages=[])
        self.assertFalse(high_result.should_handle)
        self.assertEqual(high_result.reason, "ai_router_candidate")

    def test_linux_qq_data_root_is_supported(self) -> None:
        normalized = {str(path).replace("\\", "/") for path in _QQ_DATA_ROOTS}
        self.assertTrue(
            any(item.endswith("/.config/QQ") for item in normalized),
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
