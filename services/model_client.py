from __future__ import annotations

from typing import Any

from services.anthropic import AnthropicClient
from services.deepseek import DeepSeekClient
from services.gemini import GeminiClient
from services.openai import OpenAIClient
from services.skiapi import SkiAPIClient


class ModelClient:
    """按 provider 路由到不同厂商客户端。"""

    _ALIASES = {
        "skiapi": "skiapi",
        "openai": "openai",
        "deepseek": "deepseek",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "gemini",
        "gemeni": "gemini",
    }
    _CLIENTS = {
        "skiapi": SkiAPIClient,
        "openai": OpenAIClient,
        "deepseek": DeepSeekClient,
        "anthropic": AnthropicClient,
        "gemini": GeminiClient,
    }

    def __init__(self, config: dict[str, Any]):
        raw = config or {}
        provider = self._normalize_provider(str(raw.get("provider", "skiapi")))
        provider_cfg = self._resolve_provider_config(raw, provider)

        client_cls = self._CLIENTS.get(provider)
        if client_cls is None:
            raise RuntimeError(f"不支持的 provider: {provider}")

        self.provider = provider
        self.client = client_cls(provider_cfg)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.client, "enabled", False))

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", ""))

    @property
    def base_url(self) -> str:
        return str(getattr(self.client, "base_url", ""))

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return await self.client.chat_completion(
            messages=messages,
            response_format=response_format,
            max_tokens=max_tokens,
        )

    async def chat_text(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        return await self.client.chat_text(messages, max_tokens=max_tokens)

    async def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return await self.client.chat_json(messages)

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        return await self.client.generate_image(prompt=prompt, size=size)

    @classmethod
    def _normalize_provider(cls, provider: str) -> str:
        key = (provider or "").strip().lower()
        return cls._ALIASES.get(key, key or "skiapi")

    def _resolve_provider_config(self, config: dict[str, Any], provider: str) -> dict[str, Any]:
        merged = {k: v for k, v in (config or {}).items() if k != "providers"}
        providers = config.get("providers", {}) if isinstance(config, dict) else {}
        provider_cfg: dict[str, Any] = {}
        if isinstance(providers, dict):
            item = providers.get(provider)
            if item is None and provider == "gemini":
                item = providers.get("gemeni")
            if isinstance(item, dict):
                provider_cfg = item
        merged.update(provider_cfg)
        merged["provider"] = provider
        return merged
