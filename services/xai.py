from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class XAIClient(OpenAICompatibleClient):
    """xAI (Grok) 官方接口（OpenAI 兼容）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="xai",
            default_base_url="https://api.x.ai/v1",
            default_env_key="XAI_API_KEY",
            prefer_v1=True,
        )

