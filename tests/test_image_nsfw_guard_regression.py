from __future__ import annotations

import asyncio
import unittest

from core.agent_tools import _make_image_gen_handler
from core.image import ImageEngine
from core.image_gen import ImageGenEngine, detect_qq_ban_risk_reason


class _DummyModelClient:
    def __init__(
        self,
        *,
        review_content: str = '{"legal": true, "level": "safe", "reason": ""}',
        prompt_review_content: str | None = None,
        supports_vision: bool = True,
        supports_multimodal: bool = True,
        model: str = "gpt-4o",
    ) -> None:
        self.enabled = True
        self.calls: list[tuple[str, str]] = []
        self.review_calls = 0
        self.prompt_review_calls = 0
        self.image_review_calls = 0
        self._image_review_content = review_content
        self._prompt_review_content = prompt_review_content
        self._supports_vision = supports_vision
        self._supports_multimodal = supports_multimodal
        self.model = model
        self.last_review_model: str | None = None

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str | None = None,
    ) -> str:
        self.calls.append((prompt, size, style or ""))
        return "https://example.com/generated.png"

    async def chat_completion(self, messages, response_format=None, max_tokens=None, model=None):
        _ = (messages, response_format, max_tokens)
        self.last_review_model = str(model or "")
        self.review_calls += 1
        system_text = ""
        merged_user_text_parts: list[str] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip().lower()
            content = message.get("content", "")
            if role == "system" and isinstance(content, str):
                system_text += content
            if role != "user":
                continue
            if isinstance(content, str):
                merged_user_text_parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = str(item.get("text", "")).strip()
                        if text:
                            merged_user_text_parts.append(text)
        merged_user_text = "\n".join(merged_user_text_parts)

        if "提示词合规审核器" in system_text:
            self.prompt_review_calls += 1
            if self._prompt_review_content is not None:
                content = self._prompt_review_content
            else:
                target_parts: list[str] = []
                for line in merged_user_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("提示词:") or stripped.startswith("提示词："):
                        target_parts.append(stripped.split(":", 1)[-1].split("：", 1)[-1].strip())
                    elif stripped.startswith("风格参数:") or stripped.startswith("风格参数："):
                        target_parts.append(stripped.split(":", 1)[-1].split("：", 1)[-1].strip())
                risk_text = "\n".join(item for item in target_parts if item) or merged_user_text
                reason = detect_qq_ban_risk_reason(risk_text)
                if reason:
                    content = '{"legal": false, "level": "blocked", "reason": "命中高危提示词"}'
                else:
                    content = '{"legal": true, "level": "safe", "reason": ""}'
            return {"choices": [{"message": {"content": content}}]}

        self.image_review_calls += 1
        return {"choices": [{"message": {"content": self._image_review_content}}]}

    def supports_vision_input(self, model: str | None = None) -> bool:
        if str(model or "").strip() == "dall-e-3":
            return False
        return self._supports_vision

    def supports_multimodal_messages(self) -> bool:
        return self._supports_multimodal


class ImageNsfwGuardRegressionTests(unittest.TestCase):
    def test_detect_blocks_obfuscated_nsfw_prompt(self) -> None:
        self.assertTrue(detect_qq_ban_risk_reason("请画一张 r 1 8 全 裸 写真"))
        self.assertTrue(detect_qq_ban_risk_reason("draw n s f w anime girl"))
        self.assertTrue(detect_qq_ban_risk_reason("画一个巨乳奶子特写"))
        self.assertTrue(detect_qq_ban_risk_reason("draw n a i z i anime"))
        self.assertTrue(detect_qq_ban_risk_reason("来一张露 b 的图"))
        self.assertEqual(detect_qq_ban_risk_reason("画一只可爱猫咪插画"), "")
        self.assertEqual(detect_qq_ban_risk_reason("画一个泳装少女在海边"), "")
        self.assertEqual(detect_qq_ban_risk_reason("draw a bikini beach fashion poster"), "")

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

    def test_basic_image_engine_allows_non_explicit_aesthetic_prompt(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageEngine({"enable": True}, model_client)

        result = asyncio.run(engine.generate("画一个泳装少女在海边漫步"))
        self.assertTrue(result.ok)
        self.assertEqual(len(model_client.calls), 1)

    def test_basic_image_engine_blocks_explicit_breast_or_b_prompt(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageEngine({"enable": True}, model_client)

        result = asyncio.run(engine.generate("画一个奶子特写，露 b"))
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.message)
        self.assertEqual(model_client.calls, [])

    def test_basic_image_engine_supports_custom_block_terms(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageEngine(
            {
                "enable": True,
                "prompt_review_enable": False,
                "custom_block_terms": ["擦边"],
            },
            model_client,
        )

        result = asyncio.run(engine.generate("帮我画一张擦边海报"))
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

    def test_agent_basic_image_handler_allows_bikini_prompt(self) -> None:
        model_client = _DummyModelClient()
        handler = _make_image_gen_handler(model_client)

        result = asyncio.run(
            handler({"prompt": "画一个比基尼海边写真", "size": "1024x1024"}, {})
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data.get("image_url"))
        self.assertEqual(len(model_client.calls), 1)

    def test_agent_basic_image_handler_blocks_risk_hidden_in_style(self) -> None:
        model_client = _DummyModelClient()
        handler = _make_image_gen_handler(model_client)

        result = asyncio.run(
            handler({"prompt": "画一只小猫", "style": "r 1 8 nude"}, {})
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "image_prompt_blocked_nsfw")
        self.assertEqual(model_client.calls, [])

    def test_agent_basic_image_handler_supports_custom_block_terms(self) -> None:
        model_client = _DummyModelClient()
        handler = _make_image_gen_handler(
            model_client,
            {"image_gen": {"prompt_review_enable": False, "custom_block_terms": ["陪睡图"]}},
        )

        result = asyncio.run(handler({"prompt": "来一张陪睡图宣传海报"}, {}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "image_prompt_blocked_nsfw")
        self.assertEqual(model_client.calls, [])

    def test_image_gen_engine_blocks_risk_hidden_in_style(self) -> None:
        model_client = _DummyModelClient()
        engine = ImageGenEngine(
            {"image_gen": {"enable": True, "nsfw_filter": True}},
            model_client=model_client,
        )

        result = asyncio.run(
            engine.generate("画一只戴围巾的猫", style="n s f w illustration")
        )
        self.assertFalse(result.ok)
        self.assertIn("拒绝", result.message)
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
        self.assertEqual(model_client.prompt_review_calls, 1)
        self.assertEqual(model_client.image_review_calls, 1)

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
        self.assertEqual(model_client.prompt_review_calls, 1)
        self.assertEqual(model_client.image_review_calls, 1)

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
        self.assertEqual(model_client.prompt_review_calls, 1)
        self.assertEqual(model_client.image_review_calls, 0)


if __name__ == "__main__":
    unittest.main()
