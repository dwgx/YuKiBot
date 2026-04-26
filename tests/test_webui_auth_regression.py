from __future__ import annotations

import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import core.webui as webui


class WebuiAuthRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_engine = webui._engine
        self._orig_token = os.environ.get("WEBUI_TOKEN")
        os.environ["WEBUI_TOKEN"] = "test-token"
        webui._engine = None

    def tearDown(self) -> None:
        webui._engine = self._orig_engine
        if self._orig_token is None:
            os.environ.pop("WEBUI_TOKEN", None)
        else:
            os.environ["WEBUI_TOKEN"] = self._orig_token

    def _make_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(webui.router)
        return TestClient(app)

    def test_status_requires_auth(self) -> None:
        with self._make_client() as client:
            response = client.get("/api/webui/status")
        self.assertEqual(response.status_code, 401)

    def test_auth_sets_cookie_and_cookie_unlocks_status(self) -> None:
        with self._make_client() as client:
            auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
            self.assertEqual(auth_res.status_code, 200)
            self.assertIn("yukiko_webui_session=", auth_res.headers.get("set-cookie", ""))

            status_res = client.get("/api/webui/status")
            self.assertEqual(status_res.status_code, 503)

    def test_auth_session_requires_auth(self) -> None:
        with self._make_client() as client:
            response = client.get("/api/webui/auth/session")
        self.assertEqual(response.status_code, 401)

    def test_auth_session_accepts_cookie_session(self) -> None:
        with self._make_client() as client:
            auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
            self.assertEqual(auth_res.status_code, 200)

            session_res = client.get("/api/webui/auth/session")
            self.assertEqual(session_res.status_code, 200)
            self.assertEqual(session_res.json(), {"ok": True})

    def test_chat_media_query_token_no_longer_grants_access(self) -> None:
        with self._make_client() as client:
            response = client.get(
                "/api/webui/chat/media/image",
                params={"file": "dummy-file", "token": "test-token"},
            )
        self.assertEqual(response.status_code, 401)

    def test_logs_websocket_rejects_query_token_only(self) -> None:
        with self._make_client() as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/api/webui/logs/stream?token=test-token"):
                    pass

    def test_logs_websocket_accepts_cookie_session(self) -> None:
        with self._make_client() as client:
            auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
            self.assertEqual(auth_res.status_code, 200)

            with client.websocket_connect("/api/webui/logs/stream") as websocket:
                websocket.close()


if __name__ == "__main__":
    unittest.main()
