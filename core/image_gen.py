"""增强图片生成引擎 — 支持多模型配置、NSFW 过滤、模板合成。

支持的后端:
- OpenAI DALL-E (dall-e-3)
- Flux (通过 OpenAI 兼容 API)
- Stable Diffusion WebUI (本地)
- 任何 OpenAI 兼容的图片生成 API
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger("yukiko.image_gen")

# ── NSFW 关键词黑名单（中英文） ──
_NSFW_KEYWORDS_ZH = frozenset({
    "裸体", "色情", "成人", "18禁", "无码", "性行为", "做爱",
    "露点", "情色", "淫秽", "黄色", "三级", "AV", "里番",
    "脱衣", "内衣", "比基尼", "泳装",
})
_NSFW_KEYWORDS_EN = frozenset({
    "nude", "naked", "nsfw", "porn", "xxx", "hentai", "erotic",
    "sexual", "explicit", "r18", "r-18", "lewd", "topless",
    "underwear", "lingerie", "bikini", "swimsuit",
})


@dataclass(slots=True)
class ImageGenResult:
    ok: bool
    message: str
    url: str = ""
    local_path: str = ""
    base64_data: str = ""
    model_used: str = ""
    revised_prompt: str = ""


@dataclass
class ImageGenConfig:
    """图片生成配置。"""
    enable: bool = True
    default_model: str = "dall-e-3"
    default_size: str = "1024x1024"
    nsfw_filter: bool = True
    max_prompt_length: int = 1000
    # 模型配置列表
    models: list[dict[str, Any]] = field(default_factory=list)
    # 本地模板目录
    template_dir: str = "storage/image_templates"


class ImageGenEngine:
    """增强图片生成引擎。"""

    def __init__(self, config: dict[str, Any] | None = None, model_client: Any = None):
        cfg = config if isinstance(config, dict) else {}
        img_cfg = cfg.get("image_gen", cfg)
        if not isinstance(img_cfg, dict):
            img_cfg = {}

        self.enabled = bool(img_cfg.get("enable", True))
        self.default_model = str(img_cfg.get("default_model", "dall-e-3"))
        self.default_size = str(img_cfg.get("default_size", "1024x1024"))
        self.nsfw_filter = bool(img_cfg.get("nsfw_filter", True))
        self.max_prompt_length = int(img_cfg.get("max_prompt_length", 1000))
        self.model_client = model_client

        # 解析模型配置
        self._models: dict[str, dict[str, Any]] = {}
        for m in img_cfg.get("models", []):
            if isinstance(m, dict) and m.get("name"):
                self._models[m["name"]] = m

        self._template_dir = Path(img_cfg.get("template_dir", "storage/image_templates"))
        self._template_dir.mkdir(parents=True, exist_ok=True)

    def check_nsfw(self, prompt: str) -> bool:
        """检查 prompt 是否包含 NSFW 内容。返回 True 表示安全。"""
        if not self.nsfw_filter:
            return True
        text_lower = prompt.lower()
        for kw in _NSFW_KEYWORDS_EN:
            if kw in text_lower:
                _log.warning("nsfw_blocked | keyword=%s", kw)
                return False
        for kw in _NSFW_KEYWORDS_ZH:
            if kw in prompt:
                _log.warning("nsfw_blocked | keyword=%s", kw)
                return False
        return True

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        style: str | None = None,
    ) -> ImageGenResult:
        """生成图片。"""
        if not self.enabled:
            return ImageGenResult(ok=False, message="图片生成功能未启用。")

        content = (prompt or "").strip()
        if not content:
            return ImageGenResult(ok=False, message="请提供绘图描述。")

        if len(content) > self.max_prompt_length:
            content = content[:self.max_prompt_length]

        # NSFW 过滤
        if not self.check_nsfw(content):
            return ImageGenResult(ok=False, message="检测到不适当内容，已拒绝生成。")

        use_model = model or self.default_model
        use_size = size or self.default_size

        # 尝试使用配置的模型
        model_cfg = self._models.get(use_model)
        if model_cfg:
            return await self._generate_with_config(content, model_cfg, use_size, style)

        # 回退到 model_client
        if self.model_client and hasattr(self.model_client, "generate_image"):
            try:
                url = await self.model_client.generate_image(content, size=use_size)
                if url:
                    return ImageGenResult(ok=True, message="图片已生成。", url=url, model_used=use_model)
            except Exception as exc:
                _log.warning("image_gen_fallback_error | %s", exc)

        return ImageGenResult(ok=False, message="图片生成失败，请检查模型配置。")

    async def _generate_with_config(
        self,
        prompt: str,
        model_cfg: dict[str, Any],
        size: str,
        style: str | None,
    ) -> ImageGenResult:
        """使用配置的模型生成图片。"""
        api_base = str(model_cfg.get("api_base", "")).rstrip("/")
        api_key = str(model_cfg.get("api_key", ""))
        model_name = str(model_cfg.get("model", model_cfg.get("name", "")))

        if not api_base or not api_key:
            return ImageGenResult(ok=False, message=f"模型 {model_name} 配置不完整。")

        # 确保 URL 正确：如果 api_base 已经包含 /v1，就不要再加了
        if api_base.endswith("/v1"):
            url = f"{api_base}/images/generations"
        else:
            url = f"{api_base}/v1/images/generations"

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body: dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "n": 1,
        }

        # grok-imagine 模型不支持 size 参数
        if "grok-imagine" not in model_name.lower():
            body["size"] = size

        if style:
            body["style"] = style

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                images = data.get("data", [])
                if images:
                    url_result = images[0].get("url", "")
                    b64 = images[0].get("b64_json", "")
                    revised = images[0].get("revised_prompt", "")
                    return ImageGenResult(
                        ok=True, message="图片已生成。",
                        url=url_result, base64_data=b64,
                        model_used=model_name, revised_prompt=revised,
                    )
        except Exception as exc:
            _log.warning("image_gen_api_error | model=%s | %s", model_name, exc)

        return ImageGenResult(ok=False, message=f"模型 {model_name} 生成失败。")

    def list_models(self) -> list[dict[str, str]]:
        """列出所有可用的图片生成模型。"""
        models = [{"name": self.default_model, "type": "default"}]
        for name, cfg in self._models.items():
            models.append({"name": name, "type": cfg.get("type", "openai_compatible")})
        return models
