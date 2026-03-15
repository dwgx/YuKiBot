from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.search import SearchEngine, SearchResult
from core.tools import ToolExecutor


class _DummyImageEngine:
    model_client = None

    async def generate(self, prompt: str, size: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(ok=False, url="", message="disabled")


async def _dummy_plugin_runner(_name: str, _tool_name: str, _args: dict) -> str:
    return ""


class _RecordingSearchEngine:
    def __init__(self) -> None:
        self.max_results = 6
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        if "官方 文档" in query:
            return [
                SearchResult(
                    title="Responses Overview | OpenAI API Reference",
                    snippet="Official API reference for the Responses API.",
                    url="https://developers.openai.com/api/reference/responses/overview",
                )
            ]
        return [
            SearchResult(
                title="OpenAI 讨论 - 知乎",
                snippet="社区讨论帖",
                url="https://www.zhihu.com/question/123",
            )
        ]


class SearchCliAlignmentRegressionTests(unittest.TestCase):
    def test_search_engine_prefers_duckduckgo_before_scraper_fill(self) -> None:
        engine = SearchEngine({"enable": True, "max_results": 1, "timeout_seconds": 8})

        async def _ddg(_query: str) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Responses Overview | OpenAI API Reference",
                    snippet="official docs",
                    url="https://developers.openai.com/api/reference/responses/overview",
                )
            ]

        async def _scraper(_query: str) -> list[SearchResult]:
            return [
                SearchResult(
                    title="OpenAI 讨论 - 知乎",
                    snippet="community",
                    url="https://www.zhihu.com/question/123",
                )
            ]

        async def _empty(_query: str) -> list[SearchResult]:
            return []

        engine._search_duckduckgo_html = _ddg  # type: ignore[method-assign]
        engine._search_instant_api = _empty  # type: ignore[method-assign]
        engine._search_bing_scrape = _scraper  # type: ignore[method-assign]
        engine._search_baidu_scrape = _scraper  # type: ignore[method-assign]
        engine._search_google_scrape = _scraper  # type: ignore[method-assign]

        rows = asyncio.run(engine.search("openai responses api docs"))
        self.assertEqual(len(rows), 1)
        self.assertIn("developers.openai.com", rows[0].url)

    def test_tool_executor_search_restores_query_type_variants(self) -> None:
        search_engine = _RecordingSearchEngine()
        tool = ToolExecutor(
            search_engine=search_engine,  # type: ignore[arg-type]
            image_engine=_DummyImageEngine(),  # type: ignore[arg-type]
            plugin_runner=_dummy_plugin_runner,
            config={"search": {"enable": True, "video_resolver": {"enable": False}}, "music": {"enable": False}},
        )

        result = asyncio.run(
            tool.execute(
                action="search",
                tool_name="search",
                tool_args={"query": "OpenAI Responses API"},
                message_text="OpenAI Responses API",
                conversation_id="local:test",
                user_id="1",
                user_name="tester",
                group_id=0,
                api_call=None,
                raw_segments=[],
            )
        )

        self.assertTrue(result.ok)
        self.assertTrue(any("官方 文档" in query for query in search_engine.queries))
        rows = (result.payload or {}).get("results") or []
        self.assertTrue(rows)
        self.assertIn("developers.openai.com", str(rows[0].get("url", "")))


if __name__ == "__main__":
    unittest.main()
