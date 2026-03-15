from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.image_gen import detect_nsfw_prompt_reason
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
        self.model_client = model_client

    async def generate(self, prompt: str, size: str | None = None) -> ImageResult:
        content = (prompt or "").strip()
        if not content:
            return ImageResult(ok=False, message="请提供绘图描述。")
        if not self.enabled:
            return ImageResult(ok=False, message="当前配置未启用生图功能。")
        if not self.model_client.enabled:
            return ImageResult(ok=False, message="未配置模型密钥，无法生图。")
        if detect_nsfw_prompt_reason(content):
            return ImageResult(ok=False, message="检测到不适当内容，已拒绝生成。")

        image_url = await self.model_client.generate_image(content, size=size or self.default_size)
        if not image_url:
            return ImageResult(ok=False, message="图片生成失败，请稍后重试。")
        return ImageResult(ok=True, message="图片已生成。", url=image_url)
