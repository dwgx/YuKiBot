from __future__ import annotations

from typing import Any

import httpx

from services.base_client import BaseLLMClient


class OpenAICompatibleClient(BaseLLMClient):
    """OpenAI 兼容协议客户端（chat/completions + images/generations）。"""

    def __init__(
        self,
        config: dict[str, Any],
        provider: str,
        default_base_url: str,
        default_env_key: str,
        prefer_v1: bool,
    ):
        super().__init__(
            config=config,
            provider=provider,
            default_base_url=default_base_url,
            default_env_key=default_env_key,
        )
        self.prefer_v1 = bool(config.get("prefer_v1", prefer_v1))

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(f"缺少密钥，请配置 {self.default_env_key}")

        resolved_max_tokens = self.max_tokens if max_tokens is None else max(1, int(max_tokens))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": resolved_max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        return await self._post_with_base_candidates(
            endpoint="/chat/completions",
            payload=payload,
            headers=headers,
            prefer_v1=self.prefer_v1,
        )

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        if not self.enabled:
            raise RuntimeError(f"缺少密钥，请配置 {self.default_env_key}")

        payload = {
            "model": self.image_model,
            "prompt": prompt,
            "size": size,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = await self._post_with_base_candidates(
            endpoint="/images/generations",
            payload=payload,
            headers=headers,
            prefer_v1=self.prefer_v1,
        )
        items = data.get("data") or []
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = first.get("url")
        if url:
            return str(url)
        b64 = first.get("b64_json")
        if b64:
            return f"data:image/png;base64,{b64}"
        return None

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
        raise RuntimeError(f"{self.provider} 请求失败：{tail}")

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            detail = ""
            try:
                err = response.json()
                if isinstance(err, dict):
                    maybe = err.get("error")
                    if isinstance(maybe, dict):
                        detail = str(maybe.get("message", "")).strip()
                    if not detail:
                        detail = str(err.get("message", "")).strip()
            except Exception:
                detail = ""
            if not detail:
                detail = (response.text or "")[:200].strip()
            raise RuntimeError(f"HTTP {response.status_code}: {detail or '请求失败'}")
        try:
            data = response.json()
        except ValueError as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(f"接口返回非 JSON：{preview}") from exc
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
