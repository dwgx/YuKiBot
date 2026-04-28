from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from core.admin import AdminEngine
from core.engine import YukikoEngine
from core.engine_types import EngineMessage
from core.trigger import TriggerEngine


class EngineBotStrategyDirectiveTests(unittest.TestCase):
    def _engine(self, *, super_users: list[str] | None = None) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "bot": {"name": "YuKiKo", "nicknames": ["30秒"]},
            "admin": {"enable": True, "super_users": super_users or ["100"]},
            "trigger": {"followup_reply_window_seconds": 30, "followup_max_turns": 2},
            "routing": {},
            "control": {},
        }
        engine.logger = logging.getLogger("test.yukiko.engine")
        engine.trigger = TriggerEngine(
            trigger_config=engine.config["trigger"],
            bot_config=engine.config["bot"],
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine.admin = AdminEngine(engine.config, Path(tmp.name))

        def refresh_runtime_policy_components(*, reason: str = "") -> None:
            engine.refresh_reason = reason
            engine.trigger = TriggerEngine(
                trigger_config=engine.config["trigger"],
                bot_config=engine.config["bot"],
            )

        engine.refresh_runtime_policy_components = refresh_runtime_policy_components
        return engine

    def test_detects_directed_silence_control(self) -> None:
        engine = self._engine()
        directed = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="@30秒 闭嘴",
            mentioned=True,
            group_id=1,
            bot_id="200",
        )
        undirected = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="闭嘴",
            mentioned=False,
            group_id=1,
            bot_id="200",
        )

        self.assertEqual(
            engine._detect_bot_strategy_directive(
                directed.text,
                message=directed,
                explicit_bot_addressed=True,
            ),
            "cold",
        )
        self.assertEqual(
            engine._detect_bot_strategy_directive(
                undirected.text,
                message=undirected,
                explicit_bot_addressed=False,
            ),
            "",
        )

    def test_super_admin_silence_control_updates_runtime_policy(self) -> None:
        engine = self._engine()
        message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="@30秒 闭嘴",
            mentioned=True,
            group_id=1,
            bot_id="200",
        )
        engine.trigger.activate_session("group:1", "100", False)

        response = asyncio.run(
            engine._handle_bot_strategy_directive(
                message=message,
                text=message.text,
                mode="cold",
            )
        )

        self.assertEqual(response.action, "reply")
        self.assertEqual(response.reason, "bot_strategy_directive")
        self.assertFalse(engine.config["trigger"]["ai_listen_enable"])
        self.assertFalse(engine.config["trigger"]["delegate_undirected_to_ai"])
        self.assertEqual(engine.refresh_reason, "behavior_mode:cold")
        self.assertEqual(engine.trigger._active_sessions, {})

    def test_non_admin_silence_control_only_closes_current_session(self) -> None:
        engine = self._engine(super_users=["999"])
        message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="@30秒 闭嘴",
            mentioned=True,
            group_id=1,
            bot_id="200",
        )
        engine.trigger.activate_session("group:1", "100", False)

        response = asyncio.run(
            engine._handle_bot_strategy_directive(
                message=message,
                text=message.text,
                mode="cold",
            )
        )

        self.assertEqual(response.action, "ignore")
        self.assertEqual(response.reason, "bot_strategy_directive_non_admin")
        self.assertNotIn("ai_listen_enable", engine.config["trigger"])
        self.assertEqual(engine.trigger._active_sessions, {})
