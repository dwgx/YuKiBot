from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import random
import re
import socket
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

_log = logging.getLogger("yukiko.search")


@dataclass(slots=True)
class SearchResult:
    title: str
    snippet: str
    url: str


@dataclass(slots=True)
class SearchImageResult:
    title: str
    image_url: str
    source_url: str
    thumbnail_url: str = ""


class SearchEngine:
    _CACHE_TTL = 300  # 5 分钟缓存

    # ── UA 池：模拟不同版本的 Edge / Chrome，降低指纹识别风险 ──
    _UA_POOL = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]

    # ── Accept-Encoding 变体 ──
    _ACCEPT_ENCODINGS = [
        "gzip, deflate, br, zstd",
        "gzip, deflate, br",
        "gzip, deflate",
    ]

    def __init__(self, config: dict[str, Any]):
        self.enabled = bool(config.get("enable", True))
        self.max_results = int(config.get("max_results", 8))
        self.max_image_results = max(1, int(config.get("max_image_results", 4)))
        self.endpoint = str(config.get("endpoint", "https://api.duckduckgo.com/")).strip()
        self.html_endpoint = str(config.get("html_endpoint", "https://duckduckgo.com/html/")).strip()
        self.image_endpoint = str(config.get("image_endpoint", "https://duckduckgo.com/i.js")).strip()
        self.timeout_seconds = float(config.get("timeout_seconds", 18))
        self.allow_private_network = bool(config.get("allow_private_network", False))
        self._searxng_base = str(config.get("searxng_base", "")).strip().rstrip("/")
        self.request_headers = {
            "User-Agent": random.choice(self._UA_POOL),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._bili_cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._domain_safety_cache: dict[str, bool] = {}
        self._req_count = 0  # 请求计数，用于 UA 轮换

    def _make_headers(self, *, referer: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
        """生成随机化请求头，每次请求指纹不同。"""
        self._req_count += 1
        # 每 3 次请求轮换 UA
        ua = self._UA_POOL[self._req_count % len(self._UA_POOL)]
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": random.choice([
                "zh-CN,zh;q=0.9,en;q=0.8",
                "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "zh-CN,zh;q=0.9",
            ]),
            "Accept-Encoding": random.choice(self._ACCEPT_ENCODINGS),
            "Cache-Control": random.choice(["max-age=0", "no-cache"]),
            "Sec-Ch-Ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        if extra:
            headers.update(extra)
        return headers

    async def search(self, query: str) -> list[SearchResult]:
        clean_query = query.strip()
        if not self.enabled or not clean_query:
            return []

        # 缓存命中
        cache_key = clean_query.lower()
        cached = self._cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < self._CACHE_TTL:
            return list(cached[1])

        results: list[SearchResult] = []

        # SearXNG 优先（聚合多引擎）
        if self._searxng_base:
            results.extend(await self._search_searxng(clean_query))

        # DuckDuckGo HTML 质量通常更稳，优先于弱质量抓取源。
        if len(results) < self.max_results:
            results.extend(await self._search_duckduckgo_html(clean_query))

        # DuckDuckGo instant API 补充
        if len(results) < self.max_results:
            results.extend(await self._search_instant_api(clean_query))

        # 并发网页爬虫兜底，避免单一引擎异常导致无结果。
        if len(results) < self.max_results:
            async def _safe(fn_name: str) -> list[SearchResult]:
                fn = getattr(self, fn_name, None)
                if not callable(fn):
                    return []
                try:
                    return await fn(clean_query)
                except Exception as exc:
                    _log.debug("search_engine_error | fn=%s | err=%s", fn_name, exc)
                    return []

            parallel_chunks = await asyncio.gather(
                _safe("_search_bing_scrape"),
                _safe("_search_baidu_scrape"),
                _safe("_search_google_scrape"),
            )
            for chunk in parallel_chunks:
                if chunk:
                    results.extend(chunk)
                if len(results) >= self.max_results:
                    break

        results = self._dedupe_results(results)[: self.max_results]
        if results:
            self._cache[cache_key] = (time.monotonic(), list(results))
            self._evict_cache()
            return results

        domain = self._extract_domain(clean_query)
        if not domain:
            return []
        website_result = await self._fetch_website_overview(domain)
        final = [website_result] if website_result else []
        if final:
            self._cache[cache_key] = (time.monotonic(), list(final))
            self._evict_cache()
        return final

    async def search_images(self, query: str, max_results: int | None = None) -> list[SearchImageResult]:
        clean_query = query.strip()
        if not self.enabled or not clean_query:
            return []

        limit = max(1, min(self.max_image_results, int(max_results or self.max_image_results)))
        # SearXNG 图片搜索优先
        if self._searxng_base:
            searxng_items = await self._search_searxng_images(clean_query, limit)
            if searxng_items:
                return searxng_items
        ddg_items = await self._search_duckduckgo_images(clean_query, limit)
        if ddg_items:
            return ddg_items
        return await self._search_bing_images(clean_query, limit)

    @staticmethod
    def format_results(query: str, results: list[SearchResult]) -> str:
        if not results:
            return f'查询词: "{query}"\n暂无可靠搜索结果。'
        lines = [f'查询词: "{query}"', "搜索结果:"]
        for index, item in enumerate(results, start=1):
            lines.append(f"{index}. 标题: {item.title}")
            lines.append(f"   摘要: {item.snippet}")
            lines.append(f"   链接: {item.url}")
        return "\n".join(lines)

    async def _search_searxng(self, query: str) -> list[SearchResult]:
        """通过自部署 SearXNG 实例搜索（聚合 Google/Bing/DuckDuckGo 等多引擎）。"""
        if not self._searxng_base:
            return []
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
            "categories": "general",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers=self.request_headers,
            ) as client:
                resp = await client.get(f"{self._searxng_base}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []
        raw = data.get("results", [])
        if not isinstance(raw, list):
            return []
        results: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("content", "")).strip()
            url = str(item.get("url", "")).strip()
            if not url or not title:
                continue
            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= self.max_results:
                break
        return results

    async def _search_searxng_images(self, query: str, limit: int) -> list[SearchImageResult]:
        """通过 SearXNG 搜索图片。"""
        if not self._searxng_base:
            return []
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
            "categories": "images",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers=self.request_headers,
            ) as client:
                resp = await client.get(f"{self._searxng_base}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []
        raw = data.get("results", [])
        if not isinstance(raw, list):
            return []
        items: list[SearchImageResult] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            image_url = str(item.get("img_src", "") or item.get("url", "")).strip()
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            title = str(item.get("title", "")).strip() or query
            source_url = str(item.get("url", "")).strip()
            thumb = str(item.get("thumbnail_src", "") or item.get("thumbnail", "")).strip()
            items.append(SearchImageResult(
                title=title[:120], image_url=image_url,
                source_url=source_url, thumbnail_url=thumb,
            ))
            if len(items) >= limit:
                break
        return items

    # ═══════════════════════════════════════════════════════════════════
    #  自写爬虫引擎：Bing / 百度 / Google
    #  反检测：UA 轮换 + Sec-CH 指纹 + 随机延迟 + 多层解析 + 自动重试
    # ═══════════════════════════════════════════════════════════════════

    async def _scrape_with_retry(
        self, url: str, params: dict, headers: dict, *, max_retries: int = 2,
    ) -> str:
        """带重试和随机延迟的 HTTP GET，返回 HTML 文本。"""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                # 指数退避 + 随机抖动
                delay = (1.5 ** attempt) + random.uniform(0.3, 1.2)
                _log.debug("scrape retry #%d after %.1fs", attempt, delay)
                import asyncio
                await asyncio.sleep(delay)
                # 重试时换 UA
                headers = dict(headers)
                headers["User-Agent"] = random.choice(self._UA_POOL)
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=headers,
                    http2=True,  # HTTP/2 更像真实浏览器
                ) as client:
                    resp = await client.get(url, params=params)
                    # 429 / 503 触发重试
                    if resp.status_code in (429, 503) and attempt < max_retries:
                        _log.debug("scrape %s returned %d, retrying", url, resp.status_code)
                        continue
                    resp.raise_for_status()
                    return resp.text or ""
            except httpx.HTTPStatusError:
                if attempt < max_retries:
                    continue
                return ""
            except Exception:
                if attempt < max_retries:
                    continue
                return ""
        return ""

    async def _search_bing_scrape(self, query: str) -> list[SearchResult]:
        """Bing 网页搜索爬虫 — 多层解析 + cn.bing.com 中文优化。"""
        headers = self._make_headers(referer="https://cn.bing.com/")
        # cn.bing.com 中文结果更好；ensearch=0 强制中文
        html = await self._scrape_with_retry(
            "https://cn.bing.com/search",
            {"q": query, "ensearch": "0", "count": str(self.max_results + 5)},
            headers,
        )
        if not html:
            # 回退到国际版
            headers = self._make_headers(referer="https://www.bing.com/")
            html = await self._scrape_with_retry(
                "https://www.bing.com/search",
                {"q": query, "ensearch": "0"},
                headers,
            )
        if not html:
            return []

        results: list[SearchResult] = []

        # ── 策略 1：解析 <li class="b_algo"> 标准结果块 ──
        blocks = re.findall(
            r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>',
            html, flags=re.IGNORECASE,
        )
        for block in blocks:
            link_match = re.search(
                r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                block, flags=re.IGNORECASE | re.DOTALL,
            )
            if not link_match:
                continue
            url = unescape(link_match.group(1)).strip()
            title = self._clean_html_text(link_match.group(2))
            if not url or not title:
                continue

            # 摘要：多种 class 模式
            snippet = ""
            for sp in (
                r'<p\s+class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
                r'<div\s+class="[^"]*b_caption[^"]*"[^>]*>[\s\S]*?<p[^>]*>(.*?)</p>',
                r'<p\b[^>]*>(.*?)</p>',
                r'<span\s+class="[^"]*algoSlug[^"]*"[^>]*>(.*?)</span>',
            ):
                m = re.search(sp, block, flags=re.IGNORECASE | re.DOTALL)
                if m:
                    candidate = self._clean_html_text(m.group(1))
                    if len(candidate) > 15:
                        snippet = candidate
                        break
            if not snippet:
                snippet = title

            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= self.max_results:
                return results

        # ── 策略 2：从 JSON-LD 结构化数据中提取 ──
        for ld_match in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
            html, flags=re.IGNORECASE,
        ):
            try:
                ld = json.loads(ld_match.group(1))
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "")).strip()
                    name = str(item.get("name", "")).strip()
                    desc = str(item.get("description", "")).strip()
                    if url and name and not any(r.url == url for r in results):
                        results.append(SearchResult(
                            title=name[:120], snippet=(desc or name)[:240], url=url,
                        ))
                        if len(results) >= self.max_results:
                            return results
            except (json.JSONDecodeError, TypeError):
                continue

        return results

    async def _search_baidu_scrape(self, query: str) -> list[SearchResult]:
        """百度网页搜索爬虫 — data-tools 真实 URL + 多层摘要提取。"""
        # 生成伪 BAIDUID 避免被识别为同一爬虫
        fake_baiduid = hashlib.md5(
            f"{query}{time.monotonic()}{random.random()}".encode()
        ).hexdigest()[:32].upper()

        headers = self._make_headers(
            referer="https://www.baidu.com/",
            extra={"Cookie": f"BAIDUID={fake_baiduid}:FG=1; BIDUPSID={fake_baiduid}"},
        )

        html = await self._scrape_with_retry(
            "https://www.baidu.com/s",
            {
                "wd": query,
                "rn": str(self.max_results + 8),
                "ie": "utf-8",
                "tn": "baidu",  # 标准搜索模板
                "usm": "1",
            },
            headers,
        )
        if not html:
            return []

        results: list[SearchResult] = []

        # ── 策略 1：解析 data-tools JSON 属性（包含真实 URL）──
        # 百度结果块: <div ... data-tools='{"title":"...","url":"真实URL"}' ...>
        for dt_match in re.finditer(
            r"""data-tools='(\{[^']+\})'""",
            html,
        ):
            try:
                tools = json.loads(dt_match.group(1))
                real_url = str(tools.get("url", "")).strip()
                title = self._clean_html_text(str(tools.get("title", "")).strip())
                if real_url and title and not any(r.url == real_url for r in results):
                    # 从同一结果块中提取摘要
                    # 找到包含此 data-tools 的最近 c-container
                    pos = dt_match.start()
                    snippet = self._extract_baidu_snippet_near(html, pos)
                    results.append(SearchResult(
                        title=title[:120],
                        snippet=(snippet or title)[:240],
                        url=real_url,
                    ))
                    if len(results) >= self.max_results:
                        return results
            except (json.JSONDecodeError, TypeError):
                continue

        # ── 策略 2：传统 h3 > a 解析（回退）──
        blocks = re.findall(
            r'<div[^>]+class="[^"]*(?:c-container|result)[^"]*"[^>]*>([\s\S]*?)(?=<div[^>]+class="[^"]*(?:c-container|result)|<div\s+id="page")',
            html, flags=re.IGNORECASE,
        )
        for block in blocks:
            link_match = re.search(
                r'<h3[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                block, flags=re.IGNORECASE | re.DOTALL,
            )
            if not link_match:
                continue
            raw_url = unescape(link_match.group(1)).strip()
            title = self._clean_html_text(link_match.group(2))
            if not title:
                continue
            # 跳过已通过 data-tools 拿到的结果
            if any(r.title == title[:120] for r in results):
                continue

            url = raw_url  # 百度跳转链接，后续异步解析

            snippet = ""
            for pattern in (
                r'class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
                r'class="[^"]*content-right[^"]*"[^>]*>(.*?)</(?:span|div)>',
                r'<span\s+class="[^"]*c-color-text[^"]*"[^>]*>(.*?)</span>',
            ):
                sm = re.search(pattern, block, flags=re.IGNORECASE | re.DOTALL)
                if sm:
                    candidate = self._clean_html_text(sm.group(1))
                    if len(candidate) > 20:
                        snippet = candidate
                        break
            if not snippet:
                snippet = title

            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= self.max_results:
                break

        # ── 异步批量解析百度跳转链接 → 真实 URL ──
        import asyncio
        resolve_tasks = []
        for r in results:
            if "baidu.com/link" in r.url:
                resolve_tasks.append(self._resolve_baidu_redirect(r))
        if resolve_tasks:
            await asyncio.gather(*resolve_tasks, return_exceptions=True)

        return results

    @staticmethod
    def _extract_baidu_snippet_near(html: str, pos: int) -> str:
        """从 data-tools 位置附近提取摘要文本。"""
        # 向后搜索最近的摘要 class
        window = html[pos:pos + 2000]
        for pattern in (
            r'class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
            r'class="[^"]*content-right[^"]*"[^>]*>(.*?)</(?:span|div)>',
            r'<span\s+class="[^"]*c-color-text[^"]*"[^>]*>(.*?)</span>',
        ):
            m = re.search(pattern, window, flags=re.IGNORECASE | re.DOTALL)
            if m:
                text = re.sub(r"<[^>]+>", " ", m.group(1))
                text = unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 15:
                    return text
        return ""

    async def _resolve_baidu_redirect(self, result: SearchResult) -> None:
        """解析百度跳转链接，原地替换 result.url。"""
        if "baidu.com/link" not in result.url:
            return
        try:
            async with httpx.AsyncClient(
                timeout=5.0, follow_redirects=False,
                headers=self._make_headers(),
            ) as client:
                resp = await client.head(result.url)
                location = resp.headers.get("Location", "")
                if location and location.startswith("http"):
                    result.url = location
        except Exception:
            pass

    async def _search_google_scrape(self, query: str) -> list[SearchResult]:
        """Google 网页搜索爬虫 — 多层解析 + 反跟踪 URL 清洗。"""
        headers = self._make_headers(referer="https://www.google.com/")
        html = await self._scrape_with_retry(
            "https://www.google.com/search",
            {
                "q": query,
                "hl": "zh-CN",
                "gl": "cn",
                "num": str(self.max_results + 5),
                "ie": "UTF-8",
                "oe": "UTF-8",
            },
            headers,
        )
        if not html:
            return []

        results: list[SearchResult] = []

        # ── 策略 1：标准 <div class="g"> 结果块 ──
        blocks = re.findall(
            r'<div\s+class="g"[^>]*>([\s\S]*?)</div>\s*(?=<div\s+class="g"|<div\s+id="botstuff")',
            html, flags=re.IGNORECASE,
        )
        if not blocks:
            # 备用：data-hveid 属性标记的结果块
            blocks = re.findall(
                r'<div[^>]+data-hveid[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>',
                html, flags=re.IGNORECASE,
            )

        for block in blocks:
            # 标题 + 链接
            link_match = re.search(
                r'<a[^>]+href="(https?://(?!(?:www\.)?google\.com)[^"]+)"[^>]*>[\s\S]*?<h3[^>]*>(.*?)</h3>',
                block, flags=re.IGNORECASE | re.DOTALL,
            )
            if not link_match:
                link_match = re.search(
                    r'<a[^>]+href="(https?://(?!(?:www\.)?google\.com)[^"]+)"[^>]*>(.*?)</a>',
                    block, flags=re.IGNORECASE | re.DOTALL,
                )
            if not link_match:
                continue

            url = unescape(link_match.group(1)).strip()
            # 清洗 Google 跟踪重定向
            if "/url?" in url:
                parsed = urlparse(url)
                q_param = parse_qs(parsed.query).get("q", [""])[0]
                if q_param:
                    url = q_param
            # 去掉 Google 追踪参数
            if "google.com/url" in url:
                continue

            title = self._clean_html_text(link_match.group(2))
            if not url or not title:
                continue

            # 摘要：Google 经常变 class 名，多模式匹配
            snippet = ""
            for sp in (
                r'<div[^>]+class="[^"]*VwiC3b[^"]*"[^>]*>(.*?)</div>',
                r'<span[^>]+class="[^"]*aCOpRe[^"]*"[^>]*>(.*?)</span>',
                r'<div[^>]+data-sncf[^>]*>(.*?)</div>',
                r'<div[^>]+class="[^"]*IsZvec[^"]*"[^>]*>(.*?)</div>',
                # 通用回退：结果块内第一个较长的文本段
                r'<span[^>]*>([\s\S]{30,}?)</span>',
            ):
                sm = re.search(sp, block, flags=re.IGNORECASE | re.DOTALL)
                if sm:
                    candidate = self._clean_html_text(sm.group(1))
                    if len(candidate) > 20:
                        snippet = candidate
                        break
            if not snippet:
                snippet = title

            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= self.max_results:
                return results

        # ── 策略 2：JSON-LD 结构化数据 ──
        for ld_match in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
            html, flags=re.IGNORECASE,
        ):
            try:
                ld = json.loads(ld_match.group(1))
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_url = str(item.get("url", "")).strip()
                    name = str(item.get("name", "")).strip()
                    desc = str(item.get("description", "")).strip()
                    if item_url and name and not any(r.url == item_url for r in results):
                        results.append(SearchResult(
                            title=name[:120], snippet=(desc or name)[:240], url=item_url,
                        ))
                        if len(results) >= self.max_results:
                            return results
            except (json.JSONDecodeError, TypeError):
                continue

        return results

    async def _search_instant_api(self, query: str) -> list[SearchResult]:
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=self.request_headers) as client:
                response = await client.get(self.endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception:
            return []

        results: list[SearchResult] = []
        abstract = str(data.get("AbstractText", "")).strip()
        if abstract:
            results.append(
                SearchResult(
                    title=str(data.get("Heading", query)).strip() or query,
                    snippet=abstract,
                    url=str(data.get("AbstractURL", "")).strip(),
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

    async def _search_duckduckgo_html(self, query: str) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=self.request_headers,
            ) as client:
                response = await client.get(self.html_endpoint, params={"q": query})
                response.raise_for_status()
                html = response.text or ""
        except Exception:
            return []

        blocks = re.findall(
            r'(<div[^>]+class="result__body"[\s\S]*?</div>\s*</div>)',
            html,
            flags=re.IGNORECASE,
        )
        if not blocks:
            blocks = re.findall(
                r'(<div[^>]+class="result[^"]*"[\s\S]*?</div>\s*</div>)',
                html,
                flags=re.IGNORECASE,
            )

        results: list[SearchResult] = []
        for block in blocks:
            link_match = re.search(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not link_match:
                continue

            raw_url = unescape(link_match.group(1)).strip()
            url = self._decode_duckduckgo_redirect(raw_url)
            title = self._clean_html_text(link_match.group(2))
            if not url or not title:
                continue

            snippet_match = re.search(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = self._clean_html_text(snippet_match.group(1)) if snippet_match else ""
            if not snippet:
                snippet = title

            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= self.max_results:
                break

        return results

    async def _search_duckduckgo_images(self, query: str, limit: int) -> list[SearchImageResult]:
        vqd = await self._fetch_duckduckgo_vqd(query)
        if not vqd:
            return []

        params = {
            "l": "us-en",
            "o": "json",
            "q": query,
            "vqd": vqd,
            "f": ",,,",
            "p": "1",
        }
        headers = {
            **self.request_headers,
            "Referer": f"https://duckduckgo.com/?q={quote_plus(query)}&iax=images&ia=images",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=headers) as client:
                response = await client.get(self.image_endpoint, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception:
            return []

        raw_items = data.get("results") if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            return []

        items: list[SearchImageResult] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            image_url = str(item.get("image", "")).strip()
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            title = self._clean_html_text(str(item.get("title", "")).strip()) or query
            source_url = str(item.get("url", "")).strip()
            thumb = str(item.get("thumbnail", "")).strip()
            items.append(
                SearchImageResult(
                    title=title[:120],
                    image_url=image_url,
                    source_url=source_url,
                    thumbnail_url=thumb,
                )
            )
            if len(items) >= limit:
                break
        return items

    async def _search_bing_images(self, query: str, limit: int) -> list[SearchImageResult]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=self.request_headers,
            ) as client:
                response = await client.get(
                    "https://www.bing.com/images/search",
                    params={"q": query, "form": "HDRSC3"},
                )
                response.raise_for_status()
                html = response.text or ""
        except Exception:
            return []

        matches = re.findall(r'<a[^>]+class="iusc"[^>]+m="([^"]+)"', html)
        if not matches:
            return []

        items: list[SearchImageResult] = []
        seen: set[str] = set()
        for raw in matches:
            try:
                payload = json.loads(unescape(raw))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            image_url = str(payload.get("murl", "")).strip()
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            source_url = str(payload.get("purl", "")).strip()
            thumbnail = str(payload.get("turl", "")).strip()
            title = self._clean_html_text(str(payload.get("t", "")).strip()) or query
            items.append(
                SearchImageResult(
                    title=title[:120],
                    image_url=image_url,
                    source_url=source_url,
                    thumbnail_url=thumbnail,
                )
            )
            if len(items) >= limit:
                break
        return items

    async def _fetch_duckduckgo_vqd(self, query: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=self.request_headers,
            ) as client:
                response = await client.get(
                    "https://duckduckgo.com/",
                    params={"q": query, "iax": "images", "ia": "images"},
                )
                response.raise_for_status()
                html = response.text or ""
        except Exception:
            return ""

        patterns = (
            r"vqd='([^']+)'",
            r'vqd=\"([^\"]+)\"',
            r'"vqd"\s*:\s*"([^"]+)"',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1).strip()
        
        _log.error("search_ddg_vqd_extract_failed | Unable to extract vqd token, DDG anti-bot active or front-end changed.")
        return ""

    @staticmethod
    def _decode_duckduckgo_redirect(url: str) -> str:
        if not url:
            return ""
        candidate = url.strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if "duckduckgo.com/l/?" not in candidate:
            return candidate
        parsed = urlparse(candidate)
        query = parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        return unquote(target) if target else candidate

    @staticmethod
    def _clean_html_text(text: str) -> str:
        value = re.sub(r"<[^>]+>", " ", text or "")
        value = unescape(value)
        return re.sub(r"\s+", " ", value).strip()

    def _evict_cache(self) -> None:
        """清理过期缓存条目，防止内存泄漏。"""
        now = time.monotonic()
        for store in (self._cache, self._bili_cache):
            expired = [k for k, (ts, _) in store.items() if now - ts > self._CACHE_TTL]
            for k in expired:
                del store[k]

    @staticmethod
    def _compact_match_text(text: str) -> str:
        lowered = (text or "").lower()
        cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", lowered)
        return re.sub(r"[\s\-\_·•./|\\,，;；:&()（）\[\]{}]+", "", cleaned)

    @classmethod
    def _query_relevance_tokens(cls, query: str) -> list[str]:
        base = cls._compact_match_text(query)
        if not base:
            return []
        raw = re.split(r"[\s,，;；/|]+", (query or "").lower())
        tokens: list[str] = []
        for part in raw:
            compact = cls._compact_match_text(part)
            if not compact or compact in tokens:
                continue
            tokens.append(compact)
        if not tokens and base:
            tokens.append(base)
        return tokens

    async def search_bilibili_videos(self, query: str, limit: int = 5) -> list[SearchResult]:
        """通过 bilibili-api-python 搜索视频（自动处理 WBI 签名）。"""
        clean_query = query.strip()
        if not clean_query:
            return []
        for kw in ("b站", "bilibili", "哔哩哔哩", "B站"):
            clean_query = clean_query.replace(kw, "")
        clean_query = clean_query.strip() or query.strip()

        # 缓存命中
        cache_key = f"bili:{clean_query.lower()}:{limit}"
        cached = self._bili_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < self._CACHE_TTL:
            return list(cached[1])

        # 优先使用 bilibili-api-python（自带 WBI 签名）
        try:
            from bilibili_api import search as bili_search
            resp = await bili_search.search_by_type(
                keyword=clean_query,
                search_type=bili_search.SearchObjectType.VIDEO,
                page=1,
            )
            result_list = resp.get("result", []) or []
        except Exception:
            # 回退到直接 API 调用
            result_list = await self._search_bilibili_api_fallback(clean_query, limit)

        results: list[SearchResult] = []
        for item in result_list:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid", "")).strip()
            if not bvid:
                continue
            title = self._clean_html_text(str(item.get("title", "")).strip())
            desc = str(item.get("description", "")).strip()[:200]
            duration = str(item.get("duration", "")).strip()
            snippet = f"{desc} [{duration}]" if duration else desc
            url = f"https://www.bilibili.com/video/{bvid}"
            results.append(SearchResult(title=title[:120], snippet=snippet[:240], url=url))
            if len(results) >= limit:
                break

        tokens = self._query_relevance_tokens(clean_query)
        if len(tokens) >= 2 and results:
            filtered: list[SearchResult] = []
            for row in results:
                haystack = self._compact_match_text(f"{row.title} {row.snippet}")
                if not haystack:
                    continue
                hit_count = sum(1 for token in tokens if token in haystack)
                if hit_count >= 2:
                    filtered.append(row)
            results = filtered

        if results:
            self._bili_cache[cache_key] = (time.monotonic(), list(results))
            self._evict_cache()
        return results

    async def _search_bilibili_api_fallback(self, query: str, limit: int) -> list[dict]:
        """直接调用 B站搜索 API 作为回退（可能因缺少 WBI 签名被拒）。"""
        params = {
            "search_type": "video", "keyword": query,
            "page": 1, "pagesize": min(limit, 20), "order": "",
        }
        headers = {
            **self.request_headers,
            "Referer": "https://search.bilibili.com/",
            "Origin": "https://search.bilibili.com",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True, headers=headers,
            ) as client:
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/search/type", params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            result_list = (data.get("data") or {}).get("result") or []
            return result_list if isinstance(result_list, list) else []
        except Exception:
            return []

    async def search_bing_videos(self, query: str, limit: int = 5) -> list[SearchResult]:
        """通过 Bing 视频搜索页面抓取视频平台链接。"""
        clean_query = query.strip()
        if not clean_query:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=self.request_headers,
            ) as client:
                resp = await client.get(
                    "https://www.bing.com/videos/search",
                    params={"q": clean_query, "FORM": "HDRSC3"},
                )
                resp.raise_for_status()
                html = resp.text or ""
        except Exception:
            return []

        results: list[SearchResult] = []
        seen: set[str] = set()
        # Bing 视频搜索结果中的链接
        for match in re.finditer(
            r'<a[^>]+href="(https?://(?:www\.)?bilibili\.com/video/[^"]+)"[^>]*>(.*?)</a>',
            html, flags=re.IGNORECASE | re.DOTALL,
        ):
            url = unescape(match.group(1)).split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            title = self._clean_html_text(match.group(2)) or clean_query
            results.append(SearchResult(title=title[:120], snippet="", url=url))
            if len(results) >= limit:
                return results

        # 也尝试从 JSON-LD 或 data 属性中提取视频链接
        for match in re.finditer(
            r'"(https?://(?:www\.)?(?:bilibili\.com/video/BV\w+|douyin\.com/video/\d+|kuaishou\.com/short-video/\w+))[^"]*"',
            html,
        ):
            url = match.group(1).split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(title=clean_query, snippet="", url=url))
            if len(results) >= limit:
                return results

        return results

    @staticmethod
    def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
        deduped: list[SearchResult] = []
        seen: set[str] = set()
        for item in results:
            key = (item.url or item.title).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _extract_domain(text: str) -> str:
        match = re.search(r"(?:https?://)?((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})", text or "")
        if not match:
            return ""
        return match.group(1).lower()

    @staticmethod
    def _is_public_ip_obj(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    def _is_safe_public_domain(self, domain: str) -> bool:
        host = (domain or "").strip().lower().rstrip(".")
        if not host:
            return False
        if self.allow_private_network:
            return True
        if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".localhost"):
            return False
        if host.endswith((".local", ".internal", ".localdomain", ".home", ".lan", ".arpa")):
            return False
        try:
            ip_obj = ipaddress.ip_address(host)
        except ValueError:
            ip_obj = None
        if ip_obj is not None:
            return self._is_public_ip_obj(ip_obj)

        cached = self._domain_safety_cache.get(host)
        if cached is not None:
            return cached

        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 在 async 上下文中不能同步调用，先放行，由调用方异步校验
            return True

        return self._resolve_and_check_host(host)

    def _resolve_and_check_host(self, host: str) -> bool:
        """同步 DNS 解析并检查是否为公网 IP（仅在非 async 上下文使用）。"""
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except Exception:
            self._domain_safety_cache[host] = False
            return False

        saw_ip = False
        for info in infos:
            sockaddr = info[4] if len(info) >= 5 else None
            if not sockaddr:
                continue
            address = str(sockaddr[0]).split("%", 1)[0]
            if not address:
                continue
            saw_ip = True
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not self._is_public_ip_obj(resolved_ip):
                self._domain_safety_cache[host] = False
                return False

        self._domain_safety_cache[host] = saw_ip
        return saw_ip

    async def _is_safe_public_domain_async(self, domain: str) -> bool:
        """异步版本的域名安全检查，不阻塞事件循环。"""
        host = (domain or "").strip().lower().rstrip(".")
        if not host:
            return False
        if self.allow_private_network:
            return True
        if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".localhost"):
            return False
        if host.endswith((".local", ".internal", ".localdomain", ".home", ".lan", ".arpa")):
            return False
        try:
            ip_obj = ipaddress.ip_address(host)
        except ValueError:
            ip_obj = None
        if ip_obj is not None:
            return self._is_public_ip_obj(ip_obj)

        cached = self._domain_safety_cache.get(host)
        if cached is not None:
            return cached

        import asyncio as _aio
        try:
            loop = _aio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except Exception:
            self._domain_safety_cache[host] = False
            return False

        saw_ip = False
        for info in infos:
            sockaddr = info[4] if len(info) >= 5 else None
            if not sockaddr:
                continue
            address = str(sockaddr[0]).split("%", 1)[0]
            if not address:
                continue
            saw_ip = True
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not self._is_public_ip_obj(resolved_ip):
                self._domain_safety_cache[host] = False
                return False

        self._domain_safety_cache[host] = saw_ip
        return saw_ip

    async def _fetch_website_overview(self, domain: str) -> SearchResult | None:
        if not await self._is_safe_public_domain_async(domain):
            return None
        urls = [f"https://{domain}", f"http://{domain}"]
        for url in urls:
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=self.request_headers,
                ) as client:
                    response = await client.get(url)
                response.raise_for_status()
                html = response.text or ""
                title = self._extract_html_title(html) or domain
                description = self._extract_meta_description(html)
                snippet = description or f"已访问 {domain} 首页，但未提取到简介。"
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
