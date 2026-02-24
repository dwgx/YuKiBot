from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class OpenAIClient(OpenAICompatibleClient):
    """OpenAI 官方接口。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="openai",
            default_base_url="https://api.openai.com",
            default_env_key="OPENAI_API_KEY",
            prefer_v1=True,
        )
