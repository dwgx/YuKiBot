from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek 官方接口。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="deepseek",
            default_base_url="https://api.deepseek.com",
            default_env_key="DEEPSEEK_API_KEY",
            prefer_v1=True,
        )
