from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult, _handle_analyze_local_video
from core.engine import YukikoEngine


class ToolCallLeakRegressionTests(unittest.TestCase):
    class _StubRegistry:
        def __init__(self, names: set[str]):
            self._names = set(names)

        def has_tool(self, name: str) -> bool:
            return name in self._names

    class _SequencedModelClient:
        def __init__(self, responses: list[str]):
            self._responses = list(responses)

        async def chat_text_with_retry(
            self, messages, max_tokens=0, retries=0, backoff=0.0
        ):
            _ = (messages, max_tokens, retries, backoff)
            if not self._responses:
                raise AssertionError("No more model responses prepared for test")
            return self._responses.pop(0)

    class _RunnableRegistry(_StubRegistry):
        def __init__(self, names: set[str]):
            super().__init__(names)
            self.calls: list[tuple[str, dict[str, str]]] = []

        async def call(
            self, name: str, args: dict[str, str], context: dict[str, str]
        ) -> ToolCallResult:
            _ = context
            self.calls.append((name, dict(args)))
            return ToolCallResult(
                ok=True,
                data={"image_url": "https://example.com/generated.png"},
                display="图片已生成",
            )

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
                self.assertEqual(
                    engine._sanitize_reply_output(payload, action="reply"), ""
                )

    def test_engine_no_longer_local_matches_provider_refusal_templates(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.sanitize_banned_phrases = ()
        engine._apply_privacy_output_guard = lambda text, action="": text
        engine._build_mention_only_reply = lambda text: text

        payloads = (
            "I'm Claude, an AI assistant made by Anthropic. I'm a text-based AI assistant and cannot generate images directly.",
            "抱歉，我无法查看图片内容。我是一个文本助手，只能处理文字信息。我目前无法直接生成图片，不具备图像生成功能。",
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    engine._sanitize_reply_output(payload, action="reply"),
                    payload,
                )

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
                    "data": {
                        "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
                    },
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
        self.assertEqual(
            forced,
            (
                "analyze_local_video",
                {
                    "url": "https://example.com/demo.mp4",
                    "question": "这是什么",
                },
            ),
        )

    def test_agent_forces_split_video_tool_for_structured_video_request(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        ctx = self._make_ctx(
            message_text="mode=audio 10s-20s",
            raw_segments=[
                {
                    "type": "video",
                    "data": {"url": "https://example.com/demo.mp4"},
                }
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        forced = loop._select_forced_media_tool(ctx)
        self.assertEqual(
            forced,
            (
                "split_video",
                {
                    "url": "https://example.com/demo.mp4",
                    "mode": "audio",
                    "start_seconds": 10.0,
                    "end_seconds": 20.0,
                },
            ),
        )

    def test_agent_requires_tool_first_for_media_even_without_keyword_cues(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        ctx = self._make_ctx(
            message_text="嗯",
            raw_segments=[
                {
                    "type": "video",
                    "data": {"url": "https://example.com/demo.mp4"},
                }
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        self.assertTrue(loop._should_force_tool_first(ctx))

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
        self.assertEqual(
            forced, ("analyze_voice", {"url": "https://example.com/demo.mp3"})
        )

    def test_agent_prefers_enhanced_image_generation_tool(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_registry = self._StubRegistry(
            {"generate_image_enhanced", "generate_image"}
        )
        ctx = self._make_ctx(message_text="帮我生成一张猫娘图片，眼睛里有爱心")
        forced = loop._select_forced_media_tool(ctx)
        self.assertEqual(
            forced,
            ("generate_image_enhanced", {"prompt": "猫娘图片，眼睛里有爱心"}),
        )

    def test_agent_falls_back_to_basic_image_generation_tool(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_registry = self._StubRegistry({"generate_image"})
        ctx = self._make_ctx(message_text="画个猫娘")
        forced = loop._select_forced_media_tool(ctx)
        self.assertEqual(forced, ("generate_image", {"prompt": "猫娘"}))

    def test_agent_blocks_direct_refusal_and_recovers_with_image_tool(self) -> None:
        registry = self._RunnableRegistry({"generate_image"})
        loop = AgentLoop(
            model_client=self._SequencedModelClient(
                [
                    "I'm a text-based AI assistant and cannot generate images directly.",
                    '{"tool":"generate_image","args":{}}',
                    '{"tool":"final_answer","args":{"text":"好了，已经帮你生成。"}}',
                ]
            ),
            tool_registry=registry,
            config={},
        )
        loop.max_steps = 4
        loop.high_risk_control_enable = False
        loop.fallback_on_parse_error = False
        loop._build_system_prompt = lambda ctx: "system"  # type: ignore[assignment]
        loop._build_user_message = lambda ctx: ctx.message_text  # type: ignore[assignment]

        result = asyncio.run(
            loop.run(self._make_ctx(message_text="帮我生成一张猫娘图片，眼睛里有爱心"))
        )

        self.assertEqual(result.reason, "agent_final_answer")
        self.assertEqual(result.reply_text, "好了，已经帮你生成。")
        self.assertEqual(result.tool_calls_made, 1)
        self.assertEqual(
            registry.calls,
            [("generate_image", {"prompt": "猫娘图片，眼睛里有爱心"})],
        )
        self.assertEqual(result.steps[0]["tool"], "policy_guard")
        self.assertEqual(result.steps[0]["error"], "tool_required_before_direct_reply")

    def test_analyze_local_video_handler_reuses_shared_video_analyzer(self) -> None:
        class _DummyExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def _method_video_analyze(
                self,
                method_name: str,
                method_args: dict[str, object],
                query: str,
                message_text: str,
                raw_segments: list[dict[str, object]] | None = None,
                conversation_id: str = "",
            ):
                self.calls.append(
                    {
                        "method_name": method_name,
                        "method_args": dict(method_args),
                        "query": query,
                        "message_text": message_text,
                        "raw_segments": list(raw_segments or []),
                        "conversation_id": conversation_id,
                    }
                )
                return SimpleNamespace(
                    ok=True,
                    payload={
                        "text": "这是本地视频分析结果",
                        "analysis_context": "标题: demo\n时长: 00:12",
                        "video_url": "file:///tmp/demo.mp4",
                    },
                    error="",
                )

        executor = _DummyExecutor()
        result = asyncio.run(
            _handle_analyze_local_video(
                {},
                {
                    "tool_executor": executor,
                    "message_text": "看看这段视频",
                    "conversation_id": "group:1",
                    "raw_segments": [],
                    "reply_media_segments": [
                        {
                            "type": "video",
                            "data": {"url": "file:///tmp/demo.mp4"},
                        }
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertIn("这是本地视频分析结果", result.display)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["method_name"], "analyze_local_video")
        self.assertEqual(executor.calls[0]["conversation_id"], "group:1")
        self.assertEqual(
            executor.calls[0]["raw_segments"],
            [{"type": "video", "data": {"url": "file:///tmp/demo.mp4"}}],
        )


if __name__ == "__main__":
    unittest.main()
