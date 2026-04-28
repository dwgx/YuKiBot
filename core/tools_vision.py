"""ToolExecutor 视觉/图片分析 mixin — 图片识别、OCR、GIF 处理等。

从 core/tools.py 拆分。"""
from __future__ import annotations

import base64
import io
import mimetypes
import re

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from utils.text import clip_text, normalize_text
from core.tools_types import ToolResult
from core.tools_types import _unwrap_redirect_url, _normalize_multimodal_query, _is_known_image_signature
import logging as _logging
from core.tools_types import _tool_trace_tag
from core.system_prompts import SystemPromptRelay
from core import prompt_loader as _pl

try:  # pragma: no cover
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None

_tool_log = _logging.getLogger("yukiko.tools")


class ToolVisionMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    async def _method_media_analyze_image(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        conversation_id: str = "",
        api_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> ToolResult:
        if not self._vision_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "当前没开识图能力"},
                error="vision_disabled",
            )
        if (
            self._vision_require_independent_config
            and not self._has_independent_vision_config()
        ):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "识图模型配置不完整（需要单独的 provider/base_url/model/api_key）"
                },
                error="vision_config_incomplete",
            )

        def _to_flag(value: Any, default: bool = False) -> bool:
            if isinstance(value, bool):
                return value
            text = normalize_text(str(value)).lower()
            if not text:
                return default
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"0", "false", "no", "off", "n"}:
                return False
            return default

        explicit_url = normalize_text(str(method_args.get("url", "")))
        allow_recent_fallback = _to_flag(
            method_args.get("allow_recent_fallback", False), False
        )
        recent_only_when_unique = _to_flag(
            method_args.get("recent_only_when_unique", False), False
        )
        analyze_all = _to_flag(method_args.get("analyze_all", False), False)
        max_images_raw = normalize_text(str(method_args.get("max_images", "")))
        try:
            max_images = int(max_images_raw) if max_images_raw else 0
        except ValueError:
            max_images = 0
        if max_images <= 0:
            max_images = 8 if analyze_all else 6
        max_images = max(1, min(24, max_images))
        target_source = (
            normalize_text(str(method_args.get("target_source", ""))) or "unspecified"
        )
        target_message_id = normalize_text(
            str(method_args.get("target_message_id", ""))
        )

        def _is_likely_incomplete_media_url(value: str) -> bool:
            candidate = normalize_text(value)
            if not candidate or not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
                return False
            try:
                parsed = urlparse(candidate)
            except Exception:
                return False
            host = normalize_text(parsed.netloc).lower()
            if "multimedia.nt.qq.com.cn" in host:
                lower_candidate = candidate.lower()
                # QQ CDN 图片直链常需要 rkey；缺失时通常是被截断后的无效链接。
                if "fileid=" in lower_candidate and "rkey=" not in lower_candidate:
                    return True
            return False

        candidates: list[str] = []
        candidate_meta: list[dict[str, str]] = []
        if explicit_url:
            resolved_explicit = _unwrap_redirect_url(explicit_url)
            if _is_likely_incomplete_media_url(resolved_explicit):
                _tool_log.info(
                    "vision_explicit_url_incomplete%s | source=%s",
                    _tool_trace_tag(),
                    clip_text(resolved_explicit, 160),
                )
            else:
                candidates.append(resolved_explicit)
                candidate_meta.append(
                    {
                        "source": "explicit_url",
                        "message_id": target_message_id or "-",
                        "url": resolved_explicit,
                    }
                )
        if explicit_url and conversation_id and allow_recent_fallback:
            recent = self._get_recent_media(
                conversation_id=conversation_id, media_type="image"
            )
            if recent and recent_only_when_unique and len(recent) > 1:
                _tool_log.info(
                    "vision_recent_fallback_skip%s | reason=ambiguous_recent_cache | count=%d",
                    _tool_trace_tag(),
                    len(recent),
                )
            elif recent:
                recent_candidates = recent[:1] if recent_only_when_unique else recent
                for item in recent_candidates:
                    normalized_recent = normalize_text(item)
                    if not normalized_recent:
                        continue
                    candidates.append(normalized_recent)
                    candidate_meta.append(
                        {
                            "source": "recent_cache_fallback",
                            "message_id": "-",
                            "url": normalized_recent,
                        }
                    )
        message_candidates = self._extract_message_media_urls(
            raw_segments, media_type="image"
        )
        if message_candidates:
            candidates.extend(message_candidates)
            for item in message_candidates:
                candidate_meta.append(
                    {
                        "source": target_source or "current_or_reply",
                        "message_id": target_message_id or "-",
                        "url": item,
                    }
                )
        if not candidates:
            merged_text = normalize_text(f"{query}\n{message_text}")
            text_candidates = self._extract_urls(merged_text)
            candidates.extend(text_candidates)
            for item in text_candidates:
                candidate_meta.append(
                    {
                        "source": "message_text_url",
                        "message_id": "-",
                        "url": item,
                    }
                )
        if not candidates and conversation_id and allow_recent_fallback:
            recent = self._get_recent_media(
                conversation_id=conversation_id, media_type="image"
            )
            if recent and recent_only_when_unique and len(recent) > 1:
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "我这边最近有不止一张图片，请直接回复你要分析的那张图片再问我。"
                    },
                    error="image_recent_ambiguous",
                )
            if recent:
                candidates.extend(recent[:1] if recent_only_when_unique else recent)
                for item in recent[:1] if recent_only_when_unique else recent:
                    candidate_meta.append(
                        {
                            "source": "recent_cache",
                            "message_id": "-",
                            "url": item,
                        }
                    )

        uniq: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            value = normalize_text(raw)
            if not value or value in seen:
                continue
            seen.add(value)
            uniq.append(value)
        if len(uniq) > max_images:
            uniq = uniq[:max_images]
        if not uniq:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": '没拿到可识别图片 你可以直接发图或给我图片 URL 也可以先发图再说"分析这张图"'
                },
                error="image_not_found",
            )
        url_file_map = self._extract_image_url_file_map(raw_segments)
        if conversation_id:
            recent_url_file_map = self._get_recent_image_file_map(conversation_id)
            if recent_url_file_map:
                for key, token in recent_url_file_map.items():
                    url_file_map.setdefault(key, token)

        if (
            self._vision_route_text_model_to_local
            and not self._can_use_remote_vision_model()
        ):
            _tool_log.info(
                "vision_route_local%s | method=%s | reason=model_text_only_or_unsupported",
                _tool_trace_tag(),
                method_name,
            )
            local_result = await self._analyze_image_local_fallback(
                method_name=method_name,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                api_call=api_call,
            )
            if local_result is not None:
                return local_result
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，已尝试本地识别但没拿到稳定结果。你可以切换支持图片的模型，或让我只做 OCR 文字提取。"
                },
                error="vision_local_unavailable",
            )

        animated_hint = self._has_animated_image_hint(
            query=query,
            message_text=message_text,
            raw_segments=raw_segments,
        )
        prompt = self._build_vision_prompt(
            query=query,
            message_text=message_text,
            animated_hint=animated_hint,
        )
        _tool_log.info(
            "vision_analyze_start%s | method=%s | candidates=%d | explicit=%s | target_source=%s | target_mid=%s | analyze_all=%s | max_images=%d",
            _tool_trace_tag(),
            method_name,
            len(uniq),
            bool(explicit_url),
            target_source,
            target_message_id or "-",
            analyze_all,
            max_images,
        )
        if candidate_meta:
            preview = " | ".join(
                f"{idx + 1}:{item.get('source','-')}:{item.get('message_id','-')}:{clip_text(item.get('url',''), 90)}"
                for idx, item in enumerate(candidate_meta[:6])
            )
            _tool_log.info(
                "vision_analyze_candidates%s | method=%s | %s",
                _tool_trace_tag(),
                method_name,
                preview,
            )
        low_confidence_seen = False
        successful_items: list[dict[str, Any]] = []
        collected_evidence: list[dict[str, str]] = []
        for url in uniq:
            if self._is_blocked_image_url(url):
                continue
            if re.match(
                r"^https?://", url, flags=re.IGNORECASE
            ) and not self._is_safe_public_http_url(url):
                continue
            file_token = url_file_map.get(normalize_text(url))
            image_ref = await self._prepare_vision_image_ref(url)
            if not image_ref and file_token and api_call is not None:
                onebot_data_uri = await self._data_uri_from_onebot_image_file(
                    image_file=file_token,
                    api_call=api_call,
                )
                if onebot_data_uri:
                    image_ref = onebot_data_uri
                    _tool_log.info(
                        "vision_image_ref%s | source=onebot_get_image | converted=data_uri",
                        _tool_trace_tag(),
                    )
            if (
                image_ref
                and re.match(r"^https?://", image_ref, flags=re.IGNORECASE)
                and api_call is not None
            ):
                remote_file_token = file_token or url_file_map.get(
                    normalize_text(image_ref)
                )
                if remote_file_token:
                    onebot_data_uri = await self._data_uri_from_onebot_image_file(
                        image_file=remote_file_token,
                        api_call=api_call,
                    )
                    if onebot_data_uri:
                        image_ref = onebot_data_uri
                        _tool_log.info(
                            "vision_image_ref%s | source=onebot_get_image | converted=data_uri",
                            _tool_trace_tag(),
                        )
            if not image_ref:
                _tool_log.warning(
                    "vision_image_ref_empty%s | url=%s",
                    _tool_trace_tag(),
                    clip_text(url, 120),
                )
                continue
            _tool_log.info(
                "vision_describe_start%s | image_ref=%s",
                _tool_trace_tag(),
                clip_text(image_ref, 120),
            )
            raw_answer = await self._vision_describe(image_ref=image_ref, prompt=prompt)
            _tool_log.info(
                "vision_describe_done%s | raw_answer=%s",
                _tool_trace_tag(),
                clip_text(raw_answer or "-", 200),
            )
            answer = await self._normalize_vision_answer_with_retry(
                image_ref=image_ref,
                answer=raw_answer,
                prompt=prompt,
                query=query,
                message_text=message_text,
                animated_hint=animated_hint,
            )
            if not answer:
                raw_fallback = normalize_text(str(raw_answer or ""))
                if raw_fallback:
                    answer = clip_text(raw_fallback, 1200)
                    _tool_log.info("vision_answer_fallback_to_raw%s", _tool_trace_tag())
            if not answer:
                _tool_log.warning(
                    "vision_answer_empty_after_normalize%s", _tool_trace_tag()
                )
                continue
            if self._looks_like_weak_vision_answer(answer):
                _tool_log.warning(
                    "vision_answer_weak%s | answer=%s",
                    _tool_trace_tag(),
                    clip_text(answer, 100),
                )
                low_confidence_seen = True
                continue
            source = _unwrap_redirect_url(url)
            evidence = [
                {
                    "title": "图像识别",
                    "point": clip_text(answer, 180),
                    "source": source,
                }
            ]
            _tool_log.info(
                "vision_analyze_ok%s | method=%s | source=%s",
                _tool_trace_tag(),
                method_name,
                clip_text(source, 120),
            )
            if not analyze_all:
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "text": answer,
                        "analysis": answer,
                        "source": source,
                        "evidence": evidence,
                    },
                    evidence=evidence,
                )
            successful_items.append(
                {
                    "index": len(successful_items) + 1,
                    "analysis": answer,
                    "source": source,
                }
            )
            collected_evidence.extend(evidence)

        if analyze_all and successful_items:
            lines = [f"已识别 {len(successful_items)} 张图（候选 {len(uniq)} 张）："]
            for idx, item in enumerate(successful_items, start=1):
                one_line = clip_text(item.get("analysis", ""), 220)
                one_source = clip_text(item.get("source", ""), 80)
                if one_source:
                    lines.append(f"{idx}. {one_line}（来源: {one_source}）")
                else:
                    lines.append(f"{idx}. {one_line}")
            merged = "\n".join(lines)
            sources = [
                normalize_text(item.get("source", ""))
                for item in successful_items
                if normalize_text(item.get("source", ""))
            ]
            first_source = sources[0] if sources else ""
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "text": merged,
                    "analysis": merged,
                    "source": first_source,
                    "sources": sources,
                    "analyses": successful_items,
                    "count": len(successful_items),
                    "requested": len(uniq),
                    "evidence": collected_evidence,
                },
                evidence=collected_evidence,
            )

        if low_confidence_seen:
            web_fallback = await self._vision_uncertain_web_fallback(
                query=query, message_text=message_text
            )
            if web_fallback is not None:
                return web_fallback
            single_low_confidence_text = (
                "这张动画表情/动图我已经按多帧尝试识别了，但结果还不够稳定。你可以发更清晰的静态截图，"
                "或者直接问我它大概想表达什么。"
                if animated_hint
                else "这张图识别没成功，能再发一次吗？或者告诉我这张图里你主要想看哪一块，我重点看。"
            )
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": (
                        "这些图我已经尝试识别了，但内容太模糊或信息不足，结果不稳定。你可以发更清晰截图，"
                        "或告诉我要重点看哪一块。"
                        if analyze_all
                        else single_low_confidence_text
                    )
                },
                error="vision_low_confidence",
            )

        web_fallback = await self._vision_uncertain_web_fallback(
            query=query, message_text=message_text
        )
        if web_fallback is not None:
            return web_fallback
        if api_call is not None:
            local_fallback = await self._analyze_image_local_fallback(
                method_name=method_name,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                api_call=api_call,
            )
            if local_fallback.ok:
                return local_fallback
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={
                    "text": (
                        "这些图这次没识别出来，请发更清晰的图片，或告诉我要重点看哪一块。"
                        if analyze_all
                        else (
                            "这张动画表情/动图这次还是没稳定识别出来。你可以发一张关键帧截图，"
                            "或者直接问我它像是在表达什么情绪/态度。"
                            if animated_hint
                            else "这张图识别没成功，能再发一次吗？或者告诉我这张图里你主要想看哪一块，我重点看。"
                        )
                    )
                },
                error="vision_analyze_failed",
            )

    async def _vision_uncertain_web_fallback(
        self, query: str, message_text: str
    ) -> ToolResult | None:
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            return None
        if not self._looks_like_vision_web_lookup_request(merged):
            _tool_log.info(
                "vision_web_fallback_skip%s | reason=no_web_lookup_intent | query=%s",
                _tool_trace_tag(),
                clip_text(merged, 80),
            )
            return None

        refined = re.sub(
            r"(image|picture|screenshot|analyze|analysis|identify|ocr)",
            " ",
            merged,
            flags=re.IGNORECASE,
        )
        refined = normalize_text(re.sub(r"\s+", " ", refined))
        search_query = refined if len(refined) >= 2 else merged
        if len(search_query) < 2:
            return None

        query_type = "text"
        try:
            results = await self._search_text_with_variants(
                query=search_query, query_type=query_type
            )
        except Exception:
            return None
        results = self._filter_and_rank_results(
            search_query, results, query_type=query_type
        )
        if not results:
            return None

        evidence = self._build_evidence_from_results(results)
        summary = self._format_search_text(
            search_query, results, evidence=evidence, query_type=query_type
        )
        text_out = f"图像识别不确定，已联网补查：\n{summary}"
        payload = {
            "query": search_query,
            "query_type": query_type,
            "text": text_out,
            "results": [
                {"title": item.title, "snippet": item.snippet, "url": item.url}
                for item in results
            ],
            "evidence": evidence,
            "vision_uncertain_fallback": True,
        }
        return ToolResult(
            ok=True, tool_name="vision_web_fallback", payload=payload, evidence=evidence
        )

    async def _analyze_image_from_message(
        self,
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        conversation_id: str = "",
        api_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> ToolResult | None:
        if not self._vision_enable:
            return ToolResult(
                ok=False,
                tool_name="vision_analyze_image",
                payload={"text": "当前没开识图能力"},
                error="vision_disabled",
            )
        if (
            self._vision_require_independent_config
            and not self._has_independent_vision_config()
        ):
            return ToolResult(
                ok=False,
                tool_name="vision_analyze_image",
                payload={
                    "text": "识图模型配置不完整（需要单独的 provider/base_url/model/api_key）"
                },
                error="vision_config_incomplete",
            )

        candidates = self._extract_message_media_urls(raw_segments, media_type="image")
        if not candidates and conversation_id:
            candidates = self._get_recent_media(
                conversation_id=conversation_id, media_type="image"
            )
        if not candidates:
            return None

        analyze_all = self._looks_like_analyze_all_images_request(
            f"{query}\n{message_text}"
        )
        method_args: dict[str, Any] = {
            "allow_recent_fallback": bool(conversation_id),
            "recent_only_when_unique": False,
            "target_source": "search_shortcut",
        }
        if analyze_all:
            method_args["analyze_all"] = True
            method_args["max_images"] = 8

        return await self._method_media_analyze_image(
            method_name="vision_analyze_image",
            method_args=method_args,
            query=query,
            message_text=message_text,
            raw_segments=raw_segments,
            conversation_id=conversation_id,
            api_call=api_call,
        )

    def _can_use_remote_vision_model(self) -> bool:
        mode = normalize_text(self._vision_model_supports_image).lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False

        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None:
            return False
        client = getattr(model_client, "client", None)
        model_name = self._vision_model or normalize_text(
            str(getattr(client, "model", "") or getattr(model_client, "model", ""))
        )
        checker = getattr(model_client, "supports_vision_input", None)
        if callable(checker):
            try:
                return bool(checker(model=model_name))
            except Exception:
                return False
        return False

    async def _analyze_image_local_fallback(
        self,
        method_name: str,
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> ToolResult:
        if api_call is None:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，且本地 OCR 通道不可用（缺少 OneBot API 上下文）。"
                },
                error="local_ocr_api_unavailable",
            )

        file_tokens = self._extract_image_file_tokens(raw_segments)
        if not file_tokens:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，本地 OCR 需要直接发送图片（含 file 标识）后再试。"
                },
                error="local_ocr_image_token_missing",
            )

        for token in file_tokens[:3]:
            try:
                result = await api_call("ocr_image", image=token)
            except Exception:
                continue
            text = self._extract_ocr_text(result)
            if not text:
                continue
            source = token
            evidence = [
                {
                    "title": "本地 OCR",
                    "point": clip_text(text, 180),
                    "source": source,
                }
            ]
            prompt_hint = _normalize_multimodal_query(f"{query}\n{message_text}")
            out = f"本地 OCR 识别结果：\n{text}"
            if any(
                cue in prompt_hint.lower() for cue in ("总结", "概括", "要点", "分析")
            ):
                out = f"我先走了本地 OCR（当前模型不支持图片理解）。识别到的文字如下：\n{text}"
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "text": clip_text(out, 900),
                    "analysis": text,
                    "source": source,
                    "analysis_route": "local_ocr",
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={
                "text": "当前模型不支持图片理解，已尝试本地 OCR，但这张图没有提取到可用文字。"
            },
            error="local_ocr_empty",
        )

    @staticmethod
    def _extract_image_file_tokens(raw_segments: list[dict[str, Any]]) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            if normalize_text(str(seg.get("type", ""))).lower() != "image":
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            for key in ("file", "id", "file_id"):
                value = normalize_text(str(data.get(key, "")))
                if not value:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                tokens.append(value)
        return tokens

    @staticmethod
    def _extract_ocr_text(result: Any) -> str:
        payload = result
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            payload = result.get("data")
        if not isinstance(payload, dict):
            return ""
        texts = payload.get("texts", [])
        if not isinstance(texts, list):
            return ""
        rows: list[str] = []
        for item in texts:
            if isinstance(item, dict):
                txt = normalize_text(str(item.get("text", "")))
            else:
                txt = normalize_text(str(item))
            if txt:
                rows.append(txt)
        return normalize_text("\n".join(rows))

    @staticmethod
    def _has_animated_image_hint(
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]] | None = None,
    ) -> bool:
        merged = normalize_text(f"{query}\n{message_text}").lower()
        animated_cues = (
            "动画表情",
            "动图",
            "gif",
            "动态图",
            "表情包",
            "贴纸",
            "动表情",
            "动态贴纸",
        )
        if any(cue in merged for cue in animated_cues):
            return True
        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            if normalize_text(str(seg.get("type", ""))).lower() != "image":
                continue
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            summary = normalize_text(str(data.get("summary", ""))).lower()
            file_name = normalize_text(str(data.get("file", ""))).lower()
            url = normalize_text(str(data.get("url", ""))).lower()
            sub_type = normalize_text(str(data.get("sub_type", ""))).lower()
            if sub_type == "1":
                return True
            if any(cue in summary for cue in animated_cues):
                return True
            if file_name.endswith(".gif"):
                return True
            if ".gif" in url:
                return True
        return False

    def _build_vision_prompt(
        self, query: str, message_text: str, *, animated_hint: bool = False
    ) -> str:
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            merged = "请描述这张图的主要内容，并提取可见文字。"
        extra_parts: list[str] = []
        merged_lower = merged.lower()
        if any(
            cue in merged_lower
            for cue in ("软件", "应用", "程序", "开着哪些", "任务栏", "图标", "窗口")
        ):
            extra_parts.append(
                "\n如果是桌面/任务栏截图："
                "按从左到右列出可识别的软件或窗口名称；不确定的项标注“疑似”。"
            )
        if animated_hint:
            extra_parts.append(
                "\n如果这是动画表情、GIF 或多帧拼图："
                "请综合所有帧，先判断主体是谁、在做什么、情绪/语气是什么、可能想表达什么梗或态度；"
                "即使不能百分百确定，也要给出最可能的解释，不要只说“看不清”或“可能是动图”。"
            )
        extra = "".join(extra_parts)
        base = SystemPromptRelay.vision_main_prompt(user_query=merged, extra=extra)
        return self._prompt_policy.compose_prompt(
            channel="vision",
            base_prompt=base,
            tool_name="media.analyze_image",
        )

    def _build_vision_retry_prompt(
        self, query: str, message_text: str, *, animated_hint: bool = False
    ) -> str:
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            merged = "请识别这张图。"
        if animated_hint:
            merged = (
                f"{merged}\n补充要求：如果这是动画表情/GIF/多帧图，请综合各帧动作与情绪，"
                "优先回答“这张图想表达什么”。"
            )
        base = SystemPromptRelay.vision_retry_prompt(user_query=merged)
        return self._prompt_policy.compose_prompt(
            channel="vision",
            base_prompt=base,
            tool_name="media.analyze_image",
        )

    @staticmethod
    def _extract_image_url_file_map(
        raw_segments: list[dict[str, Any]]
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            if seg_type != "image":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            url = normalize_text(str(data.get("url", "")))
            file_token = normalize_text(str(data.get("file", "")))
            if url and file_token:
                mapping[url] = file_token
                resolved = _unwrap_redirect_url(url)
                if resolved:
                    mapping[resolved] = file_token
            if file_token:
                mapping[file_token] = file_token
        return mapping

    @staticmethod
    def _extract_api_data(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return data
            return payload
        data_obj = getattr(payload, "data", None)
        if isinstance(data_obj, dict):
            return data_obj
        return {}

    def _to_data_uri_from_image_bytes(
        self,
        data: bytes,
        mime: str = "image/png",
        *,
        source: str = "unknown",
        allow_gif_keyframes: bool = True,
    ) -> str:
        if not data:
            return ""
        if (
            len(data) < self._vision_min_image_bytes
            or len(data) > self._vision_max_image_bytes
        ):
            return ""
        head = data[:16]
        if not _is_known_image_signature(head):
            return ""
        mime_norm = normalize_text(mime).lower()
        if not mime_norm.startswith("image/"):
            mime_norm = "image/png"
        if allow_gif_keyframes and self._is_gif_payload(data=data, mime=mime_norm):
            keyframe_data_uri = self._gif_keyframes_to_data_uri(data)
            if keyframe_data_uri:
                _tool_log.info(
                    "vision_image_ref%s | source=%s | gif=keyframes_collage",
                    _tool_trace_tag(),
                    source,
                )
                return keyframe_data_uri
        b64 = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime_norm};base64,{b64}"
        _tool_log.info(
            "vision_image_ref%s | source=%s | mime=%s | bytes=%d",
            _tool_trace_tag(),
            source,
            mime_norm,
            len(data),
        )
        return data_uri

    @staticmethod
    def _is_gif_payload(data: bytes, mime: str) -> bool:
        mime_l = normalize_text(mime).lower()
        if "gif" in mime_l:
            return True
        return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")

    @staticmethod
    def _pick_gif_keyframe_indexes(frame_count: int) -> list[int]:
        if frame_count <= 1:
            return [0]
        target_frames = min(6, frame_count)
        last = frame_count - 1
        picks = [
            int(round(last * idx / max(1, target_frames - 1)))
            for idx in range(target_frames)
        ]
        out: list[int] = []
        seen: set[int] = set()
        for idx in picks:
            if idx < 0 or idx >= frame_count or idx in seen:
                continue
            seen.add(idx)
            out.append(idx)
        return out or [0]

    def _gif_keyframes_to_data_uri(self, gif_bytes: bytes) -> str:
        if Image is None:
            _tool_log.info(
                "vision_gif_keyframes_skip%s | reason=pillow_unavailable",
                _tool_trace_tag(),
            )
            return ""
        try:
            with Image.open(io.BytesIO(gif_bytes)) as gif:
                if not bool(getattr(gif, "is_animated", False)):
                    return ""
                frame_count = int(getattr(gif, "n_frames", 1) or 1)
                frame_indexes = self._pick_gif_keyframe_indexes(frame_count)
                frames = []
                for idx in frame_indexes:
                    gif.seek(idx)
                    frames.append(gif.convert("RGB").copy())
        except Exception as exc:
            _tool_log.warning(
                "vision_gif_keyframes_fail%s | err=%s",
                _tool_trace_tag(),
                clip_text(str(exc), 160),
            )
            return ""

        if not frames:
            return ""

        target_h = max(180, min(640, max(frame.height for frame in frames)))
        resized = []
        for frame in frames:
            src_h = max(1, int(frame.height))
            width = max(
                1, int(round(float(frame.width) * float(target_h) / float(src_h)))
            )
            resized.append(frame.resize((width, target_h)))

        gap = 8
        cols = min(3, len(resized)) if len(resized) > 2 else (2 if len(resized) > 1 else 1)
        rows = max(1, (len(resized) + cols - 1) // cols)
        cell_w = max(frame.width for frame in resized)
        cell_h = max(frame.height for frame in resized)
        canvas = Image.new(
            "RGB",
            (
                cell_w * cols + gap * max(0, cols - 1),
                cell_h * rows + gap * max(0, rows - 1),
            ),
            (12, 12, 12),
        )
        draw = ImageDraw.Draw(canvas) if ImageDraw is not None else None
        for idx, frame in enumerate(resized):
            row = idx // cols
            col = idx % cols
            x = col * (cell_w + gap)
            y = row * (cell_h + gap)
            paste_x = x + max(0, (cell_w - frame.width) // 2)
            paste_y = y + max(0, (cell_h - frame.height) // 2)
            canvas.paste(frame, (paste_x, paste_y))
            if draw is not None:
                draw.rectangle((x + 4, y + 4, x + 34, y + 22), fill=(0, 0, 0))
                draw.text((x + 9, y + 7), f"F{idx + 1}", fill=(255, 255, 255))

        buf = io.BytesIO()
        try:
            canvas.save(buf, format="JPEG", quality=86, optimize=True)
        except Exception as exc:
            _tool_log.warning(
                "vision_gif_keyframes_encode_fail%s | err=%s",
                _tool_trace_tag(),
                clip_text(str(exc), 160),
            )
            return ""
        merged = buf.getvalue()
        if not merged:
            return ""
        if len(merged) > self._vision_max_image_bytes:
            return ""
        return self._to_data_uri_from_image_bytes(
            merged,
            mime="image/jpeg",
            source="gif_keyframes",
            allow_gif_keyframes=False,
        )

    async def _data_uri_from_onebot_image_file(
        self,
        image_file: str,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> str:
        file_token = normalize_text(image_file)
        if not file_token or api_call is None:
            return ""
        for kwargs in (
            {"file": file_token},
            {"file_id": file_token},
            {"id": file_token},
        ):
            try:
                result = await api_call("get_image", **kwargs)
            except Exception:
                continue

            payload = self._extract_api_data(result)
            for key in ("file", "file_path", "path", "local_path", "filename"):
                raw_path = normalize_text(str(payload.get(key, "")))
                if not raw_path:
                    continue
                if raw_path.startswith("file://"):
                    parsed = urlparse(raw_path)
                    file_part = unquote(parsed.path or "")
                    if re.match(r"^/[A-Za-z]:/", file_part):
                        file_part = file_part[1:]
                    raw_path = file_part
                local_path = Path(raw_path)
                if not local_path.is_absolute():
                    local_path = (self._project_root / local_path).resolve()
                if not local_path.exists() or not local_path.is_file():
                    continue
                try:
                    data = local_path.read_bytes()
                except Exception:
                    continue
                mime = mimetypes.guess_type(str(local_path))[0] or "image/png"
                data_uri = self._to_data_uri_from_image_bytes(
                    data,
                    mime=mime,
                    source="onebot_local_file",
                )
                if data_uri:
                    return data_uri

            for key in ("url", "download_url", "src"):
                remote_url = normalize_text(str(payload.get(key, "")))
                if not remote_url:
                    continue
                data_uri = await self._download_image_as_data_uri(remote_url)
                if data_uri:
                    return data_uri
        return ""

    async def _prepare_vision_image_ref(self, raw: str) -> str:
        value = normalize_text(raw)
        if not value:
            return ""
        if value.startswith("data:image"):
            mime, b64 = self._decode_data_image_ref(value)
            if mime and b64:
                try:
                    raw_bytes = base64.b64decode(b64, validate=False)
                except Exception:
                    raw_bytes = b""
                if raw_bytes:
                    prepared = self._to_data_uri_from_image_bytes(
                        raw_bytes,
                        mime=mime,
                        source="data_uri",
                    )
                    if prepared:
                        return prepared
            _tool_log.info(
                "vision_image_ref%s | source=data_uri | passthrough=true",
                _tool_trace_tag(),
            )
            return value
        if value.startswith("base64://"):
            b64 = value[len("base64://") :].strip()
            if not b64:
                return ""
            try:
                raw_bytes = base64.b64decode(b64, validate=False)
            except Exception:
                raw_bytes = b""
            if raw_bytes:
                prepared = self._to_data_uri_from_image_bytes(
                    raw_bytes,
                    mime="image/png",
                    source="base64_scheme",
                )
                if prepared:
                    return prepared
            _tool_log.info(
                "vision_image_ref%s | source=base64_scheme | passthrough=true",
                _tool_trace_tag(),
            )
            return f"data:image/png;base64,{b64}"
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            if not self._is_safe_public_http_url(value):
                return ""
            # QQ CDN 等内网图片外部 API 无法访问，统一下载转 base64
            downloaded = await self._download_image_as_data_uri(value)
            if downloaded:
                _tool_log.info(
                    "vision_image_ref%s | source=http_url | converted=data_uri",
                    _tool_trace_tag(),
                )
                return downloaded
            provider_hint = self._resolve_vision_provider_hint()
            if provider_hint in {"anthropic", "gemini", "skiapi"}:
                _tool_log.warning(
                    "vision_image_ref_empty%s | source=http_url | reason=download_failed_for_provider_%s",
                    _tool_trace_tag(),
                    provider_hint,
                )
                return ""
            # 下载失败则回退直传 URL（公网图片 API 可能能访问）
            _tool_log.info(
                "vision_image_ref%s | source=http_url | converted=direct_url",
                _tool_trace_tag(),
            )
            return value
        if value.startswith("file://"):
            parsed = urlparse(value)
            file_part = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", file_part):
                file_part = file_part[1:]
            value = file_part

        # 防止 data URI 被当作文件路径处理（会导致 "File name too long" 错误）
        if value.startswith("data:"):
            _tool_log.warning(
                "vision_image_ref%s | source=data_uri | unhandled_format | skipping_file_path_check",
                _tool_trace_tag(),
            )
            return ""

        path = Path(value)
        if not path.is_absolute():
            path = (self._project_root / path).resolve()
        if not path.exists() or not path.is_file():
            return ""
        try:
            data = path.read_bytes()
        except Exception:
            return ""
        if not data:
            return ""
        if len(data) < self._vision_min_image_bytes:
            _tool_log.warning(
                "vision_image_ref%s | source=local_file | small_image_warning | bytes=%d | will_try_anyway",
                _tool_trace_tag(),
                len(data),
            )
            # 不要拒绝小图片，继续处理
        if len(data) > self._vision_max_image_bytes:
            return ""
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return self._to_data_uri_from_image_bytes(
            data,
            mime=mime,
            source="local_file",
        )

    def _resolve_vision_provider_hint(self) -> str:
        provider_hint = normalize_text(self._vision_provider).lower()
        if provider_hint:
            return provider_hint
        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None:
            return ""
        return normalize_text(str(getattr(model_client, "provider", ""))).lower()

    async def _download_image_as_data_uri(self, url: str) -> str:
        """下载远程图片并转为 data URI（base64），用于 vision API。"""
        if not self._is_safe_public_http_url(url):
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            data = resp.content
            if not data:
                return ""
            if len(data) < self._vision_min_image_bytes:
                _tool_log.warning(
                    "vision_image_ref%s | source=http_url | small_image_warning | bytes=%d | will_try_anyway",
                    _tool_trace_tag(),
                    len(data),
                )
                # 不要拒绝小图片，继续处理
            if len(data) > self._vision_max_image_bytes:
                return ""
            content_type = str(resp.headers.get("content-type", "")).lower()
            if "image/" in content_type:
                mime = content_type.split(";")[0].strip()
            else:
                mime = "image/png"
            return self._to_data_uri_from_image_bytes(
                data,
                mime=mime,
                source="http_url_download",
            )
        except Exception:
            return ""

    async def _vision_describe(self, image_ref: str, prompt: str) -> str:
        model_client = getattr(self.image_engine, "model_client", None)
        client = (
            getattr(model_client, "client", None) if model_client is not None else None
        )

        if (
            self._vision_route_text_model_to_local
            and not self._can_use_remote_vision_model()
        ):
            return ""

        if (
            self._vision_require_independent_config
            and not self._has_independent_vision_config()
        ):
            return ""

        provider = (
            self._vision_provider
            or normalize_text(str(getattr(model_client, "provider", ""))).lower()
        )
        api_key = self._vision_api_key or normalize_text(
            str(getattr(client, "api_key", ""))
        )
        base_url = (
            self._vision_base_url
            or normalize_text(str(getattr(client, "base_url", "")))
        ).rstrip("/")
        model_name = self._vision_model or normalize_text(
            str(getattr(client, "model", ""))
        )
        if not api_key or not base_url or not model_name:
            return ""
        model_candidates = self._candidate_vision_models(model_name, client)

        timeout_seconds = float(
            getattr(client, "timeout_seconds", self._vision_timeout_seconds)
            if client is not None
            else self._vision_timeout_seconds
        )
        temperature = (
            float(getattr(client, "temperature", self._vision_temperature))
            if client is not None
            else self._vision_temperature
        )
        max_tokens = (
            int(getattr(client, "max_tokens", self._vision_max_tokens))
            if client is not None
            else self._vision_max_tokens
        )
        prefer_v1 = (
            bool(getattr(client, "prefer_v1", self._vision_prefer_v1))
            if client is not None
            else self._vision_prefer_v1
        )
        image_ref_kind = (
            "data_uri"
            if image_ref.startswith("data:image")
            else ("http_url" if image_ref.startswith("http") else "other")
        )
        _tool_log.info(
            "vision_request%s | provider=%s | model=%s | image_ref=%s | timeout=%.1fs",
            _tool_trace_tag(),
            provider or "-",
            ",".join(model_candidates) or model_name or "-",
            image_ref_kind,
            timeout_seconds,
        )

        if provider == "anthropic":
            text = await self._vision_describe_via_anthropic(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                prefer_v1=prefer_v1,
                anthropic_version=normalize_text(
                    str(getattr(client, "anthropic_version", "2023-06-01"))
                ),
            )
            if text:
                return text

        if provider == "gemini":
            text = await self._vision_describe_via_gemini(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                api_version=normalize_text(
                    str(getattr(client, "api_version", "v1beta"))
                )
                or "v1beta",
            )
            if text:
                return text

        if provider not in {
            "openai",
            "newapi",
            "deepseek",
            "skiapi",
            "openrouter",
            "xai",
            "qwen",
            "moonshot",
            "mistral",
            "zhipu",
            "siliconflow",
        }:
            # 非 OpenAI 兼容 provider 兜底到文本模式（可能无法真正看图）
            if model_client is None or not bool(
                getattr(model_client, "enabled", False)
            ):
                return ""
            try:
                return normalize_text(
                    await model_client.chat_text(
                        [
                            {
                                "role": "system",
                                "content": SystemPromptRelay.vision_system_prompt_basic(),
                            },
                            {
                                "role": "user",
                                "content": f"{prompt}\n图片链接：{image_ref}",
                            },
                        ]
                    )
                )
            except Exception:
                return ""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        candidates = self._candidate_openai_bases(
            base_url=base_url, prefer_v1=prefer_v1
        )

        for candidate_model in model_candidates:
            payload = {
                "model": candidate_model,
                "messages": [
                    {
                        "role": "system",
                        "content": SystemPromptRelay.vision_system_prompt_detailed(),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_ref, "detail": "auto"},
                            },
                        ],
                    },
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            for base in candidates:
                url = f"{base}/chat/completions"
                try:
                    async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                        resp = await client_http.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=%s",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                        str(exc)[:240],
                    )
                    continue

                choices = data.get("choices") if isinstance(data, dict) else None
                if not isinstance(choices, list) or not choices:
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_choices",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )
                    continue
                message = (
                    choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                )
                content = message.get("content", "")
                if isinstance(content, str):
                    text = normalize_text(content)
                    if text:
                        return text
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_content",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )
                    continue
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, dict):
                            parts.append(normalize_text(str(item.get("text", ""))))
                    text = normalize_text("".join(parts))
                    if text:
                        return text
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_content_parts",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )

        # 某些 OpenAI 兼容网关（如部分 skiapi/newapi）在 claude 模型下会返回空 content，
        # 这里自动补一次 Anthropic /messages 兼容路径，避免图片识别整体失效。
        if image_ref.startswith("data:image") and "claude" in model_name.lower():
            anthro_text = await self._vision_describe_via_anthropic(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                prefer_v1=prefer_v1,
                anthropic_version=normalize_text(
                    str(getattr(client, "anthropic_version", "2023-06-01"))
                ),
            )
            if anthro_text:
                _tool_log.info(
                    "vision_request_fallback%s | route=anthropic_messages_proxy",
                    _tool_trace_tag(),
                )
                return anthro_text
        return ""

    async def _vision_describe_via_anthropic(
        self,
        image_ref: str,
        prompt: str,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        prefer_v1: bool,
        anthropic_version: str,
    ) -> str:
        mime, b64 = self._decode_data_image_ref(image_ref)
        if not mime or not b64:
            return ""

        payload = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": SystemPromptRelay.vision_system_prompt_detailed(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": b64,
                            },
                        },
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version or "2023-06-01",
            "Content-Type": "application/json",
        }
        candidates = self._candidate_openai_bases(
            base_url=base_url, prefer_v1=prefer_v1
        )
        for base in candidates:
            url = f"{base}/messages"
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                    resp = await client_http.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for item in content:
                if (
                    isinstance(item, dict)
                    and normalize_text(str(item.get("type", ""))) == "text"
                ):
                    parts.append(normalize_text(str(item.get("text", ""))))
            text = normalize_text("".join(parts))
            if text:
                return text
        return ""

    async def _vision_describe_via_gemini(
        self,
        image_ref: str,
        prompt: str,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        api_version: str,
    ) -> str:
        mime, b64 = self._decode_data_image_ref(image_ref)
        if not mime or not b64:
            return ""

        base = normalize_text(base_url).rstrip("/")
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if not base:
            return ""

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64}},
                    ],
                }
            ],
            "system_instruction": {
                "parts": [{"text": SystemPromptRelay.vision_system_prompt_detailed()}]
            },
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        ver = normalize_text(api_version).strip("/") or "v1beta"
        url = f"{base}/{ver}/models/{model_name}:generateContent?key={api_key}"
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                resp = await client_http.post(
                    url, headers={"Content-Type": "application/json"}, json=payload
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return ""

        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            return ""
        content = (
            candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        )
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if not isinstance(parts, list):
            return ""
        out: list[str] = []
        for item in parts:
            if isinstance(item, dict):
                out.append(normalize_text(str(item.get("text", ""))))
        return normalize_text("".join(out))

    @staticmethod
    def _decode_data_image_ref(image_ref: str) -> tuple[str, str]:
        raw = normalize_text(image_ref)
        if not raw.startswith("data:image") or ";base64," not in raw:
            return "", ""
        head, b64 = raw.split(";base64,", 1)
        mime = normalize_text(head.replace("data:", ""))
        data = normalize_text(b64)
        if not mime or not data:
            return "", ""
        return mime, data

    def _has_independent_vision_config(self) -> bool:
        return bool(
            self._vision_provider
            and self._vision_base_url
            and self._vision_model
            and self._vision_api_key
        )

    def _candidate_vision_models(self, primary: str, client: Any | None) -> list[str]:
        values: list[Any] = [primary]
        values.extend(getattr(self, "_vision_fallback_models", []) or [])
        if client is not None and not getattr(self, "_vision_model", ""):
            client_cfg = getattr(client, "config", {}) or {}
            if isinstance(client_cfg, dict):
                values.extend(
                    self._normalize_vision_model_list(
                        client_cfg.get("vision_fallback_models", [])
                    )
                )
                values.extend(
                    self._normalize_vision_model_list(client_cfg.get("fallback_models", []))
                )
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = normalize_text(str(item or ""))
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result or [primary]

    @staticmethod
    def _normalize_vision_model_list(raw: Any) -> list[str]:
        if isinstance(raw, str):
            values = re.split(r"[,;\n]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = normalize_text(str(item or ""))
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    async def _normalize_vision_answer(self, answer: str, prompt: str) -> str:
        content = normalize_text(answer)
        if not content:
            return ""
        if content.strip().lower() in {"-", "--", "n/a", "na", "null", "none"}:
            return ""
        content = re.sub(r"\s+", " ", content).strip()

        # 尝试翻译非中文内容
        if (
            self._looks_like_non_chinese_text(content)
            and self._vision_retry_translate_enable
        ):
            translated = await self._translate_to_chinese(
                content=content, prompt=prompt
            )
            if translated:
                content = translated
        # 移除过于严格的二次检查，即使内容包含英文也应返回，而不是丢弃
        return normalize_text(content)

    async def _normalize_vision_answer_with_retry(
        self,
        image_ref: str,
        answer: str,
        prompt: str,
        query: str,
        message_text: str,
        animated_hint: bool = False,
    ) -> str:
        normalized = await self._normalize_vision_answer(answer, prompt=prompt)
        need_retry = not normalized or self._looks_like_weak_vision_answer(normalized)
        if not self._vision_second_pass_enable or not need_retry:
            return normalized

        retry_prompt = self._build_vision_retry_prompt(
            query=query,
            message_text=message_text,
            animated_hint=animated_hint,
        )
        retry_raw = await self._vision_describe(
            image_ref=image_ref, prompt=retry_prompt
        )
        retry_norm = await self._normalize_vision_answer(retry_raw, prompt=retry_prompt)
        if retry_norm and not self._looks_like_weak_vision_answer(retry_norm):
            return retry_norm
        if retry_norm and not normalized:
            return retry_norm
        return normalized or retry_norm

    async def _translate_to_chinese(self, content: str, prompt: str) -> str:
        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None or not bool(getattr(model_client, "enabled", False)):
            return ""
        try:
            translated = await model_client.chat_text(
                [
                    {
                        "role": "system",
                        "content": SystemPromptRelay.translate_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": (
                            "把下面识图结果翻译为自然中文并保持原意：\n"
                            f"用户问题：{normalize_text(prompt)}\n"
                            f"原始结果：{normalize_text(content)}"
                        ),
                    },
                ]
            )
        except Exception:
            return ""
        translated_text = normalize_text(translated)
        if not translated_text:
            return ""
        if self._looks_like_non_chinese_text(translated_text):
            return ""
        return translated_text

    @staticmethod
    def _looks_like_non_chinese_text(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
        alpha_count = sum(1 for ch in content if ch.isalpha())
        if alpha_count < 8:
            return False
        return cjk_count / max(alpha_count, 1) < 0.25

    @staticmethod
    def _looks_like_weak_vision_answer(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return True
        plain = re.sub(r"\s+", "", content).lower()
        explicit_markers = (
            "???",
            "n/a",
            "unknown",
            "无法识别???",
            "识别失败???",
        )
        if any(marker in plain for marker in explicit_markers):
            return True
        return False

    @staticmethod
    def _candidate_openai_bases(base_url: str, prefer_v1: bool) -> list[str]:
        base = normalize_text(base_url).rstrip("/")
        if not base:
            return []
        with_v1 = base if base.endswith("/v1") else f"{base}/v1"
        without_v1 = base[:-3] if base.endswith("/v1") else base
        ordered = [with_v1, without_v1] if prefer_v1 else [without_v1, with_v1]
        out: list[str] = []
        for item in ordered:
            value = normalize_text(item).rstrip("/")
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def _looks_like_vision_web_lookup_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False

        # 去掉纯指代词，避免把“这个/这张图”误当成联网查询词。
        stripped = normalize_text(
            re.sub(
                r"(分析|识别|看看|看下|看一看|看一下|图片|照片|截图|这张图|这个图|这个|这玩意|这是什么)",
                " ",
                content,
            )
        )
        stripped = normalize_text(re.sub(r"\s+", " ", stripped))
        if len(stripped) < 4:
            return False

        strong_cues = (
            "查一下",
            "搜一下",
            "联网",
            "百度",
            "维基",
            "百科",
            "官网",
            "出处",
            "原图",
            "来源",
            "是什么梗",
            "这是谁",
            "哪个动漫",
            "哪部电影",
            "哪部剧",
            "什么品牌",
            "什么型号",
        )
        if any(cue in content for cue in strong_cues):
            return True

        # 保守兜底：至少包含“身份/来源”类疑问词，且不是只剩一个代词。
        return bool(
            re.search(r"(是谁|叫什么|来自哪里|哪来的|出自|来源|资料|背景)", content)
        )

    @staticmethod
    def _looks_like_image_analysis_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        if re.search(
            r"(?:^|\s)/(?:analyze|analyse|summary|summarize)(?:\s|$)", content
        ):
            if re.search(
                r"https?://\S+\.(png|jpg|jpeg|webp|bmp|gif)(?:\?\S*)?$", content
            ):
                return True
        cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("image_question_cues")
            if normalize_text(cue)
        ]
        if not cues:
            return False
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_analyze_all_images_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        scope_cues = (
            "所有图片",
            "全部图片",
            "所有图",
            "全部图",
            "群里图片",
            "群里的图片",
            "群里所有图",
            "每张图",
            "每个图",
            "逐张",
            "一张张",
            "批量",
            "all images",
            "every image",
        )
        action_cues = (
            "识别",
            "分析",
            "看看",
            "描述",
            "提取",
            "总结",
            "识图",
            "analyze",
            "describe",
            "ocr",
            "read",
        )
        return any(cue in content for cue in scope_cues) and any(
            cue in content for cue in action_cues
        )
