from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class ZhipuClient(OpenAICompatibleClient):
    """智谱 AI（BigModel OpenAI 兼容接口）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="zhipu",
            default_base_url="https://open.bigmodel.cn/api/paas/v4",
            default_env_key="ZHIPU_API_KEY",
            prefer_v1=False,
        )

