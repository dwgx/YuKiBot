from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.config_templates import (
    deep_merge_dict as _deep_merge_template,
    ensure_prompts_file as _ensure_prompts_file_from_template,
    load_config_template,
)
from core.image_gen import generate_image_with_model_config
from utils.text import normalize_text


class WebUISetupSupport:
    _SETUP_COOKIE_PLATFORM_DOMAINS: dict[str, list[str]] = {
        "bilibili": [".bilibili.com"],
        "douyin": [".douyin.com"],
        "kuaishou": [".kuaishou.com"],
        "qzone": [".qq.com", ".i.qq.com", ".qzone.qq.com"],
    }
    _SETUP_COOKIE_PLATFORM_SITES: dict[str, str] = {
        "bilibili": "bilibili.com",
        "douyin": "douyin.com",
        "kuaishou": "kuaishou.com",
        "qzone": "qzone.qq.com",
    }
    _SETUP_COOKIE_IMPORTANT_KEYS = {
        "bilibili": ["SESSDATA", "bili_jct"],
        "douyin": ["sessionid", "ttwid"],
        "kuaishou": ["kuaishou.sid", "userId"],
        "qzone": ["p_skey", "p_uin"],
    }
    _SETUP_API_ENV_MAP = {
        "skiapi": "${SKIAPI_KEY}",
        "openai": "${OPENAI_API_KEY}",
        "deepseek": "${DEEPSEEK_API_KEY}",
        "newapi": "${NEWAPI_API_KEY}",
        "anthropic": "${ANTHROPIC_API_KEY}",
        "gemini": "${GEMINI_API_KEY}",
        "openrouter": "${OPENROUTER_API_KEY}",
        "xai": "${XAI_API_KEY}",
        "qwen": "${QWEN_API_KEY}",
        "moonshot": "${MOONSHOT_API_KEY}",
        "mistral": "${MISTRAL_API_KEY}",
        "zhipu": "${ZHIPU_API_KEY}",
        "siliconflow": "${SILICONFLOW_API_KEY}",
    }
    _SETUP_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
        "skiapi": {"model": "claude-opus-4-6", "base_url": "https://skiapi.dev", "endpoint_type": "openai"},
        "openai": {"model": "gpt-5.2", "base_url": "https://api.openai.com", "endpoint_type": "openai_response"},
        "anthropic": {"model": "claude-sonnet-4-5-20250929", "base_url": "https://api.anthropic.com", "endpoint_type": "anthropic"},
        "gemini": {"model": "gemini-2.5-pro", "base_url": "https://generativelanguage.googleapis.com", "endpoint_type": "gemini"},
        "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com", "endpoint_type": "openai"},
        "newapi": {"model": "gpt-5-codex", "base_url": "https://api.openai.com/v1", "endpoint_type": "openai"},
        "openrouter": {"model": "openrouter/auto", "base_url": "https://openrouter.ai/api/v1", "endpoint_type": "openai"},
        "xai": {"model": "grok-4.1-mini", "base_url": "https://api.x.ai/v1", "endpoint_type": "openai"},
        "qwen": {"model": "qwen-max-latest", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "endpoint_type": "openai"},
        "moonshot": {"model": "kimi-thinking-preview", "base_url": "https://api.moonshot.cn/v1", "endpoint_type": "openai"},
        "mistral": {"model": "mistral-medium-latest", "base_url": "https://api.mistral.ai", "endpoint_type": "openai"},
        "zhipu": {"model": "glm-4-plus", "base_url": "https://open.bigmodel.cn/api/paas/v4", "endpoint_type": "openai"},
        "siliconflow": {"model": "Qwen/Qwen2.5-72B-Instruct", "base_url": "https://api.siliconflow.cn/v1", "endpoint_type": "openai"},
    }
    _SETUP_IMAGE_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
        "skiapi": {"model": "gpt-image-1", "base_url": "https://skiapi.dev/v1", "env": "${SKIAPI_KEY}"},
        "openai": {"model": "gpt-image-1", "base_url": "https://api.openai.com/v1", "env": "${OPENAI_API_KEY}"},
        "gemini": {"model": "gemini-2.5-flash-image", "base_url": "https://generativelanguage.googleapis.com", "env": "${GEMINI_API_KEY}"},
        "xai": {"model": "grok-imagine-image", "base_url": "https://api.x.ai/v1", "env": "${XAI_API_KEY}"},
        "newapi": {"model": "gpt-image-1", "base_url": "https://api.openai.com/v1", "env": "${NEWAPI_API_KEY}"},
        "openrouter": {"model": "google/gemini-2.5-flash-image", "base_url": "https://openrouter.ai/api/v1", "env": "${OPENROUTER_API_KEY}"},
        "siliconflow": {"model": "black-forest-labs/FLUX.1-schnell", "base_url": "https://api.siliconflow.cn/v1", "env": "${SILICONFLOW_API_KEY}"},
        "flux": {"model": "black-forest-labs/FLUX.1-schnell", "base_url": "https://api.siliconflow.cn/v1", "env": "${SILICONFLOW_API_KEY}"},
        "sd": {"model": "stable-diffusion-xl", "base_url": "http://127.0.0.1:7860", "env": "${API_KEY}"},
        "custom": {"model": "gpt-image-1", "base_url": "", "env": "${API_KEY}"},
    }
    _SETUP_ENDPOINT_TYPE_OPTIONS = [
        {"value": "openai_response", "label": "OpenAI-Response"},
        {"value": "openai", "label": "OpenAI"},
        {"value": "anthropic", "label": "Anthropic"},
        {"value": "dmxapi", "label": "DMXAPI"},
        {"value": "gemini", "label": "Gemini"},
        {"value": "weiyi_ai", "label": "唯—AI (A)"},
    ]

    def __init__(
        self,
        *,
        root_dir: Path,
        prompts_file: Path,
        logger: Any,
        load_yaml_dict: Callable[[Path], dict[str, Any]],
        restore_masked_sensitive_values: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        is_masked_secret_placeholder: Callable[[Any], bool],
        strip_deprecated_local_paths_config: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._root_dir = root_dir
        self._prompts_file = prompts_file
        self._log = logger
        self._load_yaml_dict = load_yaml_dict
        self._restore_masked_sensitive_values = restore_masked_sensitive_values
        self._is_masked_secret_placeholder = is_masked_secret_placeholder
        self._strip_deprecated_local_paths_config = strip_deprecated_local_paths_config
        self._setup_uvicorn_server: Any | None = None
        self._setup_bili_qr_sessions: dict[str, dict[str, Any]] = {}
        self._setup_bili_qr_ttl_seconds = 150
        self._smart_extract_result: dict[str, Any] | None = None
        self._smart_extract_meta: dict[str, Any] | None = None
        self._smart_extract_status = "idle"
        self._smart_extract_error = ""
        self.router = APIRouter(prefix="/api/webui/setup", tags=["setup"])
        self._register_routes()

    def _register_routes(self) -> None:
        @self.router.get("/health")
        async def setup_health():
            return {"status": "setup_mode"}

        @self.router.get("/status")
        async def setup_status():
            config_file = self._root_dir / "config" / "config.yml"
            return {"setup_done": config_file.exists()}

        @self.router.get("/defaults")
        async def setup_defaults():
            return self.defaults_payload()

        @self.router.get("/cookie-capabilities")
        async def setup_cookie_capabilities():
            return {"ok": True, "data": self.cookie_capabilities_payload()}

        @self.router.post("/bilibili-qr/start")
        async def setup_bilibili_qr_start():
            result = await self.start_bilibili_qr_session()
            if not result.get("ok"):
                return JSONResponse(result, status_code=503)
            return result

        @self.router.get("/bilibili-qr/status")
        async def setup_bilibili_qr_status(session_id: str = Query("")):
            sid = normalize_text(session_id)
            if not sid:
                return JSONResponse({"ok": False, "status": "error", "message": "缺少 session_id"}, status_code=400)
            result = await self.bilibili_qr_status(sid)
            if not result.get("ok") and str(result.get("status", "") or "") in {"expired", "error"}:
                return JSONResponse(result, status_code=410 if result.get("status") == "expired" else 400)
            return result

        @self.router.post("/bilibili-qr/cancel")
        async def setup_bilibili_qr_cancel(request: Request):
            body = await request.json()
            sid = normalize_text(str(body.get("session_id", "")))
            if not sid:
                return JSONResponse({"ok": False, "message": "缺少 session_id"}, status_code=400)
            return self.cancel_bilibili_qr_session(sid)

        @self.router.post("/test-api")
        async def setup_test_api(request: Request):
            return await self.test_api(request)

        @self.router.post("/test-image-gen")
        async def setup_test_image_gen(request: Request):
            return await self.test_image_gen(request)

        @self.router.post("/extract-cookie")
        async def setup_extract_cookie(request: Request):
            return await self.extract_cookie(request)

        @self.router.post("/smart-extract")
        async def setup_smart_extract(request: Request):
            return await self.smart_extract(request)

        @self.router.get("/smart-extract-result")
        async def setup_smart_extract_result():
            return await self.smart_extract_result()

        @self.router.post("/save")
        async def setup_save(request: Request):
            return await self.save(request)

    def defaults_payload(self) -> dict[str, Any]:
        providers = []
        for key in [
            "skiapi", "openai", "anthropic", "gemini", "deepseek", "newapi",
            "openrouter", "xai", "qwen", "moonshot", "mistral", "zhipu", "siliconflow",
        ]:
            default_item = self._SETUP_PROVIDER_DEFAULTS.get(key, {})
            label = {
                "skiapi": "SKIAPI",
                "openai": "OpenAI",
                "anthropic": "Anthropic",
                "gemini": "Gemini",
                "deepseek": "DeepSeek",
                "newapi": "NEWAPI",
                "openrouter": "OpenRouter",
                "xai": "xAI (Grok)",
                "qwen": "Qwen",
                "moonshot": "Moonshot (Kimi)",
                "mistral": "Mistral",
                "zhipu": "Zhipu",
                "siliconflow": "SiliconFlow",
            }.get(key, key)
            providers.append(
                {
                    "value": key,
                    "label": label,
                    "default_model": default_item.get("model", ""),
                    "default_base_url": default_item.get("base_url", ""),
                    "default_endpoint_type": default_item.get("endpoint_type", "openai"),
                }
            )
        return {
            "providers": providers,
            "endpoint_types": self._SETUP_ENDPOINT_TYPE_OPTIONS,
            "verbosity_options": [
                {"value": "verbose", "label": "详细"},
                {"value": "medium", "label": "中等"},
                {"value": "brief", "label": "偏短"},
                {"value": "minimal", "label": "极简"},
            ],
        }

    def _cleanup_bilibili_qr_sessions(self) -> None:
        now = time.time()
        expired = [
            sid for sid, data in self._setup_bili_qr_sessions.items()
            if now - float(data.get("created_at", 0.0) or 0.0) >= self._setup_bili_qr_ttl_seconds
        ]
        for sid in expired:
            self._setup_bili_qr_sessions.pop(sid, None)

    def cookie_capabilities_payload(self) -> dict[str, Any]:
        from core.cookie_auth import get_cookie_runtime_capabilities

        payload = get_cookie_runtime_capabilities()
        payload["qr_session_ttl_seconds"] = self._setup_bili_qr_ttl_seconds
        return payload

    async def start_bilibili_qr_session(self) -> dict[str, Any]:
        from core.cookie_auth import bilibili_qr_create_session

        self._cleanup_bilibili_qr_sessions()
        session = await bilibili_qr_create_session()
        if not session:
            return {"ok": False, "message": "当前环境未启用 B站扫码依赖，请安装 bilibili-api-python 后重试"}

        session_id = uuid.uuid4().hex
        self._setup_bili_qr_sessions[session_id] = {
            "qr": session.get("qr"),
            "created_at": time.time(),
        }
        return {
            "ok": True,
            "session_id": session_id,
            "qr_url": str(session.get("qr_url", "") or ""),
            "qr_image_data_uri": str(session.get("qr_image_data_uri", "") or ""),
            "qr_terminal": str(session.get("qr_terminal", "") or ""),
            "expires_in_seconds": int(session.get("timeout_seconds", 120) or 120),
            "message": "请使用 B站 App 扫描二维码并在手机确认",
        }

    async def bilibili_qr_status(self, session_id: str) -> dict[str, Any]:
        from core.cookie_auth import bilibili_qr_check_state

        self._cleanup_bilibili_qr_sessions()
        item = self._setup_bili_qr_sessions.get(session_id)
        if not item:
            return {"ok": False, "status": "expired", "message": "二维码会话不存在或已过期，请重新获取"}

        qr = item.get("qr")
        if qr is None:
            self._setup_bili_qr_sessions.pop(session_id, None)
            return {"ok": False, "status": "error", "message": "二维码会话异常，请重新获取"}

        result = await bilibili_qr_check_state(qr)
        status = str(result.get("status", "") or "")
        if status in {"done", "expired", "error"}:
            self._setup_bili_qr_sessions.pop(session_id, None)
        return result

    def cancel_bilibili_qr_session(self, session_id: str) -> dict[str, Any]:
        self._cleanup_bilibili_qr_sessions()
        existed = bool(self._setup_bili_qr_sessions.pop(session_id, None))
        return {"ok": True, "cancelled": existed}

    @staticmethod
    def _setup_candidate_base_urls(base_url: str, prefer_v1: bool = True) -> list[str]:
        base = normalize_text(base_url).rstrip("/")
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
    def _setup_strip_api_version_suffix(base_url: str) -> str:
        base = normalize_text(base_url).rstrip("/")
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    def _setup_normalize_endpoint_type(self, raw: str, provider: str) -> str:
        value = normalize_text(raw).lower().replace("-", "_")
        aliases = {
            "openairesponse": "openai_response",
            "openai_response": "openai_response",
            "responses": "openai_response",
            "openai": "openai",
            "chat_completions": "openai",
            "anthropic": "anthropic",
            "gemini": "gemini",
            "dmxapi": "dmxapi",
            "weiyi": "weiyi_ai",
            "weiyi_ai": "weiyi_ai",
            "jina": "jina",
            "openai_image": "openai_image",
            "image_openai": "openai_image",
        }
        normalized = aliases.get(value, value)
        if normalized:
            return normalized
        return self._SETUP_PROVIDER_DEFAULTS.get(provider, {}).get("endpoint_type", "openai")

    def _setup_resolve_api_key(self, provider: str, raw_api_key: str) -> str:
        key = normalize_text(raw_api_key)
        if key.startswith("${") and key.endswith("}"):
            env_name = key[2:-1].strip()
            return normalize_text(os.environ.get(env_name, ""))
        if key:
            return key
        placeholder = self._SETUP_API_ENV_MAP.get(provider, "")
        if placeholder.startswith("${") and placeholder.endswith("}"):
            env_name = placeholder[2:-1].strip()
            return normalize_text(os.environ.get(env_name, ""))
        return ""

    def _setup_resolve_image_gen_api_key(
        self,
        *,
        image_provider: str,
        image_api_key_raw: str,
        primary_provider: str,
        primary_api_key_raw: str,
    ) -> str:
        key = normalize_text(image_api_key_raw)
        if key:
            return key

        primary_key = normalize_text(primary_api_key_raw)
        if image_provider == primary_provider:
            if primary_key:
                return primary_key
            return self._SETUP_API_ENV_MAP.get(primary_provider, "${API_KEY}")
        if image_provider in self._SETUP_API_ENV_MAP:
            return self._SETUP_API_ENV_MAP.get(image_provider, "${API_KEY}")
        return self._SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_provider, {}).get("env", "${API_KEY}")

    def _setup_resolve_image_gen_base_url(self, *, image_provider: str, image_base_url_raw: str, resolved_api_key: str) -> str:
        base = normalize_text(image_base_url_raw).rstrip("/")
        if base:
            return base
        if image_provider == "skiapi" and normalize_text(resolved_api_key).lower().startswith("sk-o"):
            return "https://skiapi.dev/v1"
        provider_default = self._SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_provider, {})
        base = normalize_text(provider_default.get("base_url", "")).rstrip("/")
        if not base:
            base = normalize_text(self._SETUP_PROVIDER_DEFAULTS.get(image_provider, {}).get("base_url", "")).rstrip("/")
        if not base:
            return ""
        if image_provider in {"gemini", "sd"}:
            return base
        if "/openai" in base.lower():
            return base
        if base.endswith("/v1") or base.endswith("/v1beta"):
            return base
        return f"{base}/v1"

    def _normalize_image_gen_models_for_save(
        self,
        incoming_models: Any,
        existing_models: Any,
        default_provider: str = "openai",
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        old_lookup: dict[str, dict[str, Any]] = {}
        if isinstance(existing_models, list):
            for item in existing_models:
                if not isinstance(item, dict):
                    continue
                for key in (
                    normalize_text(str(item.get("name", ""))).lower(),
                    normalize_text(str(item.get("model", ""))).lower(),
                ):
                    if key:
                        old_lookup[key] = item
        if not isinstance(incoming_models, list):
            return normalized
        for raw in incoming_models:
            if not isinstance(raw, dict):
                continue
            item = copy.deepcopy(raw)
            provider = normalize_text(str(item.get("provider", ""))).lower() or default_provider or "openai"
            model_name = normalize_text(str(item.get("model", ""))) or normalize_text(str(item.get("name", "")))
            if not model_name:
                continue
            name = normalize_text(str(item.get("name", ""))) or model_name
            item["provider"] = provider
            item["model"] = model_name
            item["name"] = name
            lookup = old_lookup.get(name.lower()) or old_lookup.get(model_name.lower())
            api_key = str(item.get("api_key", "")).strip()
            if api_key == "***":
                if isinstance(lookup, dict) and lookup.get("api_key"):
                    item["api_key"] = lookup.get("api_key")
                else:
                    item.pop("api_key", None)
            elif not api_key and isinstance(lookup, dict) and lookup.get("api_key"):
                item["api_key"] = lookup.get("api_key")
            resolved_key = normalize_text(str(item.get("api_key", "")))
            api_base = normalize_text(str(item.get("api_base", ""))).rstrip("/")
            if not api_base:
                old_base = normalize_text(str(lookup.get("api_base", ""))).rstrip("/") if isinstance(lookup, dict) else ""
                if old_base:
                    item["api_base"] = old_base
                else:
                    auto_base = self._setup_resolve_image_gen_base_url(
                        image_provider=provider,
                        image_base_url_raw="",
                        resolved_api_key=resolved_key,
                    )
                    if auto_base:
                        item["api_base"] = auto_base
            else:
                item["api_base"] = api_base
            normalized.append(item)
        return normalized

    @staticmethod
    def _ensure_image_gen_default_model(image_cfg: dict[str, Any]) -> tuple[str, bool]:
        if not isinstance(image_cfg, dict):
            return "", False
        models = image_cfg.get("models", [])
        if not isinstance(models, list) or not models:
            return normalize_text(str(image_cfg.get("default_model", ""))), False
        valid_keys: set[str] = set()
        first_model = ""
        for item in models:
            if not isinstance(item, dict):
                continue
            model_name = normalize_text(str(item.get("model", "")))
            display_name = normalize_text(str(item.get("name", "")))
            if model_name:
                if not first_model:
                    first_model = model_name
                valid_keys.add(model_name)
                valid_keys.add(model_name.lower())
            if display_name:
                if not first_model:
                    first_model = display_name
                valid_keys.add(display_name)
                valid_keys.add(display_name.lower())
        current_default = normalize_text(str(image_cfg.get("default_model", "")))
        if not first_model:
            return current_default, False
        if not current_default:
            image_cfg["default_model"] = first_model
            return first_model, True
        if current_default in valid_keys or current_default.lower() in valid_keys:
            return current_default, False
        image_cfg["default_model"] = first_model
        return first_model, True

    @staticmethod
    def _setup_extract_response_text_openai(data: dict[str, Any]) -> str:
        output_text = normalize_text(str(data.get("output_text", "")))
        if output_text:
            return output_text
        output = data.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = normalize_text(str(block.get("text", "") or block.get("output_text", "")))
                    if text:
                        parts.append(text)
            merged = normalize_text("\n".join(parts))
            if merged:
                return merged
        return ""

    def build_config_from_legacy_payload(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = normalize_text(str(body.get("provider", "skiapi"))).lower() or "skiapi"
        defaults = self._SETUP_PROVIDER_DEFAULTS.get(provider, self._SETUP_PROVIDER_DEFAULTS["skiapi"])
        model = normalize_text(str(body.get("model", ""))) or defaults.get("model", "")
        base_url = normalize_text(str(body.get("base_url", "")))
        endpoint_type = self._setup_normalize_endpoint_type(str(body.get("endpoint_type", "")), provider)
        api_key_raw = normalize_text(str(body.get("api_key", "")))
        api_cfg: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "temperature": 0.7,
            "max_tokens": 1200,
            "timeout_seconds": 120,
            "endpoint_type": endpoint_type,
        }
        if base_url:
            api_cfg["base_url"] = base_url
        if api_key_raw:
            api_cfg["api_key"] = api_key_raw
        else:
            api_cfg["api_key"] = self._SETUP_API_ENV_MAP.get(provider, "${API_KEY}")
        bot_name = normalize_text(str(body.get("bot_name", ""))) or "YuKiKo"
        allow_search = bool(body.get("search", True))
        allow_image = bool(body.get("image", True))
        allow_markdown = bool(body.get("markdown", True))
        super_admin_qq = normalize_text(str(body.get("super_admin_qq", "")))
        verbosity = normalize_text(str(body.get("verbosity", "medium"))).lower() or "medium"
        token_saving = bool(body.get("token_saving", False))
        music_enable = bool(body.get("music", True))
        music_api_base = normalize_text(str(body.get("music_api_base", ""))) or "http://mc.alger.fun/api"
        image_gen_enable = bool(body.get("image_gen_enable", True))
        image_gen_provider = normalize_text(str(body.get("image_gen_provider", ""))).lower()
        if not image_gen_provider:
            image_gen_provider = provider if provider in self._SETUP_IMAGE_PROVIDER_DEFAULTS else "openai"
        image_defaults = self._SETUP_IMAGE_PROVIDER_DEFAULTS.get(image_gen_provider, {})
        image_gen_api_key = normalize_text(str(body.get("image_gen_api_key", "")))
        image_gen_base_url = normalize_text(str(body.get("image_gen_base_url", "")))
        image_gen_model = normalize_text(str(body.get("image_gen_model", ""))) or image_defaults.get("model", "dall-e-3")
        image_gen_size = normalize_text(str(body.get("image_gen_size", ""))) or "1024x1024"
        resolved_image_gen_api_key = self._setup_resolve_image_gen_api_key(
            image_provider=image_gen_provider,
            image_api_key_raw=image_gen_api_key,
            primary_provider=provider,
            primary_api_key_raw=api_key_raw,
        )
        resolved_image_gen_base_url = self._setup_resolve_image_gen_base_url(
            image_provider=image_gen_provider,
            image_base_url_raw=image_gen_base_url,
            resolved_api_key=resolved_image_gen_api_key,
        )
        image_gen_models = []
        if image_gen_enable:
            model_config: dict[str, Any] = {
                "name": image_gen_model,
                "provider": image_gen_provider,
                "model": image_gen_model,
                "default_size": image_gen_size,
            }
            if resolved_image_gen_base_url:
                model_config["api_base"] = resolved_image_gen_base_url
            if resolved_image_gen_api_key:
                model_config["api_key"] = resolved_image_gen_api_key
            image_gen_models.append(model_config)
        return {
            "api": api_cfg,
            "bot": {
                "name": bot_name,
                "allow_search": allow_search,
                "allow_image": allow_image,
                "allow_markdown": allow_markdown,
            },
            "admin": {
                "super_admin_qq": super_admin_qq,
                "super_users": [super_admin_qq] if super_admin_qq else [],
            },
            "output": {
                "verbosity": verbosity,
                "token_saving": token_saving,
                "style_instruction": "",
                "group_overrides": {},
                "group_style_overrides": {},
            },
            "music": {"enable": music_enable, "api_base": music_api_base},
            "video_analysis": {
                "bilibili": {"enable": True, "sessdata": normalize_text(str(body.get("bili_sessdata", ""))), "bili_jct": normalize_text(str(body.get("bili_jct", "")))},
                "douyin": {"enable": True, "cookie": normalize_text(str(body.get("douyin_cookie", "")))},
                "kuaishou": {"enable": True, "cookie": normalize_text(str(body.get("kuaishou_cookie", "")))},
                "qzone": {"enable": True, "cookie": normalize_text(str(body.get("qzone_cookie", "")))},
            },
            "image_gen": {
                "enable": image_gen_enable,
                "default_model": image_gen_model,
                "default_size": image_gen_size,
                "nsfw_filter": True,
                "post_review_enable": True,
                "post_review_fail_closed": True,
                "post_review_model": "",
                "post_review_max_tokens": 260,
                "max_prompt_length": 1000,
                "models": image_gen_models,
            },
        }

    async def test_api(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        provider = normalize_text(str(body.get("provider", "skiapi"))).lower() or "skiapi"
        defaults = self._SETUP_PROVIDER_DEFAULTS.get(provider, self._SETUP_PROVIDER_DEFAULTS["skiapi"])
        endpoint_type = self._setup_normalize_endpoint_type(str(body.get("endpoint_type", "")), provider)
        model = normalize_text(str(body.get("model", ""))) or defaults.get("model", "")
        base_url = normalize_text(str(body.get("base_url", ""))) or defaults.get("base_url", "")
        api_key = self._setup_resolve_api_key(provider, str(body.get("api_key", "")))
        try:
            timeout_seconds = max(5.0, min(60.0, float(body.get("timeout_seconds", 18))))
        except Exception:
            timeout_seconds = 18.0
        if not model:
            return {"ok": False, "message": "模型名称不能为空"}
        if not base_url:
            return {"ok": False, "message": "Base URL 不能为空"}
        if not api_key:
            env_hint = self._SETUP_API_ENV_MAP.get(provider, "${API_KEY}")
            return {"ok": False, "message": f"API Key 为空（可设置环境变量 {env_hint}）"}

        async def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds / 2))) as client:
                response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                detail = ""
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        err = data.get("error")
                        if isinstance(err, dict):
                            detail = normalize_text(str(err.get("message", "")))
                        if not detail:
                            detail = normalize_text(str(data.get("message", "")))
                except Exception:
                    detail = ""
                if not detail:
                    detail = normalize_text((response.text or "")[:220])
                raise RuntimeError(f"HTTP {response.status_code}: {detail or '请求失败'}")
            try:
                data = response.json()
            except Exception as exc:
                raise RuntimeError(f"返回非 JSON: {(response.text or '')[:180]}") from exc
            if not isinstance(data, dict):
                raise RuntimeError("返回格式异常：顶层不是对象")
            return response.status_code, data

        async def _post_sse_collect(url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int, str]:
            text_parts: list[str] = []
            status_code = 0
            last_response_obj: dict[str, Any] = {}
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds / 2))) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    status_code = int(response.status_code)
                    if response.status_code >= 400:
                        body_text = normalize_text((await response.aread()).decode(errors="ignore")[:220])
                        raise RuntimeError(f"HTTP {response.status_code}: {body_text or '请求失败'}")
                    async for raw_line in response.aiter_lines():
                        line = normalize_text(raw_line)
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            line = normalize_text(line[5:])
                        if not line:
                            continue
                        if line == "[DONE]":
                            break
                        try:
                            event = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(event, dict):
                            continue
                        event_type = normalize_text(str(event.get("type", ""))).lower()
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if delta is not None:
                                text_parts.append(str(delta))
                            continue
                        if event_type == "response.completed":
                            resp = event.get("response")
                            if isinstance(resp, dict):
                                last_response_obj = resp
                            continue
                        if event_type in {"error", "response.error"}:
                            err = event.get("error")
                            if isinstance(err, dict):
                                msg = normalize_text(str(err.get("message", "")))
                                raise RuntimeError(msg or "流式接口返回错误事件")
                            raise RuntimeError(normalize_text(str(err)) or "流式接口返回错误事件")
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
                                        text_value = part.get("text")
                                        if text_value is not None:
                                            text_parts.append(str(text_value))
                                    elif part is not None:
                                        text_parts.append(str(part))
            merged = normalize_text("".join(text_parts))
            if merged:
                return status_code, merged
            if last_response_obj:
                recovered = self._setup_extract_response_text_openai(last_response_obj)
                if recovered:
                    return status_code, recovered
            raise RuntimeError("stream 成功但未返回文本")

        started = time.perf_counter()
        errors: list[str] = []
        try:
            if endpoint_type in {"openai", "openai_response", "openai_image", "dmxapi", "weiyi_ai", "jina"}:
                if endpoint_type == "dmxapi" and not normalize_text(str(body.get("base_url", ""))):
                    base_url = "https://www.dmxapi.com/v1"
                elif endpoint_type == "weiyi_ai" and not normalize_text(str(body.get("base_url", ""))):
                    base_url = "https://api.vveai.com/v1"
                elif endpoint_type == "jina" and not normalize_text(str(body.get("base_url", ""))):
                    base_url = "https://api.jina.ai/v1"
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                for base in self._setup_candidate_base_urls(base_url, prefer_v1=True):
                    try:
                        if endpoint_type == "openai_response":
                            payload = {"model": model, "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}], "max_output_tokens": 24, "temperature": 0}
                            try:
                                status_code, data = await _post_json(f"{base}/responses", headers, payload)
                                content = self._setup_extract_response_text_openai(data)
                            except Exception as exc:
                                message = normalize_text(str(exc)).lower()
                                if "stream must be set to true" not in message and "stream must be true" not in message:
                                    raise
                                stream_payload = dict(payload)
                                stream_payload["stream"] = True
                                status_code, content = await _post_sse_collect(f"{base}/responses", headers, stream_payload)
                            if not content:
                                raise RuntimeError("responses 成功但未返回文本")
                            return {"ok": True, "message": "连接成功（Responses）", "latency_ms": int((time.perf_counter() - started) * 1000), "status_code": status_code}
                        if endpoint_type == "openai_image":
                            payload = {"model": model, "prompt": "API connectivity check image", "size": "256x256"}
                            status_code, data = await _post_json(f"{base}/images/generations", headers, payload)
                            items = data.get("data")
                            if not isinstance(items, list) or not items:
                                raise RuntimeError("images 接口成功但未返回 data")
                            return {"ok": True, "message": "连接成功（Image Generation）", "latency_ms": int((time.perf_counter() - started) * 1000), "status_code": status_code}
                        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 24, "temperature": 0}
                        status_code, data = await _post_json(f"{base}/chat/completions", headers, payload)
                        choices = data.get("choices")
                        if not isinstance(choices, list) or not choices:
                            raise RuntimeError("chat/completions 成功但无 choices")
                        return {"ok": True, "message": "连接成功（Chat Completions）", "latency_ms": int((time.perf_counter() - started) * 1000), "status_code": status_code}
                    except Exception as exc:
                        errors.append(f"{base} -> {exc}")
            elif endpoint_type == "anthropic":
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
                payload = {"model": model, "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}], "max_tokens": 24, "temperature": 0}
                for base in self._setup_candidate_base_urls(base_url, prefer_v1=True):
                    try:
                        status_code, data = await _post_json(f"{base}/messages", headers, payload)
                        content = data.get("content")
                        if not isinstance(content, list) or not content:
                            raise RuntimeError("Anthropic 成功但无 content")
                        return {"ok": True, "message": "连接成功（Anthropic Messages）", "latency_ms": int((time.perf_counter() - started) * 1000), "status_code": status_code}
                    except Exception as exc:
                        errors.append(f"{base} -> {exc}")
            elif endpoint_type == "gemini":
                base_root = self._setup_strip_api_version_suffix(base_url)
                headers = {"Content-Type": "application/json"}
                model_escaped = model.replace("/", "%2F")
                payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": 24}}
                for version in ("v1beta", "v1"):
                    url = f"{base_root}/{version}/models/{model_escaped}:generateContent?key={api_key}"
                    try:
                        status_code, data = await _post_json(url, headers, payload)
                        candidates = data.get("candidates")
                        if not isinstance(candidates, list) or not candidates:
                            raise RuntimeError("Gemini 成功但无 candidates")
                        return {"ok": True, "message": f"连接成功（Gemini {version}）", "latency_ms": int((time.perf_counter() - started) * 1000), "status_code": status_code}
                    except Exception as exc:
                        errors.append(f"{url} -> {exc}")
            else:
                return {"ok": False, "message": f"不支持的端点类型: {endpoint_type}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        tail = " | ".join(errors[-2:]) if errors else "未知错误"
        return {"ok": False, "message": f"连通性检测失败: {tail}", "latency_ms": int((time.perf_counter() - started) * 1000)}

    async def test_image_gen(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        provider = normalize_text(str(body.get("provider", "openai"))).lower() or "openai"
        provider_defaults = self._SETUP_IMAGE_PROVIDER_DEFAULTS.get(provider, self._SETUP_IMAGE_PROVIDER_DEFAULTS.get("openai", {}))
        model = normalize_text(str(body.get("model", ""))) or provider_defaults.get("model", "gpt-image-1")
        api_key = normalize_text(str(body.get("api_key", "")))
        base_url = normalize_text(str(body.get("base_url", "")))
        size = normalize_text(str(body.get("size", ""))) or "1024x1024"
        if not base_url:
            base_url = self._setup_resolve_image_gen_base_url(
                image_provider=provider,
                image_base_url_raw="",
                resolved_api_key=api_key,
            )
        if not api_key and provider != "sd":
            env_placeholder = provider_defaults.get("env", "") or self._SETUP_API_ENV_MAP.get(provider, "")
            env_var = ""
            if env_placeholder.startswith("${") and env_placeholder.endswith("}"):
                env_var = env_placeholder[2:-1].strip()
            api_key = normalize_text(os.environ.get(env_var, "")) if env_var else ""
            if not api_key:
                return {"ok": False, "message": f"API Key 为空（可设置环境变量 {env_var or 'API_KEY'}）"}
        try:
            result = await generate_image_with_model_config(
                prompt="A cute anime catgirl with pink hair, wearing a maid outfit, smiling happily, high quality, detailed",
                model_cfg={
                    "name": model,
                    "provider": provider,
                    "model": model,
                    "api_base": base_url,
                    "api_key": api_key,
                },
                size=size,
            )
            image_url = normalize_text(result.url) or (f"data:image/png;base64,{result.base64_data}" if result.base64_data else "")
            return {
                "ok": bool(result.ok),
                "message": result.message if not result.ok else "生成成功",
                "image_url": image_url,
                "model_used": result.model_used,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    @staticmethod
    def _join_cookie_pairs(cookies: dict[str, str], important: list[str] | None = None) -> str:
        if not cookies:
            return ""
        parts: list[str] = []
        pinned = set(important or [])
        for key in important or []:
            val = str(cookies.get(key, "") or "")
            if val:
                parts.append(f"{key}={val}")
        for key, val in cookies.items():
            if key in pinned:
                continue
            value = str(val or "")
            if value:
                parts.append(f"{key}={value}")
        return "; ".join(parts)

    def _format_platform_cookie_payload(
        self,
        platform: str,
        raw_by_domain: dict[str, dict[str, str]],
    ) -> dict[str, str] | None:
        if platform == "bilibili":
            bili = raw_by_domain.get(".bilibili.com", {})
            sessdata = str(bili.get("SESSDATA", "") or "")
            if not sessdata:
                return None
            return {"sessdata": sessdata, "bili_jct": str(bili.get("bili_jct", "") or "")}
        if platform == "douyin":
            dy = raw_by_domain.get(".douyin.com", {})
            cookie = self._join_cookie_pairs(dy, self._SETUP_COOKIE_IMPORTANT_KEYS.get("douyin", []))
            return {"cookie": cookie} if cookie else None
        if platform == "kuaishou":
            ks = raw_by_domain.get(".kuaishou.com", {})
            cookie = self._join_cookie_pairs(ks)
            return {"cookie": cookie} if cookie else None
        if platform == "qzone":
            qq = raw_by_domain.get(".qq.com", {})
            qz = raw_by_domain.get(".qzone.qq.com", {})
            iqq = raw_by_domain.get(".i.qq.com", {})
            merged = {**qq, **iqq, **qz}
            if not str(merged.get("p_skey", "") or merged.get("skey", "") or ""):
                return None
            cookie = self._join_cookie_pairs(merged, self._SETUP_COOKIE_IMPORTANT_KEYS.get("qzone", []))
            return {"cookie": cookie} if cookie else None
        return None

    def _not_found_message(
        self,
        *,
        platform: str,
        running: bool,
        allow_close: bool,
        sources: dict[str, str],
    ) -> str:
        site = self._SETUP_COOKIE_PLATFORM_SITES.get(platform, platform)
        source_used = ",".join(sorted({str(v) for v in sources.values() if str(v)})) or "none"
        qzone_hint = ""
        if platform == "qzone":
            qzone_hint = (
                "\n\n[QQ Space Cookie Guide]\n"
                "1. Visit https://qzone.qq.com and login with YOUR QQ account\n"
                "2. After login, it redirects to https://user.qzone.qq.com/yourQQ\n"
                "3. Make sure page fully loaded with your posts/albums visible\n"
                "4. Do NOT just visit others' space, MUST login to YOUR OWN space\n"
                "5. Chrome/Edge v130+ may need admin privileges"
            )
        if running and not allow_close:
            return (
                f"Not found {site} Cookie. Tried no-close extraction (source={source_used}) failed. "
                f"Please confirm browser logged in to {site}; "
                f"if still fails, enable auto-close retry or run as admin."
                f"{qzone_hint}"
            )
        if running and allow_close:
            return (
                f"Not found {site} Cookie. Tried auto-close retry (source={source_used}) failed. "
                f"Please confirm account logged in current browser profile."
                f"{qzone_hint}"
            )
        return (
            f"Not found {site} Cookie (source={source_used}). "
            f"Please login {site} in browser first; "
            f"If Chromium v130+, recommend running as admin."
            f"{qzone_hint}"
        )

    async def extract_cookie(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        platform = str(body.get("platform", ""))
        browser = str(body.get("browser", "edge"))
        allow_close = bool(body.get("allow_close", False))
        if platform not in self._SETUP_COOKIE_PLATFORM_DOMAINS:
            raise HTTPException(400, f"Unknown platform: {platform}")
        loop = asyncio.get_running_loop()
        domains = self._SETUP_COOKIE_PLATFORM_DOMAINS[platform]
        try:
            def _extract_platform():
                from core.cookie_auth import extract_browser_cookies_with_source, is_browser_running

                raw: dict[str, dict[str, str]] = {}
                sources: dict[str, str] = {}
                for domain in domains:
                    cookies, source = extract_browser_cookies_with_source(
                        browser=browser,
                        domain=domain,
                        auto_close=allow_close,
                    )
                    sources[domain] = source
                    if cookies:
                        raw[domain] = cookies
                running = bool(is_browser_running(browser))
                return raw, sources, running

            raw_by_domain, sources, running = await loop.run_in_executor(None, _extract_platform)
            payload = self._format_platform_cookie_payload(platform, raw_by_domain)
            self._log.info(
                "setup_extract_cookie | platform=%s | browser=%s | allow_close=%s | sources=%s | ok=%s",
                platform, browser, allow_close, sources, bool(payload),
            )
            if payload:
                return {"ok": True, "data": payload, "meta": {"browser": browser, "sources": sources, "running": running}}
            return {
                "ok": False,
                "message": self._not_found_message(platform=platform, running=running, allow_close=allow_close, sources=sources),
                "meta": {"browser": browser, "sources": sources, "running": running, "allow_close": allow_close},
            }
        except ImportError as exc:
            return {"ok": False, "message": f"Missing dependency: {exc}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def smart_extract(self, request: Request) -> dict[str, Any]:
        if self._smart_extract_status == "running":
            return {"ok": False, "message": "Extraction in progress, please wait..."}
        body = await request.json()
        browser = str(body.get("browser", "edge"))
        allow_close = bool(body.get("allow_close", False))
        setup_url = str(request.base_url).rstrip("/") + "/webui/setup"
        self._smart_extract_status = "running"
        self._smart_extract_result = None
        self._smart_extract_meta = None
        self._smart_extract_error = ""
        loop = asyncio.get_running_loop()

        async def _do_extract():
            try:
                def _sync_extract():
                    from core.cookie_auth import smart_extract_all_cookies, smart_extract_all_cookies_no_restart

                    mode = "no_restart"
                    restart_attempted = False
                    raw, meta = smart_extract_all_cookies_no_restart(browser=browser, include_meta=True)
                    if allow_close:
                        missing = [d for d in [".bilibili.com", ".douyin.com", ".kuaishou.com", ".qq.com", ".qzone.qq.com"] if d not in raw or not raw[d]]
                        if missing:
                            restart_attempted = True
                            restarted = smart_extract_all_cookies(browser=browser, setup_url=setup_url, domains=missing)
                            if restarted:
                                mode = "restart"
                            for domain, cookies in restarted.items():
                                if cookies and (domain not in raw or len(cookies) > len(raw.get(domain, {}))):
                                    raw[domain] = cookies
                    return raw, meta, mode, restart_attempted

                raw, meta, mode, restart_attempted = await loop.run_in_executor(None, _sync_extract)
                result: dict[str, Any] = {}
                platform_counts: dict[str, int] = {}
                for platform in ["bilibili", "douyin", "kuaishou", "qzone"]:
                    payload = self._format_platform_cookie_payload(platform, raw)
                    if payload:
                        result[platform] = payload
                        platform_counts[platform] = len(payload)
                    else:
                        platform_counts[platform] = 0
                self._smart_extract_result = result
                self._smart_extract_meta = {
                    "browser": browser,
                    "sources": meta.get("sources", {}),
                    "warnings": meta.get("warnings", []),
                    "mode": mode,
                    "restart_attempted": restart_attempted,
                    "platform_counts": platform_counts,
                    "found_platforms": sorted(result.keys()),
                }
                self._smart_extract_status = "done"
            except Exception as exc:
                self._smart_extract_status = "error"
                self._smart_extract_error = str(exc)

        asyncio.create_task(_do_extract())
        return {"ok": True, "status": "running"}

    async def smart_extract_result(self) -> dict[str, Any]:
        if self._smart_extract_status == "idle":
            return {"status": "idle"}
        if self._smart_extract_status == "running":
            return {"status": "running"}
        if self._smart_extract_status == "error":
            return {"status": "error", "message": self._smart_extract_error}
        return {"status": "done", "data": self._smart_extract_result, "meta": self._smart_extract_meta or {}}

    async def save(self, request: Request) -> dict[str, Any]:
        body = await request.json()
        config_data = body.get("config")
        if not isinstance(config_data, dict):
            config_data = self.build_config_from_legacy_payload(body if isinstance(body, dict) else {})
        if not isinstance(config_data, dict):
            raise HTTPException(400, "config 必须是对象")

        current_config_file = self._root_dir / "config" / "config.yml"
        current_config_data = self._load_yaml_dict(current_config_file) if current_config_file.exists() else {}
        if isinstance(current_config_data, dict):
            config_data = self._restore_masked_sensitive_values(config_data, current_config_data)

        try:
            from core.crypto import SecretManager

            sm = SecretManager(self._root_dir / "storage" / ".secret_key")
            api_cfg = config_data.get("api", {})
            if "api_key" in api_cfg:
                api_key = normalize_text(str(api_cfg.get("api_key", "")))
                if not api_key:
                    api_cfg.pop("api_key", None)
                elif self._is_masked_secret_placeholder(api_key):
                    api_cfg.pop("api_key", None)
                elif not (api_key.startswith("${") and api_key.endswith("}")) and not SecretManager.is_encrypted(api_key):
                    api_cfg["api_key"] = sm.encrypt(api_key)

            video_cfg = config_data.get("video_analysis", {})
            if "bilibili" in video_cfg:
                bili = video_cfg["bilibili"]
                if "sessdata" in bili and bili["sessdata"]:
                    bili["sessdata"] = sm.encrypt(str(bili["sessdata"]))
                if "bili_jct" in bili and bili["bili_jct"]:
                    bili["bili_jct"] = sm.encrypt(str(bili["bili_jct"]))
            for platform in ["douyin", "kuaishou", "qzone"]:
                if platform in video_cfg and "cookie" in video_cfg[platform]:
                    cookie = video_cfg[platform]["cookie"]
                    if cookie:
                        video_cfg[platform]["cookie"] = sm.encrypt(str(cookie))

            image_gen_cfg = config_data.get("image_gen", {})
            models = image_gen_cfg.get("models", []) if isinstance(image_gen_cfg, dict) else []
            if isinstance(models, list):
                for model_cfg in models:
                    if not isinstance(model_cfg, dict):
                        continue
                    api_key = normalize_text(str(model_cfg.get("api_key", "")))
                    if not api_key:
                        continue
                    if api_key.startswith("${") and api_key.endswith("}"):
                        continue
                    if SecretManager.is_encrypted(api_key):
                        continue
                    model_cfg["api_key"] = sm.encrypt(api_key)
        except Exception as exc:
            self._log.warning("加密失败，使用明文存储: %s", exc)

        template = load_config_template()
        merged = _deep_merge_template(template, self._strip_deprecated_local_paths_config(config_data))
        config_file = self._root_dir / "config" / "config.yml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# YuKiKo Bot 配置文件\n"
            "# 由 WebUI Setup 自动生成\n"
            "# 修改后发送 /yukibot 或 /yukiko 即可热重载\n\n"
        )
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.safe_dump(merged, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        _ensure_prompts_file_from_template(self._prompts_file)
        self._log.info("Setup 配置已保存")
        if self._setup_uvicorn_server:
            self._setup_uvicorn_server.should_exit = True
        return {"ok": True, "message": "配置已保存，Setup 完成"}

    def _make_spa_app(self, dist_dir: Path, api_router: APIRouter):
        from fastapi import FastAPI
        from starlette.responses import FileResponse, Response
        from starlette.staticfiles import StaticFiles

        app = FastAPI()
        app.include_router(api_router)
        if dist_dir.exists():
            index_file = dist_dir / "index.html"
            assets_dir = dist_dir / "assets"
            if assets_dir.exists():
                app.mount("/webui/assets", StaticFiles(directory=str(assets_dir)), name="assets")

            @app.get("/webui/{path:path}")
            async def spa_handler(path: str):
                if path.lower().startswith("setup"):
                    if index_file.exists():
                        return FileResponse(index_file)
                file_path = dist_dir / path
                if file_path.is_file() and ".." not in path:
                    return FileResponse(file_path)
                if index_file.exists():
                    return FileResponse(index_file)
                return Response("Not found", status_code=404)

            @app.get("/webui")
            async def spa_root():
                if index_file.exists():
                    return FileResponse(index_file)
                return Response("Not found", status_code=404)

            @app.get("/")
            async def root_redirect():
                from starlette.responses import RedirectResponse

                return RedirectResponse(url="/webui/setup", status_code=307)
        return app

    def run_setup_server(self, host: str = "127.0.0.1", port: int = 8081):
        import uvicorn

        webui_dist = self._root_dir / "webui" / "dist"
        app = self._make_spa_app(webui_dist, api_router=self.router)
        print(f"\n  YuKiKo 首次运行配置向导")
        print(f"  请在浏览器打开: http://{host}:{port}/webui/setup\n")

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        self._setup_uvicorn_server = server
        try:
            server.run()
        finally:
            self._setup_uvicorn_server = None
