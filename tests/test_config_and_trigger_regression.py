from __future__ import annotations

import unittest

from core.config_templates import _built_in_config_defaults
from core.sticker import _QQ_DATA_ROOTS
from core.trigger import TriggerEngine


class ConfigAndTriggerRegressionTests(unittest.TestCase):
    def test_builtin_defaults_are_conservative_for_undirected_messages(self) -> None:
        defaults = _built_in_config_defaults()

        self.assertEqual(defaults["control"]["undirected_policy"], "mention_only")
        self.assertFalse(defaults["bot"]["allow_non_to_me"])
        self.assertFalse(defaults["trigger"]["ai_listen_enable"])
        self.assertFalse(defaults["trigger"]["delegate_undirected_to_ai"])

    def test_trigger_engine_does_not_delegate_undirected_by_default(self) -> None:
        trigger = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})

        self.assertFalse(trigger.ai_listen_enable)
        self.assertFalse(trigger.delegate_undirected_to_ai)

    def test_linux_qq_data_root_is_supported(self) -> None:
        normalized = {str(path).replace("\\", "/") for path in _QQ_DATA_ROOTS}
        self.assertTrue(
            any(item.endswith("/.config/QQ") for item in normalized),
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
