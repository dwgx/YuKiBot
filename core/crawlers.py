"""专用爬虫模块 — 知乎/百度百科/维基百科/热梗追踪。

提供结构化数据抓取，供 Agent 工具和知识库使用。
所有方法均为 async，使用 httpx 复用连接池。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.crawlers")

# ── UA 池 ──
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return normalize_text(_HTML_TAG_RE.sub("", html.unescape(text)))


def _rand_ua() -> str:
    return random.choice(_UA_POOL)


# ── 数据结构 ──

@dataclass(slots=True)
class CrawlResult:
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""  # zhihu / baike / wikipedia / weibo / douyin / bilibili / baidu
    heat: str = ""  # 热度值 (热搜用)
    extra: dict[str, Any] = field(default_factory=dict)


# ── 知乎爬虫 ──

class ZhihuCrawler:
    """知乎热榜 + 问答 + 搜索。"""

    _HOT_LIST_URL = "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50"
    _HOT_LIST_FALLBACK_URL = "https://www.zhihu.com/api/v4/creators/rank/hot?domain=0&period=day"
    _SEARCH_URL = "https://www.zhihu.com/api/v4/search_v3"
    _ANSWER_URL = "https://www.zhihu.com/api/v4/questions/{qid}/answers"

    def __init__(self, timeout: float = 12.0):
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=6.0),
                follow_redirects=True,
                headers={
                    "User-Agent": _rand_ua(),
                    "Referer": "https://www.zhihu.com/",
                    "Accept": "application/json, text/plain, */*",
                },
            )
        return self._client

    async def hot_list(self, limit: int = 20) -> list[CrawlResult]:
        """获取知乎热榜。"""
        client = await self._get_client()
        data: dict[str, Any] = {}
        for _attempt in range(2):
            try:
                resp = await client.get(self._HOT_LIST_URL)
                if resp.status_code != 200:
                    _log.warning("zhihu_hot_list | status=%d | attempt=%d", resp.status_code, _attempt)
                    if _attempt == 0:
                        await asyncio.sleep(1)
                    continue
                data = resp.json()
                break
            except Exception as e:
                _log.warning("zhihu_hot_list_error | attempt=%d | %s", _attempt, e)
                if _attempt == 0:
                    await asyncio.sleep(1)

        results: list[CrawlResult] = []
        for item in (data.get("data") or [])[:limit]:
            target = item.get("target") or {}
            qid = str(target.get("id", ""))
            results.append(CrawlResult(
                title=normalize_text(str(target.get("title", ""))),
                url=f"https://www.zhihu.com/question/{qid}" if qid else "",
                snippet=clip_text(normalize_text(str(target.get("excerpt", ""))), 200),
                source="zhihu",
                heat=normalize_text(str(item.get("detail_text", ""))),
                extra={
                    "answer_count": int(target.get("answer_count", 0) or 0),
                    "follower_count": int(target.get("follower_count", 0) or 0),
                },
            ))
        if results:
            return results[:limit]

        # 备用接口：v4 热门问题榜
        try:
            resp = await client.get(self._HOT_LIST_FALLBACK_URL)
            if resp.status_code == 200:
                fallback_data = resp.json()
                for item in (fallback_data.get("data") or [])[:limit]:
                    question = item.get("question") or {}
                    qurl = normalize_text(str(question.get("url", "")))
                    qtitle = normalize_text(str(question.get("title", "")))
                    if not qtitle:
                        continue
                    if qurl and not qurl.startswith("http"):
                        qurl = f"https://www.zhihu.com{qurl}" if qurl.startswith("/") else ""
                    results.append(CrawlResult(
                        title=qtitle,
                        url=qurl,
                        snippet=clip_text(normalize_text(str(question.get("highlight_title", ""))), 180),
                        source="zhihu",
                        heat=normalize_text(str((item.get("reaction") or {}).get("zans", ""))),
                        extra={
                            "answer_count": int(question.get("answer_count", 0) or 0),
                            "follower_count": int(question.get("follower_count", 0) or 0),
                        },
                    ))
        except Exception as e:
            _log.warning("zhihu_hot_list_fallback_error | %s", e)
        return results

    async def search(self, query: str, limit: int = 8) -> list[CrawlResult]:
        """搜索知乎内容。"""
        client = await self._get_client()
        try:
            resp = await client.get(self._SEARCH_URL, params={
                "q": query, "t": "general", "correction": 1,
                "offset": 0, "limit": limit,
            })
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as e:
            _log.warning("zhihu_search_error | %s", e)
            return []

        results: list[CrawlResult] = []
        for item in (data.get("data") or [])[:limit]:
            obj = item.get("object") or {}
            item_type = str(item.get("type", ""))
            title = normalize_text(str(
                obj.get("title", "") or (obj.get("question") or {}).get("title", "")
            ))
            excerpt = _strip_html(str(obj.get("excerpt", "")))
            url = normalize_text(str(obj.get("url", "")))
            if url and not url.startswith("http"):
                url = f"https://www.zhihu.com{url}" if url.startswith("/") else ""
            results.append(CrawlResult(
                title=title, url=url,
                snippet=clip_text(excerpt, 200),
                source="zhihu",
                extra={"type": item_type},
            ))
        return results

    async def get_top_answers(self, question_id: str, limit: int = 3) -> list[CrawlResult]:
        """获取问题的高赞回答。"""
        client = await self._get_client()
        url = self._ANSWER_URL.format(qid=question_id)
        try:
            resp = await client.get(url, params={
                "include": "content,voteup_count,comment_count",
                "limit": limit, "offset": 0, "sort_by": "default",
            })
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as e:
            _log.warning("zhihu_answers_error | %s", e)
            return []

        results: list[CrawlResult] = []
        for item in (data.get("data") or [])[:limit]:
            content = _strip_html(str(item.get("content", "")))
            author = normalize_text(str((item.get("author") or {}).get("name", "")))
            votes = int(item.get("voteup_count", 0) or 0)
            results.append(CrawlResult(
                title=f"{author} ({votes}赞)",
                url=f"https://www.zhihu.com/question/{question_id}/answer/{item.get('id', '')}",
                snippet=clip_text(content, 500),
                source="zhihu",
                extra={"voteup_count": votes, "author": author},
            ))
        return results

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

# ── 百度百科 / 维基百科 ──

class WikiCrawler:
    """百度百科 + 维基百科知识检索。"""

    def __init__(self, timeout: float = 12.0):
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=6.0),
                follow_redirects=True,
                headers={"User-Agent": _rand_ua(), "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
        return self._client

    async def baidu_baike(self, keyword: str) -> CrawlResult:
        """查询百度百科词条摘要。"""
        client = await self._get_client()
        url = f"https://baike.baidu.com/item/{quote(keyword)}"
        try:
            resp = await client.get(url, headers={"Referer": "https://baike.baidu.com/"})
            if resp.status_code != 200:
                return CrawlResult(title=keyword, source="baike", url=url)
            text = resp.text
        except Exception as e:
            _log.warning("baike_error | keyword=%s | %s", keyword, e)
            return CrawlResult(title=keyword, source="baike", url=url)

        # 提取摘要 — 百度百科的 lemma-summary 或 J-summary
        summary = ""
        # 方法1: meta description
        meta_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', text)
        if meta_match:
            summary = _strip_html(meta_match.group(1))

        # 方法2: lemma-summary div
        if not summary or len(summary) < 30:
            summary_match = re.search(
                r'class="lemma-summary[^"]*"[^>]*>(.*?)</div>',
                text, re.DOTALL,
            )
            if summary_match:
                summary = _strip_html(summary_match.group(1))

        # 方法3: 第一段 para
        if not summary or len(summary) < 30:
            para_match = re.search(r'class="para[^"]*"[^>]*>(.*?)</div>', text, re.DOTALL)
            if para_match:
                summary = _strip_html(para_match.group(1))

        # 提取标题
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", text)
        title = _strip_html(title_match.group(1)) if title_match else keyword

        return CrawlResult(
            title=title, url=url,
            snippet=clip_text(summary, 600),
            source="baike",
        )

    async def wikipedia(self, keyword: str, lang: str = "zh") -> CrawlResult:
        """查询维基百科摘要 (使用 MediaWiki API)。"""
        client = await self._get_client()
        api_url = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            # 先搜索
            resp = await client.get(api_url, params={
                "action": "query", "list": "search",
                "srsearch": keyword, "format": "json",
                "srlimit": 1, "utf8": 1,
            })
            if resp.status_code != 200:
                return CrawlResult(title=keyword, source="wikipedia")
            search_data = resp.json()
            results = (search_data.get("query") or {}).get("search") or []
            if not results:
                return CrawlResult(title=keyword, source="wikipedia", snippet="未找到相关词条")

            page_title = results[0]["title"]

            # 获取摘要
            resp2 = await client.get(api_url, params={
                "action": "query", "prop": "extracts",
                "exintro": True, "explaintext": True,
                "titles": page_title, "format": "json", "utf8": 1,
            })
            if resp2.status_code != 200:
                return CrawlResult(title=page_title, source="wikipedia")
            pages = (resp2.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                extract = normalize_text(str(page.get("extract", "")))
                return CrawlResult(
                    title=normalize_text(str(page.get("title", page_title))),
                    url=f"https://{lang}.wikipedia.org/wiki/{quote(page_title)}",
                    snippet=clip_text(extract, 600),
                    source="wikipedia",
                )
        except Exception as e:
            _log.warning("wikipedia_error | keyword=%s | %s", keyword, e)

        return CrawlResult(title=keyword, source="wikipedia")

    async def lookup(self, keyword: str) -> list[CrawlResult]:
        """同时查百度百科和维基百科，返回两个结果。"""
        baike_task = asyncio.create_task(self.baidu_baike(keyword))
        wiki_task = asyncio.create_task(self.wikipedia(keyword))
        results: list[CrawlResult] = []
        for task in asyncio.as_completed([baike_task, wiki_task]):
            try:
                r = await task
                if r.snippet:
                    results.append(r)
            except Exception:
                pass
        return results

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

# ── 热梗追踪 ──

class TrendTracker:
    """多平台热搜/热榜聚合: 微博、知乎、B站、抖音、百度。"""

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, list[CrawlResult]]] = {}
        self._cache_ttl = 300  # 5分钟缓存

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=6.0),
                follow_redirects=True,
                headers={"User-Agent": _rand_ua()},
            )
        return self._client

    def _get_cached(self, key: str) -> list[CrawlResult] | None:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: list[CrawlResult]) -> None:
        self._cache[key] = (time.time(), data)
        # 清理过期缓存
        if len(self._cache) > 20:
            now = time.time()
            expired = [k for k, (ts, _) in self._cache.items() if now - ts > self._cache_ttl]
            for k in expired:
                self._cache.pop(k, None)

    async def weibo_hot(self, limit: int = 20) -> list[CrawlResult]:
        """微博热搜。"""
        cached = self._get_cached("weibo")
        if cached is not None:
            return cached[:limit]

        client = await self._get_client()
        # containerid 包含预编码的 = 和 &，必须拼进 URL 避免 httpx 二次编码
        url = (
            "https://m.weibo.cn/api/container/getIndex"
            "?containerid=106003type%3D25%26t%3D3%26disable_hot%3D1%26filter_type%3Drealtimehot"
        )
        data: dict[str, Any] = {}
        for _attempt in range(3):
            try:
                resp = await client.get(
                    url,
                    headers={"Referer": "https://m.weibo.cn/", "X-Requested-With": "XMLHttpRequest"},
                )
                if resp.status_code != 200:
                    _log.debug("weibo_hot | status=%d | attempt=%d", resp.status_code, _attempt)
                    if _attempt < 2:
                        await asyncio.sleep(1 + _attempt)
                    continue
                raw_text = resp.content.decode("utf-8", errors="replace").strip()
                if not raw_text or raw_text[0] not in ('{', '['):
                    _log.debug("weibo_hot | non_json_response | len=%d", len(raw_text))
                    if _attempt < 2:
                        await asyncio.sleep(1 + _attempt)
                    continue
                data = json.loads(raw_text)
                break
            except Exception as e:
                _log.warning("weibo_hot_error | attempt=%d | %s", _attempt, e)
                if _attempt < 2:
                    await asyncio.sleep(1 + _attempt)

        results: list[CrawlResult] = []
        for card in (data.get("data") or {}).get("cards") or []:
            for item in card.get("card_group") or []:
                desc = normalize_text(str(item.get("desc", "")))
                if not desc:
                    continue
                results.append(CrawlResult(
                    title=desc,
                    url=normalize_text(str(item.get("scheme", ""))),
                    heat=normalize_text(str(item.get("desc_extr", ""))),
                    source="weibo",
                ))
        if not results:
            # 备用接口：新版微博侧边热搜
            try:
                resp = await client.get(
                    "https://weibo.com/ajax/side/hotSearch",
                    headers={"Referer": "https://weibo.com/", "X-Requested-With": "XMLHttpRequest"},
                )
                if resp.status_code == 200:
                    data2 = resp.json()
                    for item in (data2.get("data") or {}).get("realtime") or []:
                        word = normalize_text(str(item.get("word", "")))
                        if not word:
                            continue
                        scheme = normalize_text(str(item.get("word_scheme", "")))
                        url = (
                            f"https://s.weibo.com/weibo?q={quote(word)}"
                            if not scheme
                            else f"https://s.weibo.com/weibo?q={quote(scheme)}"
                        )
                        results.append(CrawlResult(
                            title=word,
                            url=url,
                            heat=normalize_text(str(item.get("num", ""))),
                            source="weibo",
                        ))
            except Exception as e:
                _log.warning("weibo_hot_fallback_error | %s", e)
        self._set_cache("weibo", results)
        return results[:limit]

    async def bilibili_hot(self, limit: int = 20) -> list[CrawlResult]:
        """B站热门视频。"""
        cached = self._get_cached("bilibili")
        if cached is not None:
            return cached[:limit]

        client = await self._get_client()
        data: dict[str, Any] = {}
        for _attempt in range(2):
            try:
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/popular",
                    params={"ps": 20, "pn": 1},
                    headers={"Referer": "https://www.bilibili.com/"},
                )
                if resp.status_code != 200:
                    _log.debug("bilibili_hot | status=%d | attempt=%d", resp.status_code, _attempt)
                    if _attempt == 0:
                        await asyncio.sleep(1)
                    continue
                data = resp.json()
                if data.get("code") != 0:
                    _log.debug("bilibili_hot | api_code=%s | attempt=%d", data.get("code"), _attempt)
                    if _attempt == 0:
                        await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                _log.warning("bilibili_hot_error | attempt=%d | %s", _attempt, e)
                if _attempt == 0:
                    await asyncio.sleep(1)

        results: list[CrawlResult] = []
        for item in (data.get("data") or {}).get("list") or []:
            stat = item.get("stat") or {}
            results.append(CrawlResult(
                title=normalize_text(str(item.get("title", ""))),
                url=f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                snippet=clip_text(normalize_text(str(item.get("desc", ""))), 120),
                source="bilibili",
                heat=f"{stat.get('view', 0)}播放",
                extra={
                    "author": normalize_text(str((item.get("owner") or {}).get("name", ""))),
                    "view": int(stat.get("view", 0) or 0),
                    "like": int(stat.get("like", 0) or 0),
                },
            ))
        self._set_cache("bilibili", results)
        return results[:limit]

    async def douyin_hot(self, limit: int = 20) -> list[CrawlResult]:
        """抖音热榜（多端点容错）。"""
        cached = self._get_cached("douyin")
        if cached is not None:
            return cached[:limit]

        client = await self._get_client()
        # 主端点: 抖音官方热搜 API
        endpoints = [
            {
                "url": "https://www.douyin.com/aweme/v1/web/hot/search/list/",
                "headers": {
                    "Referer": "https://www.douyin.com/",
                    "Accept": "application/json, text/plain, */*",
                },
                "parser": self._parse_douyin_hot_v1,
            },
            {
                "url": "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
                "headers": {"Referer": "https://www.iesdouyin.com/"},
                "parser": self._parse_douyin_hot_v2,
            },
        ]
        for ep in endpoints:
            try:
                resp = await client.get(ep["url"], headers=ep.get("headers", {}))
                if resp.status_code != 200:
                    _log.debug("douyin_hot | url=%s | status=%d", ep["url"], resp.status_code)
                    continue
                data = resp.json()
                results = ep["parser"](data)
                if results:
                    self._set_cache("douyin", results)
                    return results[:limit]
            except Exception as e:
                _log.debug("douyin_hot_error | url=%s | %s", ep["url"], e)
        _log.warning("douyin_hot | all_endpoints_failed")
        return []

    @staticmethod
    def _parse_douyin_hot_v1(data: dict[str, Any]) -> list[CrawlResult]:
        results: list[CrawlResult] = []
        for item in (data.get("data") or {}).get("word_list") or []:
            word = normalize_text(str(item.get("word", "")))
            if not word:
                continue
            results.append(CrawlResult(
                title=word,
                url=f"https://www.douyin.com/search/{quote(word)}",
                source="douyin",
                heat=str(item.get("hot_value", "")),
            ))
        return results

    @staticmethod
    def _parse_douyin_hot_v2(data: dict[str, Any]) -> list[CrawlResult]:
        results: list[CrawlResult] = []
        for item in data.get("word_list") or []:
            word = normalize_text(str(item.get("word", "")))
            if not word:
                continue
            results.append(CrawlResult(
                title=word,
                url=f"https://www.douyin.com/search/{quote(word)}",
                source="douyin",
                heat=str(item.get("hot_value", "")),
            ))
        return results

    async def baidu_hot(self, limit: int = 20) -> list[CrawlResult]:
        """百度热搜 (通过移动端 API)。"""
        cached = self._get_cached("baidu")
        if cached is not None:
            return cached[:limit]

        client = await self._get_client()
        data: dict[str, Any] = {}
        for _attempt in range(2):
            try:
                resp = await client.get(
                    "https://top.baidu.com/api/board?platform=wise&tab=realtime",
                    headers={"Referer": "https://top.baidu.com/"},
                )
                if resp.status_code != 200:
                    _log.debug("baidu_hot | status=%d | attempt=%d", resp.status_code, _attempt)
                    if _attempt == 0:
                        await asyncio.sleep(1)
                    continue
                data = resp.json()
                if not isinstance(data.get("data"), dict):
                    _log.debug("baidu_hot | invalid_data_structure | attempt=%d", _attempt)
                    if _attempt == 0:
                        await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                _log.warning("baidu_hot_error | attempt=%d | %s", _attempt, e)
                if _attempt == 0:
                    await asyncio.sleep(1)

        results: list[CrawlResult] = []
        for item in (data.get("data") or {}).get("cards") or []:
            for content in item.get("content") or []:
                word = normalize_text(str(content.get("word", "") or content.get("query", "")))
                if not word:
                    continue
                results.append(CrawlResult(
                    title=word,
                    url=normalize_text(str(content.get("url", ""))),
                    snippet=clip_text(normalize_text(str(content.get("desc", ""))), 120),
                    source="baidu",
                    heat=normalize_text(str(content.get("hotScore", ""))),
                ))
        if not results:
            # 备用：解析 top.baidu.com 页面中的 s-data 注释 JSON。
            try:
                resp = await client.get(
                    "https://top.baidu.com/board?tab=realtime",
                    headers={"Referer": "https://top.baidu.com/"},
                )
                if resp.status_code == 200:
                    page = resp.text or ""
                    m = re.search(r"<!--s-data:(\{.*?\})-->", page, re.DOTALL)
                    if m:
                        payload = json.loads(m.group(1))
                        cards = ((payload.get("data") or {}).get("cards") or [])
                        for card in cards:
                            if normalize_text(str(card.get("component", ""))).lower() != "hotlist":
                                continue
                            for content in card.get("content") or []:
                                word = normalize_text(str(content.get("word", "") or content.get("query", "")))
                                if not word:
                                    continue
                                raw_url = normalize_text(str(content.get("rawUrl", "") or content.get("appUrl", "")))
                                results.append(CrawlResult(
                                    title=word,
                                    url=raw_url,
                                    snippet=clip_text(normalize_text(str(content.get("desc", ""))), 120),
                                    source="baidu",
                                    heat=normalize_text(str(content.get("hotScore", ""))),
                                ))
            except Exception as e:
                _log.warning("baidu_hot_fallback_error | %s", e)
        self._set_cache("baidu", results)
        return results[:limit]

    async def all_hot(self, limit_per_platform: int = 10) -> dict[str, list[CrawlResult]]:
        """并行获取所有平台热搜。"""
        tasks = {
            "weibo": self.weibo_hot(limit_per_platform),
            "bilibili": self.bilibili_hot(limit_per_platform),
            "douyin": self.douyin_hot(limit_per_platform),
            "baidu": self.baidu_hot(limit_per_platform),
        }
        results: dict[str, list[CrawlResult]] = {}
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), gathered):
            if isinstance(result, list):
                results[name] = result
            else:
                _log.warning("trend_%s_error | %s", name, result)
                results[name] = []
        return results

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

# ── 统一入口 ──

class CrawlerHub:
    """统一爬虫管理器，供 engine 初始化和 agent 工具使用。"""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        timeout = float(cfg.get("crawler_timeout", 12))
        self.zhihu = ZhihuCrawler(timeout=timeout)
        self.wiki = WikiCrawler(timeout=timeout)
        self.trends = TrendTracker(timeout=timeout)
        self._last_trends: dict[str, list[CrawlResult]] = {}
        self._last_trends_ts: float = 0

    async def get_trends_cached(self, max_age: float = 300) -> dict[str, list[CrawlResult]]:
        """获取热搜 (带缓存)。"""
        if self._last_trends and (time.time() - self._last_trends_ts) < max_age:
            return self._last_trends
        self._last_trends = await self.trends.all_hot(limit_per_platform=15)
        self._last_trends_ts = time.time()
        return self._last_trends

    def format_trends_text(self, trends: dict[str, list[CrawlResult]], limit: int = 5) -> str:
        """格式化热搜为文本摘要。"""
        platform_names = {
            "weibo": "微博热搜", "bilibili": "B站热门",
            "douyin": "抖音热榜", "baidu": "百度热搜",
        }
        parts: list[str] = []
        for key, name in platform_names.items():
            items = trends.get(key, [])[:limit]
            if not items:
                continue
            lines = [f"【{name}】"]
            for i, item in enumerate(items, 1):
                heat_tag = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat_tag}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts) if parts else "暂无热搜数据"

    async def close(self) -> None:
        await asyncio.gather(
            self.zhihu.close(),
            self.wiki.close(),
            self.trends.close(),
            return_exceptions=True,
        )

