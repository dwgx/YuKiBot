from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.webui_setup_support import WebUISetupSupport


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


class WebuiSetupAuthRegressionTests(unittest.TestCase):
    def _make_support(self, root_dir: Path) -> WebUISetupSupport:
        prompts_file = root_dir / "config" / "prompts.yml"
        prompts_file.parent.mkdir(parents=True, exist_ok=True)
        prompts_file.write_text("prompts: {}\n", encoding="utf-8")
        return WebUISetupSupport(
            root_dir=root_dir,
            prompts_file=prompts_file,
            logger=logging.getLogger("test.setup"),
            load_yaml_dict=lambda _path: {},
            restore_masked_sensitive_values=lambda new, _old: new,
            is_masked_secret_placeholder=lambda _value: False,
            strip_deprecated_local_paths_config=lambda data: data,
        )

    def _make_dist(self, root_dir: Path) -> Path:
        dist_dir = root_dir / "webui" / "dist"
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<!doctype html><html><body>setup</body></html>", encoding="utf-8")
        (assets_dir / "app.js").write_text("console.log('setup');", encoding="utf-8")
        return dist_dir

    def test_setup_api_requires_setup_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            support = self._make_support(root_dir)
            dist_dir = self._make_dist(root_dir)
            app = support._make_spa_app(dist_dir, support.router)

            with TestClient(app) as client:
                response = client.get("/api/webui/setup/status")

            self.assertEqual(response.status_code, 401)

    def test_setup_page_query_token_sets_cookie_and_unlocks_api_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            support = self._make_support(root_dir)
            dist_dir = self._make_dist(root_dir)
            app = support._make_spa_app(dist_dir, support.router)
            token = support._setup_access_token

            with TestClient(app) as client:
                page = client.get(f"/webui/setup?setup_token={token}")
                self.assertEqual(page.status_code, 200)
                self.assertIn("yukiko_setup_session=", page.headers.get("set-cookie", ""))

                status = client.get("/api/webui/setup/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json(), {"setup_done": False})

                asset = client.get("/webui/assets/app.js")
                self.assertEqual(asset.status_code, 200)
                self.assertIn("console.log", asset.text)

    def test_defaults_payload_no_longer_exposes_skiapi_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            support = self._make_support(root_dir)

            payload = support.defaults_payload()

        providers = [str(item.get("value", "")) for item in payload.get("providers", []) if isinstance(item, dict)]
        self.assertNotIn("skiapi", providers)
        self.assertIn("newapi", providers)

    def test_legacy_setup_payload_defaults_to_newapi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            support = self._make_support(root_dir)

            config = support.build_config_from_legacy_payload({})

        self.assertEqual(config["api"]["provider"], "newapi")
        self.assertEqual(config["api"]["model"], "gpt-5-codex")

    def test_setup_test_api_429_message_explains_gateway_case(self) -> None:
        async def fake_post(_self, url, headers=None, json=None):
            _ = (url, headers, json)
            return _FakeResponse(
                status_code=429,
                json_data={"error": {"message": "quota exceeded"}},
                text='{"error":{"message":"quota exceeded"}}',
            )

        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            support = self._make_support(root_dir)
            dist_dir = self._make_dist(root_dir)
            app = support._make_spa_app(dist_dir, support.router)

            with patch("httpx.AsyncClient.post", new=fake_post):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/webui/setup/test-api",
                        headers={support._SETUP_AUTH_HEADER: support._setup_access_token},
                        json={
                            "provider": "newapi",
                            "endpoint_type": "openai",
                            "model": "gpt-5-codex",
                            "api_key": "sk-demo",
                            "base_url": "https://aixj.vip",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload.get("ok"))
        self.assertIn("HTTP 429", str(payload.get("message", "")))
        self.assertIn("不是本地 Base URL 拼错", str(payload.get("message", "")))


if __name__ == "__main__":
    unittest.main()
