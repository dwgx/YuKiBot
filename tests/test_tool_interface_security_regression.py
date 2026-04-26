from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from core.tools import ToolExecutor


async def _dummy_plugin_runner(_name: str, _tool_name: str, _args: dict) -> str:
    return ""


def _make_response(
    url: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=content,
        request=httpx.Request("GET", url),
    )


class _FakeAsyncClient:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, object]] = []
        self.is_closed = False

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = (exc_type, exc, tb)
        return False

    async def aclose(self) -> None:
        self.is_closed = True

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        target = str(url)
        self.calls.append(
            {
                "url": target,
                "headers": dict(headers or {}),
                "follow_redirects": bool(follow_redirects),
            }
        )
        response = self._responses.get(target)
        if response is None:
            raise AssertionError(f"unexpected url: {target}")
        return response


class _DummyExecutor(ToolExecutor):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(None, None, _dummy_plugin_runner, config or {})


class ToolInterfaceSecurityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_fetch_url_blocks_redirect_to_private_network(self) -> None:
        executor = _DummyExecutor()
        executor._is_safe_public_http_url = lambda url: not str(url).startswith("http://127.0.0.1")  # type: ignore[method-assign]
        executor._is_safe_public_http_url_async = AsyncMock(
            side_effect=lambda url: not str(url).startswith("http://127.0.0.1")
        )

        fake_client = _FakeAsyncClient(
            {
                "https://public.example/start": _make_response(
                    "https://public.example/start",
                    302,
                    headers={"location": "http://127.0.0.1:8080/secret"},
                )
            }
        )

        with patch("core.tools.httpx.AsyncClient", return_value=fake_client):
            result = await executor._method_browser_fetch_url(
                "browser.fetch_url",
                {"url": "https://public.example/start"},
                "https://public.example/start",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "fetch_failed")
        self.assertEqual(
            [str(item["url"]) for item in fake_client.calls],
            ["https://public.example/start"],
        )

    async def test_sendable_image_probe_blocks_redirect_to_private_network(self) -> None:
        executor = _DummyExecutor()
        executor._is_safe_public_http_url = lambda url: not str(url).startswith("http://127.0.0.1")  # type: ignore[method-assign]
        executor._is_safe_public_http_url_async = AsyncMock(
            side_effect=lambda url: not str(url).startswith("http://127.0.0.1")
        )

        fake_client = _FakeAsyncClient(
            {
                "https://public.example/image": _make_response(
                    "https://public.example/image",
                    302,
                    headers={"location": "http://127.0.0.1:9000/internal.png"},
                )
            }
        )
        executor._shared_http_client = fake_client  # type: ignore[assignment]

        ok = await executor._is_sendable_image_url("https://public.example/image")

        self.assertFalse(ok)
        self.assertEqual(
            [str(item["url"]) for item in fake_client.calls],
            ["https://public.example/image"],
        )

    async def test_web_fetch_does_not_forward_platform_cookie_to_redirect_target(self) -> None:
        executor = _DummyExecutor(
            {
                "video_analysis": {
                    "bilibili": {"cookie": "SESSDATA=test-cookie; bili_jct=test"}
                }
            }
        )
        executor._is_safe_public_http_url = lambda _url: True  # type: ignore[method-assign]
        executor._is_safe_public_http_url_async = AsyncMock(return_value=True)
        executor._is_low_signal_web_summary = lambda **_kwargs: False  # type: ignore[method-assign]

        fake_client = _FakeAsyncClient(
            {
                "https://www.bilibili.com/video/BV1xx411c7mD": _make_response(
                    "https://www.bilibili.com/video/BV1xx411c7mD",
                    302,
                    headers={"location": "https://evil.example/post"},
                ),
                "https://evil.example/post": _make_response(
                    "https://evil.example/post",
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=(
                        b"<html><head><title>Test Article</title></head>"
                        b"<body><p>Useful redirected content for summary.</p></body></html>"
                    ),
                ),
            }
        )

        with patch("core.tools.httpx.AsyncClient", return_value=fake_client):
            page = await executor._fetch_webpage_summary(
                "https://www.bilibili.com/video/BV1xx411c7mD"
            )

        self.assertIsNotNone(page)
        self.assertIn("Cookie", fake_client.calls[0]["headers"])
        self.assertNotIn("Cookie", fake_client.calls[1]["headers"])


if __name__ == "__main__":
    unittest.main()
