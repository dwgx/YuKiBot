from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx


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

    def __init__(self, config: dict[str, Any]):
        self.enabled = bool(config.get("enable", True))
        self.max_results = int(config.get("max_results", 5))
        self.max_image_results = max(1, int(config.get("max_image_results", 3)))
        self.endpoint = str(config.get("endpoint", "https://api.duckduckgo.com/")).strip()
        self.html_endpoint = str(config.get("html_endpoint", "https://duckduckgo.com/html/")).strip()
        self.image_endpoint = str(config.get("image_endpoint", "https://duckduckgo.com/i.js")).strip()
        self.timeout_seconds = float(config.get("timeout_seconds", 15))
        self.allow_private_network = bool(config.get("allow_private_network", False))
        self._searxng_base = str(config.get("searxng_base", "")).strip().rstrip("/")
        self.request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._bili_cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._domain_safety_cache: dict[str, bool] = {}

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

        # SearXNG 优先（聚合多引擎，中文搜索质量最好）
        if self._searxng_base:
            results.extend(await self._search_searxng(clean_query))

        # DuckDuckGo instant API 补充
        if len(results) < self.max_results:
            results.extend(await self._search_instant_api(clean_query))

        # DuckDuckGo HTML 兜底
        if len(results) < self.max_results:
            results.extend(await self._search_duckduckgo_html(clean_query))

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
            return data.get("data", {}).get("result", []) or []
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

    async def _fetch_website_overview(self, domain: str) -> SearchResult | None:
        if not self._is_safe_public_domain(domain):
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
