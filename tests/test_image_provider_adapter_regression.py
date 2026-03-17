from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.image_gen import (
    ImageGenResult,
    generate_image_with_model_config,
    resolve_image_provider_for_config,
)
from core.webui_setup_support import WebUISetupSupport
from services.gemini import GeminiClient
from services.xai import XAIClient


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text or '请求失败'}")

    def json(self):
        return self._json_data


class ImageProviderAdapterRegressionTests(unittest.TestCase):
    def test_resolve_provider_handles_aliases_and_inference(self) -> None:
        self.assertEqual(
            resolve_image_provider_for_config({"provider": "flux", "model": "black-forest-labs/FLUX.1-schnell"}),
            "siliconflow",
        )
        self.assertEqual(
            resolve_image_provider_for_config(
                {
                    "provider": "custom",
                    "model": "gemini-2.5-flash-image",
                    "api_base": "https://generativelanguage.googleapis.com",
                }
            ),
            "gemini",
        )
        self.assertEqual(
            resolve_image_provider_for_config(
                {
                    "provider": "custom",
                    "model": "google/gemini-2.5-flash-image",
                    "api_base": "https://openrouter.ai/api/v1",
                }
            ),
            "openrouter",
        )
        self.assertEqual(
            resolve_image_provider_for_config({"provider": "custom", "api_base": "http://127.0.0.1:7860"}),
            "sd",
        )

    def test_openai_compatible_omits_size_for_grok_imagine(self) -> None:
        client = XAIClient({"api_key": "x-key", "image_model": "grok-imagine-image"})
        mock_post = AsyncMock(return_value={"data": [{"url": "https://example.com/out.png"}]})
        with patch.object(client, "_post_with_base_candidates", mock_post):
            result = asyncio.run(client.generate_image("draw cat", size="1024x1024", style="anime"))
        self.assertEqual(result, "https://example.com/out.png")
        payload = mock_post.await_args.kwargs["payload"]
        self.assertNotIn("size", payload)
        self.assertEqual(payload.get("style"), "anime")

    def test_gemini_native_generate_image_parses_inline_data(self) -> None:
        async def fake_post(_self, url, headers=None, json=None):
            _ = (url, headers, json)
            return _FakeResponse(
                json_data={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": "YWJjZA==",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        client = GeminiClient(
            {
                "api_key": "gemini-key",
                "image_model": "gemini-2.5-flash-image",
                "base_url": "https://generativelanguage.googleapis.com",
            }
        )
        with patch("httpx.AsyncClient.post", new=fake_post):
            result = asyncio.run(client.generate_image("draw cat"))
        self.assertEqual(result, "data:image/png;base64,YWJjZA==")

    def test_generate_image_with_model_config_parses_sd_webui(self) -> None:
        seen_payloads: list[dict] = []

        async def fake_post(_self, url, headers=None, json=None):
            _ = (url, headers)
            seen_payloads.append(dict(json or {}))
            return _FakeResponse(json_data={"images": ["YWJjZA=="]})

        with patch("httpx.AsyncClient.post", new=fake_post):
            result = asyncio.run(
                generate_image_with_model_config(
                    prompt="draw cat",
                    model_cfg={
                        "provider": "sd",
                        "model": "stable-diffusion-xl",
                        "api_base": "http://127.0.0.1:7860",
                    },
                    size="512x768",
                    style="anime poster",
                )
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.base64_data, "YWJjZA==")
        self.assertTrue(result.url.startswith("data:image/png;base64,"))
        self.assertEqual(seen_payloads[0].get("width"), 512)
        self.assertEqual(seen_payloads[0].get("height"), 768)
        self.assertIn("Style: anime poster", str(seen_payloads[0].get("prompt", "")))

    def test_setup_test_image_gen_reuses_shared_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            )
            support = WebUISetupSupport(
                root_dir=Path(tmp),
                prompts_file=Path(tmp) / "prompts.yml",
                logger=logger,
                load_yaml_dict=lambda path: {},
                restore_masked_sensitive_values=lambda incoming, existing: incoming,
                is_masked_secret_placeholder=lambda value: False,
                strip_deprecated_local_paths_config=lambda config: config,
            )
            app = FastAPI()
            app.include_router(support.router)

            helper = AsyncMock(
                return_value=ImageGenResult(
                    ok=True,
                    message="图片已生成。",
                    url="https://example.com/test.png",
                    model_used="gemini-2.5-flash-image",
                )
            )
            with patch("core.webui_setup_support.generate_image_with_model_config", helper):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/webui/setup/test-image-gen",
                        json={
                            "provider": "gemini",
                            "model": "gemini-2.5-flash-image",
                            "api_key": "gemini-key",
                            "base_url": "https://generativelanguage.googleapis.com",
                            "size": "1024x1024",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("image_url"), "https://example.com/test.png")
        helper.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
