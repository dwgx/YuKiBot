"""ToolExecutor 视频处理 mixin — 视频搜索、下载、解析、字幕提取等。

从 core/tools.py 拆分。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess

from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

from utils.text import clip_text, normalize_text
from utils.intent import looks_like_video_request as _shared_video_request
from core.tools_types import ToolResult
from core.tools_types import _unwrap_redirect_url, _normalize_multimodal_query, _is_known_image_signature
import logging as _logging
from core.tools_types import _SilentYTDLPLogger, _prompt_cues, _tool_trace_tag, _write_netscape_cookie_file
from core.video_analyzer import VideoAnalysisResult, VideoAnalyzer
from core.search import SearchResult

try:
    from yt_dlp import YoutubeDL
except Exception:  # pragma: no cover
    YoutubeDL = None

_tool_log = _logging.getLogger("yukiko.tools")
_ytdlp_log = _logging.getLogger("yukiko.ytdlp")


class ToolVideoMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    # ── 抖音视频下载（通过移动端分享页提取 video_id）──────────────
    _DOUYIN_MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    )

    # 多 CDN 主机回退列表（aweme.snssdk.com 失败时依次尝试）
    _DOUYIN_CDN_HOSTS = [
        "aweme.snssdk.com",
        "v26-web.douyinvod.com",
        "v3-web.douyinvod.com",
        "v9-web.douyinvod.com",
        "v5-web.douyinvod.com",
        "v11-web.douyinvod.com",
    ]

    def _pick_best_video_url(self, urls: list[str], text: str) -> str:
        for item in urls:
            url = normalize_text(item)
            if not url:
                continue
            if self._is_direct_video_url(url):
                return url
            lower = url.lower()
            if any(
                host in lower
                for host in (
                    "bilibili.com/video/",
                    "b23.tv/",
                    "douyin.com/",
                    "kuaishou.com/",
                    "acfun.cn/v/ac",
                    "acfun.com/v/ac",
                    "m.acfun.cn/v/",
                )
            ):
                return url
        content = normalize_text(text)
        if not content:
            return ""
        bv_match = re.search(r"\b(BV[0-9A-Za-z]{10})\b", content, flags=re.IGNORECASE)
        if bv_match:
            return f"https://www.bilibili.com/video/{bv_match.group(1)}"
        return ""

    async def _method_browser_resolve_video(
        self, method_name: str, method_args: dict[str, Any], query: str
    ) -> ToolResult:
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(query)
            url = urls[0] if urls else ""
        url = _unwrap_redirect_url(url)
        if not url:
            # 兜底：直接从 query 里提取 BV 号转标准链接
            bv_match = re.search(r"\b(BV[0-9A-Za-z]{10})\b", query, flags=re.IGNORECASE)
            if bv_match:
                url = f"https://www.bilibili.com/video/{bv_match.group(1)}"
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给完整的视频 URL。"},
                error="invalid_url",
            )

        if self._is_blocked_video_text(query) or self._is_blocked_video_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这类视频我不能处理"},
                error="blocked_video_request",
            )
        if not self._is_safe_public_http_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这个视频链接命中了安全限制（内网/本地地址不可访问）"},
                error="unsafe_url",
            )

        if self._is_direct_video_url(url):
            resolved_direct = url
            resolved_local: Path | None = None
            if self._video_validate_direct_url and YoutubeDL is not None:
                try:
                    self._cleanup_video_cache()
                    downloaded_path = await asyncio.to_thread(
                        self._download_platform_video_sync, url
                    )
                    if downloaded_path:
                        resolved_local = downloaded_path
                        resolved_direct = str(downloaded_path.resolve())
                except Exception as exc:
                    _ytdlp_log.warning(
                        "video_direct_validate_error | url=%s | %s",
                        url[:100],
                        str(exc)[:200],
                    )
            if (
                self._video_validate_direct_url
                and YoutubeDL is not None
                and resolved_local is None
            ):
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "这条直链视频没通过下载校验（可能失效或格式不兼容），换个链接我继续试。"
                    },
                    error="direct_video_validate_failed",
                )
            if resolved_local is not None:
                if not self._is_video_size_ok(resolved_local):
                    return ToolResult(
                        ok=False,
                        tool_name=method_name,
                        payload={
                            "text": "这条直链视频下载到了，但文件不完整或超出大小限制。"
                        },
                        error="direct_video_invalid_file",
                    )
                if (
                    self._video_require_audio_for_send
                    and not self._video_has_audio_stream(resolved_local)
                ):
                    return ToolResult(
                        ok=False,
                        tool_name=method_name,
                        payload={
                            "text": "这条直链视频没有可用音轨，我已拦截，避免发出后没声音。"
                        },
                        error="direct_video_no_audio",
                    )
            evidence = [
                {
                    "title": "视频直链",
                    "point": "用户提供了可发送视频直链",
                    "source": url,
                }
            ]
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "mode": "video",
                    "text": "已拿到直链视频，马上发你",
                    "video_url": resolved_direct,
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        if not self._is_supported_platform_video_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "当前只支持抖音/快手/B站/AcFun 视频链接解析"},
                error="unsupported_video_platform",
            )
        if not self._is_platform_video_detail_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "这个是平台搜索/频道页，不是视频详情链接 发我具体视频分享链接我就能解析"
                },
                error="video_detail_url_required",
            )

        # 抖音图文（note/ 或 v.douyin.com 短链）先走图文分支，避免先下载视频导致高频 403 日志。
        try:
            parsed_url = urlparse(url)
            host = normalize_text(parsed_url.netloc).lower()
            path = normalize_text(unquote(parsed_url.path or "")).lower()
        except Exception:
            host = ""
            path = ""
        if self._is_douyin_video_or_note_url(url) and (
            "/note/" in path or host.endswith("v.douyin.com")
        ):
            douyin_image_result = await self._try_resolve_douyin_image_post(
                url=url,
                query=query,
                method_name=method_name,
                resolve_diag="douyin_note_precheck",
            )
            if douyin_image_result is not None:
                return douyin_image_result

        resolved, resolve_diag = (
            await self._resolve_platform_video_safe_with_diagnostic(url)
        )
        if not resolved:
            douyin_image_result = await self._try_resolve_douyin_image_post(
                url=url,
                query=query,
                method_name=method_name,
                resolve_diag=resolve_diag,
            )
            if douyin_image_result is not None:
                return douyin_image_result
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": self._build_video_resolve_failed_text(resolve_diag),
                    "diagnostic": resolve_diag,
                },
                error="video_resolve_failed",
            )

        resolved_local: Path | None = None
        if resolved and not re.match(r"^https?://", resolved, flags=re.IGNORECASE):
            try:
                candidate_path = Path(resolved)
                if candidate_path.exists() and candidate_path.is_file():
                    resolved_local = candidate_path
            except Exception:
                resolved_local = None
        elif (
            self._video_validate_direct_url
            and self._is_direct_video_url(resolved)
            and YoutubeDL is not None
        ):
            # parse_api 可能返回可播但不稳定的外链；下载校验后统一转本地路径发送。
            try:
                self._cleanup_video_cache()
                downloaded_path = await asyncio.to_thread(
                    self._download_platform_video_sync, resolved
                )
                if downloaded_path:
                    resolved_local = downloaded_path
                    resolved = str(downloaded_path.resolve())
            except Exception as exc:
                _ytdlp_log.warning(
                    "video_resolved_direct_validate_error | url=%s | %s",
                    resolved[:100],
                    str(exc)[:200],
                )
            if resolved_local is None:
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "解析到的直链没通过下载校验（可能已过期或平台限流），请重发新链接。"
                    },
                    error="resolved_direct_validate_failed",
                )

        if resolved_local is not None and self._video_require_audio_for_send:
            if not self._video_has_audio_stream(resolved_local):
                self._last_video_resolve_diagnostic[url] = "video_no_audio_all_formats"
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "这条视频没有可用音轨，我已拦截，避免发出后没声音。"
                    },
                    error="video_no_audio_all_formats",
                )

        meta = await self._inspect_platform_video_metadata_safe(url)

        # 如果是分析请求，使用深度分析
        if self._looks_like_video_analysis_request(query):
            local_path = (
                resolved
                if resolved
                and not resolved.startswith("http")
                and Path(resolved).exists()
                else ""
            )
            analysis = await self._video_analyzer.analyze(
                source_url=url,
                local_video_path=local_path,
                depth="auto",
                yt_dlp_meta=meta,
            )
            text = analysis.to_context_block()
            evidence = self._build_video_analysis_evidence(analysis)
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "mode": "video",
                    "text": text,
                    "video_url": resolved,
                    "video_analysis": True,
                    "analysis_depth": analysis.analysis_depth,
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        text = self._compose_video_result_text(
            source_url=url,
            query=query,
            meta=meta,
            for_analysis=False,
        )
        evidence = self._build_video_evidence(source_url=url, meta=meta)

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "mode": "video",
                "text": text,
                "video_url": resolved,
                "evidence": evidence,
            },
            evidence=evidence,
        )

    async def _method_douyin_search_video(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
    ) -> ToolResult:
        keyword = normalize_text(str(method_args.get("query", ""))) or normalize_text(
            query
        )
        keyword = _normalize_multimodal_query(keyword)
        if not keyword:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给要搜索的抖音视频关键词。"},
                error="empty_query",
            )
        if self._is_blocked_video_text(keyword):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这类视频我不能处理。"},
                error="blocked_video_request",
            )

        limit_raw = method_args.get("limit", 5)
        try:
            limit = max(1, min(10, int(limit_raw)))
        except Exception:
            limit = 5

        results = await self._search_douyin_video_candidates(keyword, limit=limit)
        if not results:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": f"我按“{keyword}”查了抖音视频，暂时没拿到稳定结果。你可以换个更具体的关键词再试。"
                },
                error="douyin_search_empty",
            )

        cookie_required = False
        try_count = 0
        max_resolve_attempts = 2
        for item in results:
            candidate = _unwrap_redirect_url(normalize_text(item.url))
            if not candidate or not self._is_douyin_video_or_note_url(candidate):
                continue

            try_count += 1
            if try_count > max_resolve_attempts:
                break

            resolved, resolve_diag = (
                await self._resolve_platform_video_safe_with_diagnostic(candidate)
            )
            if resolved:
                meta = await self._inspect_platform_video_metadata_safe(candidate)
                return await self._build_video_result_with_analysis(
                    source_url=candidate,
                    resolved=resolved,
                    query=f"{keyword} 抖音 视频",
                    meta=meta,
                    tool_name=method_name,
                    extra_payload={
                        "platform": "douyin",
                        "query": keyword,
                        "results": [
                            {"title": r.title, "snippet": r.snippet, "url": r.url}
                            for r in results
                        ],
                    },
                )

            image_post_result = await self._try_resolve_douyin_image_post(
                url=candidate,
                query=keyword,
                method_name=method_name,
                resolve_diag=resolve_diag,
            )
            if image_post_result is not None:
                image_post_result.payload["platform"] = "douyin"
                image_post_result.payload["query"] = keyword
                image_post_result.payload["results"] = [
                    {"title": r.title, "snippet": r.snippet, "url": r.url}
                    for r in results
                ]
                return image_post_result

            diag_lower = normalize_text(resolve_diag).lower()
            if (
                "fresh cookies" in diag_lower
                or ("cookie" in diag_lower and "required" in diag_lower)
                or "cookie_unavailable:" in diag_lower
            ):
                cookie_required = True
                break

        evidence = self._build_evidence_from_results(results)
        text = self._format_search_text(
            keyword, results, evidence=evidence, query_type="video"
        )
        if cookie_required:
            text = (
                "抖音候选已搜到，但当前解析需要新 cookies。\n"
                "你可以先执行 `/yuki cookie douyin edge force` 再重试。\n"
                f"{text}"
            )
        else:
            text = f"我先给你抖音候选来源（还没拿到稳定可直发链接）：\n{text}"
        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "mode": "video",
                "platform": "douyin",
                "query": keyword,
                "text": text,
                "results": [
                    {"title": r.title, "snippet": r.snippet, "url": r.url}
                    for r in results
                ],
                "evidence": evidence,
            },
            evidence=evidence,
        )

    async def _search_douyin_video_candidates(
        self, query: str, limit: int = 5
    ) -> list[SearchResult]:
        base = normalize_text(query)
        if not base:
            return []

        cleaned = base
        for cue in ("抖音", "douyin", "视频", "video"):
            cleaned = re.sub(re.escape(cue), " ", cleaned, flags=re.IGNORECASE)
        cleaned = normalize_text(cleaned) or base

        query_variants: list[str] = [
            f"{cleaned} site:douyin.com/video",
            f"{cleaned} site:douyin.com/note",
            f"抖音 {cleaned} 视频",
        ]

        async def _safe_search(q: str) -> list[SearchResult]:
            try:
                return await self.search_engine.search(q)
            except Exception:
                return []

        batches = await asyncio.gather(*[_safe_search(q) for q in query_variants])
        merged: list[SearchResult] = []
        seen: set[str] = set()

        for batch in batches:
            for row in batch:
                candidate = _unwrap_redirect_url(normalize_text(row.url))
                if not self._is_douyin_video_or_note_url(candidate):
                    continue
                key = normalize_text(candidate).lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(
                    SearchResult(
                        title=normalize_text(row.title) or "抖音候选",
                        snippet=normalize_text(row.snippet),
                        url=candidate,
                    )
                )
                if len(merged) >= max(3, limit * 2):
                    break
            if len(merged) >= max(3, limit * 2):
                break

        if len(merged) < max(2, limit):
            try:
                bing_rows = await self.search_engine.search_bing_videos(
                    f"{cleaned} 抖音 视频", limit=max(5, limit * 2)
                )
            except Exception:
                bing_rows = []
            for row in bing_rows:
                candidate = _unwrap_redirect_url(normalize_text(row.url))
                if not self._is_douyin_video_or_note_url(candidate):
                    continue
                key = normalize_text(candidate).lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(
                    SearchResult(
                        title=normalize_text(row.title) or "抖音候选",
                        snippet=normalize_text(row.snippet),
                        url=candidate,
                    )
                )
                if len(merged) >= max(3, limit * 2):
                    break

        merged = self._filter_safe_video_results(query, merged)
        return merged[: max(1, limit)]

    async def _try_resolve_douyin_image_post(
        self,
        url: str,
        query: str,
        method_name: str,
        resolve_diag: str = "",
    ) -> ToolResult | None:
        target = _unwrap_redirect_url(url)
        if not self._is_douyin_video_or_note_url(target):
            return None

        detail = await self._fetch_douyin_image_post_detail(target)
        image_urls = [
            normalize_text(str(item))
            for item in detail.get("image_urls", [])
            if normalize_text(str(item))
        ]
        title = normalize_text(str(detail.get("title", "")))
        uploader = normalize_text(str(detail.get("uploader", "")))
        source = normalize_text(str(detail.get("source_url", ""))) or target

        if not image_urls:
            meta = await self._inspect_platform_video_metadata_safe(target)
            analysis = await self._video_analyzer.analyze(
                source_url=target,
                local_video_path="",
                depth="rich_metadata",
                yt_dlp_meta=meta,
            )
            image_urls = [
                normalize_text(str(item))
                for item in getattr(analysis, "image_urls", [])
                if normalize_text(str(item))
            ]
            if not image_urls:
                return None
            if not title:
                title = normalize_text(analysis.title)
            if not uploader:
                uploader = normalize_text(analysis.uploader)
            source = normalize_text(analysis.webpage_url) or source

        sendable_image = ""
        for item in image_urls[:5]:
            if await self._is_sendable_image_url(item):
                sendable_image = item
                break
        if not sendable_image:
            sendable_image = image_urls[0]

        title = title or "抖音图文作品"
        evidence = [
            {
                "title": clip_text(title, 64),
                "point": clip_text(f"图文作品，共 {len(image_urls)} 张图。", 80),
                "source": source,
            }
        ]
        text = self._compose_douyin_image_post_text(
            title=title,
            uploader=uploader,
            source=source,
            image_urls=image_urls,
        )
        payload: dict[str, Any] = {
            "mode": "image",
            "post_type": "image_text",
            "query": query,
            "text": text,
            "image_urls": image_urls,
            "evidence": evidence,
        }
        if sendable_image:
            payload["image_url"] = sendable_image
        if normalize_text(resolve_diag):
            payload["diagnostic"] = normalize_text(resolve_diag)
        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload=payload,
            evidence=evidence,
        )

    async def _fetch_douyin_image_post_detail(self, source_url: str) -> dict[str, Any]:
        target = _unwrap_redirect_url(source_url)
        final_url = target
        html = ""

        headers = {
            "User-Agent": self._DOUYIN_MOBILE_UA,
            "Referer": "https://www.douyin.com/",
            "Accept": "text/html,application/json,*/*",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(12.0, connect=5.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(target)
                final_url = str(resp.url)
                html = resp.text or ""
        except Exception:
            pass

        # 重定向后如果是 /video/ 路径，说明是视频不是图文，直接返回空
        # /note/ 或 /share/note/ 视为图文（aweme_type 在不同版本可能不是固定值）。
        try:
            final_path = urlparse(final_url).path.lower()
        except Exception:
            final_path = ""
        is_note_path = ("/note/" in final_path) or ("/share/note/" in final_path)
        if "/video/" in final_path:
            return {}

        decoded_html = (
            html.replace("\\u002F", "/")
            .replace("\\/", "/")
            .replace("\\u0026", "&")
            .replace("&amp;", "&")
        )

        # 从 HTML 检查 aweme_type。抖音不同版本对图文的 aweme_type 不稳定，
        # 已确认存在 aweme_type=2 的 note 图文；因此 note 路径优先，非 note 时再按类型拦截。
        aweme_type_match = re.search(r'"aweme_type"\s*:\s*(\d+)', decoded_html)
        if aweme_type_match:
            aweme_type_val = int(aweme_type_match.group(1))
            # 常见图文类型: 2/68/150；其余通常是视频。
            if (not is_note_path) and aweme_type_val not in (2, 68, 150):
                return {}

        aweme_id = self._extract_douyin_aweme_id(
            final_url
        ) or self._extract_douyin_aweme_id(target)
        if not aweme_id and decoded_html:
            match = re.search(r'"aweme_id"\s*:\s*"(\d{8,24})"', decoded_html)
            if match:
                aweme_id = normalize_text(match.group(1))
        if not aweme_id and decoded_html:
            match = re.search(r'"itemId"\s*:\s*"(\d{8,24})"', decoded_html)
            if match:
                aweme_id = normalize_text(match.group(1))

        item: dict[str, Any] = {}
        if aweme_id:
            api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12.0, connect=5.0),
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    api_resp = await client.get(api_url)
                    if api_resp.is_success and api_resp.content:
                        data = api_resp.json()
                        if isinstance(data, dict) and data.get("status_code", 0) == 0:
                            item_list = data.get("item_list", [])
                            if (
                                isinstance(item_list, list)
                                and item_list
                                and isinstance(item_list[0], dict)
                            ):
                                item = item_list[0]
            except Exception:
                item = {}

        image_urls: list[str] = []
        seen_url: set[str] = set()
        seen_asset: set[str] = set()

        def _image_key(url_value: str) -> str:
            try:
                parsed = urlparse(url_value)
            except Exception:
                return normalize_text(url_value).lower()
            filename = Path(unquote(parsed.path)).name.lower()
            if "~" in filename:
                filename = filename.split("~", 1)[0]
            if "." in filename:
                filename = filename.rsplit(".", 1)[0]
            if filename:
                return filename
            return re.sub(r"[^a-z0-9]+", "", parsed.path.lower())

        def _pick_best_image_url(urls: Any) -> str:
            if not isinstance(urls, list):
                return ""
            for raw in urls:
                value = normalize_text(str(raw))
                if re.match(r"^https?://", value, flags=re.IGNORECASE):
                    return value
            return ""

        def _add_image(url_value: Any) -> None:
            value = normalize_text(str(url_value))
            if not value:
                return
            if not re.match(r"^https?://", value, flags=re.IGNORECASE):
                return
            asset_key = _image_key(value)
            if asset_key and asset_key in seen_asset:
                return
            if value in seen_url:
                return
            seen_url.add(value)
            if asset_key:
                seen_asset.add(asset_key)
            image_urls.append(value)

        if item:
            # 检查 aweme_type。note 路径优先按图文处理，避免误判后进入视频下载链。
            aweme_type = item.get("aweme_type")
            if (
                isinstance(aweme_type, int)
                and (not is_note_path)
                and aweme_type not in (2, 68, 150)
            ):
                return {}

            for row in (
                item.get("images", [])
                if isinstance(item.get("images", []), list)
                else []
            ):
                if not isinstance(row, dict):
                    continue
                urls = row.get("url_list", [])
                best = _pick_best_image_url(urls)
                if best:
                    _add_image(best)
                if len(image_urls) >= 40:
                    break

            image_post_info = item.get("image_post_info", {})
            if isinstance(image_post_info, dict):
                rows = image_post_info.get("images", [])
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        for key in ("display_image", "owner_watermark_image"):
                            obj = row.get(key, {})
                            if not isinstance(obj, dict):
                                continue
                            urls = obj.get("url_list", [])
                            best = _pick_best_image_url(urls)
                            if best:
                                _add_image(best)
                            if len(image_urls) >= 40:
                                break
                        if len(image_urls) >= 40:
                            break

        def _looks_like_note_image(url_value: str) -> bool:
            lower = url_value.lower()
            if not lower.startswith(("http://", "https://")):
                return False
            if ("douyinpic.com" not in lower) and ("douyinstatic.com" not in lower):
                return False
            if any(
                cue in lower
                for cue in (
                    "aweme-avatar",
                    "/avatar/",
                    "/100x100/",
                    "/50x50/",
                    "emoji",
                    "/obj/ies-music/",
                    "aweme/v1/play",
                    "playwm",
                )
            ):
                return False
            if "biz_tag=aweme_images" in lower:
                return True
            if "/tos-cn-i-" in lower and re.search(
                r"\.(?:jpe?g|png|webp)(?:\?|$)", lower
            ):
                return True
            return False

        if not image_urls and decoded_html:
            html_candidates: list[str] = []
            for raw in re.findall(r"https?://[^\"'\s<>]{24,}", decoded_html):
                candidate = normalize_text(unescape(str(raw)))
                if not _looks_like_note_image(candidate):
                    continue
                html_candidates.append(candidate)
            html_candidates.sort(
                key=lambda item: (
                    "water" in item.lower(),
                    "biz_tag=aweme_images" not in item.lower(),
                    "sc=image" not in item.lower(),
                    len(item),
                )
            )
            for candidate in html_candidates:
                key = _image_key(candidate)
                if not key or key in seen_asset:
                    continue
                _add_image(candidate)
                if len(image_urls) >= 40:
                    break

        if not image_urls:
            return {}

        title = ""
        uploader = ""
        if item:
            title = normalize_text(str(item.get("desc", "")))
            author = (
                item.get("author", {})
                if isinstance(item.get("author", {}), dict)
                else {}
            )
            uploader = normalize_text(str(author.get("nickname", "")))
        if not title and decoded_html:
            desc_match = re.search(r'"desc"\s*:\s*"([^"]{1,300})"', decoded_html)
            if desc_match:
                title = normalize_text(unescape(desc_match.group(1)))
        if not uploader and decoded_html:
            nick_match = re.search(r'"nickname"\s*:\s*"([^"]{1,80})"', decoded_html)
            if nick_match:
                uploader = normalize_text(unescape(nick_match.group(1)))
        if not title and html:
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL
            )
            if title_match:
                title = normalize_text(unescape(title_match.group(1)))
                title = title.split(" - ??", 1)[0].strip()

        source = normalize_text(final_url) or normalize_text(target)
        if aweme_id:
            source = f"https://www.douyin.com/note/{aweme_id}"
        return {
            "aweme_id": aweme_id,
            "title": title,
            "uploader": uploader,
            "source_url": source,
            "image_urls": image_urls,
            "is_note": is_note_path,
        }

    @staticmethod
    def _compose_douyin_image_post_text(
        title: str, uploader: str, source: str, image_urls: list[str]
    ) -> str:
        lines = [f"识别到这是抖音图文作品，共 {len(image_urls)} 张图。"]
        if title:
            lines.append(f"标题：{title}")
        if uploader:
            lines.append(f"作者：{uploader}")
        lines.append(f"来源：{source}")
        for idx, item in enumerate(image_urls[:3], 1):
            lines.append(f"{idx}. {item}")
        if len(image_urls) > 3:
            lines.append(f"其余 {len(image_urls) - 3} 张已省略。")
        return "\n".join(lines)

    async def _method_media_pick_video(
        self, method_name: str, raw_segments: list[dict[str, Any]]
    ) -> ToolResult:
        candidates = self._extract_message_media_urls(raw_segments, media_type="video")
        for url in candidates:
            if self._is_blocked_video_url(url):
                continue
            if url.startswith("file://"):
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "mode": "video",
                        "text": "拿到这条消息里的视频，发你看",
                        "video_url": url,
                    },
                )
            if self._is_direct_video_url(url):
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "mode": "video",
                        "text": "拿到这条消息里的视频，发你看",
                        "video_url": url,
                    },
                )
            if self._is_supported_platform_video_url(url):
                if not self._is_platform_video_detail_url(url):
                    continue
                resolved = await self._resolve_platform_video_safe(url)
                if resolved:
                    return ToolResult(
                        ok=True,
                        tool_name=method_name,
                        payload={
                            "mode": "video",
                            "text": "拿到这条消息里的视频，解析后发你",
                            "video_url": resolved,
                        },
                    )
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={"text": "这条消息里没拿到可发送视频"},
            error="message_video_not_found",
        )

    async def _method_video_analyze(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]] | None = None,
        conversation_id: str = "",
    ) -> ToolResult:
        """深度视频分析：关键帧 + Vision API + 平台富元数据。"""
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(f"{query}\n{message_text}")
            url = urls[0] if urls else ""
        url = _unwrap_redirect_url(url)
        local_path = ""

        if not url:
            media_candidates = self._extract_message_media_urls(
                raw_segments or [], media_type="video"
            )
            if conversation_id:
                media_candidates.extend(
                    self._get_recent_media(
                        conversation_id=conversation_id, media_type="video"
                    )
                )
            for raw_candidate in media_candidates:
                candidate = _unwrap_redirect_url(normalize_text(raw_candidate))
                if not candidate:
                    continue
                if re.match(r"^https?://", candidate, flags=re.IGNORECASE):
                    url = candidate
                    break
                local_candidate = candidate
                if local_candidate.startswith("file://"):
                    parsed = urlparse(local_candidate)
                    file_part = unquote(parsed.path or "")
                    if re.match(r"^/[A-Za-z]:/", file_part):
                        file_part = file_part[1:]
                    local_candidate = file_part
                path = Path(local_candidate)
                if path.exists() and path.is_file():
                    local_path = str(path.resolve())
                    url = path.resolve().as_uri()
                    break

        if not url:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给完整的视频URL。"},
                error="invalid_url",
            )

        is_remote_url = bool(re.match(r"^https?://", url, flags=re.IGNORECASE))
        if is_remote_url:
            if self._is_blocked_video_text(
                f"{query}\n{message_text}"
            ) or self._is_blocked_video_url(url):
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={"text": "这类视频我不能处理。"},
                    error="blocked_video_request",
                )
            if not self._is_safe_public_http_url(url):
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "这个视频链接命中了安全限制（内网/本地地址不可访问）。"
                    },
                    error="unsafe_url",
                )

        depth = normalize_text(str(method_args.get("depth", "auto"))) or "auto"
        text_only_requested = self._looks_like_analysis_text_only_request(
            f"{query}\n{message_text}"
        )

        resolved = ""
        if not text_only_requested:
            if local_path:
                resolved = local_path
            elif self._is_supported_platform_video_url(
                url
            ) and self._is_platform_video_detail_url(url):
                resolved = await self._resolve_platform_video_safe(url)
            elif self._is_direct_video_url(url):
                resolved = url

        meta: dict[str, Any] = {}
        if is_remote_url:
            meta = await self._inspect_platform_video_metadata_safe(url)

        if (
            not local_path
            and resolved
            and not resolved.startswith("http")
            and Path(resolved).exists()
        ):
            local_path = resolved
        analysis = await self._video_analyzer.analyze(
            source_url=url,
            local_video_path=local_path,
            depth=depth,
            yt_dlp_meta=meta,
        )

        context_block = analysis.to_context_block()
        strict_text = self._build_strict_video_analysis_text(analysis)
        evidence = self._build_video_analysis_evidence(analysis)
        payload: dict[str, Any] = {
            "mode": "video",
            "text": strict_text or context_block,
            "analysis_context": context_block,
            "video_url": resolved or local_path,
            "video_analysis": True,
            "analysis_strict": True,
            "analysis_depth": analysis.analysis_depth,
            "evidence": evidence,
        }
        if text_only_requested:
            payload["mode"] = "text"
            payload["video_url"] = ""
        image_urls = [
            normalize_text(str(item))
            for item in getattr(analysis, "image_urls", [])
            if normalize_text(str(item))
        ]
        if image_urls:
            payload["mode"] = "image"
            payload["post_type"] = "image_text"
            payload["image_urls"] = image_urls
            payload["image_url"] = image_urls[0]

        return ToolResult(
            ok=True, tool_name=method_name, payload=payload, evidence=evidence
        )

    @staticmethod
    def _build_video_analysis_evidence(
        analysis: VideoAnalysisResult,
    ) -> list[dict[str, str]]:
        title = analysis.title or "视频分析"
        parts: list[str] = []
        if analysis.uploader:
            parts.append(f"作者: {analysis.uploader}")
        if analysis.duration > 0:
            parts.append(f"时长: {analysis.duration}s")
        if getattr(analysis, "post_type", "") == "image_text":
            parts.append(f"图文: {len(getattr(analysis, 'image_urls', []) or [])} 张")
        if analysis.analysis_depth == "multimodal":
            parts.append(f"已分析{len(analysis.keyframe_descriptions)}个关键帧")
        elif analysis.analysis_depth == "rich_metadata":
            parts.append("已获取富元数据")
        if getattr(analysis, "subtitle_text", ""):
            parts.append("已提取字幕证据")
        if analysis.tags:
            parts.append(f"标签: {', '.join(analysis.tags[:3])}")
        return [
            {
                "title": clip_text(title, 64),
                "point": clip_text("，".join(parts) or "已完成视频分析", 120),
                "source": analysis.webpage_url or analysis.source_url,
            }
        ]

    async def _inspect_platform_video_metadata(self, source_url: str) -> dict[str, Any]:
        if YoutubeDL is None:
            return {}
        url = _unwrap_redirect_url(source_url)
        if not self._is_supported_platform_video_url(url):
            return {}
        return await asyncio.to_thread(self._inspect_platform_video_metadata_sync, url)

    async def _inspect_platform_video_metadata_safe(
        self, source_url: str
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._inspect_platform_video_metadata(source_url),
                timeout=float(self._video_metadata_timeout_seconds),
            )
        except Exception:
            return {}

    def _pick_video_duration_limit(self, query: str) -> tuple[int, str]:
        content = normalize_text(query).lower()
        if self._looks_like_video_analysis_request(content):
            return self._video_search_analysis_max_duration_seconds, "analysis"
        send_cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("local_media_request_cues")
            if normalize_text(cue)
        ]
        if not send_cues:
            send_cues = [
                "发送",
                "发我",
                "发到群",
                "转发",
                "下载",
                "解析",
                "直发",
                "发视频",
                "把视频发",
            ]
        if any(cue in content for cue in send_cues):
            return self._video_search_send_max_duration_seconds, "send"
        return self._video_search_max_duration_seconds, "default"

    def _is_video_duration_acceptable_for_search(
        self, meta: dict[str, Any], query: str
    ) -> tuple[bool, int, int, str]:
        duration = int(meta.get("duration", 0) or 0)
        limit, scene = self._pick_video_duration_limit(query)
        if duration <= 0:
            return True, limit, duration, scene
        return duration <= limit, limit, duration, scene

    def _inspect_platform_video_metadata_sync(self, source_url: str) -> dict[str, Any]:
        if YoutubeDL is None:
            return {}
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self._video_download_timeout_seconds,
            "http_headers": self._http_headers,
            "logger": _SilentYTDLPLogger(),
        }
        if self._video_cookies_file:
            options["cookiefile"] = self._video_cookies_file
        if self._video_cookies_from_browser:
            options["cookiesfrombrowser"] = (self._video_cookies_from_browser,)
        _tmp_cookie_file = self._inject_platform_cookiefile(options, source_url)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(source_url, download=False)
        except Exception as exc:
            self._disable_cookie_browser_on_error(str(exc))
            return {}
        finally:
            if _tmp_cookie_file:
                try:
                    os.unlink(_tmp_cookie_file)
                except OSError:
                    pass
        if not isinstance(info, dict):
            return {}
        duration = info.get("duration")
        duration_int = int(duration) if isinstance(duration, (int, float)) else 0
        subtitle_text, subtitle_lang, subtitle_source = (
            self._extract_subtitle_text_from_info_sync(
                info=info,
                source_url=source_url,
            )
        )
        return {
            "title": normalize_text(str(info.get("title", ""))),
            "uploader": normalize_text(
                str(info.get("uploader", "") or info.get("channel", ""))
            ),
            "duration": duration_int,
            "description": clip_text(
                normalize_text(str(info.get("description", ""))), 220
            ),
            "webpage_url": normalize_text(
                str(info.get("webpage_url", "") or source_url)
            ),
            "thumbnail": normalize_text(str(info.get("thumbnail", ""))),
            "view_count": int(info.get("view_count", 0) or 0),
            "like_count": int(info.get("like_count", 0) or 0),
            "subtitle_text": subtitle_text,
            "subtitle_lang": subtitle_lang,
            "subtitle_source": subtitle_source,
        }

    def _extract_subtitle_text_from_info_sync(
        self, info: dict[str, Any], source_url: str
    ) -> tuple[str, str, str]:
        candidates: list[tuple[int, str, str, str]] = []  # score, lang, ext, url

        def _score_lang(lang: str) -> int:
            value = normalize_text(lang).lower()
            score = 0
            if any(token in value for token in ("zh", "cn", "中文", "汉")):
                score += 10
            if "auto" in value:
                score -= 2
            return score

        def _score_ext(ext: str) -> int:
            value = normalize_text(ext).lower()
            if value in {"json3", "json"}:
                return 6
            if value in {"vtt", "srt"}:
                return 4
            return 1

        def _norm_url(raw: str) -> str:
            url = normalize_text(raw)
            if not url:
                return ""
            if url.startswith("//"):
                url = f"https:{url}"
            elif url.startswith("/"):
                url = f"https://api.bilibili.com{url}"
            return url

        def _add(lang: str, ext: str, raw_url: str, base_score: int) -> None:
            url = _norm_url(raw_url)
            if not url or not re.match(r"^https?://", url, flags=re.IGNORECASE):
                return
            candidates.append(
                (base_score + _score_lang(lang) + _score_ext(ext), lang, ext, url)
            )

        requested = info.get("requested_subtitles", {})
        if isinstance(requested, dict):
            for lang, payload in requested.items():
                if isinstance(payload, dict):
                    _add(
                        str(lang),
                        str(payload.get("ext", "")),
                        str(payload.get("url", "")),
                        30,
                    )

        for source_name, base in (("subtitles", 20), ("automatic_captions", 10)):
            source = info.get(source_name, {})
            if not isinstance(source, dict):
                continue
            for lang, items in source.items():
                if not isinstance(items, list):
                    continue
                for row in items[:5]:
                    if not isinstance(row, dict):
                        continue
                    _add(
                        str(lang),
                        str(row.get("ext", "")),
                        str(row.get("url", "")),
                        base,
                    )

        if not candidates:
            return "", "", ""
        candidates.sort(key=lambda x: x[0], reverse=True)

        headers = {
            "User-Agent": self._http_headers.get("User-Agent", "Mozilla/5.0"),
            "Referer": normalize_text(str(info.get("webpage_url", "") or source_url)),
        }
        with httpx.Client(
            timeout=max(6, int(self._video_metadata_timeout_seconds)),
            follow_redirects=True,
            headers=headers,
        ) as client:
            for _, lang, ext, url in candidates[:6]:
                try:
                    resp = client.get(url)
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                text = self._subtitle_payload_to_text(
                    ext=ext,
                    body=resp.text,
                    content_type=str(resp.headers.get("content-type", "")),
                )
                text = normalize_text(re.sub(r"\s+", " ", text))
                if len(text) < 20:
                    continue
                return clip_text(text, 8000), normalize_text(lang), url
        return "", "", ""

    @staticmethod
    def _subtitle_payload_to_text(ext: str, body: str, content_type: str) -> str:
        if not body:
            return ""
        ext_norm = normalize_text(ext).lower()
        ctype = normalize_text(content_type).lower()
        raw = body.strip()
        if ext_norm in {"json3", "json"} or "json" in ctype:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                # youtube/json3 style
                events = payload.get("events", [])
                if isinstance(events, list):
                    rows: list[str] = []
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        segs = event.get("segs", [])
                        if not isinstance(segs, list):
                            continue
                        line = "".join(
                            normalize_text(str(seg.get("utf8", "")))
                            for seg in segs
                            if isinstance(seg, dict)
                        )
                        line = normalize_text(line)
                        if line:
                            rows.append(line)
                    if rows:
                        return "\n".join(rows)
                # bilibili subtitle json style
                body_rows = payload.get("body", [])
                if isinstance(body_rows, list):
                    rows = [
                        normalize_text(str(item.get("content", "")))
                        for item in body_rows
                        if isinstance(item, dict)
                        and normalize_text(str(item.get("content", "")))
                    ]
                    if rows:
                        return "\n".join(rows)
            return ""

        if ext_norm == "vtt" or "vtt" in ctype:
            lines = []
            for line in raw.splitlines():
                t = normalize_text(line)
                if not t:
                    continue
                if t.upper().startswith("WEBVTT"):
                    continue
                if "-->" in t:
                    continue
                if re.fullmatch(r"\d+", t):
                    continue
                t = re.sub(r"<[^>]+>", " ", t)
                t = normalize_text(t)
                if t:
                    lines.append(t)
            return "\n".join(lines)

        if ext_norm == "srt" or "srt" in ctype:
            lines = []
            for line in raw.splitlines():
                t = normalize_text(line)
                if not t:
                    continue
                if re.fullmatch(r"\d+", t):
                    continue
                if re.search(
                    r"\d{2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{1,3}",
                    t,
                ):
                    continue
                t = re.sub(r"<[^>]+>", " ", t)
                t = normalize_text(t)
                if t:
                    lines.append(t)
            return "\n".join(lines)

        return normalize_text(raw)

    async def _build_video_result_with_analysis(
        self,
        source_url: str,
        resolved: str,
        query: str,
        meta: dict[str, Any],
        message_text: str = "",
        tool_name: str = "search_video",
        extra_payload: dict[str, Any] | None = None,
    ) -> ToolResult:
        """视频搜索结果处理，优先使用 VideoAnalyzer 进行深度分析"""
        is_analysis = self._looks_like_video_analysis_request(query)

        if is_analysis:
            local_path = (
                resolved
                if resolved
                and not resolved.startswith("http")
                and Path(resolved).exists()
                else ""
            )
            analysis = await self._video_analyzer.analyze(
                source_url=source_url,
                local_video_path=local_path,
                depth="auto",
                yt_dlp_meta=meta,
            )
            context_block = analysis.to_context_block()
            text = self._build_strict_video_analysis_text(analysis) or context_block
            evidence = self._build_video_analysis_evidence(analysis)
            payload: dict[str, Any] = {
                "mode": "video",
                "text": text,
                "video_url": resolved,
                "video_analysis": True,
                "analysis_strict": True,
                "analysis_context": context_block,
                "analysis_depth": analysis.analysis_depth,
                "evidence": evidence,
            }
            if self._looks_like_analysis_text_only_request(f"{query}\n{message_text}"):
                payload["mode"] = "text"
                payload["video_url"] = ""
            image_urls = [
                normalize_text(str(item))
                for item in getattr(analysis, "image_urls", [])
                if normalize_text(str(item))
            ]
            if image_urls:
                payload["mode"] = "image"
                payload["post_type"] = "image_text"
                payload["image_urls"] = image_urls
                payload["image_url"] = image_urls[0]
        else:
            text = self._compose_video_result_text(
                source_url=source_url,
                query=query,
                meta=meta,
                for_analysis=False,
            )
            evidence = self._build_video_evidence(source_url=source_url, meta=meta)
            payload = {
                "mode": "video",
                "text": text,
                "video_url": resolved,
                "evidence": evidence,
            }

        cover_url = normalize_text(str(meta.get("thumbnail", "")))
        if cover_url:
            payload["cover_url"] = cover_url

        if extra_payload:
            payload.update(extra_payload)
        return ToolResult(
            ok=True, tool_name=tool_name, payload=payload, evidence=evidence
        )

    def _build_strict_video_analysis_text(self, analysis: VideoAnalysisResult) -> str:
        lines: list[str] = ["视频分析（严格模式，不做无依据脑补）"]
        if analysis.title:
            lines.append(f"- 标题：{analysis.title}")
        if analysis.uploader:
            lines.append(f"- 作者：{analysis.uploader}")
        if analysis.duration > 0:
            lines.append(
                f"- 时长：{self._format_duration(analysis.duration) or str(analysis.duration)}"
            )
        if analysis.webpage_url:
            lines.append(f"- 来源：{analysis.webpage_url}")

        if analysis.subtitle_text:
            highlights = self._extract_transcript_highlights(
                analysis.subtitle_text, max_items=8
            )
            lines.append("依据：视频字幕（可核验）")
            if highlights:
                lines.append("字幕要点：")
                for idx, row in enumerate(highlights, 1):
                    lines.append(f"{idx}. {row}")
            else:
                lines.append(f"字幕摘录：{clip_text(analysis.subtitle_text, 260)}")
            return "\n".join(lines)

        if analysis.keyframe_descriptions:
            lines.append("当前未拿到可用字幕，只拿到画面关键帧。")
            lines.append("为避免瞎猜，我不能把画面当口播内容来总结。")
            lines.append("可用信息（仅画面）：")
            for idx, row in enumerate(analysis.keyframe_descriptions[:4], 1):
                lines.append(f"{idx}. {clip_text(normalize_text(row), 90)}")
            return "\n".join(lines)

        lines.append("当前未拿到字幕/转写内容，只能看到元数据。")
        lines.append("为保证准确性，我不会编造视频具体讲解内容。")
        return "\n".join(lines)

    @staticmethod
    def _extract_transcript_highlights(text: str, max_items: int = 8) -> list[str]:
        content = normalize_text(text)
        if not content:
            return []
        parts = [
            normalize_text(x)
            for x in re.split(r"[\n\r]+", content)
            if normalize_text(x)
        ]
        if len(parts) <= 1:
            parts = [
                normalize_text(x)
                for x in re.split(r"(?<=[銆傦紒锛?!?])", content)
                if normalize_text(x)
            ]
        if len(parts) <= 1:
            parts = [
                normalize_text(x)
                for x in re.split(r"[锛?銆侊紱;]", content)
                if normalize_text(x)
            ]
        out: list[str] = []
        seen: set[str] = set()
        for part in parts:
            row = normalize_text(re.sub(r"\s+", " ", part))
            if len(row) < 4:
                continue
            expanded: list[str] = [row]
            if len(row) > 72:
                # 把长口语串切成更可读的片段，避免整段挤在一条里
                if " " in row:
                    tokens = [
                        normalize_text(tok)
                        for tok in row.split(" ")
                        if normalize_text(tok)
                    ]
                    expanded = []
                    buf = ""
                    for tok in tokens:
                        candidate = f"{buf} {tok}".strip() if buf else tok
                        if len(candidate) > 42:
                            if buf:
                                expanded.append(buf)
                            buf = tok
                        else:
                            buf = candidate
                    if buf:
                        expanded.append(buf)
                else:
                    expanded = [row[i : i + 36] for i in range(0, len(row), 36)]
            for item in expanded:
                clean = normalize_text(item)
                if len(clean) < 4:
                    continue
                if clean in seen:
                    continue
                seen.add(clean)
                out.append(clip_text(clean, 90))
                if len(out) >= max(1, int(max_items)):
                    break
            if len(out) >= max(1, int(max_items)):
                break
        return out

    @staticmethod
    def _looks_like_analysis_text_only_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        explicit_tokens = (
            "output=text",
            "format=text",
            "response=text",
            "mode=text",
            "text_only=1",
            "textonly=1",
            "only_text=1",
            "onlytext=1",
            "/summary",
            "/text",
            "--text-only",
        )
        if any(token in plain for token in explicit_tokens):
            return True
        explicit_patterns = (
            r"(?:^|\s)/(?:summary|text)(?:\s|$)",
            r"(?:^|\s)output\s*=\s*text(?:\s|$)",
            r"(?:^|\s)format\s*=\s*text(?:\s|$)",
            r"(?:^|\s)mode\s*=\s*text(?:\s|$)",
        )
        return any(re.search(pattern, content) for pattern in explicit_patterns)

    def _compose_video_result_text(
        self,
        source_url: str,
        query: str,
        meta: dict[str, Any],
        for_analysis: bool,
    ) -> str:
        title = normalize_text(str(meta.get("title", "")))
        uploader = normalize_text(str(meta.get("uploader", "")))
        duration = self._format_duration(int(meta.get("duration", 0)))
        desc = normalize_text(str(meta.get("description", "")))
        source = normalize_text(str(meta.get("webpage_url", ""))) or normalize_text(
            source_url
        )

        if not for_analysis:
            parts = [f"标题：{title}"] if title else []
            if uploader:
                parts.append(f"UP主：{uploader}")
            if duration:
                parts.append(f"时长：{duration}")
            if desc:
                parts.append(f"简介：{clip_text(desc, 120)}")
            parts.append(f"来源：{source}")
            return "视频信息：\n" + "\n".join(parts) if parts else f"来源：{source}"

        lines = ["解析完成，关键结果："]
        if title:
            lines.append(f"- 标题：{title}")
        if uploader:
            lines.append(f"- UP：{uploader}")
        if duration:
            lines.append(f"- 时长：{duration}")
        lines.append(f"- 来源：{source}")
        if title:
            lines.append(f"简评：从标题看主题主要是“{clip_text(title, 40)}”。")
        if desc:
            lines.append(f"简介要点：{clip_text(desc, 90)}")
        else:
            lines.append("简介要点：这条视频没拿到完整简介。")
        lines.append("提示：这份分析基于标题/简介元数据，不是逐帧内容识别。")
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        sec = int(seconds or 0)
        if sec <= 0:
            return ""
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _build_video_duration_filtered_text(
        self, query: str, candidates: list[dict[str, Any]]
    ) -> str:
        if not candidates:
            return ""
        limit = min(
            int(
                item.get("limit", self._video_search_max_duration_seconds)
                or self._video_search_max_duration_seconds
            )
            for item in candidates
        )
        longest = max(int(item.get("duration", 0) or 0) for item in candidates)
        scene = normalize_text(str(candidates[0].get("scene", "default"))).lower()
        scene_label = {
            "default": "普通搜索",
            "send": "发送/转发",
            "analysis": "视频解析/分析",
        }.get(scene, "视频搜索")
        longest_text = self._format_duration(longest) or f"{longest}s"
        return (
            f"这次命中的候选视频里有 {len(candidates)} 条超出时长上限（{scene_label}上限 {limit} 秒，"
            f"最长约 {longest_text}），我先跳过了这些结果。"
        )

    async def _search_video(self, query: str) -> ToolResult:
        if self._is_blocked_video_text(query):
            return ToolResult(
                ok=False,
                tool_name="search_video",
                payload={"text": "这类视频我不能发 换一个合规主题我继续帮你找"},
                error="blocked_video_request",
            )

        direct = self._extract_direct_video_url(query)
        if direct:
            evidence = [
                {
                    "title": "视频直链",
                    "point": "用户消息里已包含可发送视频直链",
                    "source": direct,
                }
            ]
            return ToolResult(
                ok=True,
                tool_name="search_video",
                payload={
                    "mode": "video",
                    "text": "先发你这个视频",
                    "video_url": direct,
                    "query": query,
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        platform_page_hint = ""
        douyin_cookie_required = False
        duration_filtered_candidates: list[dict[str, Any]] = []
        urls = [_unwrap_redirect_url(url) for url in self._extract_urls(query)]
        for url in urls:
            if not self._is_supported_platform_video_url(url):
                continue
            if self._is_blocked_video_url(url):
                continue
            if not self._is_platform_video_detail_url(url):
                if not platform_page_hint:
                    platform_page_hint = url
                continue
            resolved, resolve_diag = (
                await self._resolve_platform_video_safe_with_diagnostic(url)
            )
            if resolved:
                meta = await self._inspect_platform_video_metadata_safe(url)
                return await self._build_video_result_with_analysis(
                    source_url=url,
                    resolved=resolved,
                    query=query,
                    meta=meta,
                )
            if (
                "douyin.com" in normalize_text(urlparse(url).netloc).lower()
                and "fresh cookies" in normalize_text(resolve_diag).lower()
            ):
                douyin_cookie_required = True
                break

        # ── 优先：Bilibili API 直接搜索（最可靠的中文视频来源）──
        try:
            bili_results = await self.search_engine.search_bilibili_videos(
                query, limit=5
            )
        except Exception:
            bili_results = []
        bili_results = self._filter_safe_video_results(query, bili_results)

        _dl_attempts_bili = 0
        for item in bili_results:
            candidate = normalize_text(item.url)
            if not candidate or self._is_blocked_video_url(candidate):
                continue
            meta = await self._inspect_platform_video_metadata_safe(candidate)
            duration_ok, duration_limit, duration_value, duration_scene = (
                self._is_video_duration_acceptable_for_search(meta, query=query)
            )
            if not duration_ok:
                _ytdlp_log.info(
                    "bili_skip_too_long%s | url=%s | dur=%s | limit=%s | scene=%s",
                    _tool_trace_tag(),
                    candidate[:80],
                    duration_value,
                    duration_limit,
                    duration_scene,
                )
                duration_filtered_candidates.append(
                    {
                        "url": candidate,
                        "duration": duration_value,
                        "limit": duration_limit,
                        "scene": duration_scene,
                    }
                )
                continue
            _dl_attempts_bili += 1
            if _dl_attempts_bili > 2:
                break
            resolved = await self._resolve_platform_video_safe(candidate)
            if not resolved:
                continue
            return await self._build_video_result_with_analysis(
                source_url=candidate,
                resolved=resolved,
                query=query,
                meta=meta,
                extra_payload={
                    "results": [
                        {"title": r.title, "snippet": r.snippet, "url": r.url}
                        for r in bili_results
                    ]
                },
            )

        # ── 回退：DuckDuckGo 并行搜索 ──
        async def _safe_search(q: str) -> list[SearchResult]:
            try:
                return await self.search_engine.search(q)
            except Exception:
                return []

        search_queries = [query]
        q_lower = query.lower()
        if "视频" not in query and "video" not in q_lower:
            search_queries.append(f"{query} 视频")
        raw_batches = await asyncio.gather(*[_safe_search(q) for q in search_queries])
        # 合并去重（包含 Bilibili API 结果）
        results: list[SearchResult] = []
        seen_search: set[str] = set()
        # 先加入 Bilibili API 结果（已尝试过但可能因时长跳过）
        for item in bili_results:
            key = normalize_text(item.url)
            if key and key not in seen_search:
                seen_search.add(key)
                results.append(item)
        for batch in raw_batches:
            for item in batch:
                key = normalize_text(item.url)
                if key and key not in seen_search:
                    seen_search.add(key)
                    results.append(item)
        results = self._filter_safe_video_results(query, results)
        results = self._filter_and_rank_results(query, results, query_type="video")

        _dl_attempts = 0
        for item in results:
            candidate = _unwrap_redirect_url(normalize_text(item.url))
            if not candidate or self._is_blocked_video_url(candidate):
                continue
            if not self._is_supported_platform_video_url(candidate):
                continue
            if not self._is_platform_video_detail_url(candidate):
                if not platform_page_hint:
                    platform_page_hint = candidate
                continue
            meta = await self._inspect_platform_video_metadata_safe(candidate)
            duration_ok, duration_limit, duration_value, duration_scene = (
                self._is_video_duration_acceptable_for_search(meta, query=query)
            )
            if not duration_ok:
                _ytdlp_log.info(
                    "video_skip_too_long%s | url=%s | dur=%s | limit=%s | scene=%s",
                    _tool_trace_tag(),
                    candidate[:80],
                    duration_value,
                    duration_limit,
                    duration_scene,
                )
                duration_filtered_candidates.append(
                    {
                        "url": candidate,
                        "duration": duration_value,
                        "limit": duration_limit,
                        "scene": duration_scene,
                    }
                )
                continue
            _dl_attempts += 1
            if _dl_attempts > 2:
                break
            resolved, resolve_diag = (
                await self._resolve_platform_video_safe_with_diagnostic(candidate)
            )
            if not resolved:
                if (
                    "douyin.com" in normalize_text(urlparse(candidate).netloc).lower()
                    and "fresh cookies" in normalize_text(resolve_diag).lower()
                ):
                    douyin_cookie_required = True
                    break
                continue
            return await self._build_video_result_with_analysis(
                source_url=candidate,
                resolved=resolved,
                query=query,
                meta=meta,
                extra_payload={
                    "results": [
                        {"title": r.title, "snippet": r.snippet, "url": r.url}
                        for r in results
                    ]
                },
            )

        # 二次浏览器搜索：主动构造详情页定向查询，提升无直链请求成功率。
        targeted_results = await self._search_video_with_targeted_queries(query)
        targeted_results = self._filter_safe_video_results(query, targeted_results)
        if targeted_results:
            for item in targeted_results:
                candidate = _unwrap_redirect_url(normalize_text(item.url))
                if not candidate or self._is_blocked_video_url(candidate):
                    continue
                if not self._is_supported_platform_video_url(candidate):
                    continue
                if not self._is_platform_video_detail_url(candidate):
                    continue
                meta = await self._inspect_platform_video_metadata_safe(candidate)
                duration_ok, duration_limit, duration_value, duration_scene = (
                    self._is_video_duration_acceptable_for_search(meta, query=query)
                )
                if not duration_ok:
                    _ytdlp_log.info(
                        "video_skip_too_long%s | url=%s | dur=%s | limit=%s | scene=%s",
                        _tool_trace_tag(),
                        candidate[:80],
                        duration_value,
                        duration_limit,
                        duration_scene,
                    )
                    duration_filtered_candidates.append(
                        {
                            "url": candidate,
                            "duration": duration_value,
                            "limit": duration_limit,
                            "scene": duration_scene,
                        }
                    )
                    continue
                resolved, resolve_diag = (
                    await self._resolve_platform_video_safe_with_diagnostic(candidate)
                )
                if not resolved:
                    if (
                        "douyin.com"
                        in normalize_text(urlparse(candidate).netloc).lower()
                        and "fresh cookies" in normalize_text(resolve_diag).lower()
                    ):
                        douyin_cookie_required = True
                        break
                    continue
                return await self._build_video_result_with_analysis(
                    source_url=candidate,
                    resolved=resolved,
                    query=query,
                    meta=meta,
                    extra_payload={
                        "results": [
                            {"title": r.title, "snippet": r.snippet, "url": r.url}
                            for r in targeted_results
                        ]
                    },
                )

        if douyin_cookie_required:
            return ToolResult(
                ok=False,
                tool_name="search_video",
                payload={
                    "text": "抖音视频解析需要新的浏览器 cookies，当前环境缺少可用 cookies，暂时无法直发抖音视频。你可以先发 B站/快手链接，或补充抖音 cookies 后再试。"
                },
                error="douyin_cookie_required",
            )

        video_url = ""
        for item in results:
            candidate = normalize_text(item.url)
            candidate = _unwrap_redirect_url(candidate)
            if self._is_blocked_video_url(candidate):
                continue
            if self._is_direct_video_url(candidate):
                video_url = candidate
                break

        if video_url:
            evidence = [
                {
                    "title": "视频来源",
                    "point": "已找到可发视频链接。",
                    "source": video_url,
                }
            ]
            return ToolResult(
                ok=True,
                tool_name="search_video",
                payload={
                    "mode": "video",
                    "query": query,
                    "text": f"先发你一个视频，来源：{video_url}",
                    "video_url": video_url,
                    "results": [
                        {"title": item.title, "snippet": item.snippet, "url": item.url}
                        for item in results
                    ],
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        duration_filtered_text = self._build_video_duration_filtered_text(
            query=query,
            candidates=duration_filtered_candidates,
        )
        if duration_filtered_candidates and not results and not platform_page_hint:
            return ToolResult(
                ok=False,
                tool_name="search_video",
                payload={"text": duration_filtered_text},
                error="video_result_duration_filtered",
            )

        if platform_page_hint:
            hint_text = (
                "我拿到的是平台搜索/频道页，不是具体视频详情链接，暂时没法直接转发。\n"
                "你发我抖音/快手/B站的分享链接，我就能直接解析并发到群里。"
            )
            if duration_filtered_text:
                hint_text = f"{duration_filtered_text}\n{hint_text}"
            return ToolResult(
                ok=True,
                tool_name="search_video",
                payload={
                    "mode": "video",
                    "query": query,
                    "text": hint_text,
                    "results": [
                        {"title": r.title, "snippet": r.snippet, "url": r.url}
                        for r in results
                    ],
                },
            )

        text = self._format_search_text(query, results, query_type="video")
        if duration_filtered_text:
            text = (
                f"{duration_filtered_text}\n{text}" if text else duration_filtered_text
            )
        if results:
            evidence = self._build_evidence_from_results(results)
            text = f"没拿到可直发的视频链接，我先给你来源：\n{text}"
            return ToolResult(
                ok=True,
                tool_name="search_video",
                payload={
                    "mode": "video",
                    "query": query,
                    "text": text,
                    "results": [
                        {"title": item.title, "snippet": item.snippet, "url": item.url}
                        for item in results
                    ],
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        return ToolResult(
            ok=False,
            tool_name="search_video",
            payload={
                "text": (
                    (f"{duration_filtered_text}\n" if duration_filtered_text else "")
                    + "这次没拿到可发送的视频。你可以直接发抖音/快手/B站链接给我，"
                    "我会尝试本地解析后发到群里。"
                )
            },
            error="video_result_unavailable",
        )

    async def _search_video_with_targeted_queries(
        self, query: str
    ) -> list[SearchResult]:
        target_queries = self._build_targeted_video_queries(query)
        if not target_queries:
            return []

        # 并行执行所有定向搜索，大幅减少等待时间
        async def _safe_search(q: str) -> list[SearchResult]:
            try:
                rows = await self.search_engine.search(q)
                return self._filter_and_rank_results(q, rows, query_type="video")
            except Exception:
                return []

        all_results = await asyncio.gather(*[_safe_search(q) for q in target_queries])

        merged: list[SearchResult] = []
        seen_urls: set[str] = set()
        for rows in all_results:
            for row in rows:
                key = normalize_text(row.url)
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                merged.append(row)
                if len(merged) >= 12:
                    return merged

        # DuckDuckGo site: 查询经常失败，用 Bing 视频搜索补充
        if len(merged) < 3:
            try:
                bing_rows = await self.search_engine.search_bing_videos(query, limit=5)
                for row in bing_rows:
                    key = normalize_text(row.url)
                    if not key or key in seen_urls:
                        continue
                    seen_urls.add(key)
                    merged.append(row)
                    if len(merged) >= 12:
                        return merged
            except Exception:
                pass

        return merged

    @staticmethod
    def _build_targeted_video_queries(query: str) -> list[str]:
        content = normalize_text(query)
        if not content:
            return []
        lower = content.lower()
        out: list[str] = []
        plain = re.sub(r"\s+", "", lower)

        # 只在显式参数中收窄平台，避免把普通描述词当成强约束。
        explicit_platform = ""
        m = re.search(
            r"(?:^|\s)platform\s*=\s*(bilibili|douyin|kuaishou|acfun|youtube|tencent|qq|iqiyi|youku)(?:\s|$)",
            lower,
        )
        if m:
            explicit_platform = normalize_text(m.group(1)).lower()
        elif "site:douyin.com" in plain:
            explicit_platform = "douyin"
        elif "site:bilibili.com" in plain:
            explicit_platform = "bilibili"
        elif "site:kuaishou.com" in plain:
            explicit_platform = "kuaishou"
        elif "site:acfun.cn" in plain:
            explicit_platform = "acfun"
        elif "site:youtube.com" in plain or "site:youtu.be" in plain:
            explicit_platform = "youtube"
        elif "site:v.qq.com" in plain or "site:m.v.qq.com" in plain:
            explicit_platform = "tencent"
        elif "site:iqiyi.com" in plain or "site:qiyi.com" in plain or "site:iq.com" in plain:
            explicit_platform = "iqiyi"
        elif "site:youku.com" in plain:
            explicit_platform = "youku"

        if explicit_platform == "bilibili":
            out.append(f"{content} site:bilibili.com/video")
        elif explicit_platform == "douyin":
            out.append(f"{content} site:douyin.com/video")
        elif explicit_platform == "kuaishou":
            out.append(f"{content} site:kuaishou.com/short-video")
        elif explicit_platform == "acfun":
            out.append(f"{content} site:acfun.cn/v/ac")
        elif explicit_platform == "youtube":
            out.extend([f"{content} site:youtube.com/watch", f"{content} site:youtu.be"])
        elif explicit_platform in {"tencent", "qq"}:
            out.append(f"{content} site:v.qq.com/x")
        elif explicit_platform == "iqiyi":
            out.extend([f"{content} site:iqiyi.com/v_", f"{content} site:iqiyi.com/a_", f"{content} site:iq.com/play"])
        elif explicit_platform == "youku":
            out.append(f"{content} site:youku.com/v_show")

        # 未显式指定平台时给一组通用详情页搜索
        if not out:
            out.extend(
                [
                    f"{content} site:bilibili.com/video",
                    f"{content} site:douyin.com/video",
                    f"{content} site:kuaishou.com/short-video",
                    f"{content} site:acfun.cn/v/ac",
                    f"{content} site:youtube.com/watch",
                    f"{content} site:v.qq.com/x",
                    f"{content} site:iqiyi.com/v_",
                    f"{content} site:iqiyi.com/a_",
                    f"{content} site:iq.com/play",
                ]
            )

        # 去重保序
        uniq: list[str] = []
        seen: set[str] = set()
        for q in out:
            key = normalize_text(q)
            if not key or key in seen:
                continue
            seen.add(key)
            uniq.append(key)
        return uniq

    async def _bilibili_audio_extract(
        self,
        tool_args: dict[str, Any],
        message_text: str,
        api_call: Callable[..., Awaitable[Any]] | None,
        group_id: int,
    ) -> ToolResult:
        """从 Bilibili 提取音频作为音乐回退方案。"""
        keyword = normalize_text(str(tool_args.get("keyword", ""))) or normalize_text(
            message_text
        )
        if not keyword:
            return ToolResult(
                ok=False, tool_name="bilibili_audio_extract", error="empty_keyword"
            )

        try:
            search_cfg = self._raw_config.get("search", {})
            search_engine = SearchEngine(search_cfg)
            search_query = f"site:bilibili.com {keyword}"
            preferred_results = await search_engine.search_bilibili_videos(
                keyword, limit=5
            )
            fallback_results = await search_engine.search(search_query)
            results = [*(preferred_results or []), *(fallback_results or [])]

            if not results:
                return ToolResult(
                    ok=False,
                    tool_name="bilibili_audio_extract",
                    payload={"text": f"在 B 站未找到「{keyword}」相关视频。"},
                    error="no_bilibili_results",
                )

            # 去重并保留前若干候选，避免单一结果偶发失败导致全局失败。
            dedup_results: list[SearchResult] = []
            seen_url: set[str] = set()
            for row in results:
                row_url = normalize_text(getattr(row, "url", ""))
                if not row_url:
                    continue
                if row_url in seen_url:
                    continue
                seen_url.add(row_url)
                dedup_results.append(row)
                if len(dedup_results) >= 8:
                    break

            if not dedup_results:
                return ToolResult(
                    ok=False,
                    tool_name="bilibili_audio_extract",
                    payload={"text": f"在 B 站未找到「{keyword}」可用结果。"},
                    error="no_bilibili_results",
                )

            def _candidate_urls(raw_url: str) -> list[str]:
                out: list[str] = []
                for url in (
                    normalize_text(raw_url),
                    _unwrap_redirect_url(raw_url),
                ):
                    if not url or url in out:
                        continue
                    out.append(url)
                target = out[0] if out else ""
                try:
                    parsed = urlparse(target)
                except Exception:
                    return out
                host = normalize_text(parsed.netloc).lower()
                if "bilibili.com" not in host:
                    return out
                path = normalize_text(unquote(parsed.path or ""))
                match_av = re.search(r"/(av\d+)", path, flags=re.IGNORECASE)
                if not match_av:
                    return out
                q = parse_qs(parsed.query)
                kept: dict[str, str] = {}
                for key in ("p", "t"):
                    vals = q.get(key, [])
                    if vals:
                        kept[key] = str(vals[0])
                suffix = f"?{urlencode(kept)}" if kept else ""
                normalized_av = (
                    f"https://www.bilibili.com/video/{match_av.group(1)}{suffix}"
                )
                if normalized_av not in out:
                    out.append(normalized_av)
                return out

            async def _download_audio_once(target_url: str) -> tuple[Path | None, str]:
                from yt_dlp import YoutubeDL

                digest = hashlib.sha1(
                    target_url.encode("utf-8", errors="ignore")
                ).hexdigest()[:12]
                audio_path = self._video_cache_dir / f"{digest}_audio.mp3"

                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bestaudio/best",
                    "outtmpl": str(audio_path.with_suffix("")),
                    "postprocessors": (
                        [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": "192",
                            }
                        ]
                        if self._ffmpeg_available
                        else []
                    ),
                    "socket_timeout": self._video_download_timeout_seconds,
                    "retries": 2,
                    "logger": _SilentYTDLPLogger(),
                }

                if self._video_cookies_file:
                    ydl_opts["cookiefile"] = self._video_cookies_file
                if self._video_cookies_from_browser:
                    ydl_opts["cookiesfrombrowser"] = (self._video_cookies_from_browser,)
                if self._ffmpeg_location:
                    ydl_opts["ffmpeg_location"] = self._ffmpeg_location
                _tmp_cookie_file = self._inject_platform_cookiefile(
                    ydl_opts, target_url
                )
                try:
                    while True:
                        try:
                            with YoutubeDL(ydl_opts) as ydl:
                                ydl.download([target_url])
                            break
                        except Exception as exc:
                            err_text = normalize_text(str(exc))
                            lowered = err_text.lower()
                            # 浏览器 cookie 数据库读取失败时，自动切到非 browser-cookie 重试一次。
                            if (
                                self._disable_cookie_browser_on_error(err_text)
                                or "cookies database" in lowered
                                or "cookiesfrombrowser" in lowered
                            ) and "cookiesfrombrowser" in ydl_opts:
                                _tool_log.warning(
                                    "bilibili_audio_retry_without_cookie_browser | url=%s | reason=%s",
                                    target_url,
                                    clip_text(err_text, 140),
                                )
                                ydl_opts.pop("cookiesfrombrowser", None)
                                continue
                            if "did not get any data blocks" in lowered:
                                return None, "did_not_get_any_data_blocks"
                            if (
                                "keyerror('bvid')" in lowered
                                or 'keyerror("bvid")' in lowered
                            ):
                                return None, "bilibili_bvid_keyerror"
                            return None, err_text or "download_error"
                finally:
                    if _tmp_cookie_file:
                        try:
                            os.unlink(_tmp_cookie_file)
                        except OSError:
                            pass

                # 查找生成的音频文件
                if not audio_path.exists():
                    # 可能是 .mp3.mp3 或其他后缀
                    for candidate in self._video_cache_dir.glob(f"{digest}_audio*"):
                        if candidate.suffix in [".mp3", ".m4a", ".opus", ".webm"]:
                            audio_path = candidate
                            break

                if not audio_path.exists():
                    return None, "download_failed"
                return audio_path, ""

            selected_audio_path: Path | None = None
            selected_title = keyword
            selected_url = ""
            last_error = ""

            for row in dedup_results:
                row_title = normalize_text(getattr(row, "title", "")) or keyword
                row_url = normalize_text(getattr(row, "url", ""))
                if not row_url:
                    continue
                for candidate_url in _candidate_urls(row_url):
                    _tool_log.info(
                        "bilibili_audio_extract | keyword=%s | url=%s",
                        keyword,
                        candidate_url,
                    )
                    try:
                        audio_path, err = await _download_audio_once(candidate_url)
                    except Exception as exc:
                        audio_path, err = (
                            None,
                            normalize_text(str(exc)) or "download_error",
                        )
                    if audio_path and audio_path.exists():
                        selected_audio_path = audio_path
                        selected_title = row_title
                        selected_url = candidate_url
                        break
                    if err:
                        last_error = err
                if selected_audio_path is not None:
                    break

            if selected_audio_path is None:
                _tool_log.warning(
                    "bilibili_audio_download_error | keyword=%s | last_error=%s",
                    keyword,
                    clip_text(last_error or "download_failed", 220),
                )
                detail = f"：{clip_text(last_error, 120)}" if last_error else ""
                return ToolResult(
                    ok=False,
                    tool_name="bilibili_audio_extract",
                    payload={"text": f"B 站音频下载失败{detail}"},
                    error="download_error",
                )

            # 转换为 SILK
            silk_path = None
            if (
                self._music_engine
                and self._music_engine._pilk_available
                and selected_audio_path
            ):
                silk_path = await self._music_engine._convert_to_silk(
                    selected_audio_path
                )

            payload: dict[str, Any] = {
                "text": f"已从 B 站提取音频：{selected_title}",
                "source_url": selected_url,
            }
            if silk_path and silk_path.exists():
                payload["audio_file_silk"] = str(silk_path)
                payload["audio_file"] = str(selected_audio_path)
            elif selected_audio_path and selected_audio_path.exists():
                payload["audio_file"] = str(selected_audio_path)

            return ToolResult(
                ok=True, tool_name="bilibili_audio_extract", payload=payload
            )

        except Exception as exc:
            _tool_log.warning(
                "bilibili_audio_extract_error | keyword=%s | %s", keyword, exc
            )
            return ToolResult(
                ok=False,
                tool_name="bilibili_audio_extract",
                payload={"text": f"B 站音频提取失败: {exc}"},
                error="extract_error",
            )

    def _is_risky_video_result(self, item: SearchResult) -> bool:
        merged = normalize_text(f"{item.title}\n{item.snippet}\n{item.url}").lower()
        if not merged:
            return False
        return any(keyword in merged for keyword in self._risky_video_result_keywords)

    def _filter_safe_video_results(
        self, query: str, results: list[SearchResult]
    ) -> list[SearchResult]:
        if not results:
            return []
        # 如果用户明确是违规成人请求，前面会被拦截；这里主要过滤"中性查询被误回"
        if self._is_blocked_video_text(query):
            return results
        safe: list[SearchResult] = []
        for item in results:
            if self._is_risky_video_result(item):
                continue
            safe.append(item)
        return safe

    @staticmethod
    def _build_video_evidence(
        source_url: str, meta: dict[str, Any]
    ) -> list[dict[str, str]]:
        title = normalize_text(str(meta.get("title", ""))) or "视频元数据"
        uploader = normalize_text(str(meta.get("uploader", "")))
        duration = int(meta.get("duration", 0) or 0)
        point_parts = []
        if uploader:
            point_parts.append(f"发布者：{uploader}")
        if duration > 0:
            point_parts.append(f"时长：{duration}s")
        if not point_parts:
            point_parts.append("已解析到可发送视频链接。")
        return [
            {
                "title": clip_text(title, 64),
                "point": clip_text("，".join(point_parts), 120),
                "source": normalize_text(
                    str(meta.get("webpage_url", "")) or source_url
                ),
            }
        ]

    @staticmethod
    def _extract_direct_video_url(text: str) -> str:
        match = re.search(
            r"https?://[^\s]+?\.(?:mp4|webm|mov|m4v)(?:\?[^\s]*)?",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        return match.group(0).strip()

    @staticmethod
    def _is_direct_video_url(url: str) -> bool:
        return bool(
            re.search(r"\.(?:mp4|webm|mov|m4v)(?:\?|$)", url or "", flags=re.IGNORECASE)
        )

    @staticmethod
    def _is_douyin_video_or_note_url(url: str) -> bool:
        target = normalize_text(url)
        if not target or not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return False
        try:
            parsed = urlparse(target)
        except Exception:
            return False
        host = normalize_text(parsed.netloc).lower()
        path = normalize_text(unquote(parsed.path or "")).lower()
        query = normalize_text(unquote(parsed.query or "")).lower()
        if not host:
            return False
        if "douyin.com" not in host and "iesdouyin.com" not in host:
            return False
        if host.endswith("v.douyin.com"):
            return True
        blocked_cues = ("/search", "/hot", "/discover", "/challenge", "/topic")
        if any(cue in path for cue in blocked_cues):
            return False
        if "/video/" in path or "/note/" in path or "share/video" in path:
            return True
        if any(key in query for key in ("modal_id=", "aweme_id=", "item_id=")):
            return True
        short_path = path.strip("/")
        return bool(short_path and len(short_path) >= 6)

    @staticmethod
    def _resolve_video_cache_dir(raw: str) -> Path:
        candidate = Path(str(raw or "").strip() or "storage/cache/videos")
        if candidate.is_absolute():
            return candidate
        project_root = Path(__file__).resolve().parents[1]
        return (project_root / candidate).resolve()

    @staticmethod
    def _browser_cookie_roots(browser: str) -> list[Path]:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming = Path(os.environ.get("APPDATA", ""))
        name = normalize_text(browser).lower()
        if not name:
            return []
        mapping: dict[str, list[Path]] = {
            "chrome": [local / "Google" / "Chrome" / "User Data"],
            "edge": [local / "Microsoft" / "Edge" / "User Data"],
            "brave": [local / "BraveSoftware" / "Brave-Browser" / "User Data"],
            "chromium": [local / "Chromium" / "User Data"],
            "firefox": [roaming / "Mozilla" / "Firefox" / "Profiles"],
            "opera": [roaming / "Opera Software" / "Opera Stable"],
        }
        out: list[Path] = []
        for path in mapping.get(name, []):
            if str(path):
                out.append(path)
        return out

    def _has_cookie_store_for_browser(cls, browser: str) -> bool:
        name = normalize_text(browser).lower()
        roots = cls._browser_cookie_roots(name)
        if not roots:
            return False

        def _any_exists(paths: list[Path]) -> bool:
            for path in paths:
                try:
                    if path.exists() and path.is_file():
                        return True
                except Exception:
                    continue
            return False

        for root in roots:
            if not root.exists():
                continue
            if name in {"chrome", "edge", "brave", "chromium"}:
                candidates: list[Path] = [
                    root / "Default" / "Network" / "Cookies",
                    root / "Default" / "Cookies",
                ]
                try:
                    for profile in root.glob("Profile *"):
                        candidates.append(profile / "Network" / "Cookies")
                        candidates.append(profile / "Cookies")
                except Exception:
                    pass
                if _any_exists(candidates):
                    return True
            elif name == "firefox":
                try:
                    for profile in root.glob("*.default*"):
                        if (profile / "cookies.sqlite").exists():
                            return True
                    for profile in root.iterdir():
                        if profile.is_dir() and (profile / "cookies.sqlite").exists():
                            return True
                except Exception:
                    continue
            elif name == "opera":
                candidates = [root / "Network" / "Cookies", root / "Cookies"]
                if _any_exists(candidates):
                    return True
        return False

    def _pick_available_cookie_browser(cls) -> str:
        # Windows 常见可用顺序：Edge -> Chrome -> Brave -> Chromium -> Firefox -> Opera
        for browser in ("edge", "chrome", "brave", "chromium", "firefox", "opera"):
            if cls._has_cookie_store_for_browser(browser):
                return browser
        return ""

    def _resolve_video_cookies_from_browser(self, raw: str) -> str:
        value = normalize_text(raw).lower()
        if value in {"", "auto", "default"}:
            chosen = self._pick_available_cookie_browser()
            if chosen:
                _tool_log.info("video_cookie_browser_auto | browser=%s", chosen)
            return chosen
        if value in {"none", "off", "false", "disabled", "disable"}:
            return ""

        base = value.split(":", 1)[0]
        if self._has_cookie_store_for_browser(base):
            return value

        fallback = self._pick_available_cookie_browser()
        if fallback and fallback != base:
            _tool_log.warning(
                "video_cookie_browser_unavailable | requested=%s | fallback=%s",
                value,
                fallback,
            )
            if ":" in value:
                return value.replace(base, fallback, 1)
            return fallback

        _tool_log.warning(
            "video_cookie_browser_unavailable | requested=%s | disabled=true", value
        )
        return ""

    def _disable_cookie_browser_on_error(self, error_message: str) -> bool:
        """检测到 cookie 错误时禁用浏览器自动提取 cookiesfrombrowser"""
        if not self._video_cookies_from_browser:
            return False
        lower = normalize_text(error_message).lower()
        if not lower:
            return False
        if (
            ("could not find" in lower and "cookies database" in lower)
            or ("could not copy" in lower and "cookie database" in lower)
            or "cookiesfrombrowser" in lower
        ):
            _tool_log.warning(
                "video_cookie_browser_disabled_runtime | browser=%s | reason=%s",
                self._video_cookies_from_browser,
                clip_text(normalize_text(error_message), 120),
            )
            self._video_cookies_from_browser = ""
            return True
        return False


    def _build_bilibili_cookie(self) -> str:
        parts: list[str] = []
        if self._bilibili_sessdata:
            parts.append(f"SESSDATA={self._bilibili_sessdata}")
        if self._bilibili_jct:
            parts.append(f"bili_jct={self._bilibili_jct}")
        if parts:
            # 常见默认参数，提升兼容性。
            parts.extend(["CURRENT_FNVAL=4048", "CURRENT_QUALITY=80"])
        return "; ".join(parts)

    def _inject_platform_cookiefile(
        self, options: dict[str, Any], source_url: str
    ) -> str:
        """
        Inject platform-specific cookie file for yt-dlp request.
        Returns a temporary cookiefile path that caller must clean up.
        """
        if not isinstance(options, dict):
            return ""
        try:
            host = normalize_text(urlparse(source_url).netloc).lower()
        except Exception:
            host = ""
        if not host:
            return ""

        is_bilibili = "bilibili.com" in host or host.endswith("b23.tv")
        is_douyin = "douyin.com" in host or "iesdouyin.com" in host
        is_kuaishou = "kuaishou.com" in host or "chenzhongtech.com" in host

        tmp_cookie_file = ""
        if is_bilibili and not options.get("cookiefile") and self._bilibili_cookie:
            tmp_cookie_file = _write_netscape_cookie_file(
                self._bilibili_cookie, "bilibili.com"
            )
            if tmp_cookie_file:
                options["cookiefile"] = tmp_cookie_file
                # Prefer explicit cookiefile; avoid browser DB copy failures.
                options.pop("cookiesfrombrowser", None)
        elif (
            is_douyin
            and not options.get("cookiefile")
            and self._douyin_cookie
            and not self._video_cookies_from_browser
        ):
            # Douyin prefers cookiesfrombrowser (fresher tokens).
            tmp_cookie_file = _write_netscape_cookie_file(
                self._douyin_cookie, "douyin.com"
            )
            if tmp_cookie_file:
                options["cookiefile"] = tmp_cookie_file
        elif is_kuaishou and not options.get("cookiefile") and self._kuaishou_cookie:
            tmp_cookie_file = _write_netscape_cookie_file(
                self._kuaishou_cookie, "kuaishou.com"
            )
            if tmp_cookie_file:
                options["cookiefile"] = tmp_cookie_file
                options.pop("cookiesfrombrowser", None)
        return tmp_cookie_file

    def _is_supported_platform_video_url(self, url: str) -> bool:
        target = normalize_text(url)
        if not target:
            return False
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return False
        try:
            host = normalize_text(urlparse(target).netloc).lower()
        except Exception:
            return False
        if not host:
            return False
        return any(
            host == domain or host.endswith(f".{domain}")
            for domain in self._platform_video_domains
        )

    def _is_platform_video_detail_url(self, url: str) -> bool:
        target = normalize_text(url)
        if not target:
            return False
        if not self._is_supported_platform_video_url(target):
            return False
        try:
            parsed = urlparse(target)
        except Exception:
            return False
        host = normalize_text(parsed.netloc).lower()
        path = normalize_text(unquote(parsed.path or "")).lower()
        query = normalize_text(unquote(parsed.query or "")).lower()

        if host.endswith("b23.tv"):
            return path not in {"", "/"}

        if "bilibili.com" in host:
            if path.startswith("/video/") or path.startswith("/bangumi/play/"):
                return True
            if re.match(r"^/(bv|av)[a-z0-9]+", path, flags=re.IGNORECASE):
                return True
            return False

        if "douyin.com" in host or "iesdouyin.com" in host:
            blocked_cues = ("/search", "/hot", "/discover", "/challenge", "/topic")
            if any(cue in path for cue in blocked_cues):
                return False
            if "/video/" in path or "/note/" in path or "share/video" in path:
                return True
            short_path = path.strip("/")
            if short_path and len(short_path) >= 6:
                return True
            return False

        if "kuaishou.com" in host or "chenzhongtech.com" in host:
            blocked_cues = (
                "/search",
                "/hot",
                "/channel",
                "/feed",
                "/new-reco",
                "/explore",
                "/live",
                "/profile",
            )
            if any(cue in path for cue in blocked_cues):
                return False
            detail_cues = ("/short-video/", "/photo/", "/f/", "/s/", "/video/")
            if any(cue in path for cue in detail_cues):
                return True
            if "photoid=" in query or "shareid=" in query:
                return True
            short_path = path.strip("/")
            if short_path and len(short_path) >= 6:
                return True
            return False

        if "acfun.cn" in host or "acfun.com" in host:
            blocked_cues = ("/search", "/rank", "/a/")
            if any(cue in path for cue in blocked_cues):
                return False
            if re.search(r"/v/ac\d+", path, flags=re.IGNORECASE):
                return True
            if re.search(r"(?:^|&)ac=\d+", query, flags=re.IGNORECASE):
                return True
            if re.search(r"/bangumi/aa\d+", path, flags=re.IGNORECASE):
                return True
            return False

        if "youku.com" in host:
            # 优酷视频详情页: /v_show/id_xxx.html
            if "/v_show/" in path:
                return True
            if re.search(r"/id_[a-zA-Z0-9]+", path):
                return True
            return False

        if "youtube.com" in host:
            if path.startswith("/watch") and re.search(r"(?:^|&)v=[a-z0-9_-]{6,}", query, flags=re.IGNORECASE):
                return True
            if path.startswith("/shorts/") or path.startswith("/embed/"):
                return bool(path.strip("/").split("/")[-1])
            return False

        if "youtu.be" in host:
            return bool(path.strip("/"))

        if "iqiyi.com" in host or "qiyi.com" in host or "iq.com" in host:
            blocked_cues = ("/search", "/so/", "/playlist", "/album")
            if any(cue in path for cue in blocked_cues):
                return False
            if "iq.com" in host and path.startswith("/play/"):
                return True
            if re.search(r"/(?:v|w)_[a-z0-9]+", path, flags=re.IGNORECASE):
                return True
            if path.endswith(".html") and re.search(r"/(?:v|w|a)_", path, flags=re.IGNORECASE):
                return True
            if re.search(r"(?:^|&)(?:tvid|vid)=[a-z0-9_-]+", query, flags=re.IGNORECASE):
                return True
            return False

        if "v.qq.com" in host or ("qq.com" in host and "/x/" in path):
            # 腾讯视频详情页: /x/cover/xxx/xxx.html 或 /x/page/xxx.html
            if "/x/cover/" in path or "/x/page/" in path:
                return True
            if re.search(r"/[a-z]\d{10}", path):
                return True
            return False

        return False

    def _is_blocked_video_text(self, text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        return any(keyword in content for keyword in self._blocked_video_text_keywords)

    def _is_blocked_video_url(self, url: str) -> bool:
        target = normalize_text(url).lower()
        if not target:
            return False
        parsed = urlparse(target)
        host = normalize_text(parsed.netloc).lower()
        path = normalize_text(unquote(parsed.path or "")).lower()
        query = normalize_text(unquote(parsed.query or "")).lower()
        merged = f"{host} {path} {query}"
        return any(
            self._url_contains_keyword(merged, keyword)
            for keyword in self._blocked_video_domain_keywords
        )

    async def _resolve_platform_video_safe(self, source_url: str) -> str:
        resolved, _ = await self._resolve_platform_video_safe_with_diagnostic(
            source_url
        )
        return resolved

    async def _resolve_platform_video_safe_with_diagnostic(
        self, source_url: str
    ) -> tuple[str, str]:
        url = _unwrap_redirect_url(source_url)
        try:
            resolved = await asyncio.wait_for(
                self._resolve_platform_video(url),
                timeout=float(self._video_resolve_total_timeout_seconds),
            )
            if resolved:
                self._last_video_resolve_diagnostic.pop(url, None)
                return resolved, "ok"
            diag = self._last_video_resolve_diagnostic.pop(url, "resolve_failed")
            return "", diag or "resolve_failed"
        except TimeoutError:
            self._last_video_resolve_diagnostic[url] = "resolve_timeout"
            return "", "resolve_timeout"
        except Exception:
            self._last_video_resolve_diagnostic[url] = "resolve_exception"
            return "", "resolve_exception"

    def _build_video_resolve_failed_text(self, diagnostic: str) -> str:
        code = normalize_text(diagnostic).lower()
        if "fresh cookies" in code:
            return (
                "这条抖音视频解析需要新的浏览器 cookies。"
                "你可以先执行 `/yuki cookie douyin edge force` 刷新，"
                "或直接发抖音 App 的分享短链（v.douyin.com）再试。"
            )
        if code == "resolve_timeout":
            return "这条视频解析超时了，可能是平台限流或链接失效。你可以稍后重试，或者发一个新的分享链接给我。"
        if code == "format_unavailable":
            return "这条视频当前可用格式不稳定（平台返回格式不可用）。你可以换一个清晰度或换条链接再试。"
        if "412" in code:
            return "B站限流了（412），稍等一会儿再试就好。也可以换个链接。"
        if code == "video_no_audio_all_formats":
            return "这条视频解析到的都是无音轨版本（发送后会没声音），我已拦截。你换个分享链接我再试。"
        if code == "parse_api_failed":
            return "解析服务这次没拿到可用直链。你可以重发原视频链接，我会继续尝试本地解析。"
        if code == "resolver_disabled":
            return "当前环境关闭了视频解析能力，请联系管理员开启。"
        if code == "ytdlp_missing":
            return "当前环境缺少视频解析依赖（yt-dlp），暂时无法解析这条链接。"
        if (
            code.startswith("cookie_unavailable:")
            or "cookies database" in code
            or "cookiesfrombrowser" in code
        ):
            return (
                "这次解析卡在浏览器 Cookie 读取上了。"
                "我会自动切换到内置 Cookie 方案重试；"
                "如果仍失败，你可以先执行 `/yuki cookie bilibili edge force` 刷新后再试。"
            )
        return "这条视频这次没解析出来。你换个链接我继续试。"

    async def _resolve_platform_video(self, source_url: str) -> str:
        url = _unwrap_redirect_url(source_url)
        self._last_video_resolve_diagnostic.pop(url, None)
        if not self._video_resolver_enable:
            self._last_video_resolve_diagnostic[url] = "resolver_disabled"
            return ""
        if not self._is_supported_platform_video_url(url):
            self._last_video_resolve_diagnostic[url] = "unsupported_video_platform"
            return ""
        if not self._is_platform_video_detail_url(url):
            self._last_video_resolve_diagnostic[url] = "video_detail_url_required"
            return ""

        if self._video_parse_enable and self._video_parse_api_base:
            parsed_url = await self._resolve_platform_video_via_parse_api(url)
            if parsed_url:
                return parsed_url
            self._last_video_resolve_diagnostic[url] = "parse_api_failed"

        # 抖音优先走分享页提取（不需要 cookie / 签名）
        host = normalize_text(urlparse(url).netloc).lower()
        is_douyin = "douyin.com" in host or "iesdouyin.com" in host
        if is_douyin:
            self._cleanup_video_cache()
            try:
                dy_path = await self._download_douyin_via_share_page(url)
                if dy_path:
                    return str(dy_path.resolve())
            except Exception as exc:
                _ytdlp_log.warning("douyin_share_error: %s", str(exc)[:300])
            # 分享页失败不立即返回，继续尝试 yt-dlp
            _ytdlp_log.info("douyin_share fallback failed, trying yt-dlp")

        if YoutubeDL is not None:
            if self._video_prefer_direct_stream and self._allow_platform_direct_stream(
                url
            ):
                direct_url = await asyncio.to_thread(
                    self._extract_platform_video_direct_url_sync, url
                )
                if direct_url and self._is_direct_video_url(direct_url):
                    return direct_url

            self._cleanup_video_cache()

            # 使用混合解析器（bilix优先用于B站）
            if self._hybrid_resolver:
                try:
                    downloaded_path = await self._hybrid_resolver.download_video(url)
                    if downloaded_path:
                        return str(downloaded_path.resolve())
                except Exception as e:
                    _ytdlp_log.warning("hybrid_resolver_error | error=%s", str(e)[:200])

            # fallback到原有的yt-dlp方法
            downloaded_path = await asyncio.to_thread(
                self._download_platform_video_sync, url
            )
            if downloaded_path:
                return str(downloaded_path.resolve())
            download_error = normalize_text(
                self._last_video_download_error.pop(url, "")
            )
            if download_error == "video_no_audio_all_formats":
                self._last_video_resolve_diagnostic[url] = "video_no_audio_all_formats"
            elif "Requested format is not available" in download_error:
                self._last_video_resolve_diagnostic[url] = "format_unavailable"
            elif (
                "cookies database" in download_error.lower()
                or "cookiesfrombrowser" in download_error.lower()
            ):
                self._last_video_resolve_diagnostic[url] = (
                    f"cookie_unavailable:{clip_text(download_error, 120)}"
                )
            elif download_error:
                self._last_video_resolve_diagnostic[url] = (
                    f"ytdlp:{clip_text(download_error, 120)}"
                )
        else:
            self._last_video_resolve_diagnostic[url] = "ytdlp_missing"

        # 兜底：可选接入 parse-video 这类本地解析服务，拿直链再转发
        parsed_url = await self._resolve_platform_video_via_parse_api(url)
        if parsed_url:
            return parsed_url
        if self._last_video_resolve_diagnostic.get(url, "").startswith("ytdlp:"):
            return ""
        self._last_video_resolve_diagnostic[url] = (
            self._last_video_resolve_diagnostic.get(url, "resolve_failed")
        )
        return ""

    @staticmethod
    def _allow_platform_direct_stream(url: str) -> bool:
        """
        平台详情页提取到的 CDN 直链通常依赖 Referer/Cookie。
        直接把这类直链丢给 QQ 客户端，容易拿到占位图或损坏片段。
        这里默认禁用"平台页直链直发"，统一走本地静默下载后再发送。
        """
        _ = url
        return False

    def _extract_platform_video_direct_url_sync(self, source_url: str) -> str:
        if YoutubeDL is None:
            return ""
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self._video_download_timeout_seconds,
            "format": "b[ext=mp4]/bv*+ba/b/best[ext=mp4]/best",
            "http_headers": self._http_headers,
            "logger": _SilentYTDLPLogger(),
        }
        if self._video_cookies_file:
            options["cookiefile"] = self._video_cookies_file
        if self._video_cookies_from_browser:
            options["cookiesfrombrowser"] = (self._video_cookies_from_browser,)
        _tmp_cookie_file = self._inject_platform_cookiefile(options, source_url)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(source_url, download=False)
        except Exception as exc:
            self._disable_cookie_browser_on_error(str(exc))
            return ""
        finally:
            if _tmp_cookie_file:
                try:
                    os.unlink(_tmp_cookie_file)
                except OSError:
                    pass
        if not isinstance(info, dict):
            return ""

        candidates: list[str] = []

        def add(value: Any) -> None:
            if not isinstance(value, str):
                return
            url = _unwrap_redirect_url(normalize_text(value))
            if not re.match(r"^https?://", url, flags=re.IGNORECASE):
                return
            if self._is_blocked_video_url(url):
                return
            if ".m3u8" in url.lower():
                return
            candidates.append(url)

        add(info.get("url"))
        for key in ("requested_formats", "formats"):
            rows = info.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                add(row.get("url"))

        for item in candidates:
            if self._is_direct_video_url(item):
                return item
        return candidates[0] if candidates else ""

    async def _resolve_platform_video_via_parse_api(self, source_url: str) -> str:
        if not self._video_parse_enable or not self._video_parse_api_base:
            return ""
        endpoint = f"{self._video_parse_api_base}/video/share/url/parse"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    float(self._video_parse_timeout_seconds), connect=6.0
                ),
                follow_redirects=True,
                headers=self._http_headers,
            ) as client:
                resp = await client.get(endpoint, params={"url": source_url})
        except Exception:
            return ""
        if resp.status_code >= 400:
            return ""
        try:
            payload = resp.json()
        except Exception:
            return ""
        # 部分解析 API 返回 HTTP 200 但 JSON body 含错误码
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and code not in (0, 200, "0", "200"):
                return ""
        return self._extract_video_url_from_parse_payload(payload)

    def _extract_video_url_from_parse_payload(self, payload: Any) -> str:
        if payload is None:
            return ""

        candidates: list[str] = []

        def add_candidate(value: Any) -> None:
            if isinstance(value, str):
                url = _unwrap_redirect_url(normalize_text(value))
                if re.match(
                    r"^https?://", url, flags=re.IGNORECASE
                ) and not self._is_blocked_video_url(url):
                    candidates.append(url)
            elif isinstance(value, list):
                for item in value:
                    add_candidate(item)
            elif isinstance(value, dict):
                preferred_keys = (
                    "video_url",
                    "url",
                    "play_url",
                    "playAddr",
                    "play_addr",
                    "nwm_video_url",
                    "wm_video_url",
                    "download_url",
                    "video",
                    "videos",
                    "data",
                    "result",
                )
                for key in preferred_keys:
                    if key in value:
                        add_candidate(value.get(key))
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        add_candidate(item)

        add_candidate(payload)
        if not candidates:
            return ""

        # 优先带视频扩展名的直链，其次返回第一个 http 链接交由发送端判定
        for item in candidates:
            if self._is_direct_video_url(item):
                return item
        return candidates[0]

    def _download_platform_video_sync(self, source_url: str) -> Path | None:
        if YoutubeDL is None:
            return None

        digest = hashlib.sha1(source_url.encode("utf-8", errors="ignore")).hexdigest()[
            :12
        ]
        output_template = str(self._video_cache_dir / f"{digest}_%(id)s.%(ext)s")
        host = normalize_text(urlparse(source_url).netloc).lower()
        is_douyin = "douyin.com" in host or "iesdouyin.com" in host
        is_kuaishou = "kuaishou.com" in host or "chenzhongtech.com" in host
        is_youku = "youku.com" in host
        is_acfun = "acfun.cn" in host or "acfun.com" in host
        is_tencent = "v.qq.com" in host or (
            "qq.com" in host
            and "/x/" in normalize_text(urlparse(source_url).path).lower()
        )
        self._last_video_download_error.pop(source_url, None)
        last_error = ""
        common_options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self._video_download_timeout_seconds,
            "retries": 3,
            "extractor_retries": 2,
            "fragment_retries": 3,
            "skip_unavailable_fragments": True,
            "outtmpl": output_template,
            "http_headers": self._http_headers,
            "logger": _SilentYTDLPLogger(),
        }
        # B站: 添加 try_look=1 参数，无需登录即可获取 720p/1080p
        if "bilibili.com" in host or host.endswith("b23.tv"):
            common_options["extractor_args"] = {"BiliBili": {"try_look": ["1"]}}
            bili_headers = dict(common_options.get("http_headers") or {})
            bili_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            bili_headers["Referer"] = "https://www.bilibili.com/"
            bili_headers["Cookie"] = "BUVID3=infoc;"
            common_options["http_headers"] = bili_headers
        if self._ffmpeg_available:
            common_options["merge_output_format"] = "mp4"
            if self._ffmpeg_location:
                # imageio-ffmpeg 的二进制名不是 ffmpeg.exe，显式指定给 yt-dlp 才能完成音视频合并。
                common_options["ffmpeg_location"] = self._ffmpeg_location
        if self._video_cookies_file:
            common_options["cookiefile"] = self._video_cookies_file
        if self._video_cookies_from_browser:
            common_options["cookiesfrombrowser"] = (self._video_cookies_from_browser,)
        _tmp_cookie_file = self._inject_platform_cookiefile(common_options, source_url)

        if "bilibili.com" in host or host.endswith("b23.tv"):
            # B站经常是分段流；无 ffmpeg 时优先单文件中低清晰度，保证可发
            # 优先 h264(avc) 编码，避免 av1 导致 NapCat 缩略图生成失败。
            # 放宽格式限制，增加更多 fallback 避免 "Requested format is not available"
            format_candidates: list[str] = []
            if self._ffmpeg_available:
                format_candidates.extend(
                    [
                        # 优先 avc 720p 带音频
                        "bv*[vcodec^=avc][height<=720]+ba/bv*[vcodec^=avc]+ba",
                        # 放宽到任意编码 720p 带音频
                        "bv*[height<=720]+ba/bv*+ba",
                        # 单文件带音频
                        "best[vcodec^=avc][acodec!=none][height<=720][ext=mp4]/best[vcodec^=avc][acodec!=none][ext=mp4]",
                        "best[acodec!=none][height<=720][ext=mp4]/best[acodec!=none][ext=mp4]",
                        # 任意带音频的格式
                        "bv*[vcodec!=none][acodec!=none][height<=720][ext=mp4]/bv*[vcodec!=none][acodec!=none]/b[ext=mp4]/b",
                        "bv*[vcodec!=none]+ba/bv*+ba/b",
                    ]
                )
            else:
                # 无 ffmpeg 时，先尝试单文件格式，如果都不可用则下载纯视频流（无音频）
                format_candidates.extend(
                    [
                        "bv*[vcodec!=none][acodec!=none][height<=480]/bv*[vcodec!=none][acodec!=none][height<=360]",
                        "bv*[vcodec!=none][acodec!=none][height<=720]/bv*[vcodec!=none][acodec!=none]",
                        "b[ext=mp4]/b",
                        # 如果上面都失败，下载纯视频流（无音频但至少有画面）
                        "bv*[vcodec^=avc][height<=480]/bv*[vcodec^=avc][height<=360]",
                        "bv*[vcodec^=avc][height<=720]/bv*[vcodec^=avc]",
                        "bv*[height<=480]/bv*[height<=720]/bv*",
                        # 最后的fallback：下载任何可用的视频流
                        "bestvideo[height<=720]/bestvideo",
                    ]
                )
            if self._ffmpeg_available:
                # 有 ffmpeg 时也添加纯视频流作为最后的fallback
                format_candidates.extend(
                    [
                        "bv*[vcodec!=none][height<=480]/bv*[vcodec!=none][height<=360]",
                        "bv*[vcodec!=none][height<=720]/bv*[vcodec!=none]",
                        "b[ext=mp4]/b",
                        "bestvideo[height<=720]/bestvideo",
                    ]
                )
        elif is_douyin or is_kuaishou:
            # 抖音/快手：优先 h264 720p，回退到 best mp4
            # 注意：快手目前 yt-dlp 官方不支持，可能需要第三方插件
            format_candidates = [
                f"best[vcodec^=avc][height<=720][ext=mp4]/best[vcodec^=avc][ext=mp4]",
                f"best[ext=mp4][filesize<{self._video_download_max_mb}M]/best[ext=mp4]",
                "best[ext=mp4]",
                "best",
            ]
        elif is_youku:
            # 优酷：优先 mp4 格式，限制文件大小
            format_candidates = [
                f"best[ext=mp4][height<=720][filesize<{self._video_download_max_mb}M]/best[ext=mp4][height<=720]",
                f"best[ext=mp4][filesize<{self._video_download_max_mb}M]/best[ext=mp4]",
                "best[ext=mp4]",
                "best",
            ]
        elif is_acfun:
            # ACFUN：优先 mp4 格式
            format_candidates = [
                f"best[ext=mp4][height<=720]/best[ext=mp4]",
                "best[ext=mp4]",
                "best",
            ]
        elif is_tencent:
            # 腾讯视频：优先 mp4 格式，限制文件大小
            format_candidates = [
                f"best[ext=mp4][height<=720][filesize<{self._video_download_max_mb}M]/best[ext=mp4][height<=720]",
                f"best[ext=mp4][filesize<{self._video_download_max_mb}M]/best[ext=mp4]",
                "best[ext=mp4]",
                "best",
            ]
        else:
            format_candidates = [
                f"best[ext=mp4][filesize<{self._video_download_max_mb}M]/best[ext=mp4]",
                "best[ext=mp4]",
            ]

        _ytdlp_log.info(
            "video_download%s | ffmpeg=%s | formats=%d | url=%s",
            _tool_trace_tag(),
            self._ffmpeg_available,
            len(format_candidates),
            source_url[:60],
        )
        require_audio = bool(
            "bilibili.com" in host
            or host.endswith("b23.tv")
            or is_douyin
            or is_kuaishou
            or is_youku
            or is_acfun
            or is_tencent
        )
        no_audio_fallback: Path | None = None
        try:
            for fmt in format_candidates:
                options = dict(common_options)
                options["format"] = fmt
                info: dict[str, Any] | None = None
                try:
                    with YoutubeDL(options) as ydl:
                        info = ydl.extract_info(source_url, download=True)
                        if isinstance(info, dict):
                            requested = info.get("requested_downloads", [])
                            if isinstance(requested, list) and requested:
                                first = requested[0]
                                if isinstance(first, dict):
                                    maybe = normalize_text(
                                        str(first.get("filepath", ""))
                                    )
                                    if maybe:
                                        path = Path(maybe)
                                        if path.exists():
                                            if self._is_video_size_ok(
                                                path
                                            ) and self._is_video_file_path(path):
                                                if (
                                                    require_audio
                                                    and not self._video_has_audio_stream(
                                                        path
                                                    )
                                                ):
                                                    _ytdlp_log.warning(
                                                        "video_download_no_audio%s | fmt=%s | path=%s | retry_next_format",
                                                        _tool_trace_tag(),
                                                        fmt[:40],
                                                        path.name,
                                                    )
                                                    if no_audio_fallback is None:
                                                        no_audio_fallback = path
                                                    else:
                                                        self._safe_unlink(path)
                                                    continue
                                                self._last_video_download_error.pop(
                                                    source_url, None
                                                )
                                                _ytdlp_log.info(
                                                    "video_download_ok%s | fmt=%s | path=%s | size=%d",
                                                    _tool_trace_tag(),
                                                    fmt[:40],
                                                    path.name,
                                                    path.stat().st_size,
                                                )
                                                return path
                                            self._safe_unlink(path)
                                            continue
                        prepared = normalize_text(str(ydl.prepare_filename(info or {})))
                        if prepared:
                            prepared_path = Path(prepared)
                            if prepared_path.exists():
                                if self._is_video_size_ok(
                                    prepared_path
                                ) and self._is_video_file_path(prepared_path):
                                    if (
                                        require_audio
                                        and not self._video_has_audio_stream(
                                            prepared_path
                                        )
                                    ):
                                        _ytdlp_log.warning(
                                            "video_download_no_audio%s | fmt=%s | path=%s | retry_next_format",
                                            _tool_trace_tag(),
                                            fmt[:40],
                                            prepared_path.name,
                                        )
                                        if no_audio_fallback is None:
                                            no_audio_fallback = prepared_path
                                        else:
                                            self._safe_unlink(prepared_path)
                                        continue
                                    self._last_video_download_error.pop(
                                        source_url, None
                                    )
                                    return prepared_path
                                self._safe_unlink(prepared_path)
                                continue
                except Exception as exc:
                    last_error = normalize_text(str(exc))
                    if self._disable_cookie_browser_on_error(last_error):
                        common_options.pop("cookiesfrombrowser", None)
                    if "fresh cookies" in last_error.lower():
                        _ytdlp_log.warning(
                            "video_download_cookie_required%s | url=%s",
                            _tool_trace_tag(),
                            source_url[:80],
                        )
                        break
                    # 412 Precondition Failed: B站限流，等待后重试下一个格式
                    if "412" in last_error:
                        _ytdlp_log.warning(
                            "video_download_412_throttled%s | url=%s | waiting",
                            _tool_trace_tag(),
                            source_url[:80],
                        )
                        import time

                        time.sleep(2)
                    continue

            fallback = self._pick_downloaded_video_fallback(digest)
            if fallback is not None:
                if require_audio and not self._video_has_audio_stream(fallback):
                    _ytdlp_log.warning(
                        "video_download_fallback_no_audio%s | path=%s",
                        _tool_trace_tag(),
                        fallback.name,
                    )
                else:
                    self._last_video_download_error.pop(source_url, None)
                    return fallback
            if no_audio_fallback is not None and no_audio_fallback.exists():
                if self._video_allow_silent_fallback:
                    _ytdlp_log.warning(
                        "video_download_no_audio_all_formats%s | return=%s | allow_silent=true",
                        _tool_trace_tag(),
                        no_audio_fallback.name,
                    )
                    self._last_video_download_error.pop(source_url, None)
                    return no_audio_fallback
                _ytdlp_log.warning(
                    "video_download_no_audio_all_formats%s | drop=%s | allow_silent=false",
                    _tool_trace_tag(),
                    no_audio_fallback.name,
                )
                self._safe_unlink(no_audio_fallback)
                self._last_video_download_error[source_url] = (
                    "video_no_audio_all_formats"
                )
                return None
            if last_error:
                self._last_video_download_error[source_url] = last_error
            return None
        finally:
            if _tmp_cookie_file:
                try:
                    os.unlink(_tmp_cookie_file)
                except OSError:
                    pass

    def _pick_downloaded_video_fallback(self, digest: str) -> Path | None:
        candidates = sorted(
            self._video_cache_dir.glob(f"{digest}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            if not path.is_file():
                continue
            lower_name = path.name.lower()
            if (
                lower_name.endswith(".qq.mp4")
                or ".qq." in lower_name
                or lower_name.endswith(".thumb.jpg")
            ):
                continue
            if not self._is_video_file_path(path):
                continue
            if self._is_video_size_ok(path):
                return path
            self._safe_unlink(path)
        return None

    async def _download_douyin_via_share_page(self, source_url: str) -> Path | None:
        """
        通过移动端分享页下载抖音视频（无需 cookie / 签名）。
        流程：短链接 → iesdouyin 分享页 → 提取 video_id → 多 CDN 回退下载
        """
        try:
            video_id, aweme_id = await self._extract_douyin_video_id(source_url)
        except Exception as exc:
            _ytdlp_log.warning(
                "douyin_share: extract video_id failed: %s", str(exc)[:200]
            )
            return None
        if not video_id:
            _ytdlp_log.warning("douyin_share: no video_id from %s", source_url[:80])
            return None
        _ytdlp_log.info(
            "douyin_share: video_id=%s aweme_id=%s", video_id, aweme_id or "?"
        )

        digest = hashlib.sha1(source_url.encode("utf-8", errors="ignore")).hexdigest()[
            :12
        ]
        tag = aweme_id or video_id
        out_path = self._video_cache_dir / f"{digest}_{tag}.mp4"

        # 多 CDN 回退下载，增加重试
        host_timeout = max(8.0, min(float(self._video_download_timeout_seconds), 20.0))
        for host in self._DOUYIN_CDN_HOSTS:
            for ratio in ("720p", "540p", "480p"):
                video_url = f"https://{host}/aweme/v1/play/?video_id={video_id}&ratio={ratio}&line=0"
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(host_timeout, connect=8.0),
                        follow_redirects=True,
                        headers={
                            "User-Agent": self._DOUYIN_MOBILE_UA,
                            "Referer": "https://www.douyin.com/",
                            "Accept": "*/*",
                        },
                    ) as client:
                        resp = await client.get(video_url)
                        resp.raise_for_status()
                        # CDN 可能返回 200 + HTML 错误页，检查 Content-Type
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if "text/html" in ctype or "text/plain" in ctype:
                            _ytdlp_log.warning(
                                "douyin_share: %s returned non-video content-type: %s",
                                host,
                                ctype[:60],
                            )
                            break  # 这个 host 不行，换下一个
                        content = resp.content
                        if len(content) < 4096:
                            _ytdlp_log.warning(
                                "douyin_share: %s content too small (%d bytes)",
                                host,
                                len(content),
                            )
                            break
                        out_path.write_bytes(content)
                        if self._is_video_size_ok(
                            out_path
                        ) and self._is_video_file_path(out_path):
                            _ytdlp_log.info(
                                "douyin_share_ok | host=%s | ratio=%s | path=%s | size=%d",
                                host,
                                ratio,
                                out_path.name,
                                out_path.stat().st_size,
                            )
                            return out_path
                        self._safe_unlink(out_path)
                except Exception as exc:
                    _ytdlp_log.warning(
                        "douyin_share: %s/%s download failed: %s",
                        host,
                        ratio,
                        str(exc)[:200],
                    )
                    continue

        return None

    async def _extract_douyin_video_id(self, source_url: str) -> tuple[str, str]:
        """
        从抖音 URL 提取 video_id（用于构造直链）和 aweme_id。
        返回 (video_id, aweme_id)。
        """

        def _extract_video_id_from_text(
            text: str, final_url: str, aweme_id_hint: str
        ) -> tuple[str, str]:
            aweme_local = aweme_id_hint or self._extract_douyin_aweme_id(final_url)

            # 先从 URL query 中提取 video_id
            try:
                qs = parse_qs(urlparse(final_url).query)
                for item in qs.get("video_id", []):
                    candidate = normalize_text(str(item))
                    if self._is_valid_douyin_video_id(candidate):
                        return candidate, aweme_local
            except Exception:
                pass

            # 从 HTML 里提取 play_addr.uri / video_id
            patterns = (
                r'"play_addr"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
                r'"uri"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
                r'"video_id"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
                r"video_id=([A-Za-z0-9_-]{8,80})",
            )
            for pattern in patterns:
                for raw in re.findall(pattern, text):
                    candidate = normalize_text(unquote(str(raw)))
                    if self._is_valid_douyin_video_id(candidate):
                        return candidate, aweme_local
            return "", aweme_local

        # 1) 先尝试从 URL 提取 aweme_id
        aweme_id = self._extract_douyin_aweme_id(source_url)

        # 2) 用移动端 UA 访问，跟踪重定向到 iesdouyin 分享页
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers={
                "User-Agent": self._DOUYIN_MOBILE_UA,
                "Referer": "https://www.douyin.com/",
            },
        ) as client:
            # 2.1) 如果已有 aweme_id，先直连分享页（通常比 douyin 主站链接更稳定）
            if aweme_id:
                try:
                    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
                    share_resp = await client.get(share_url)
                    vid, aweme_id = _extract_video_id_from_text(
                        share_resp.text, str(share_resp.url), aweme_id
                    )
                    if vid:
                        return vid, aweme_id
                except Exception:
                    pass

            resp = await client.get(source_url)
            final_url = str(resp.url)
            html = resp.text

            # 从最终 URL 提取 aweme_id（如果之前没拿到）
            if not aweme_id:
                aweme_id = self._extract_douyin_aweme_id(final_url)

            # 3) 从最终 URL + HTML 提取 video_id
            vid, aweme_id = _extract_video_id_from_text(html, final_url, aweme_id)
            if vid:
                return vid, aweme_id

            # 4) 回退：通过 aweme 接口拿 play_addr.uri
            if aweme_id:
                try:
                    api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
                    api_resp = await client.get(api_url)
                    if api_resp.is_success and api_resp.content:
                        data = api_resp.json()
                        item_list = (
                            data.get("item_list", [])
                            if isinstance(data, dict)
                            and data.get("status_code", 0) == 0
                            else []
                        )
                        item = (
                            item_list[0]
                            if isinstance(item_list, list) and item_list
                            else {}
                        )
                        if isinstance(item, dict):
                            video = item.get("video", {}) or {}
                            play_addr = video.get("play_addr", {}) or {}
                            uri = normalize_text(str(play_addr.get("uri", "")))
                            if self._is_valid_douyin_video_id(uri):
                                return uri, aweme_id
                            url_list = play_addr.get("url_list", [])
                            if isinstance(url_list, list):
                                for row in url_list:
                                    val = normalize_text(str(row))
                                    match = re.search(
                                        r"video_id=([A-Za-z0-9_-]{8,80})", val
                                    )
                                    if not match:
                                        continue
                                    candidate = normalize_text(match.group(1))
                                    if self._is_valid_douyin_video_id(candidate):
                                        return candidate, aweme_id
                except Exception:
                    pass

            # 5) 回退：用 PC UA 访问抖音页面提取 video_id
            if aweme_id:
                try:
                    pc_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": "https://www.douyin.com/",
                    }
                    pc_url = f"https://www.douyin.com/video/{aweme_id}"
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(12.0, connect=5.0),
                        follow_redirects=True,
                        headers=pc_headers,
                    ) as pc_client:
                        pc_resp = await pc_client.get(pc_url)
                        vid, aweme_id = _extract_video_id_from_text(
                            pc_resp.text, str(pc_resp.url), aweme_id
                        )
                        if vid:
                            return vid, aweme_id
                except Exception:
                    pass

        return "", aweme_id

    @staticmethod
    def _is_valid_douyin_video_id(value: str) -> bool:
        content = normalize_text(value).strip()
        if not content:
            return False
        lower = content.lower()
        if lower in {"http", "https", "play", "video"}:
            return False
        if lower.startswith(("http://", "https://")):
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", content):
            return False
        # 抖音 video_id 常见是 v 开头或含数字。
        return bool(lower.startswith("v") or re.search(r"\d", content))

    @staticmethod
    def _extract_douyin_aweme_id(url: str) -> str:
        """从抖音 URL 中直接提取 aweme_id（纯数字）。"""
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/note/(\d+)", url)
        if m:
            return m.group(1)
        # query 参数中的 modal_id / aweme_id / item_id
        try:
            qs = parse_qs(urlparse(url).query)
            for key in ("modal_id", "aweme_id", "item_id"):
                vals = qs.get(key, [])
                if vals and str(vals[0]).isdigit():
                    return str(vals[0])
        except Exception:
            pass
        return ""

    def _is_video_size_ok(self, path: Path) -> bool:
        try:
            size = path.stat().st_size
        except Exception:
            return False
        max_bytes = self._video_download_max_mb * 1024 * 1024
        if not (0 < size <= max_bytes):
            return False
        return self._is_video_container_signature_ok(path)

    def _video_has_audio_stream(self, path: Path) -> bool:
        """检测视频是否包含音轨。优先 ffprobe，缺失时回退 ffmpeg -i 文本解析。"""
        target = str(path)
        ffprobe_bin = ""
        if self._ffmpeg_probe_dir:
            probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            probe_path = Path(self._ffmpeg_probe_dir) / probe_name
            if probe_path.is_file():
                ffprobe_bin = str(probe_path)

        if ffprobe_bin:
            try:
                cmd = [
                    ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    target,
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10, check=False
                )
                if proc.returncode == 0:
                    payload = json.loads(proc.stdout or "{}")
                    streams = payload.get("streams", [])
                    if isinstance(streams, list):
                        return any(
                            isinstance(item, dict)
                            and normalize_text(str(item.get("codec_type", ""))).lower()
                            == "audio"
                            for item in streams
                        )
            except Exception:
                pass

        if not self._ffmpeg_bin:
            return False
        try:
            cmd = [self._ffmpeg_bin, "-hide_banner", "-i", target]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, check=False
            )
            text = normalize_text((proc.stderr or "") + "\n" + (proc.stdout or ""))
            return bool(
                re.search(r"\bAudio:\s*[A-Za-z0-9_]+", text, flags=re.IGNORECASE)
            )
        except Exception:
            return False

    @staticmethod
    def _is_video_container_signature_ok(path: Path) -> bool:
        try:
            with path.open("rb") as fp:
                head = fp.read(32)
        except Exception:
            return False
        if len(head) < 4:
            return False
        if _is_known_image_signature(head):
            return False
        if len(head) >= 12 and (head[4:8] == b"ftyp" or head[8:12] == b"ftyp"):
            return True
        if head.startswith(b"\x1A\x45\xDF\xA3"):  # WebM/MKV EBML
            return True
        if head.startswith(b"FLV"):
            return True
        if head.startswith(b"OggS"):
            return True
        if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"AVI ":
            return True
        return False

    @staticmethod
    def _is_video_file_path(path: Path) -> bool:
        ext = path.suffix.lower()
        if ext in {".mp4", ".webm", ".mov", ".m4v", ".flv", ".mkv"}:
            return True
        mime = (mimetypes.guess_type(str(path))[0] or "").lower()
        return mime.startswith("video/")

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            return

    def _cleanup_video_cache(self) -> None:
        try:
            files = [p for p in self._video_cache_dir.glob("*") if p.is_file()]
        except Exception:
            return
        if len(files) <= self._video_cache_keep_files:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for item in files[: max(0, len(files) - self._video_cache_keep_files)]:
            self._safe_unlink(item)

    def _looks_like_video_send_request(self, text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        send_cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("local_media_request_cues")
            if normalize_text(cue)
        ]
        if not send_cues:
            send_cues = [
                "发送",
                "发给我",
                "发视频",
                "把视频发我",
                "转发这个视频",
                "给我这个视频",
            ]
        return self._looks_like_video_request(content) and any(
            cue in content for cue in send_cues
        )

    def _looks_like_video_request(self, text: str) -> bool:
        content = _normalize_multimodal_query(text)
        if _shared_video_request(content, config=self._raw_config):
            return True
        lower = normalize_text(content).lower()
        if not lower:
            return False
        if re.search(
            r"https?://\S+\.(mp4|mov|m4v|webm|mkv|avi|flv|wmv|m3u8)(?:\?\S*)?$", lower
        ):
            return True
        if re.search(r"(?:^|\s)/(?:video|vid)(?:\s|$)", lower):
            return True
        return False

    def _looks_like_douyin_search_request(self, text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        platform_hit = ("抖音" in content) or ("douyin" in content)
        if not platform_hit:
            return False
        search_cues = _prompt_cues(
            "douyin_search_cues", ("搜索", "搜", "找", "推荐", "来点", "给我来", "查")
        )
        return any(
            cue in content for cue in search_cues
        ) and self._looks_like_video_request(content)

    @staticmethod
    def _looks_like_video_analysis_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        if re.search(
            r"(?:^|\s)/(?:analyze|analyse|summary|summarize)(?:\s|$)", content
        ):
            if re.search(
                r"https?://\S+\.(mp4|mov|m4v|webm|mkv|avi|flv|wmv|m3u8)(?:\?\S*)?$",
                content,
            ):
                return True
        if "output=text" in re.sub(r"\s+", "", content):
            return True
        cues = _prompt_cues(
            "video_analysis_cues",
            (
                "解析",
                "分析",
                "评价",
                "解读",
                "讲讲",
                "讲了什么",
                "内容是什么",
                "总结一下",
                "怎么看",
            ),
        )
        return any(cue in content for cue in cues)
