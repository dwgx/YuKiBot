from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class SkiAPIClient(OpenAICompatibleClient):
    """SkiAPI 第三方聚合接口。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="skiapi",
            default_base_url="https://skiapi.dev",
            default_env_key="SKIAPI_KEY",
            # SkiAPI 根路径通常是官网页面，API 实际走 /v1。
            prefer_v1=True,
        )
