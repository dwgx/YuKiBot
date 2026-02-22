from __future__ import annotations

import re
from html import unescape
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

        # 对“域名类查询”做兜底：若通用搜索无结果，直接抓取官网首页标题与简介
        if not results:
            domain = self._extract_domain(query)
            if domain:
                website_result = await self._fetch_website_overview(domain)
                if website_result:
                    results.append(website_result)

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

    @staticmethod
    def _extract_domain(text: str) -> str:
        match = re.search(r"(?:https?://)?((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})", text or "")
        if not match:
            return ""
        return match.group(1).lower()

    async def _fetch_website_overview(self, domain: str) -> SearchResult | None:
        urls = [f"https://{domain}", f"http://{domain}"]
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    response = await client.get(url)
                response.raise_for_status()
                html = response.text or ""
                title = self._extract_html_title(html) or domain
                description = self._extract_meta_description(html)
                snippet = description or f"已访问 {domain} 首页，未提取到简介。"
                return SearchResult(title=title, snippet=snippet, url=str(response.url))
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_html_title(html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        title = unescape(match.group(1))
        return re.sub(r"\s+", " ", title).strip()[:120]

    @staticmethod
    def _extract_meta_description(html: str) -> str:
        patterns = (
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if match:
                content = unescape(match.group(1))
                return re.sub(r"\s+", " ", content).strip()[:220]
        return ""
