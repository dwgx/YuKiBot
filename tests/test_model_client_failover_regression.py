from __future__ import annotations

import asyncio
import unittest

from services.model_client import ModelClient


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

    async def generate_image(self, prompt: str, size: str = "1024x1024"):
        return f"https://backup.example/{size}.png"


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


if __name__ == "__main__":
    unittest.main()
