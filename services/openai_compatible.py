from __future__ import annotations

import json
import time
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
        # NEWAPI/Codex 等部分代理在非 stream 下兼容性较差，默认开启流式聚合。
        self.stream_chat_completions = self._as_bool(
            config.get("stream_chat_completions", config.get("stream", provider == "newapi"))
        )
        endpoint_raw = config.get("endpoint_type", config.get("wire_api", "openai"))
        self.endpoint_type = self._normalize_endpoint_type(str(endpoint_raw))
        # 默认不在 openai_response 失败后回退 chat/completions，避免 Codex 通道误触发不支持端点。
        self.allow_response_fallback_to_chat = self._as_bool(
            config.get("allow_response_fallback_to_chat", False)
        )
        self._http_client: httpx.AsyncClient | None = None
        self.supports_multimodal_messages = True

    def _get_client(self) -> httpx.AsyncClient:
        """获取或创建持久化 httpx 客户端 (连接池复用)。"""
        if self._http_client is None or self._http_client.is_closed:
            # transport(proxy=None) 才能真正绕过系统代理
            # AsyncClient(proxy=None) 只是"使用默认代理检测"，不会禁用
            transport = httpx.AsyncHTTPTransport(
                proxy=None,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            )
            self._http_client = httpx.AsyncClient(
                timeout=float(self.timeout_seconds),
                transport=transport,
            )
        return self._http_client

    async def close(self) -> None:
        """关闭 httpx 客户端。"""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(f"缺少密钥，请配置 {self.default_env_key}")

        resolved_max_tokens = self.max_tokens if max_tokens is None else max(1, int(max_tokens))
        model_name = str(model or self.model).strip() or self.model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": resolved_max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        if self.stream_chat_completions:
            payload["stream"] = True

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if self.endpoint_type == "openai_response":
            try:
                return await self._chat_completion_via_responses(
                    messages=messages,
                    max_tokens=resolved_max_tokens,
                    headers=headers,
                    model_name=model_name,
                )
            except Exception as exc:
                if not self.allow_response_fallback_to_chat:
                    raise
                # 仅在显式允许时回退，默认保持 responses 错误原样暴露，便于定位网关问题。
                if not self._is_responses_fallback_worthy(exc):
                    raise

        stream_enabled = self.stream_chat_completions
        try:
            data = await self._post_with_base_candidates(
                endpoint="/chat/completions",
                payload=payload,
                headers=headers,
                prefer_v1=self.prefer_v1,
                stream_response=stream_enabled,
            )
            if stream_enabled and self._looks_empty_completion(data):
                return await self._post_with_base_candidates(
                    endpoint="/chat/completions",
                    payload=self._without_stream(payload),
                    headers=headers,
                    prefer_v1=self.prefer_v1,
                    stream_response=False,
                )
            return data
        except Exception:
            if not stream_enabled:
                raise
            # 部分 NEWAPI 网关在 stream=true 下不稳定，自动降级为非流式再试一次
            return await self._post_with_base_candidates(
                endpoint="/chat/completions",
                payload=self._without_stream(payload),
                headers=headers,
                prefer_v1=self.prefer_v1,
                stream_response=False,
            )

    async def _chat_completion_via_responses(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        headers: dict[str, str],
        model_name: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "input": self._messages_to_responses_input(messages),
            "temperature": self.temperature,
            "max_output_tokens": max(1, int(max_tokens)),
        }
        try:
            data = await self._post_with_base_candidates(
                endpoint="/responses",
                payload=payload,
                headers=headers,
                prefer_v1=self.prefer_v1,
                stream_response=False,
            )
        except Exception as exc:
            # 兼容 SkiAPI/Codex 等要求 Responses 必须 stream=true 的网关。
            err_text = str(exc).lower()
            if (
                "stream must be set to true" not in err_text
                and "stream must be true" not in err_text
            ):
                raise
            stream_payload = dict(payload)
            stream_payload["stream"] = True
            data = await self._post_responses_stream_with_base_candidates(
                payload=stream_payload,
                headers=headers,
                prefer_v1=self.prefer_v1,
            )
        content = self._extract_text_from_responses(data)
        if not content:
            raise RuntimeError("responses 返回为空")
        return {
            "id": str(data.get("id", "")),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(data.get("model", model_name)),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "raw": data,
        }

    async def _post_responses_stream_with_base_candidates(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        prefer_v1: bool,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for base in self._candidate_base_urls(self.base_url, prefer_v1=prefer_v1):
            url = f"{base}/responses"
            try:
                return await self._post_responses_stream(url=url, payload=payload, headers=headers)
            except Exception as exc:
                errors.append(f"{url} -> {type(exc).__name__}: {exc}")
        tail = " | ".join(errors[-2:]) if errors else "未知错误"
        raise RuntimeError(f"{self.provider} 请求失败：{tail}")

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        style: str | None = None,
    ) -> str | None:
        if not self.enabled:
            raise RuntimeError(f"缺少密钥，请配置 {self.default_env_key}")

        payload = {
            "model": self.image_model,
            "prompt": prompt,
        }
        model_name = str(self.image_model or "").strip().lower()
        if size and "grok-imagine" not in model_name:
            payload["size"] = size
        if style:
            payload["style"] = style
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = await self._post_with_base_candidates(
            endpoint="/images/generations",
            payload=payload,
            headers=headers,
            prefer_v1=self.prefer_v1,
            stream_response=False,
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
        stream_response: bool = False,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for base in self._candidate_base_urls(self.base_url, prefer_v1=prefer_v1):
            url = f"{base}{endpoint}"
            try:
                if stream_response:
                    return await self._post_json_stream(url=url, payload=payload, headers=headers)
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
        client = self._get_client()
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

    async def _post_json_stream(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        client = self._get_client()
        role = "assistant"
        text_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None

        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors="ignore")[:200].strip()
                raise RuntimeError(f"HTTP {response.status_code}: {detail or '请求失败'}")

            async for raw_line in response.aiter_lines():
                line = (raw_line or "").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage

                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                c0 = choices[0] if isinstance(choices[0], dict) else {}
                if not isinstance(c0, dict):
                    continue

                delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
                if isinstance(delta, dict):
                    maybe_role = delta.get("role")
                    if isinstance(maybe_role, str) and maybe_role.strip():
                        role = maybe_role.strip()
                    maybe_content = delta.get("content")
                    if maybe_content is not None:
                        if isinstance(maybe_content, str):
                            text_parts.append(maybe_content)
                        elif isinstance(maybe_content, list):
                            for part in maybe_content:
                                if isinstance(part, dict):
                                    text = part.get("text")
                                    if text is not None:
                                        text_parts.append(str(text))
                                elif part is not None:
                                    text_parts.append(str(part))
                        else:
                            text_parts.append(str(maybe_content))

                msg = c0.get("message")
                if isinstance(msg, dict):
                    maybe_role = msg.get("role")
                    if isinstance(maybe_role, str) and maybe_role.strip():
                        role = maybe_role.strip()
                    maybe_content = msg.get("content")
                    if isinstance(maybe_content, str):
                        text_parts.append(maybe_content)

                maybe_finish = c0.get("finish_reason")
                if maybe_finish is not None:
                    finish_reason = str(maybe_finish)

        content = "".join(text_parts)
        return {
            "id": f"stream-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(payload.get("model", "")),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": role, "content": content},
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage or {},
        }

    async def _post_responses_stream(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Responses API SSE 聚合器。"""
        client = self._get_client()
        text_parts: list[str] = []
        usage: dict[str, Any] | None = None
        response_obj: dict[str, Any] = {}
        response_id = f"resp-stream-{int(time.time() * 1000)}"
        model_name = str(payload.get("model", self.model))

        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors="ignore")[:220].strip()
                raise RuntimeError(f"HTTP {response.status_code}: {detail or '请求失败'}")

            async for raw_line in response.aiter_lines():
                line = (raw_line or "").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                # Responses API 事件
                event_type = str(event.get("type", "")).strip().lower()
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if delta is not None:
                        text_parts.append(str(delta))
                    continue
                if event_type == "response.completed":
                    resp = event.get("response")
                    if isinstance(resp, dict):
                        response_obj = resp
                        maybe_id = str(resp.get("id", "")).strip()
                        if maybe_id:
                            response_id = maybe_id
                        maybe_model = str(resp.get("model", "")).strip()
                        if maybe_model:
                            model_name = maybe_model
                    continue
                if event_type in {"error", "response.error"}:
                    err = event.get("error")
                    if isinstance(err, dict):
                        msg = str(err.get("message", "")).strip()
                        raise RuntimeError(msg or "responses 流式错误事件")
                    raise RuntimeError(str(err or "responses 流式错误事件"))

                # 兼容某些网关返回 chat.completions 风格流式
                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage
                choices = event.get("choices")
                if isinstance(choices, list) and choices:
                    c0 = choices[0] if isinstance(choices[0], dict) else {}
                    delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
                    maybe_content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(maybe_content, str):
                        text_parts.append(maybe_content)
                    elif isinstance(maybe_content, list):
                        for part in maybe_content:
                            if isinstance(part, dict):
                                txt = part.get("text")
                                if txt is not None:
                                    text_parts.append(str(txt))
                            elif part is not None:
                                text_parts.append(str(part))

        merged = "".join(text_parts).strip()
        if not merged and response_obj:
            merged = self._extract_text_from_responses(response_obj)
        if not merged:
            raise RuntimeError("responses 流式返回为空")

        if not response_obj:
            response_obj = {
                "id": response_id,
                "model": model_name,
                "output_text": merged,
            }
        elif not isinstance(response_obj.get("output_text"), str) or not str(response_obj.get("output_text", "")).strip():
            response_obj["output_text"] = merged
        if usage and "usage" not in response_obj:
            response_obj["usage"] = usage
        return response_obj

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
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "on", "y"}

    @staticmethod
    def _normalize_endpoint_type(value: str) -> str:
        raw = (value or "").strip().lower().replace("-", "_")
        aliases = {
            "openairesponse": "openai_response",
            "openai_response": "openai_response",
            "responses": "openai_response",
            "openai": "openai",
            "chat_completions": "openai",
            "openai_image": "openai_image",
        }
        return aliases.get(raw, raw or "openai")

    @staticmethod
    def _messages_to_responses_input(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role", "user")).strip().lower()
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            normalized_role = role if role in {"user", "assistant", "system", "developer"} else "user"
            # Responses API: assistant 历史消息应使用 output_text；其余角色使用 input_text。
            content_type = "output_text" if normalized_role == "assistant" else "input_text"
            inputs.append(
                {
                    "role": normalized_role,
                    "content": [{"type": content_type, "text": content}],
                }
            )
        if not inputs:
            inputs = [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}]
        return inputs

    @staticmethod
    def _extract_text_from_responses(data: dict[str, Any]) -> str:
        direct = data.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        output = data.get("output")
        parts: list[str] = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    txt = block.get("text")
                    if isinstance(txt, str) and txt.strip():
                        parts.append(txt.strip())
        return "\n".join(parts).strip()

    @staticmethod
    def _without_stream(payload: dict[str, Any]) -> dict[str, Any]:
        copied = dict(payload)
        copied.pop("stream", None)
        return copied

    @staticmethod
    def _looks_empty_completion(data: dict[str, Any]) -> bool:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return True
        c0 = choices[0]
        if not isinstance(c0, dict):
            return True
        msg = c0.get("message")
        if not isinstance(msg, dict):
            return True
        content = msg.get("content")
        if isinstance(content, str):
            return not bool(content.strip())
        if isinstance(content, list):
            return len(content) == 0
        return content is None

    @staticmethod
    def _is_responses_fallback_worthy(exc: Exception) -> bool:
        msg = str(exc).lower()
        cues = (
            "404",
            "not found",
            "unsupported",
            "not support",
            "unknown endpoint",
            "responses endpoint",
            "no route",
            "method not allowed",
        )
        return any(cue in msg for cue in cues)
