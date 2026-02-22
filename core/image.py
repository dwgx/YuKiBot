from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.skiapi import SkiAPIClient


@dataclass(slots=True)
class ImageResult:
    ok: bool
    message: str
    url: str = ""


class ImageEngine:
    def __init__(self, config: dict[str, Any], skiapi: SkiAPIClient):
        self.enabled = bool(config.get("enable", True))
        self.default_size = str(config.get("default_size", "1024x1024"))
        self.skiapi = skiapi

    async def generate(self, prompt: str, size: str | None = None) -> ImageResult:
        content = prompt.strip()
        if not content:
            return ImageResult(ok=False, message="请提供绘图描述。")
        if not self.enabled:
            return ImageResult(ok=False, message="当前配置未启用生图功能。")
        if not self.skiapi.enabled:
            return ImageResult(ok=False, message="未配置 SkiAPI 密钥，无法生图。")

        image_url = await self.skiapi.generate_image(content, size=size or self.default_size)
        if not image_url:
            return ImageResult(ok=False, message="图片生成失败，请稍后重试。")
        return ImageResult(ok=True, message="已为你生成图片。", url=image_url)
