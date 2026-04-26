from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.testclient import TestClient

from core.webui_auth_routes import build_auth_status_router
from core.webui_route_context import WebUIRouteContext


def _make_context(store_path: Path) -> WebUIRouteContext:
    async def _check_auth(_request: Request) -> None:
        raise HTTPException(401, "auth required")

    async def _check_ws_auth(_ws: WebSocket) -> bool:
        return False

    def _set_auth_cookie(response: Response, _request: Request, token: str) -> None:
        response.set_cookie("yukiko_webui_session", token, httponly=True)

    def _clear_auth_cookie(response: Response) -> None:
        response.delete_cookie("yukiko_webui_session")

    return WebUIRouteContext(
        get_engine=lambda: None,
        get_start_time=lambda: 0.0,
        get_token=lambda: "test-token",
        check_auth=_check_auth,
        check_ws_auth=_check_ws_auth,
        set_auth_cookie=_set_auth_cookie,
        clear_auth_cookie=_clear_auth_cookie,
        count_registered_napcat_tools=lambda: 0,
        collect_napcat_status=lambda _bot_id="": {"ok": True},  # type: ignore[return-value]
        resolve_log_file_path=lambda: store_path.parent / "test.log",
        resolve_auth_attempt_store_path=lambda: store_path,
        read_log_tail=lambda _path, _lines: [],
        split_log_chunks=lambda raw: [raw] if raw else [],
        cookie_capabilities_payload=lambda: {},
        start_bilibili_qr_session=lambda: {"ok": False},  # type: ignore[return-value]
        bilibili_qr_status=lambda _sid: {"ok": False},  # type: ignore[return-value]
        cancel_bilibili_qr_session=lambda _sid: {"ok": True},
        logger=None,
    )


def _make_client(store_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_auth_status_router(_make_context(store_path)))
    return TestClient(app)


class WebuiAuthRateLimitRegressionTests(unittest.TestCase):
    def test_failed_auth_attempts_persist_across_router_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "webui_auth_attempts.json"

            with _make_client(store_path) as client:
                for _ in range(10):
                    response = client.post("/auth", json={"token": "wrong-token"})
                    self.assertEqual(response.status_code, 401)
                limited = client.post("/auth", json={"token": "wrong-token"})
                self.assertEqual(limited.status_code, 429)

            with _make_client(store_path) as client:
                persisted = client.post("/auth", json={"token": "wrong-token"})
                self.assertEqual(persisted.status_code, 429)

    def test_proxy_headers_can_partition_rate_limit_when_explicitly_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "webui_auth_attempts.json"
            old = os.environ.get("WEBUI_TRUST_PROXY_HEADERS")
            os.environ["WEBUI_TRUST_PROXY_HEADERS"] = "1"
            try:
                with _make_client(store_path) as client:
                    for _ in range(10):
                        response = client.post(
                            "/auth",
                            json={"token": "wrong-token"},
                            headers={"X-Forwarded-For": "203.0.113.10"},
                        )
                        self.assertEqual(response.status_code, 401)

                    limited = client.post(
                        "/auth",
                        json={"token": "wrong-token"},
                        headers={"X-Forwarded-For": "203.0.113.10"},
                    )
                    self.assertEqual(limited.status_code, 429)

                    other_ip = client.post(
                        "/auth",
                        json={"token": "wrong-token"},
                        headers={"X-Forwarded-For": "203.0.113.11"},
                    )
                    self.assertEqual(other_ip.status_code, 401)
            finally:
                if old is None:
                    os.environ.pop("WEBUI_TRUST_PROXY_HEADERS", None)
                else:
                    os.environ["WEBUI_TRUST_PROXY_HEADERS"] = old


if __name__ == "__main__":
    unittest.main()
