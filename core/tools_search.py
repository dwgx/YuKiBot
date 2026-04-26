"""ToolExecutor 搜索/网页 mixin — 文本搜索、图片搜索、网页抓取等。

从 core/tools.py 拆分。"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import unquote, urlparse
import logging as _logging

from utils.text import clip_text, normalize_text
from core.tools_types import ToolResult, _tool_trace_tag, _normalize_multimodal_query, _unwrap_redirect_url, _is_known_image_signature
from core.search import SearchResult

_tool_log = _logging.getLogger("yukiko.tools")


class ToolSearchMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    async def _search(
        self,
        tool_args: dict[str, Any],
        message_text: str,
        conversation_id: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        raw_segments: list[dict[str, Any]],
        bot_id: str,
    ) -> ToolResult:
        raw_message_text = normalize_text(message_text)
        query = normalize_text(str(tool_args.get("query", ""))) or raw_message_text
        if not query:
            return ToolResult(ok=False, tool_name="search", error="empty_query")
        self._remember_recent_media(
            conversation_id=conversation_id, raw_segments=raw_segments
        )
        query = _normalize_multimodal_query(query)
        query_type = self._detect_query_type(query)
        query = self._apply_query_type_hints(query, query_type)

        method_name = normalize_text(str(tool_args.get("method", "")))
        if self._tool_interface_enable and method_name:
            method_args = tool_args.get("method_args", {})
            if not isinstance(method_args, dict):
                method_args = {}
            method_result = await self._execute_ai_method(
                method_name=method_name,
                method_args=method_args,
                query=query,
                message_text=message_text,
                conversation_id=conversation_id,
                user_id=user_id,
                user_name=user_name,
                group_id=group_id,
                api_call=api_call,
                raw_segments=raw_segments,
                bot_id=bot_id,
            )
            if method_result is not None:
                return method_result

        direct_image = await self._try_direct_image_fetch(
            query=query, message_text=message_text
        )
        if direct_image is not None:
            return direct_image

        mode = normalize_text(str(tool_args.get("mode", "text"))).lower() or "text"
        if mode in {"image", "img", "picture", "photo"}:
            return await self._search_image(
                query=query,
                conversation_id=conversation_id,
                user_id=user_id,
                user_name=user_name,
                group_id=group_id,
                api_call=api_call,
                message_text=message_text,
                raw_segments=raw_segments,
                bot_id=bot_id,
            )
        if mode in {"video", "movie", "clip"}:
            return await self._search_video(query)

        try:
            results = await self._search_text_with_variants(
                query=query, query_type=query_type
            )
        except Exception as exc:
            return ToolResult(
                ok=False, tool_name="search", error=f"search_failed:{exc}"
            )
        results = self._filter_and_rank_results(query, results, query_type=query_type)

        evidence = self._build_evidence_from_results(results)
        text = self._format_search_text(
            query, results, evidence=evidence, query_type=query_type
        )
        payload = {
            "query": query,
            "query_type": query_type,
            "results": [
                {"title": item.title, "snippet": item.snippet, "url": item.url}
                for item in results
            ],
            "text": text,
            "evidence": evidence,
        }
        return ToolResult(
            ok=True, tool_name="search", payload=payload, evidence=evidence
        )

    async def _search_text_with_variants(
        self, query: str, query_type: str
    ) -> list[SearchResult]:
        variants = self._build_query_variants(query=query, query_type=query_type)
        merged: list[SearchResult] = []
        seen: set[str] = set()
        max_keep = max(8, int(getattr(self.search_engine, "max_results", 8)) * 2)
        first_error: Exception | None = None

        for item in variants:
            try:
                rows = await self.search_engine.search(item)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue
            for row in rows:
                key = normalize_text(f"{row.url}|{row.title}").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(row)
                if len(merged) >= max_keep:
                    return merged

        if merged:
            return merged
        if first_error is not None:
            raise first_error
        return []

    async def _search_image(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        bot_id: str,
    ) -> ToolResult:
        if self._is_blocked_image_text(f"{query}\n{message_text}"):
            return ToolResult(
                ok=False,
                tool_name="search_image",
                payload={"text": "这类图片我不能发 换一个合规主题我可以继续帮你找"},
                error="blocked_image_request",
            )

        try:
            image_results = await self.search_engine.search_images(query, max_results=3)
        except Exception as exc:
            image_results = []
            search_error = f"image_search_failed:{exc}"
        else:
            search_error = ""

        if image_results:
            first = await self._pick_sendable_image(
                image_results, conversation_id=conversation_id
            )
            if first is None:
                first = image_results[0]
            self._remember_image_source(conversation_id, first)
            source = normalize_text(first.source_url) or normalize_text(first.image_url)
            evidence = [
                {
                    "title": normalize_text(first.title) or "图片来源",
                    "point": "已找到可发送图片",
                    "source": source,
                }
            ]
            payload = {
                "query": query,
                "mode": "image",
                "text": f"先给你一张图，来源：{source}",
                "image_url": normalize_text(first.image_url),
                "evidence": evidence,
                "results": [
                    {
                        "title": item.title,
                        "image_url": item.image_url,
                        "source_url": item.source_url,
                        "thumbnail_url": item.thumbnail_url,
                    }
                    for item in image_results
                ],
            }
            return ToolResult(
                ok=True, tool_name="search_image", payload=payload, evidence=evidence
            )

        try:
            generated = await self.image_engine.generate(prompt=query)
        except Exception as exc:
            gen_error = f"image_generate_failed:{exc}"
            generated = None
        else:
            gen_error = ""

        if generated and generated.ok and normalize_text(generated.url):
            payload = {
                "query": query,
                "mode": "image",
                "text": "没拿到可直发的网页图片，我先给你生成一张同主题图",
                "image_url": normalize_text(generated.url),
                "results": [],
            }
            return ToolResult(
                ok=True, tool_name="search_image_fallback_generate", payload=payload
            )

        error = search_error or gen_error or "image_result_unavailable"
        return ToolResult(
            ok=False,
            tool_name="search_image",
            payload={"text": "这次没拿到可发送的图片，换一个更具体的描述我再试一次"},
            error=error,
        )

    async def _search_qq_avatar(
        self,
        query: str,
        message_text: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        raw_segments: list[dict[str, Any]],
        bot_id: str,
    ) -> ToolResult | None:
        merged = normalize_text(f"{query}\n{message_text}")
        if not self._looks_like_qq_avatar_request(merged):
            return None

        target = (
            self._extract_qq_number(merged)
            or self._extract_qq_from_at_segments(raw_segments, bot_id=bot_id)
            or (
                self._normalize_qq_id(user_id)
                if self._contains_self_avatar_cue(merged)
                else ""
            )
        )
        if not target:
            target = await self._resolve_avatar_target_from_group(
                merged=merged,
                fallback_user_name=user_name,
                group_id=group_id,
                api_call=api_call,
            )

        api_tmpl_1 = "https://q1.qlogo.cn/g?b=qq&nk=QQ号&s=640"
        api_tmpl_2 = "https://q2.qlogo.cn/headimg_dl?dst_uin=QQ号&spec=640"
        if not target:
            return ToolResult(
                ok=True,
                tool_name="search_qq_avatar",
                payload={
                    "text": (
                        '可以直接抓 QQ 头像。你给我一个 QQ 号，或者说"我的头像"。\n'
                        f"接口模板1：{api_tmpl_1}\n"
                        f"接口模板2：{api_tmpl_2}"
                    )
                },
            )

        url_1 = f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640"
        url_2 = f"https://q2.qlogo.cn/headimg_dl?dst_uin={target}&spec=640"

        image_url = ""
        if await self._is_sendable_image_url(url_1):
            image_url = url_1
        elif await self._is_sendable_image_url(url_2):
            image_url = url_2

        text = (
            f"抓到了，QQ {target} 的头像已经发给你。\n"
            f"接口模板1：{api_tmpl_1}\n"
            f"接口模板2：{api_tmpl_2}"
        )
        if image_url:
            return ToolResult(
                ok=True,
                tool_name="search_qq_avatar",
                payload={
                    "mode": "image",
                    "query": f"qq头像 {target}",
                    "text": text,
                    "image_url": image_url,
                },
            )

        return ToolResult(
            ok=False,
            tool_name="search_qq_avatar",
            payload={"text": f"识别到 QQ {target}，但头像链接暂不可达，请稍后重试。"},
            error="qq_avatar_unavailable",
        )

    async def _method_browser_fetch_url(
        self, method_name: str, method_args: dict[str, Any], query: str
    ) -> ToolResult:
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(query)
            url = urls[0] if urls else ""
        url = _unwrap_redirect_url(url)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给完整 URL（http/https）。"},
                error="invalid_url",
            )
        if self._is_blocked_video_url(url) or self._is_blocked_image_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这个链接不在可处理范围内"},
                error="blocked_url",
            )
        if not self._is_safe_public_http_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这个链接命中了安全限制（内网/本地地址不可访问）"},
                error="unsafe_url",
            )

        page = await self._fetch_webpage_summary(url)
        if not page:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "网页访问失败。"},
                error="fetch_failed",
            )

        status_code = int(page.get("status_code", 0) or 0)
        final_url = normalize_text(str(page.get("final_url", "")))
        content_type = normalize_text(str(page.get("content_type", "")))
        title = normalize_text(str(page.get("title", "")))
        summary = normalize_text(str(page.get("summary", "")))
        paragraphs = page.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            paragraphs = []

        lines = [
            f"抓取完成：{status_code}",
            f"最终链接：{final_url}",
            f"内容类型：{content_type or 'unknown'}",
        ]
        if title:
            lines.append(f"标题：{title}")
        if summary:
            lines.append(f"摘要：{summary}")
        for idx, para in enumerate(paragraphs[:2], start=1):
            para_text = normalize_text(str(para))
            if not para_text:
                continue
            lines.append(f"要点{idx}：{clip_text(para_text, 180)}")
        evidence = self._build_page_evidence(
            {
                "title": title,
                "summary": summary,
                "paragraphs": paragraphs,
                "final_url": final_url,
            }
        )

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(lines),
                "status_code": status_code,
                "final_url": final_url,
                "content_type": content_type,
                "title": title,
                "summary": summary,
                "paragraphs": paragraphs[:3],
                "evidence": evidence,
            },
            evidence=evidence,
        )

    def _format_search_text(
        self,
        query: str,
        results: list[SearchResult],
        evidence: list[dict[str, str]] | None = None,
        query_type: str = "general",
    ) -> str:
        if not results:
            return f"我搜索了”{query}”，但没有找到相关的公开结果。你可以试试换个关键词，或者告诉我更具体的方向。"

        if self._summary_mode not in {"evidence_first", "evidence", "structured"}:
            lines = [f"我查了“{query}”，先给你 {min(3, len(results))} 条："]
            for idx, item in enumerate(results[:3], start=1):
                title = normalize_text(item.title) or f"来源 {idx}"
                url = normalize_text(item.url)
                if url:
                    lines.append(f"{idx}. {title} - {url}")
                else:
                    lines.append(f"{idx}. {title}")
            return "\n".join(lines)

        evidence_rows = [row for row in (evidence or []) if isinstance(row, dict)]
        if not evidence_rows:
            evidence_rows = self._build_evidence_from_results(results)

        lead = ""
        if evidence_rows:
            lead = normalize_text(str(evidence_rows[0].get("point", "")))
        if not lead:
            lead = normalize_text(results[0].snippet) or normalize_text(
                results[0].title
            )
        if not lead:
            lead = "查到一些相关结果。"

        lines: list[str] = [
            f"我先给你 {min(3, len(evidence_rows) or len(results))} 个可核验来源："
        ]
        lines.append(f"首条要点：{clip_text(lead, 128)}")
        if query_type == "person":
            lines[-1] = f"首条要点：关于“{query}”可核验信息：{clip_text(lead, 112)}"
        for idx, row in enumerate(evidence_rows[:3], start=1):
            title = normalize_text(str(row.get("title", ""))) or f"来源{idx}"
            point = normalize_text(str(row.get("point", "")))
            source = normalize_text(str(row.get("source", "")))
            summary = point or title
            if source:
                lines.append(
                    f"{idx}. {clip_text(title, 32)}：{clip_text(summary, 78)}（{source}）"
                )
            else:
                lines.append(f"{idx}. {clip_text(title, 32)}：{clip_text(summary, 92)}")
        if len(lines) <= 2:
            lines.append(
                f"1. {clip_text(normalize_text(results[0].title) or '来源1', 32)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _is_generic_search_command(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        generic = (
            "你去搜",
            "你去搜索",
            "去搜",
            "去搜索",
            "上网搜",
            "网上搜",
            "帮我搜",
            "查一下",
            "查查",
            "搜一下",
            "搜索一下",
        )
        if any(cue in content for cue in generic):
            return True
        return len(content) <= 8 and content in {"搜", "搜索", "查", "去搜", "去查"}
