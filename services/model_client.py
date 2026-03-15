from __future__ import annotations

import logging
import re
from typing import Any

from services.anthropic import AnthropicClient
from services.deepseek import DeepSeekClient
from services.gemini import GeminiClient
from services.mistral import MistralClient
from services.moonshot import MoonshotClient
from services.newapi import NewAPIClient
from services.openai import OpenAIClient
from services.openrouter import OpenRouterClient
from services.qwen import QwenClient
from services.siliconflow import SiliconFlowClient
from services.skiapi import SkiAPIClient
from services.xai import XAIClient
from services.zhipu import ZhipuClient

_log = logging.getLogger("yukiko.model_client")

# 触发 provider 降级的错误关键词
_FATAL_ERROR_CUES = (
    "suspended", "forbidden", "unauthorized", "banned",
    "account", "disabled", "quota", "rate_limit",
)


class ModelClient:
    """按 provider 路由到不同厂商客户端，支持自动降级。

    配置示例 (config.yml):
        api:
        provider: skiapi
        fallback_providers:
            - anthropic
            - openai
        providers:
            anthropic:
                api_key: sk-xxx
                model: claude-sonnet-4-20250514
            openai:
                api_key: sk-xxx
                model: gpt-4.1
    """

    _ALIASES = {
        "skiapi": "skiapi",
        "openai": "openai",
        "deepseek": "deepseek",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "gemini",
        "gemeni": "gemini",
        "openrouter": "openrouter",
        "open_router": "openrouter",
        "newapi": "newapi",
        "new_api": "newapi",
        "new-api": "newapi",
        "xai": "xai",
        "x.ai": "xai",
        "grok": "xai",
        "qwen": "qwen",
        "tongyi": "qwen",
        "dashscope": "qwen",
        "moonshot": "moonshot",
        "kimi": "moonshot",
        "mistral": "mistral",
        "zhipu": "zhipu",
        "bigmodel": "zhipu",
        "glm": "zhipu",
        "siliconflow": "siliconflow",
        "silicon_flow": "siliconflow",
    }
    _CLIENTS = {
        "skiapi": SkiAPIClient,
        "openai": OpenAIClient,
        "deepseek": DeepSeekClient,
        "anthropic": AnthropicClient,
        "gemini": GeminiClient,
        "openrouter": OpenRouterClient,
        "newapi": NewAPIClient,
        "xai": XAIClient,
        "qwen": QwenClient,
        "moonshot": MoonshotClient,
        "mistral": MistralClient,
        "zhipu": ZhipuClient,
        "siliconflow": SiliconFlowClient,
    }

    def __init__(self, config: dict[str, Any]):
        raw = config or {}
        provider = self._normalize_provider(str(raw.get("provider", "skiapi")))
        provider_cfg = self._resolve_provider_config(raw, provider)

        client_cls = self._CLIENTS.get(provider)
        if client_cls is None:
            raise RuntimeError(f"不支持的 provider: {provider}")

        self.provider = provider
        self.client = client_cls(provider_cfg)
        self._config = raw

        # 降级链: 主 provider 失败时依次尝试
        self._fallback_providers: list[str] = []
        self._fallback_clients: dict[str, Any] = {}
        self._active_provider = provider  # 当前实际使用的 provider
        self._init_fallbacks(raw)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.client, "enabled", False))

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", ""))

    @property
    def base_url(self) -> str:
        return str(getattr(self.client, "base_url", ""))

    def _init_fallbacks(self, config: dict[str, Any]) -> None:
        """初始化降级 provider 链。"""
        fb_list = config.get("fallback_providers", [])
        if not isinstance(fb_list, list):
            return
        for name in fb_list:
            prov = self._normalize_provider(str(name))
            if prov == self.provider or prov in self._fallback_clients:
                continue
            cls = self._CLIENTS.get(prov)
            if cls is None:
                continue
            try:
                pcfg = self._resolve_provider_config(config, prov)
                client = cls(pcfg)
                if getattr(client, "enabled", False):
                    self._fallback_providers.append(prov)
                    self._fallback_clients[prov] = client
                    _log.info("fallback_provider_ready | %s", prov)
            except Exception as exc:
                _log.warning("fallback_provider_init_fail | %s | %s", prov, exc)

    @staticmethod
    def _is_fatal_error(exc: Exception) -> bool:
        """判断是否为不可恢复错误（应触发 provider 降级）。"""
        msg = str(exc).lower()
        return any(cue in msg for cue in _FATAL_ERROR_CUES) or "403" in msg or "401" in msg

    def _get_active_client(self) -> Any:
        """返回当前活跃的 client（可能已降级）。"""
        if self._active_provider != self.provider:
            fb = self._fallback_clients.get(self._active_provider)
            if fb:
                return fb
        return self.client

    @staticmethod
    def _supports_method(client: Any, method_name: str) -> bool:
        return callable(getattr(client, method_name, None))

    @staticmethod
    def _fallback_supported_error(method_name: str, exc: Exception) -> bool:
        raw = str(exc or "").strip()
        msg = raw.lower()
        if ModelClient._is_fatal_error(exc):
            return True
        if "不支持" in raw:
            return True
        if any(cue in msg for cue in ("not support", "unsupported", "not implemented")):
            return True
        if any(cue in raw for cue in ("缺少密钥", "未配置", "未启用")):
            return True
        if any(cue in msg for cue in ("missing key", "api key", "not configured", "disabled")):
            return True
        return False

    async def _invoke_with_failover(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        active_provider = self._active_provider
        active_client = self._get_active_client()
        if not self._supports_method(active_client, method_name):
            exc = RuntimeError(f"{active_provider} 不支持 {method_name}")
            if not self._fallback_providers or not self._fallback_supported_error(method_name, exc):
                raise exc
            _log.warning(
                "provider_fatal | %s | method=%s | %s | trying fallback",
                active_provider,
                method_name,
                exc,
            )
        else:
            try:
                return await getattr(active_client, method_name)(*args, **kwargs)
            except Exception as exc:
                if not self._fallback_providers or not self._fallback_supported_error(method_name, exc):
                    raise
                _log.warning(
                    "provider_fatal | %s | method=%s | %s | trying fallback",
                    active_provider,
                    method_name,
                    exc,
                )

        for prov in self._fallback_providers:
            if prov == active_provider:
                continue
            client = self._fallback_clients.get(prov)
            if not client or not self._supports_method(client, method_name):
                continue
            try:
                result = await getattr(client, method_name)(*args, **kwargs)
                _log.info(
                    "provider_failover_ok | %s -> %s | method=%s",
                    active_provider,
                    prov,
                    method_name,
                )
                self._active_provider = prov
                return result
            except Exception as fb_exc:
                _log.warning(
                    "fallback_also_failed | %s | method=%s | %s",
                    prov,
                    method_name,
                    fb_exc,
                )

        raise RuntimeError("所有 provider 均不可用")

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "response_format": response_format,
            "max_tokens": max_tokens,
        }
        if str(model or "").strip():
            kwargs["model"] = str(model).strip()
        return await self._invoke_with_failover("chat_completion", **kwargs)

    async def chat_text(self, messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
        data = await self.chat_completion(messages=messages, max_tokens=max_tokens)
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text", "")))
            return "".join(parts)
        return str(content)

    async def chat_text_with_retry(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        retries: int = 2,
        backoff: float = 1.0,
    ) -> str:
        # 统一走 ModelClient.chat_text，确保可触发 provider 级 failover。
        import asyncio as _aio

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await self.chat_text(messages=messages, max_tokens=max_tokens)
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    await _aio.sleep(backoff * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    async def chat_json(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._invoke_with_failover("chat_json", messages)

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        return await self._invoke_with_failover("generate_image", prompt=prompt, size=size)

    def supports_vision_input(self, model: str | None = None) -> bool:
        """判断当前模型是否支持图片输入（自动启发式，可被配置覆盖）。"""
        active = self._get_active_client()
        cfg = getattr(active, "config", {}) or {}
        override = str(cfg.get("supports_vision_input", "auto")).strip().lower()
        if override in {"1", "true", "yes", "on"}:
            return True
        if override in {"0", "false", "no", "off"}:
            return False

        model_name = str(model or getattr(active, "model", "") or self.model).strip().lower()
        provider = self._active_provider
        return self._infer_vision_support(provider=provider, model_name=model_name)

    @staticmethod
    def _infer_vision_support(provider: str, model_name: str) -> bool:
        name = (model_name or "").strip().lower()
        if not name:
            return False

        # 明显文本模型（优先排除，避免误发图片）
        text_only_cues = (
            "embedding",
            "rerank",
            "bge",
            "text-embedding",
            "gpt-3.5",
            "instruct",
            "reasoner",
            "coder",
            "code-",
            "whisper",
            "tts",
            "asr",
        )
        if any(cue in name for cue in text_only_cues):
            return False

        # 常见视觉模型命名
        vision_cues = (
            "gpt-4o",
            "gpt-4.1",
            "gpt-4.5",
            "o1",
            "o3",
            "o4",
            "claude-3",
            "claude-4",
            "gemini",
            "vision",
            "-vl",
            "vl-",
            "qwen-vl",
            "glm-4v",
            "llava",
            "minicpm-v",
        )
        if any(cue in name for cue in vision_cues):
            return True

        # claude 新模型默认按可看图处理（老 2.x 除外）
        if "claude" in name:
            if re.search(r"claude[-_ ]?2", name):
                return False
            return True

        # provider 兜底（仅在名字没有明显冲突时启用）
        if provider in {"gemini"}:
            return True
        if provider in {"openai", "newapi", "skiapi", "deepseek", "anthropic"}:
            # 这些 provider 同时有文本模型和视觉模型，没有明确信号时保守关闭
            return False
        return False

    def supports_multimodal_messages(self) -> bool:
        """判断当前通道是否支持 OpenAI 风格的图片消息块。"""
        active = self._get_active_client()
        value = getattr(active, "supports_multimodal_messages", False)
        if isinstance(value, bool):
            return value
        if callable(value):
            try:
                return bool(value())
            except Exception:
                return False
        return False

    @classmethod
    def _normalize_provider(cls, provider: str) -> str:
        key = (provider or "").strip().lower()
        return cls._ALIASES.get(key, key or "skiapi")

    def _resolve_provider_config(self, config: dict[str, Any], provider: str) -> dict[str, Any]:
        merged = {k: v for k, v in (config or {}).items() if k != "providers"}
        providers = config.get("providers", {}) if isinstance(config, dict) else {}
        provider_cfg: dict[str, Any] = {}
        if isinstance(providers, dict):
            item = providers.get(provider)
            if item is None and provider == "gemini":
                item = providers.get("gemeni")
            if isinstance(item, dict):
                provider_cfg = item
        merged.update(provider_cfg)
        merged["provider"] = provider
        return merged
