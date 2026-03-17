from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.image_gen import (
    IMAGE_PROMPT_BLOCKED_MESSAGE,
    assess_prompt_qq_ban_risk,
    detect_custom_prompt_risk_reason,
    detect_qq_ban_risk_reason,
)
from services.model_client import ModelClient


@dataclass(slots=True)
class ImageResult:
    ok: bool
    message: str
    url: str = ""


class ImageEngine:
    def __init__(self, config: dict[str, Any], model_client: ModelClient):
        self.enabled = bool(config.get("enable", True))
        self.default_size = str(config.get("default_size", "1024x1024"))
        self.prompt_review_enable = bool(config.get("prompt_review_enable", True))
        self.prompt_review_fail_closed = bool(
            config.get("prompt_review_fail_closed", False)
        )
        self.prompt_review_model = str(config.get("prompt_review_model", "")).strip()
        self.prompt_review_max_tokens = max(
            80, min(600, int(config.get("prompt_review_max_tokens", 180)))
        )
        self.custom_block_terms = list(config.get("custom_block_terms", []) or [])
        self.custom_allow_terms = list(config.get("custom_allow_terms", []) or [])
        self.model_client = model_client

    async def generate(self, prompt: str, size: str | None = None) -> ImageResult:
        content = (prompt or "").strip()
        if not content:
            return ImageResult(ok=False, message="请提供绘图描述。")
        if not self.enabled:
            return ImageResult(ok=False, message="当前配置未启用生图功能。")
        if not self.model_client.enabled:
            return ImageResult(ok=False, message="未配置模型密钥，无法生图。")
        if self.prompt_review_enable:
            safe, _reason = await assess_prompt_qq_ban_risk(
                content,
                model_client=self.model_client,
                review_model=self.prompt_review_model,
                max_tokens=self.prompt_review_max_tokens,
                fail_closed=self.prompt_review_fail_closed,
                custom_block_terms=self.custom_block_terms,
                custom_allow_terms=self.custom_allow_terms,
            )
            if not safe:
                return ImageResult(ok=False, message=IMAGE_PROMPT_BLOCKED_MESSAGE)
        elif detect_custom_prompt_risk_reason(
            content,
            custom_block_terms=self.custom_block_terms,
            custom_allow_terms=self.custom_allow_terms,
        ) or detect_qq_ban_risk_reason(content):
            return ImageResult(ok=False, message=IMAGE_PROMPT_BLOCKED_MESSAGE)

        image_url = await self.model_client.generate_image(content, size=size or self.default_size)
        if not image_url:
            return ImageResult(ok=False, message="图片生成失败，请稍后重试。")
        return ImageResult(ok=True, message="图片已生成。", url=image_url)
