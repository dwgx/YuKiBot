from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class SearchResult:
    title: str
    snippet: str
    url: str


class SearchEngine:
    def __init__(self, config: dict[str, Any]):
        self.enabled = bool(config.get("enable", True))
        self.max_results = int(config.get("max_results", 5))
        self.endpoint = str(config.get("endpoint", "https://api.duckduckgo.com/"))
        self.timeout_seconds = float(config.get("timeout_seconds", 15))

    async def search(self, query: str) -> list[SearchResult]:
        if not self.enabled or not query.strip():
            return []

        params = {
            "q": query.strip(),
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            data = response.json()

        results: list[SearchResult] = []
        abstract = str(data.get("AbstractText", "")).strip()
        if abstract:
            results.append(
                SearchResult(
                    title=str(data.get("Heading", query)),
                    snippet=abstract,
                    url=str(data.get("AbstractURL", "")),
                )
            )

        def add_topic(topic: dict[str, Any]) -> None:
            text = str(topic.get("Text", "")).strip()
            if not text:
                return
            url = str(topic.get("FirstURL", "")).strip()
            title = text.split(" - ")[0][:80]
            results.append(SearchResult(title=title, snippet=text, url=url))

        for item in data.get("RelatedTopics", []):
            if isinstance(item, dict) and "Topics" in item:
                for topic in item.get("Topics", []):
                    if isinstance(topic, dict):
                        add_topic(topic)
            elif isinstance(item, dict):
                add_topic(item)
            if len(results) >= self.max_results:
                break

        return results[: self.max_results]

    @staticmethod
    def format_results(query: str, results: list[SearchResult]) -> str:
        if not results:
            return f'查询词="{query}"\n暂无结果。'
        lines = [f'查询词="{query}"', "搜索结果："]
        for i, item in enumerate(results, start=1):
            lines.append(f"{i}. 标题：{item.title}")
            lines.append(f"   摘要：{item.snippet}")
            lines.append(f"   链接：{item.url}")
        return "\n".join(lines)
