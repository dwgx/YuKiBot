from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class MistralClient(OpenAICompatibleClient):
    """Mistral 官方接口（OpenAI 兼容）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="mistral",
            default_base_url="https://api.mistral.ai",
            default_env_key="MISTRAL_API_KEY",
            prefer_v1=True,
        )

