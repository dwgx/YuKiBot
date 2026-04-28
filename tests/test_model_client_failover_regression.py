from __future__ import annotations

import asyncio
import unittest

from services.model_client import ModelClient
from services.openai_compatible import OpenAICompatibleClient


class _Primary401Client:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = True
        self.model = "primary"
        self.base_url = "https://primary.example/v1"

    async def chat_completion(self, messages, response_format=None, max_tokens=None):
        raise RuntimeError("HTTP 401: invalid token")


class _BackupOKClient:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = True
        self.model = "backup"
        self.base_url = "https://backup.example/v1"

    async def chat_completion(self, messages, response_format=None, max_tokens=None):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "fallback-ok",
                    }
                }
            ]
        }

    async def chat_json(self, messages):
        return {"ok": True, "source": "backup"}

    async def generate_image(self, prompt: str, size: str = "1024x1024", style: str | None = None):
        _ = style
        return f"https://backup.example/{size}.png"


class _PrimaryTimeoutClient:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = True
        self.model = "primary-timeout"
        self.base_url = "https://primary-timeout.example/v1"

    async def chat_completion(self, messages, response_format=None, max_tokens=None):
        raise TimeoutError("request timed out while waiting for upstream")


class _OpenAIModelFallbackClient(OpenAICompatibleClient):
    def __init__(self) -> None:
        super().__init__(
            config={
                "api_key": "x",
                "base_url": "https://newapi.example",
                "model": "bad-model",
                "fallback_models": ["good-model"],
                "stream_chat_completions": False,
            },
            provider="newapi",
            default_base_url="https://newapi.example/v1",
            default_env_key="NEWAPI_API_KEY",
            prefer_v1=True,
        )
        self.models_seen: list[str] = []

    async def _post_with_base_candidates(
        self,
        endpoint: str,
        payload: dict,
        headers: dict,
        prefer_v1: bool,
        stream_response: bool = False,
    ) -> dict:
        _ = endpoint, headers, prefer_v1, stream_response
        model = str(payload.get("model", ""))
        self.models_seen.append(model)
        if model == "bad-model":
            raise RuntimeError("HTTP 503: All credentials for model bad-model are cooling down")
        return {"choices": [{"message": {"role": "assistant", "content": "model-ok"}}]}


class ModelClientFailoverRegressionTests(unittest.TestCase):
    def _build_client(self) -> ModelClient:
        cfg = {
            "provider": "primary_test_provider",
            "fallback_providers": ["backup_test_provider"],
            "providers": {
                "primary_test_provider": {"api_key": "x-primary"},
                "backup_test_provider": {"api_key": "x-backup"},
            },
        }
        return ModelClient(cfg)

    def test_chat_text_with_retry_uses_failover_path(self) -> None:
        original_clients = dict(ModelClient._CLIENTS)
        try:
            ModelClient._CLIENTS["primary_test_provider"] = _Primary401Client
            ModelClient._CLIENTS["backup_test_provider"] = _BackupOKClient

            client = self._build_client()

            result = asyncio.run(
                client.chat_text_with_retry(
                    messages=[{"role": "user", "content": "ping"}],
                    retries=0,
                )
            )
            self.assertEqual(result, "fallback-ok")
            self.assertEqual(client._active_provider, "backup_test_provider")
        finally:
            ModelClient._CLIENTS.clear()
            ModelClient._CLIENTS.update(original_clients)

    def test_chat_json_uses_failover_path(self) -> None:
        original_clients = dict(ModelClient._CLIENTS)
        try:
            ModelClient._CLIENTS["primary_test_provider"] = _Primary401Client
            ModelClient._CLIENTS["backup_test_provider"] = _BackupOKClient

            client = self._build_client()

            result = asyncio.run(
                client.chat_json(
                    messages=[{"role": "user", "content": "ping"}],
                )
            )
            self.assertEqual(result, {"ok": True, "source": "backup"})
            self.assertEqual(client._active_provider, "backup_test_provider")
        finally:
            ModelClient._CLIENTS.clear()
            ModelClient._CLIENTS.update(original_clients)

    def test_generate_image_uses_failover_path(self) -> None:
        original_clients = dict(ModelClient._CLIENTS)
        try:
            ModelClient._CLIENTS["primary_test_provider"] = _Primary401Client
            ModelClient._CLIENTS["backup_test_provider"] = _BackupOKClient

            client = self._build_client()

            result = asyncio.run(client.generate_image(prompt="draw cat", size="512x512"))
            self.assertEqual(result, "https://backup.example/512x512.png")
            self.assertEqual(client._active_provider, "backup_test_provider")
        finally:
            ModelClient._CLIENTS.clear()
            ModelClient._CLIENTS.update(original_clients)

    def test_transient_timeout_can_trigger_failover(self) -> None:
        original_clients = dict(ModelClient._CLIENTS)
        try:
            ModelClient._CLIENTS["primary_test_provider"] = _PrimaryTimeoutClient
            ModelClient._CLIENTS["backup_test_provider"] = _BackupOKClient

            client = self._build_client()

            result = asyncio.run(
                client.chat_text_with_retry(
                    messages=[{"role": "user", "content": "ping"}],
                    retries=0,
                )
            )
            self.assertEqual(result, "fallback-ok")
            self.assertEqual(client._active_provider, "backup_test_provider")
        finally:
            ModelClient._CLIENTS.clear()
            ModelClient._CLIENTS.update(original_clients)

    def test_openai_compatible_can_fallback_between_models(self) -> None:
        client = _OpenAIModelFallbackClient()

        result = asyncio.run(
            client.chat_text(messages=[{"role": "user", "content": "ping"}])
        )

        self.assertEqual(result, "model-ok")
        self.assertEqual(client.models_seen, ["bad-model", "good-model"])


if __name__ == "__main__":
    unittest.main()
