from __future__ import annotations

import asyncio
import unittest

from core.agent_tools import _make_image_gen_handler
from core.image import ImageEngine
from core.image_gen import ImageGenEngine, detect_nsfw_prompt_reason


class _DummyModelClient:
    def __init__(
        self,
        *,
        review_content: str = '{"legal": true, "level": "safe", "reason": ""}',
        supports_vision: bool = True,
        supports_multimodal: bool = True,
        model: str = "gpt-4o",
    ) -> None:
        self.enabled = True
        self.calls: list[tuple[str, str]] = []
        self.review_calls = 0
        self._review_content = review_content
        self._supports_vision = supports_vision
        self._supports_multimodal = supports_multimodal
        self.model = model
        self.last_review_model: str | None = None

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str:
        self.calls.append((prompt, size))
        return "https://example.com/generated.png"

    async def chat_completion(self, messages, response_format=None, max_tokens=None, model=None):
        _ = (messages, response_format, max_tokens)
        self.last_review_model = str(model or "")
        self.review_calls += 1
        return {"choices": [{"message": {"content": self._review_content}}]}

    def supports_vision_input(self, model: str | None = None) -> bool:
        if str(model or "").strip() == "dall-e-3":
            return False
        return self._supports_vision

    def supports_multimodal_messages(self) -> bool:
        return self._supports_multimodal


class ImageNsfwGuardRegressionTests(unittest.TestCase):
    def test_detect_blocks_obfuscated_nsfw_prompt(self) -> None:
        self.assertTrue(detect_nsfw_prompt_reason("请画一张 r 1 8 全 裸 写真"))
        self.assertTrue(detect_nsfw_prompt_reason("draw n s f w anime girl"))
        self.assertEqual(detect_nsfw_prompt_reason("画一只可爱猫咪插画"), "")

    def test_image_gen_engine_blocks_before_model_call(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageGenEngine(
            {"image_gen": {"enable": True, "nsfw_filter": True}},
            model_client=model_client,
        )

        result = asyncio.run(engine.generate("请生成一张R 1 8 裸体动漫少女"))
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.message)
        self.assertEqual(model_client.calls, [])

    def test_basic_image_engine_blocks_before_model_call(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageEngine({"enable": True}, model_client)

        result = asyncio.run(engine.generate("帮我画个 n s f w 图"))
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.message)
        self.assertEqual(model_client.calls, [])

    def test_agent_basic_image_handler_blocks_nsfw_prompt(self) -> None:
        model_client = _DummyModelClient()
        handler = _make_image_gen_handler(model_client)

        result = asyncio.run(handler({"prompt": "来个 r18 全裸猫娘"}, {}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "image_prompt_blocked_nsfw")
        self.assertEqual(model_client.calls, [])

    def test_image_gen_post_review_blocks_generated_result(self) -> None:
        model_client = _DummyModelClient(
            review_content='{"legal": false, "level": "blocked", "reason": "包含成人裸露"}'
        )
        engine = ImageGenEngine(
            {
                "image_gen": {
                    "enable": True,
                    "nsfw_filter": True,
                    "post_review_enable": True,
                    "post_review_fail_closed": True,
                }
            },
            model_client=model_client,
        )

        result = asyncio.run(engine.generate("画一只戴围巾的猫"))
        self.assertFalse(result.ok)
        self.assertIn("合规审查", result.message)
        self.assertEqual(len(model_client.calls), 1)
        self.assertEqual(model_client.review_calls, 1)

    def test_image_gen_post_review_allows_safe_result(self) -> None:
        model_client = _DummyModelClient(
            review_content='{"legal": true, "level": "safe", "reason": ""}'
        )
        engine = ImageGenEngine(
            {
                "image_gen": {
                    "enable": True,
                    "nsfw_filter": True,
                    "post_review_enable": True,
                    "post_review_fail_closed": True,
                }
            },
            model_client=model_client,
        )

        result = asyncio.run(engine.generate("画一只戴围巾的猫"))
        self.assertTrue(result.ok)
        self.assertTrue(result.url)
        self.assertEqual(len(model_client.calls), 1)
        self.assertEqual(model_client.review_calls, 1)

    def test_image_gen_post_review_uses_main_model_not_image_model(self) -> None:
        model_client = _DummyModelClient(model="gpt-4.1")
        engine = ImageGenEngine(
            {
                "image_gen": {
                    "enable": True,
                    "default_model": "dall-e-3",
                    "post_review_enable": True,
                    "post_review_fail_closed": True,
                }
            },
            model_client=model_client,
        )

        result = asyncio.run(engine.generate("画一只戴围巾的猫"))
        self.assertTrue(result.ok)
        self.assertEqual(model_client.last_review_model, "gpt-4.1")

    def test_image_gen_post_review_blocks_when_protocol_unsupported(self) -> None:
        model_client = _DummyModelClient(supports_multimodal=False)
        engine = ImageGenEngine(
            {
                "image_gen": {
                    "enable": True,
                    "post_review_enable": True,
                    "post_review_fail_closed": True,
                }
            },
            model_client=model_client,
        )

        result = asyncio.run(engine.generate("画一只戴围巾的猫"))
        self.assertFalse(result.ok)
        self.assertIn("图片审查协议", result.message)
        self.assertEqual(model_client.review_calls, 0)


if __name__ == "__main__":
    unittest.main()
