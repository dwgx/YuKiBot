from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.webui as webui


class WebuiImageGenRouteRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_engine = webui._engine
        self._orig_root_dir = webui._ROOT_DIR
        self._orig_token = os.environ.get("WEBUI_TOKEN")
        os.environ["WEBUI_TOKEN"] = "test-token"

    def tearDown(self) -> None:
        webui._engine = self._orig_engine
        webui._ROOT_DIR = self._orig_root_dir
        if self._orig_token is None:
            os.environ.pop("WEBUI_TOKEN", None)
        else:
            os.environ["WEBUI_TOKEN"] = self._orig_token

    def _make_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(webui.router)
        return TestClient(app)

    def test_put_image_gen_route_keeps_helper_wiring_after_setup_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            config_dir = root_dir / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yml").write_text("image_gen:\n  enable: true\n  models: []\n", encoding="utf-8")

            webui._ROOT_DIR = root_dir
            webui._engine = SimpleNamespace(
                config_manager=SimpleNamespace(raw={"image_gen": {"enable": True, "models": []}}),
                reload_config=lambda: (True, "ok"),
            )

            with self._make_client() as client:
                auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
                self.assertEqual(auth_res.status_code, 200)

                response = client.put(
                    "/api/webui/image-gen",
                    json={
                        "image_gen": {
                            "provider": "xai",
                            "default_model": "grok-imagine-1.0",
                            "models": [
                                {
                                    "name": "grok-imagine-1.0",
                                    "provider": "xai",
                                    "model": "grok-imagine-1.0",
                                    "api_key": "${XAI_API_KEY}",
                                }
                            ],
                        }
                    },
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("ok"))
            saved = (config_dir / "config.yml").read_text(encoding="utf-8")
            self.assertIn("grok-imagine-1.0", saved)
            self.assertIn("https://api.x.ai/v1", saved)


if __name__ == "__main__":
    unittest.main()
