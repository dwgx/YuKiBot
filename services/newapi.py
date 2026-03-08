from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class NewAPIClient(OpenAICompatibleClient):
    """NEWAPI 聚合接口（OpenAI 兼容）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="newapi",
            default_base_url="https://api.openai.com/v1",
            default_env_key="NEWAPI_API_KEY",
            prefer_v1=True,
        )

