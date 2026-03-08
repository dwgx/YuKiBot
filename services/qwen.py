from __future__ import annotations

from typing import Any

from services.openai_compatible import OpenAICompatibleClient


class QwenClient(OpenAICompatibleClient):
    """Qwen / 通义千问（DashScope OpenAI 兼容模式）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="qwen",
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            default_env_key="QWEN_API_KEY",
            prefer_v1=True,
        )

