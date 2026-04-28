from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from core.agent import AgentResult
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
        engine._recent_directed_hints = {}
        engine.directed_grace_seconds = 90
        engine._async_init_done = True
        engine._seen_message_ids = OrderedDict()
        engine._seen_message_ids_max = 1024
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

    def test_directed_silence_control_runs_before_non_whitelist_silent_gate(self) -> None:
        engine = self._engine()
        message = EngineMessage(
            conversation_id="group:901738883",
            user_id="100",
            text="@30秒 闭嘴",
            mentioned=True,
            group_id=901738883,
            bot_id="200",
            message_id="m-1",
        )

        response = asyncio.run(engine.handle_message(message))

        self.assertEqual(response.action, "reply")
        self.assertEqual(response.reason, "bot_strategy_directive")
        self.assertFalse(engine.config["trigger"]["ai_listen_enable"])

    def test_blocks_undirected_agent_plain_reply_from_listen_probe(self) -> None:
        engine = self._engine()
        message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="吃哪个",
            mentioned=False,
            group_id=1,
            bot_id="200",
        )
        trigger = SimpleNamespace(reason="ai_listen_probe_score")
        result = AgentResult(
            action="reply",
            reply_text="黄油那个吧。",
            tool_calls_made=0,
        )

        self.assertTrue(
            engine._should_block_undirected_agent_plain_reply(
                message=message,
                text=message.text,
                trigger=trigger,
                agent_result=result,
            )
        )

    def test_keeps_directed_or_artifact_agent_results(self) -> None:
        engine = self._engine()
        trigger = SimpleNamespace(reason="active_session")
        mentioned_message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="@30秒 吃哪个",
            mentioned=True,
            group_id=1,
            bot_id="200",
        )
        reply_to_bot_message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="那你发",
            mentioned=False,
            group_id=1,
            bot_id="200",
            reply_to_user_id="200",
        )
        artifact_message = EngineMessage(
            conversation_id="group:1",
            user_id="100",
            text="解析这个视频",
            mentioned=False,
            group_id=1,
            bot_id="200",
        )

        self.assertFalse(
            engine._should_block_undirected_agent_plain_reply(
                message=mentioned_message,
                text=mentioned_message.text,
                trigger=trigger,
                agent_result=AgentResult(action="reply", reply_text="可以。"),
            )
        )
        self.assertFalse(
            engine._should_block_undirected_agent_plain_reply(
                message=reply_to_bot_message,
                text=reply_to_bot_message.text,
                trigger=trigger,
                agent_result=AgentResult(action="reply", reply_text="发。"),
            )
        )
        self.assertFalse(
            engine._should_block_undirected_agent_plain_reply(
                message=artifact_message,
                text=artifact_message.text,
                trigger=trigger,
                agent_result=AgentResult(
                    action="reply",
                    reply_text="解析好了。",
                    video_url="/tmp/video.mp4",
                ),
            )
        )
