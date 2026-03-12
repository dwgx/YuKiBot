from __future__ import annotations

import unittest

from core.agent import AgentLoop
from core.engine import YukikoEngine


class ToolCallLeakRegressionTests(unittest.TestCase):
    def test_agent_recovers_truncated_named_final_answer_payload(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.fallback_on_parse_error = True

        parsed = loop._parse_llm_output(
            '{"name":"final_answer","arguments":{"text":"「生于忧患死于安乐」这句话"}'
        )

        self.assertEqual(
            parsed,
            {
                "tool": "final_answer",
                "args": {"text": "「生于忧患死于安乐」这句话"},
            },
        )

    def test_engine_sanitizes_unclosed_fenced_tool_call_payload(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.sanitize_banned_phrases = ()
        engine._apply_privacy_output_guard = lambda text, action="": text
        engine._build_mention_only_reply = lambda text: text

        payloads = (
            '```json\n{"tool":"final_answer","args":{"text":"hello"',
            '```json\n{"name":"final_answer","arguments":{"text":"hello"',
            '```json\n{"tool":"learn_knowledge","args":{"title":"用户称呼偏好","content":"以后叫我"妈妈""}',
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(engine._sanitize_reply_output(payload, action="reply"), "")

    def test_agent_marks_generic_fenced_tool_payload_as_leak(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        payload = (
            '```json { "tool": "learn_knowledge", "args": { "title": "用户称呼偏好", '
            '"content": "以后叫我"妈妈"" } } ```'
        )
        self.assertTrue(loop._looks_like_embedded_tool_payload_text(payload))


if __name__ == "__main__":
    unittest.main()
