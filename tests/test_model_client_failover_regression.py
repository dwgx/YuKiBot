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


class ModelClientFailoverRegressionTests(unittest.TestCase):
    def test_chat_text_with_retry_uses_failover_path(self) -> None:
        original_clients = dict(ModelClient._CLIENTS)
        try:
            ModelClient._CLIENTS["primary_test_provider"] = _Primary401Client
            ModelClient._CLIENTS["backup_test_provider"] = _BackupOKClient

            cfg = {
                "provider": "primary_test_provider",
                "fallback_providers": ["backup_test_provider"],
                "providers": {
                    "primary_test_provider": {"api_key": "x-primary"},
                    "backup_test_provider": {"api_key": "x-backup"},
                },
            }
            client = ModelClient(cfg)

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


if __name__ == "__main__":
    unittest.main()
