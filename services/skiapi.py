from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class SkiAPIClient(OpenAICompatibleClient):
    """SkiAPI 第三方聚合接口。"""

    def __init__(self, config: dict[str, Any]):
        # SkiAPI 常见部署是 NEWAPI 兼容层，默认开启流式更稳。
        cfg = dict(config or {})
        cfg.setdefault("stream_chat_completions", True)
        super().__init__(
            config=cfg,
            provider="skiapi",
            default_base_url="https://skiapi.dev",
            default_env_key="SKIAPI_KEY",
            prefer_v1=True,
        )
