from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from core.agent import AgentContext, AgentLoop
from core.agent_tools import (
    _looks_like_explicit_sticker_send_message,
    _looks_like_sticker_management_message,
    _should_block_sticker_send_for_management_turn,
)
from core.engine import EngineMessage, YukikoEngine


class _DummyStickerManager:
    face_count = 0
    emoji_count = 3

    def last_learned_emoji(self, source_user: str = ""):
        if source_user == "10001":
            return (
                "add/10001/demo.png",
                SimpleNamespace(
                    description="猫猫震惊",
                    category="反应",
                    tags=["猫猫", "震惊", "meme"],
                ),
            )
        return (
            "add/99999/global.png",
            SimpleNamespace(
                description="全局最新",
                category="搞笑",
                tags=["最新"],
            ),
        )

    def face_list_for_prompt(self) -> str:
        return ""


class _DummyMemory:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add_message(self, **kwargs):  # type: ignore[no-untyped-def]
        self.rows.append(kwargs)


class DialogAndStickerRegressionTests(unittest.TestCase):
    def test_user_message_starts_with_explicit_current_speaker_anchor(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop._rebuild_query_with_context = lambda text, ctx: text  # type: ignore[attr-defined]
        loop._build_napcat_event_anchor = lambda ctx: ""  # type: ignore[attr-defined]

        ctx = AgentContext(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="@妈妈 meme更新了吗",
            sender_role="admin",
            reply_to_user_id="20002",
            reply_to_user_name="风风",
            reply_to_text="刚才那张学会了吗",
            sticker_manager=_DummyStickerManager(),
        )

        payload = AgentLoop._build_user_message(loop, ctx)

        self.assertIn("[当前说话人: 妈妈(QQ:10001) | role=admin]", payload)
        self.assertIn("[用户在回复: 风风(QQ:20002)", payload)

    def test_sticker_management_turn_is_not_treated_as_explicit_send(self) -> None:
        self.assertTrue(_looks_like_sticker_management_message("学习这个表情包成功了吗"))
        self.assertFalse(_looks_like_explicit_sticker_send_message("学习这个表情包成功了吗"))
        self.assertTrue(
            _should_block_sticker_send_for_management_turn(
                {"message_text": "meme更新了吗，刚学的那个表情包现在有了吗"}
            )
        )
        self.assertFalse(
            _should_block_sticker_send_for_management_turn(
                {"message_text": "把刚学的表情包发出来看看"}
            )
        )

    def test_agent_side_effect_memory_keeps_sticker_description(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {"bot": {"allow_memory": True, "name": "YuKiKo"}}
        engine.memory = _DummyMemory()

        message = EngineMessage(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            text="学一下这张表情",
            timestamp=datetime.now(timezone.utc),
        )
        agent_result = SimpleNamespace(
            steps=[
                {
                    "tool": "learn_sticker",
                    "display": "",
                    "data": {"description": "猫猫震惊", "key": "add/10001/demo.png"},
                },
                {
                    "tool": "send_emoji",
                    "display": "",
                    "data": {"desc": "猫猫震惊", "key": "add/10001/demo.png"},
                },
            ]
        )

        YukikoEngine._record_agent_side_effects(engine, message, agent_result)

        self.assertEqual(len(engine.memory.rows), 1)
        content = engine.memory.rows[0]["content"]
        self.assertIn("学习了表情包", content)
        self.assertIn("发送了表情包", content)
        self.assertIn("猫猫震惊", content)


if __name__ == "__main__":
    unittest.main()
