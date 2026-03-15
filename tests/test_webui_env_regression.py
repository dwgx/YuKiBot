from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.webui as webui


class WebuiEnvRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_env_file = webui._ENV_FILE
        self._orig_env_example_file = webui._ENV_EXAMPLE_FILE
        self._orig_engine = webui._engine

    def tearDown(self) -> None:
        webui._ENV_FILE = self._orig_env_file
        webui._ENV_EXAMPLE_FILE = self._orig_env_example_file
        webui._engine = self._orig_engine

    async def test_apply_env_updates_webui_token_requires_reauth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("WEBUI_TOKEN=oldtoken\nHOST=0.0.0.0\nPORT=8081\n", encoding="utf-8")
            webui._ENV_FILE = env_file
            webui._ENV_EXAMPLE_FILE = Path(tmp) / ".env.example"
            webui._engine = None

            result = await webui._apply_env_updates({"WEBUI_TOKEN": "newtoken"})

            self.assertIn("WEBUI_TOKEN", result["changed_keys"])
            self.assertTrue(result["reauth_required"])
            self.assertFalse(result["restart_required"])
            self.assertTrue(result["reload_ok"])
            text = env_file.read_text(encoding="utf-8")
            self.assertIn("WEBUI_TOKEN=newtoken", text)

    async def test_apply_env_updates_port_change_marks_restart_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("HOST=0.0.0.0\nPORT=8081\n", encoding="utf-8")
            webui._ENV_FILE = env_file
            webui._ENV_EXAMPLE_FILE = Path(tmp) / ".env.example"
            webui._engine = None

            result = await webui._apply_env_updates({"PORT": "9090"})

            self.assertEqual(result["changed_keys"], ["PORT"])
            self.assertTrue(result["restart_required"])
            self.assertFalse(result["reauth_required"])
            text = env_file.read_text(encoding="utf-8")
            self.assertIn("PORT=9090", text)

    async def test_apply_env_updates_secret_placeholder_keeps_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("ONEBOT_ACCESS_TOKEN=abc123\n", encoding="utf-8")
            webui._ENV_FILE = env_file
            webui._ENV_EXAMPLE_FILE = Path(tmp) / ".env.example"
            webui._engine = None

            result = await webui._apply_env_updates({"ONEBOT_ACCESS_TOKEN": "***"})

            self.assertEqual(result["changed_keys"], [])
            text = env_file.read_text(encoding="utf-8")
            self.assertIn("ONEBOT_ACCESS_TOKEN=abc123", text)

    def test_restore_masked_sensitive_values_keeps_existing_api_secret(self) -> None:
        submitted = {"api": {"provider": "skiapi", "api_key": "***"}}
        current = {"api": {"provider": "skiapi", "api_key": "ENC(existing-secret)"}}

        restored = webui._restore_masked_sensitive_values(submitted, current)

        self.assertEqual(restored["api"]["api_key"], "ENC(existing-secret)")

    def test_restore_masked_sensitive_values_supports_wildcard_paths(self) -> None:
        submitted = {
            "image_gen": {
                "models": [
                    {"name": "a", "api_key": "***"},
                    {"name": "b", "api_key": "plain-b"},
                ]
            }
        }
        current = {
            "image_gen": {
                "models": [
                    {"name": "a", "api_key": "ENC(existing-a)"},
                    {"name": "b", "api_key": "ENC(existing-b)"},
                ]
            }
        }

        restored = webui._restore_masked_sensitive_values(submitted, current)

        self.assertEqual(
            restored["image_gen"]["models"][0]["api_key"],
            "ENC(existing-a)",
        )
        self.assertEqual(restored["image_gen"]["models"][1]["api_key"], "plain-b")


if __name__ == "__main__":
    unittest.main()
