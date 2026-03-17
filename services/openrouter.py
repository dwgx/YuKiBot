from __future__ import annotations

from math import gcd
from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class OpenRouterClient(OpenAICompatibleClient):
    """OpenRouter 聚合接口（OpenAI 兼容）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="openrouter",
            default_base_url="https://openrouter.ai/api/v1",
            default_env_key="OPENROUTER_API_KEY",
            prefer_v1=True,
        )

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str | None = None,
    ) -> str | None:
        if not self.enabled:
            raise RuntimeError(f"缺少密钥，请配置 {self.default_env_key}")

        merged_prompt = str(prompt or "").strip()
        if style:
            merged_prompt = f"{merged_prompt}\nStyle: {style.strip()}".strip()

        payload: dict[str, Any] = {
            "model": self.image_model,
            "messages": [{"role": "user", "content": merged_prompt}],
            "modalities": ["image", "text"],
            "stream": False,
        }
        image_config = self._build_image_config(size)
        if image_config:
            payload["image_config"] = image_config

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = await self._post_with_base_candidates(
            endpoint="/chat/completions",
            payload=payload,
            headers=headers,
            prefer_v1=self.prefer_v1,
            stream_response=False,
        )
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else {}
        if not isinstance(message, dict):
            return None
        images = message.get("images") or []
        if not isinstance(images, list) or not images:
            return None
        for item in images:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if not isinstance(item, dict):
                continue
            nested = item.get("image_url")
            if isinstance(nested, dict):
                nested_url = str(nested.get("url", "") or "").strip()
                if nested_url:
                    return nested_url
            url = str(item.get("image_url", "") or item.get("url", "") or "").strip()
            if url:
                return url
            data_uri = str(item.get("data", "") or item.get("image", "") or "").strip()
            if data_uri:
                return data_uri
        return None

    @staticmethod
    def _build_image_config(size: str) -> dict[str, Any]:
        width, height = OpenRouterClient._parse_size(size)
        if width is None or height is None:
            return {}
        config: dict[str, Any] = {"size": f"{width}x{height}"}
        ratio = OpenRouterClient._size_to_aspect_ratio(width, height)
        if ratio:
            config["aspect_ratio"] = ratio
        return config

    @staticmethod
    def _parse_size(size: str) -> tuple[int | None, int | None]:
        parts = str(size or "").lower().split("x", 1)
        if len(parts) != 2:
            return None, None
        try:
            width = int(parts[0].strip())
            height = int(parts[1].strip())
        except Exception:
            return None, None
        if width <= 0 or height <= 0:
            return None, None
        return width, height

    @staticmethod
    def _size_to_aspect_ratio(width: int, height: int) -> str:
        common = {
            (1, 1): "1:1",
            (16, 9): "16:9",
            (9, 16): "9:16",
            (4, 3): "4:3",
            (3, 4): "3:4",
            (3, 2): "3:2",
            (2, 3): "2:3",
        }
        div = gcd(width, height)
        reduced = (width // div, height // div)
        if reduced in common:
            return common[reduced]
        return f"{reduced[0]}:{reduced[1]}"
