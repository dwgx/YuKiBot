from __future__ import annotations

import unittest

from core.agent import AgentContext, AgentLoop
from core.engine import YukikoEngine


class ToolCallLeakRegressionTests(unittest.TestCase):
    @staticmethod
    def _make_ctx(**overrides) -> AgentContext:
        base = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="",
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

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

    def test_agent_detects_image_hint_from_multimodal_event_text(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        text = (
            "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: "
            "image:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
        )
        self.assertTrue(loop._text_has_image_hint(text))

    def test_agent_treats_ntqq_download_url_as_image(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
        self.assertTrue(loop._looks_like_image_url(url))

    def test_agent_forces_image_tool_for_short_image_question(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        ctx = self._make_ctx(
            message_text="MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123\n這是什麽",
            raw_segments=[
                {
                    "type": "image",
                    "data": {"url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"},
                }
            ],
        )
        forced = loop._select_forced_media_tool(ctx)
        self.assertIsNotNone(forced)
        assert forced is not None
        self.assertEqual(forced[0], "analyze_image")
        self.assertEqual(
            forced[1].get("url"),
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123",
        )
        self.assertIn("這是什麽", forced[1].get("question", ""))

    def test_agent_forces_local_video_tool_for_short_question(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        ctx = self._make_ctx(
            message_text="这是什么",
            raw_segments=[
                {
                    "type": "video",
                    "data": {"url": "https://example.com/demo.mp4"},
                }
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        forced = loop._select_forced_media_tool(ctx)
        self.assertEqual(forced, ("analyze_local_video", {"url": "https://example.com/demo.mp4"}))

    def test_agent_forces_voice_tool_for_short_question(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        ctx = self._make_ctx(
            message_text="说了什么",
            raw_segments=[
                {
                    "type": "record",
                    "data": {"url": "https://example.com/demo.mp3"},
                }
            ],
            media_summary=["record:https://example.com/demo.mp3"],
        )
        forced = loop._select_forced_media_tool(ctx)
        self.assertEqual(forced, ("analyze_voice", {"url": "https://example.com/demo.mp3"}))


if __name__ == "__main__":
    unittest.main()
