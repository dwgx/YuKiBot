from __future__ import annotations

from typing import Any

import httpx

from services.base_client import BaseLLMClient


class AnthropicClient(BaseLLMClient):
    """Anthropic 官方接口（Claude）。"""

    def __init__(self, config: dict[str, Any]):
        super().__init__(
            config=config,
            provider="anthropic",
            default_base_url="https://api.anthropic.com",
            default_env_key="ANTHROPIC_API_KEY",
        )
        self.anthropic_version = str(config.get("anthropic_version", "2023-06-01"))
        self.prefer_v1 = bool(config.get("prefer_v1", True))

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = (response_format, tools, tool_choice)
        if not self.enabled:
            raise RuntimeError("缺少密钥，请配置 ANTHROPIC_API_KEY")

        system_text, converted_messages = self._convert_messages(messages)
        resolved_max_tokens = self.max_tokens if max_tokens is None else max(1, int(max_tokens))
        model_name = str(model or self.model).strip() or self.model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": converted_messages,
            "temperature": self.temperature,
            "max_tokens": resolved_max_tokens,
        }
        if system_text:
            payload["system"] = system_text

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "Content-Type": "application/json",
        }
        data = await self._post_with_base_candidates(
            endpoint="/messages",
            payload=payload,
            headers=headers,
            prefer_v1=self.prefer_v1,
        )
        text = self._extract_text(data)
        return {"choices": [{"message": {"content": text}}], "raw": data}

    async def _post_with_base_candidates(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        prefer_v1: bool,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for base in self._candidate_base_urls(self.base_url, prefer_v1=prefer_v1):
            url = f"{base}{endpoint}"
            try:
                return await self._post_json(url=url, payload=payload, headers=headers)
            except Exception as exc:
                errors.append(f"{url} -> {type(exc).__name__}: {exc}")

        tail = " | ".join(errors[-2:]) if errors else "未知错误"
        raise RuntimeError(f"anthropic 请求失败：{tail}")

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("接口返回格式异常，顶层不是对象")
        return data

    @staticmethod
    def _candidate_base_urls(base_url: str, prefer_v1: bool) -> list[str]:
        base = (base_url or "").rstrip("/")
        if not base:
            return []
        with_v1 = base if base.endswith("/v1") else f"{base}/v1"
        without_v1 = base[:-3] if base.endswith("/v1") else base
        candidates = [with_v1, without_v1] if prefer_v1 else [without_v1, with_v1]
        uniq: list[str] = []
        for item in candidates:
            value = item.rstrip("/")
            if value and value not in uniq:
                uniq.append(value)
        return uniq

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role", "user")).strip().lower()
            content = str(msg.get("content", ""))
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
                continue
            anth_role = "assistant" if role == "assistant" else "user"
            out.append({"role": anth_role, "content": [{"type": "text", "text": content}]})
        if not out:
            out = [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
        return "\n".join(system_parts).strip(), out

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        content = data.get("content") or []
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and str(item.get("type", "")) == "text":
                    parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
