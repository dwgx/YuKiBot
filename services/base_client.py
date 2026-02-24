from __future__ import annotations

import json
import os
import re
from typing import Any


class BaseLLMClient:
    """模型客户端基类。"""

    def __init__(
        self,
        config: dict[str, Any],
        provider: str,
        default_base_url: str,
        default_env_key: str,
    ):
        self.config = config or {}
        self.provider = provider
        self.default_env_key = default_env_key

        self.base_url = str(self.config.get("base_url", default_base_url)).rstrip("/")
        self.model = str(self.config.get("model", "gpt-4.1"))
        self.temperature = float(self.config.get("temperature", 0.8))
        self.max_tokens = int(self.config.get("max_tokens", 8192))
        self.timeout_seconds = int(self.config.get("timeout_seconds", 60))
        self.image_model = str(self.config.get("image_model", "gpt-image-1"))

        self.api_key = self._resolve_api_key()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def chat_text(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        data = await self.chat_completion(messages=messages, max_tokens=max_tokens)
        choices = data.get("choices") or []
        if not choices:
            return ""

        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
            return "".join(parts)
        return str(content)

    async def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        text = (await self.chat_text(messages)).strip()
        if not text:
            return {}

        clean = self._strip_code_fence(text)
        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", clean)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        raise RuntimeError(f"{self.provider} 不支持生图接口")

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        content = text.strip()
        if content.startswith("```") and content.endswith("```"):
            content = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return content.strip()

    def _resolve_api_key(self) -> str:
        raw = str(self.config.get("api_key", "")).strip()
        if raw.startswith("${") and raw.endswith("}"):
            raw = os.getenv(raw[2:-1], "")
        if raw:
            return raw
        return os.getenv(self.default_env_key, "").strip()
