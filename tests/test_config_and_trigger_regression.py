from __future__ import annotations

from datetime import datetime, timezone
import unittest

from core.config_templates import _built_in_config_defaults
from core.engine import YukikoEngine
from core.sticker import _QQ_DATA_ROOTS
from core.trigger import TriggerEngine, TriggerInput


class ConfigAndTriggerRegressionTests(unittest.TestCase):
    def test_builtin_defaults_enable_high_confidence_ai_listen(self) -> None:
        defaults = _built_in_config_defaults()

        self.assertEqual(defaults["control"]["undirected_policy"], "high_confidence_only")
        self.assertTrue(defaults["bot"]["allow_non_to_me"])
        self.assertTrue(defaults["trigger"]["ai_listen_enable"])
        self.assertTrue(defaults["trigger"]["delegate_undirected_to_ai"])
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

    def test_memory_keywords_can_trigger_ai_listen_probe(self) -> None:
        trigger = TriggerEngine(
            trigger_config={
                "ai_listen_enable": True,
                "ai_listen_min_messages": 8,
                "ai_listen_min_unique_users": 3,
                "ai_listen_min_score": 3.8,
                "ai_listen_keyword_enable": True,
                "ai_listen_min_keyword_hits": 1,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)

        payload = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="projectx 这个怎么弄",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        result = trigger.evaluate(
            payload,
            recent_messages=[
                "[Alice] 刚才 projectx 又报错了",
                "[Bob] projectx 的配置是不是丢了",
            ],
            memory_keywords=["projectx", "配置"],
        )
        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "ai_listen_probe_memory_keyword")

    def test_explicit_ai_listen_enables_non_directed_gate_under_mention_only(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "control": {"undirected_policy": "mention_only"},
            "bot": {"allow_non_to_me": False},
            "trigger": {
                "ai_listen_enable": True,
                "delegate_undirected_to_ai": True,
            },
        }

        trigger_cfg = YukikoEngine._build_effective_trigger_config(engine)
        self.assertTrue(trigger_cfg.get("ai_listen_enable", False))
        self.assertTrue(trigger_cfg.get("delegate_undirected_to_ai", False))

    def test_high_confidence_policy_forces_ai_listen_gate(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "control": {"undirected_policy": "high_confidence_only"},
            "bot": {"allow_non_to_me": False},
            "trigger": {
                "ai_listen_enable": False,
            },
        }

        trigger_cfg = YukikoEngine._build_effective_trigger_config(engine)
        self.assertTrue(trigger_cfg.get("ai_listen_enable", False))
        self.assertTrue(trigger_cfg.get("delegate_undirected_to_ai", False))

    def test_linux_qq_data_root_is_supported(self) -> None:
        normalized = {str(path).replace("\\", "/") for path in _QQ_DATA_ROOTS}
        self.assertTrue(
            any(item.endswith("/.config/QQ") for item in normalized),
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
