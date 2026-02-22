from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


class SkiAPIClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config.get("base_url", "https://skiapi.dev/v1")).rstrip("/")
        self.model = str(config.get("model", "gpt-4.1"))
        self.temperature = float(config.get("temperature", 0.8))
        self.max_tokens = int(config.get("max_tokens", 8192))
        self.timeout_seconds = int(config.get("timeout_seconds", 60))

        api_key = str(config.get("api_key", "")).strip()
        if api_key.startswith("${") and api_key.endswith("}"):
            api_key = os.getenv(api_key[2:-1], "")
        self.api_key = api_key or os.getenv("SKIAPI_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("缺少 SkiAPI 密钥。")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(f"{self.base_url}{endpoint}", headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        return await self._post("/chat/completions", payload)

    async def chat_text(self, messages: list[dict[str, str]]) -> str:
        data = await self.chat_completion(messages=messages)
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
        text = await self.chat_text(messages)
        text = text.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        payload = {
            "model": "gpt-image-1",
            "prompt": prompt,
            "size": size,
        }
        data = await self._post("/images/generations", payload)
        items = data.get("data") or []
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = first.get("url")
        if url:
            return str(url)
        b64 = first.get("b64_json")
        if b64:
            return f"data:image/png;base64,{b64}"
        return None
