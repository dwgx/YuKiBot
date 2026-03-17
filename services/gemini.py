from __future__ import annotations

from math import gcd
from typing import Any
from urllib.parse import quote

import httpx

from services.base_client import BaseLLMClient


class GeminiClient(BaseLLMClient):
    """Gemini 官方接口。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="gemini",
            default_base_url="https://generativelanguage.googleapis.com",
            default_env_key="GEMINI_API_KEY",
        )
        self.api_version = str(config.get("gemini_api_version", "v1beta")).strip("/")

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        _ = response_format
        if not self.enabled:
            raise RuntimeError("缺少密钥，请配置 GEMINI_API_KEY")

        payload = self._convert_messages(messages, max_tokens=max_tokens)
        model_name_raw = str(model or self.model).strip() or self.model
        model_name = quote(model_name_raw, safe="-_.")
        base = self._strip_api_version_suffix(self.base_url)
        url = f"{base}/{self.api_version}/models/{model_name}:generateContent?key={self.api_key}"

        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("接口返回格式异常，顶层不是对象")
        text = self._extract_text(data)
        return {"choices": [{"message": {"content": text}}], "raw": data}

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str | None = None,
    ) -> str | None:
        if not self.enabled:
            raise RuntimeError("缺少密钥，请配置 GEMINI_API_KEY")

        model_name = str(self.image_model or self.model).strip() or self.model
        merged_prompt = str(prompt or "").strip()
        if style:
            merged_prompt = f"{merged_prompt}\nStyle: {style.strip()}".strip()

        if "/openai" in self.base_url.lower():
            return await self._generate_image_openai_compatible(
                model_name=model_name,
                prompt=merged_prompt,
                size=size,
            )
        return await self._generate_image_native(
            model_name=model_name,
            prompt=merged_prompt,
            size=size,
        )

    @staticmethod
    def _strip_api_version_suffix(base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    def _convert_messages(self, messages: list[dict[str, Any]], max_tokens: int | None = None) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        resolved_max_tokens = self.max_tokens if max_tokens is None else max(1, int(max_tokens))

        for msg in messages:
            role = str(msg.get("role", "user")).strip().lower()
            content = str(msg.get("content", ""))
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
                continue
            gemini_role = "model" if role == "assistant" else "user"
            part = {"text": content}
            if contents and contents[-1].get("role") == gemini_role:
                contents[-1].setdefault("parts", []).append(part)
            else:
                contents.append({"role": gemini_role, "parts": [part]})

        if not contents:
            contents = [{"role": "user", "parts": [{"text": "你好"}]}]

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": resolved_max_tokens,
            },
        }
        system_text = "\n".join(system_parts).strip()
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}
        return payload

    async def _generate_image_openai_compatible(
        self,
        *,
        model_name: str,
        prompt: str,
        size: str,
    ) -> str | None:
        payload: dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
        }
        if size:
            payload["size"] = size

        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Gemini OpenAI 兼容接口返回格式异常")
        items = data.get("data") or []
        if not isinstance(items, list) or not items:
            return None
        first = items[0] if isinstance(items[0], dict) else {}
        url = str(first.get("url", "") or "").strip()
        if url:
            return url
        b64 = str(first.get("b64_json", "") or "").strip()
        if b64:
            return f"data:image/png;base64,{b64}"
        return None

    async def _generate_image_native(
        self,
        *,
        model_name: str,
        prompt: str,
        size: str,
    ) -> str | None:
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        image_config = self._build_native_image_config(size)
        if image_config:
            payload["generationConfig"]["imageConfig"] = image_config

        base = self._strip_api_version_suffix(self.base_url)
        versions: list[str] = []
        for value in (self.api_version, "v1beta", "v1"):
            version = str(value or "").strip("/")
            if version and version not in versions:
                versions.append(version)

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            for version in versions:
                url = f"{base}/{version}/models/{quote(model_name, safe='-_.')}:generateContent?key={self.api_key}"
                try:
                    response = await client.post(
                        url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise RuntimeError("Gemini 返回格式异常")
                    inline = self._extract_inline_image_data(data)
                    if inline:
                        return f"data:image/png;base64,{inline}"
                    text = self._extract_text(data)
                    raise RuntimeError(text or "Gemini 未返回图片数据")
                except Exception as exc:
                    last_error = exc

        if last_error is not None:
            raise last_error
        return None

    @staticmethod
    def _extract_inline_image_data(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not isinstance(candidates, list):
            return ""
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts") or []
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData")
                if not isinstance(inline, dict):
                    inline = part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                raw = str(inline.get("data", "") or "").strip()
                if raw:
                    return raw
        return ""

    @staticmethod
    def _build_native_image_config(size: str) -> dict[str, Any]:
        width, height = GeminiClient._parse_size(size)
        if width is None or height is None:
            return {}
        config: dict[str, Any] = {}
        aspect_ratio = GeminiClient._size_to_aspect_ratio(width, height)
        if aspect_ratio:
            config["aspectRatio"] = aspect_ratio
        if max(width, height) >= 1536:
            config["imageSize"] = "2K"
        return config

    @staticmethod
    def _parse_size(size: str) -> tuple[int | None, int | None]:
        parts = str(size or "").lower().split("x", 1)
        if len(parts) != 2:
            return None, None
        try:
            width = int(parts[0].strip())
            height = int(parts[1].strip())
        except Exception:
            return None, None
        if width <= 0 or height <= 0:
            return None, None
        return width, height

    @staticmethod
    def _size_to_aspect_ratio(width: int, height: int) -> str:
        common = {
            (1, 1): "1:1",
            (16, 9): "16:9",
            (9, 16): "9:16",
            (4, 3): "4:3",
            (3, 4): "3:4",
            (3, 2): "3:2",
            (2, 3): "2:3",
        }
        div = gcd(width, height)
        reduced = (width // div, height // div)
        if reduced in common:
            return common[reduced]
        return f"{reduced[0]}:{reduced[1]}"

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            block_reason = str(feedback.get("blockReason", "")).strip()
            return f"模型未返回内容（{block_reason or '未知原因'}）"
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first, dict) else {}
        parts = content.get("parts") if isinstance(content, dict) else []
        out: list[str] = []
        if isinstance(parts, list):
            for item in parts:
                if isinstance(item, dict):
                    out.append(str(item.get("text", "")))
        return "".join(out).strip()
