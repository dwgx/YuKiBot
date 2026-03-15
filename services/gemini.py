from __future__ import annotations

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
