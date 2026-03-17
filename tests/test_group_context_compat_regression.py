from __future__ import annotations

import unittest

from core.agent import AgentContext, AgentLoop
from core.context_compat import CompatContextInput, build_context_compat_block
from core.thinking import ThinkingEngine


class _StubRegistry:
    tool_count = 1

    def select_tools_for_intent(self, text: str, perm_level: str) -> list[str]:
        _ = (text, perm_level)
        return ["final_answer"]

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        _ = selected_tools
        return "- final_answer"

    def get_prompt_hints_text(self, section: str, tool_names=None) -> str:
        _ = (section, tool_names)
        return ""

    def get_dynamic_context(self, runtime_info: dict, tool_names=None) -> str:
        _ = (runtime_info, tool_names)
        return ""


class _StubPromptPolicy:
    def build_tool_guidance_block(self) -> str:
        return ""

    def compose_prompt(self, channel: str, base_prompt: str) -> str:
        _ = channel
        return base_prompt


class GroupContextCompatRegressionTests(unittest.TestCase):
    def test_build_context_compat_block_keeps_group_relationship_clues(self) -> None:
        block = build_context_compat_block(
            CompatContextInput(
                conversation_id="group:1",
                user_id="10001",
                user_name="妈妈",
                preferred_name="妈咪",
                scene_hint="emotion_support",
                mentioned=True,
                bot_id="99999",
                reply_to_user_id="20002",
                reply_to_user_name="小雨",
                reply_to_text="我刚才真的有点难过",
                at_other_user_ids=["30003"],
                at_other_user_names={"30003": "阿风"},
                recent_speakers=[
                    ("20002", "小雨", "今天状态不太好"),
                    ("30003", "阿风", "先别想太多"),
                ],
                thread_state={
                    "last_topic": "感情",
                    "last_action": "reply",
                    "last_user_id": "10001",
                },
                user_profile_summary="妈妈：日常口语；偏短句；情绪偏焦虑",
                affinity_summary="关系热度 Lv.4 好朋友 / 好感度 66/100 / 累计互动 12 次",
                bot_mood="melancholy",
            )
        )

        self.assertIn("【群聊关系兼容层】", block)
        self.assertIn("当前主要回应对象: 妈妈(QQ:10001)", block)
        self.assertIn("建议优先称呼当前用户为: 妈咪", block)
        self.assertIn("当前用户可能在和 小雨(QQ:20002) 继续同一段对话", block)
        self.assertIn("当前消息还点名了: 阿风(QQ:30003)", block)
        self.assertIn("线程延续提示: 上一轮主线话题=感情", block)
        self.assertIn("场景策略: 情绪场景", block)

    def test_thinking_payload_includes_compat_context_block(self) -> None:
        thinking = ThinkingEngine.__new__(ThinkingEngine)
        thinking.memory_recall_level = "light"

        payload = ThinkingEngine._build_payload(
            thinking,
            user_text="她刚才是不是不开心",
            trigger_reason="mentioned",
            memory_context=[],
            related_memories=[],
            search_summary="",
            sensitive_context="",
            user_profile_summary="妈妈：日常口语；偏短句",
            scene_tag="chat",
            compat_context="【群聊关系兼容层】\n- 当前主要回应对象: 妈妈(QQ:10001)",
        )

        self.assertIn("【群聊关系兼容层】", payload)
        self.assertIn("当前主要回应对象: 妈妈(QQ:10001)", payload)

    def test_agent_system_prompt_includes_compat_context_block(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_registry = _StubRegistry()
        loop.prompt_policy = _StubPromptPolicy()
        loop.config = {}
        loop.persona_text = ""
        loop.max_steps = 6
        loop._resolve_permission_level = lambda ctx: "user"  # type: ignore[attr-defined]

        ctx = AgentContext(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            group_id=1,
            bot_id="99999",
            is_private=False,
            mentioned=True,
            message_text="她刚才是在说我吗",
            compat_context="【群聊关系兼容层】\n- 当前主要回应对象: 妈妈(QQ:10001)\n- 当前用户可能在和 小雨(QQ:20002) 继续同一段对话",
        )

        prompt = AgentLoop._build_system_prompt(loop, ctx)

        self.assertIn("【群聊关系兼容层】", prompt)
        self.assertIn("当前用户可能在和 小雨(QQ:20002) 继续同一段对话", prompt)

    def test_agent_turn_target_line_prefers_reply_anchor_then_mentions(self) -> None:
        ctx_reply = AgentContext(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            group_id=1,
            bot_id="99999",
            is_private=False,
            mentioned=True,
            message_text="她刚才是在说我吗",
            reply_to_user_id="20002",
            reply_to_user_name="小雨",
            at_other_user_ids=["30003"],
            at_other_user_names={"30003": "阿风"},
        )
        line_reply = AgentLoop._build_turn_target_line(ctx_reply)
        self.assertIn("本轮主要对象: 小雨(QQ:20002)", line_reply)
        self.assertIn("来源: reply_anchor", line_reply)

        ctx_mention = AgentContext(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            group_id=1,
            bot_id="99999",
            is_private=False,
            mentioned=True,
            message_text="你觉得阿风呢",
            at_other_user_ids=["30003"],
            at_other_user_names={"30003": "阿风"},
        )
        line_mention = AgentLoop._build_turn_target_line(ctx_mention)
        self.assertIn("本轮主要对象: 阿风(QQ:30003)", line_mention)
        self.assertIn("来源: mention", line_mention)


if __name__ == "__main__":
    unittest.main()
