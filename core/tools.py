from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.parse import urlencode

import httpx

from core import prompt_loader as _pl
from core.image import ImageEngine
from core.music import MusicEngine, MusicPlayResult
from core.prompt_policy import PromptPolicy
from core.search import SearchEngine, SearchResult
from core.system_prompts import SystemPromptRelay
from core.video_analyzer import VideoAnalyzer, VideoAnalysisResult
from utils.intent import (
    looks_like_github_request as _shared_github_request,
    looks_like_repo_readme_request as _shared_repo_readme_request,
    looks_like_video_request as _shared_video_request,
)
from utils.text import clip_text, normalize_matching_text, normalize_text

try:
    from yt_dlp import YoutubeDL
except Exception:  # pragma: no cover - optional runtime dependency
    YoutubeDL = None

try:  # pragma: no cover - optional runtime dependency
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageDraw = None


import logging as _logging

_ytdlp_log = _logging.getLogger("yukiko.ytdlp")
_tool_log = _logging.getLogger("yukiko.tools")
_tool_trace_id_ctx: ContextVar[str] = ContextVar("yukiko_tool_trace_id", default="")
_ytdlp_error_dedupe: set[tuple[str, str]] = set()
_TOOLS_HEURISTIC_CUES_ENABLED = False
_HTTP_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class _UnsafeToolUrlError(RuntimeError):
    """Raised when a tool-side HTTP fetch crosses into a blocked target."""



# ── Re-imports from tools_types (拆分后兼容) ──
from core.tools_types import (  # noqa: F401
    _unwrap_redirect_url,
    _normalize_multimodal_query,
    _is_known_image_signature,
    ToolResult,
    _find_ffmpeg,
    _SilentYTDLPLogger,
    _tool_trace_tag,
    _prompt_cues,
    _write_netscape_cookie_file,
)

from core.tools_ai_method import ToolAiMethodMixin  # noqa: F401
from core.tools_music_exec import ToolMusicExecMixin  # noqa: F401
from core.tools_github import ToolGithubMixin  # noqa: F401
from core.tools_search import ToolSearchMixin  # noqa: F401
from core.tools_video import ToolVideoMixin  # noqa: F401
from core.tools_vision import ToolVisionMixin  # noqa: F401


class ToolExecutor(ToolAiMethodMixin, ToolMusicExecMixin, ToolGithubMixin, ToolSearchMixin, ToolVideoMixin, ToolVisionMixin):
    def __init__(
        self,
        search_engine: SearchEngine,
        image_engine: ImageEngine,
        plugin_runner: Callable[[str, str, dict[str, Any]], Awaitable[str]],
        config: dict[str, Any] | None = None,
    ):
        raw_cfg = config if isinstance(config, dict) else {}
        if isinstance(raw_cfg.get("search"), dict):
            search_cfg = raw_cfg.get("search", {})
        else:
            search_cfg = raw_cfg
        if not isinstance(search_cfg, dict):
            search_cfg = {}

        cfg = search_cfg
        video_cfg = search_cfg.get("video_resolver", raw_cfg.get("video_resolver", {}))
        if not isinstance(video_cfg, dict):
            video_cfg = {}
        self._raw_config = raw_cfg
        control_cfg = raw_cfg.get("control", {}) if isinstance(raw_cfg, dict) else {}
        if not isinstance(control_cfg, dict):
            control_cfg = {}
        global _TOOLS_HEURISTIC_CUES_ENABLED
        _TOOLS_HEURISTIC_CUES_ENABLED = bool(
            control_cfg.get("heuristic_rules_enable", False)
        )

        self.search_engine = search_engine
        self.image_engine = image_engine
        self.plugin_runner = plugin_runner
        self._http_timeout = httpx.Timeout(8.0, connect=5.0)
        self._http_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
        # Shared httpx.AsyncClient instances (lazy-init, reused across calls)
        self._shared_http_client: httpx.AsyncClient | None = None
        self._shared_github_client: httpx.AsyncClient | None = None
        self._recent_image_sources: dict[str, list[str]] = {}
        self._last_search_query: dict[str, str] = {}
        self._recent_media_cache_ttl_seconds = max(
            10, int(search_cfg.get("recent_media_cache_ttl_seconds", 300))
        )
        self._recent_media_cache_limit = max(
            4, min(24, int(search_cfg.get("recent_media_cache_limit", 12)))
        )
        self._recent_media_by_conversation: dict[str, dict[str, Any]] = {}
        self._video_resolver_enable = bool(video_cfg.get("enable", True))
        self._video_download_max_mb = max(8, int(video_cfg.get("download_max_mb", 64)))
        self._video_download_timeout_seconds = max(
            10, int(video_cfg.get("download_timeout_seconds", 50))
        )
        self._video_metadata_timeout_seconds = max(
            6, int(video_cfg.get("metadata_timeout_seconds", 12))
        )
        self._video_resolve_total_timeout_seconds = max(
            12,
            int(video_cfg.get("resolve_total_timeout_seconds", 65)),
        )
        self._video_search_max_duration_seconds = max(
            30,
            int(video_cfg.get("search_max_duration_seconds", 600)),
        )
        self._video_search_send_max_duration_seconds = max(
            self._video_search_max_duration_seconds,
            int(video_cfg.get("search_send_max_duration_seconds", 1800)),
        )
        self._video_search_analysis_max_duration_seconds = max(
            self._video_search_send_max_duration_seconds,
            int(video_cfg.get("search_analysis_max_duration_seconds", 2400)),
        )
        self._video_cache_keep_files = max(
            5, int(video_cfg.get("cache_keep_files", 24))
        )
        self._video_prefer_direct_stream = bool(
            video_cfg.get("prefer_direct_stream", False)
        )
        self._video_silent_mode = bool(video_cfg.get("silent_mode", True))
        # 默认不发送无音轨视频，避免“能播放但没声音”。
        self._video_allow_silent_fallback = bool(
            video_cfg.get("allow_silent_video_fallback", False)
        )
        # 返回 parse_video 前强校验音轨，避免把“无声视频”继续发给用户。
        self._video_require_audio_for_send = bool(
            video_cfg.get("require_audio_for_send", True)
        )
        # 对直链做下载校验，可显著减少“链接可播但客户端发不出去/无声”的情况。
        self._video_validate_direct_url = bool(
            video_cfg.get("validate_direct_url", True)
        )
        self._video_cache_dir = self._resolve_video_cache_dir(
            str(video_cfg.get("cache_dir", "storage/cache/videos"))
        )
        self._video_cache_dir.mkdir(parents=True, exist_ok=True)
        self._video_cookies_file = normalize_text(
            str(video_cfg.get("cookies_file", ""))
        )
        requested_cookie_browser = normalize_text(
            str(video_cfg.get("cookies_from_browser", "auto"))
        )
        self._video_cookies_from_browser = self._resolve_video_cookies_from_browser(
            requested_cookie_browser
        )
        self._video_parse_api_base = normalize_text(
            str(video_cfg.get("parse_api_base", ""))
        ).rstrip("/")
        self._video_parse_enable = bool(
            video_cfg.get("parse_api_enable", bool(self._video_parse_api_base))
        )
        self._video_parse_timeout_seconds = max(
            6, int(video_cfg.get("parse_api_timeout_seconds", 12))
        )
        self._ffmpeg_bin = normalize_text(_find_ffmpeg())
        self._ffmpeg_available = bool(self._ffmpeg_bin)
        # yt-dlp 在 imageio_ffmpeg 场景下必须拿到可执行文件路径，目录路径无法触发合并。
        self._ffmpeg_location = ""
        self._ffmpeg_probe_dir = ""
        if self._ffmpeg_available:
            try:
                ffmpeg_path = Path(self._ffmpeg_bin)
                if ffmpeg_path.is_file():
                    resolved = ffmpeg_path.resolve()
                    self._ffmpeg_location = str(resolved)
                    self._ffmpeg_probe_dir = str(resolved.parent)
                elif ffmpeg_path.is_dir():
                    resolved_dir = ffmpeg_path.resolve()
                    self._ffmpeg_location = str(resolved_dir)
                    self._ffmpeg_probe_dir = str(resolved_dir)
            except Exception:
                self._ffmpeg_location = ""
                self._ffmpeg_probe_dir = ""
        self._video_analyzer = VideoAnalyzer(raw_cfg)
        self._music_engine = MusicEngine(raw_cfg)
        self._prompt_policy = PromptPolicy.from_config(raw_cfg)

        # 平台专属 cookie（B站/抖音/快手）
        va_cfg = (
            raw_cfg.get("video_analysis", search_cfg.get("video_analysis", {})) or {}
        )
        bili_cfg = va_cfg.get("bilibili", {}) or {}
        self._bilibili_sessdata = normalize_text(str(bili_cfg.get("sessdata", "")))
        self._bilibili_jct = normalize_text(str(bili_cfg.get("bili_jct", "")))
        self._bilibili_cookie = (
            normalize_text(str(bili_cfg.get("cookie", "")))
            or self._build_bilibili_cookie()
        )
        dy_cfg = va_cfg.get("douyin", {}) or {}
        self._douyin_cookie = normalize_text(str(dy_cfg.get("cookie", "")))
        ks_cfg = va_cfg.get("kuaishou", {}) or {}
        self._kuaishou_cookie = normalize_text(str(ks_cfg.get("cookie", "")))

        # 初始化混合视频解析器（bilix + yt-dlp）
        self._hybrid_resolver = None
        try:
            from core.video_resolver_hybrid import create_hybrid_resolver

            self._hybrid_resolver = create_hybrid_resolver(
                ytdlp_download_func=self._download_platform_video_sync,
                cache_dir=self._video_cache_dir,
                ffmpeg_location=self._ffmpeg_location,
                bilibili_sessdata=self._bilibili_sessdata,
            )
            _tool_log.info(
                "hybrid_resolver_enabled | bilix_available=%s",
                self._hybrid_resolver.bilix_resolver._bilix_available,
            )
        except Exception as e:
            _tool_log.warning("hybrid_resolver_init_failed | error=%s", str(e)[:100])

        vision_cfg = raw_cfg.get("vision", search_cfg.get("vision", {}))
        if not isinstance(vision_cfg, dict):
            vision_cfg = {}
        self._vision_enable = bool(vision_cfg.get("enable", True))
        self._vision_timeout_seconds = max(
            8, int(vision_cfg.get("timeout_seconds", 35))
        )
        self._vision_min_image_bytes = max(
            128, int(vision_cfg.get("min_image_bytes", 1200))
        )
        self._vision_max_image_bytes = max(
            256 * 1024, int(vision_cfg.get("max_image_bytes", 6 * 1024 * 1024))
        )
        self._vision_provider = normalize_text(
            str(vision_cfg.get("provider", ""))
        ).lower()
        self._vision_base_url = normalize_text(
            str(vision_cfg.get("base_url", ""))
        ).rstrip("/")
        self._vision_api_key = normalize_text(str(vision_cfg.get("api_key", "")))
        self._vision_model = normalize_text(str(vision_cfg.get("model", "")))
        fallback_models_raw = vision_cfg.get(
            "fallback_models", vision_cfg.get("model_fallbacks", [])
        )
        if isinstance(fallback_models_raw, str):
            fallback_model_items = re.split(r"[,;\n]+", fallback_models_raw)
        elif isinstance(fallback_models_raw, (list, tuple, set)):
            fallback_model_items = list(fallback_models_raw)
        else:
            fallback_model_items = []
        self._vision_fallback_models: list[str] = []
        seen_vision_models: set[str] = set()
        for item in fallback_model_items:
            model_text = normalize_text(str(item or ""))
            model_key = model_text.lower()
            if model_text and model_key not in seen_vision_models:
                seen_vision_models.add(model_key)
                self._vision_fallback_models.append(model_text)
        self._vision_prefer_v1 = bool(vision_cfg.get("prefer_v1", True))
        self._vision_temperature = float(vision_cfg.get("temperature", 0.2))
        self._vision_max_tokens = max(200, int(vision_cfg.get("max_tokens", 1200)))
        self._vision_retry_translate_enable = bool(
            vision_cfg.get("retry_translate_enable", True)
        )
        self._vision_second_pass_enable = bool(
            vision_cfg.get("second_pass_enable", True)
        )
        self._vision_require_independent_config = bool(
            vision_cfg.get("require_independent_config", False)
        )
        self._vision_model_supports_image = (
            normalize_text(str(vision_cfg.get("model_supports_image", "auto"))).lower()
            or "auto"
        )
        self._vision_route_text_model_to_local = bool(
            vision_cfg.get("route_text_model_to_local", True)
        )
        self._project_root = Path(__file__).resolve().parents[1]
        tool_iface_cfg = search_cfg.get(
            "tool_interface", raw_cfg.get("tool_interface", {})
        )
        if not isinstance(tool_iface_cfg, dict):
            tool_iface_cfg = {}
        self._tool_interface_enable = bool(tool_iface_cfg.get("enable", True))
        self._tool_interface_browser_enable = bool(
            tool_iface_cfg.get("browser_enable", True)
        )
        self._tool_interface_local_enable = bool(
            tool_iface_cfg.get("local_enable", True)
        )
        self._tool_interface_allow_private_network = bool(
            tool_iface_cfg.get("allow_private_network", False)
        )
        self._tool_interface_local_allow_project_root = bool(
            tool_iface_cfg.get("local_allow_project_root", False)
        )
        self._tool_interface_local_allow_sensitive_files = bool(
            tool_iface_cfg.get("local_allow_sensitive_files", False)
        )
        self._tool_interface_local_read_max_chars = max(
            200, int(tool_iface_cfg.get("local_read_max_chars", 2000))
        )
        self._web_fetch_timeout_seconds = max(
            6, int(tool_iface_cfg.get("web_fetch_timeout_seconds", 12))
        )
        self._web_fetch_max_chars = max(
            280, int(tool_iface_cfg.get("web_fetch_max_chars", 1100))
        )
        self._web_fetch_max_pages = max(
            1, min(3, int(tool_iface_cfg.get("web_fetch_max_pages", 2)))
        )
        roots_raw = tool_iface_cfg.get(
            "local_allowed_roots",
            ["storage", "config", "docs", "core", "services", "plugins"],
        )
        if not isinstance(roots_raw, list):
            roots_raw = ["storage", "config", "docs"]
        self._tool_interface_local_roots = self._resolve_local_roots(roots_raw)
        self._url_host_safety_cache: dict[str, bool] = {}
        self._tool_interface_github_enable = bool(
            tool_iface_cfg.get("github_enable", True)
        )
        self._github_api_base = normalize_text(
            str(tool_iface_cfg.get("github_api_base", "https://api.github.com"))
        )
        self._github_api_base = (
            self._github_api_base or "https://api.github.com"
        ).rstrip("/")
        self._github_search_per_page = max(
            1, min(10, int(tool_iface_cfg.get("github_search_per_page", 5)))
        )
        self._github_readme_max_chars = max(
            200, min(12000, int(tool_iface_cfg.get("github_readme_max_chars", 2400)))
        )
        self._github_token = normalize_text(str(tool_iface_cfg.get("github_token", "")))
        self._summary_mode = (
            normalize_text(
                str(
                    search_cfg.get(
                        "summary_mode", raw_cfg.get("summary_mode", "evidence_first")
                    )
                )
            ).lower()
            or "evidence_first"
        )
        self._language_preference = (
            normalize_text(
                str(
                    search_cfg.get(
                        "language_preference", raw_cfg.get("language_preference", "zh")
                    )
                )
            ).lower()
            or "zh"
        )
        self._last_video_download_error: dict[str, str] = {}
        self._last_video_resolve_diagnostic: dict[str, str] = {}
        self._github_headers = {
            "User-Agent": "YukikoBot/1.0 (+https://github.com)",
            "Accept": "application/vnd.github+json",
        }
        if self._github_token:
            self._github_headers["Authorization"] = f"Bearer {self._github_token}"

        self._platform_video_domains = {
            "douyin.com",
            "iesdouyin.com",
            "kuaishou.com",
            "chenzhongtech.com",
            "bilibili.com",
            "b23.tv",
            "acfun.cn",
            "acfun.com",
            "youku.com",
            "youtube.com",
            "youtu.be",
            "iqiyi.com",
            "qiyi.com",
            "iq.com",
            "v.qq.com",
            "m.v.qq.com",
            "qq.com",
        }

        self._blocked_image_domain_keywords = {
            "porn",
            "xvideos",
            "xnxx",
            "xhamster",
            "hentai",
            "adult",
            "sex",
            "nsfw",
            "rule34",
            "r18",
            "18comic",
            "av",
        }
        self._blocked_image_text_keywords = {
            "黄色",
            "黄图",
            "色图",
            "成人",
            "无码",
            "本子",
            "里番",
            "porn",
            "hentai",
            "nsfw",
            "r18",
            "18禁",
            "成人视频",
            "裸照",
            "露点",
            "性行为",
            "未成年",
            "幼女",
        }
        self._blocked_video_domain_keywords = {
            "pornhub",
            "xvideos",
            "xnxx",
            "xhamster",
            "hentai",
            "adult",
            "sex",
            "nsfw",
            "rule34",
            "r18",
            "18comic",
            "jav",
            "av",
        }
        self._blocked_video_text_keywords = {
            "黄色视频",
            "成人视频",
            "成人网站",
            "无码",
            "本子",
            "里番",
            "黄网",
            "porn",
            "hentai",
            "nsfw",
            "r18",
            "18禁",
            "未成年",
            "幼女",
        }
        self._risky_video_result_keywords = {
            "成人网站",
            "成人视频",
            "成人向",
            "无码",
            "露点",
            "偷拍",
            "脱衣舞",
            "走光",
            "私拍",
            "性行为",
            "黄网",
            "里番",
            "sex",
            "porn",
            "hentai",
            "nsfw",
            "r18",
            "18禁",
        }
        self._ai_method_schemas = self._build_ai_method_schemas()

    # ------------------------------------------------------------------
    # Shared httpx.AsyncClient accessors (lazy init, connection reuse)
    # ------------------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return a shared AsyncClient for general HTTP requests.

        Uses ``self._http_timeout`` and ``self._http_headers``.  The client
        is created on first access and reused for subsequent calls to avoid
        the overhead of creating a new connection pool on every request.
        """
        if self._shared_http_client is None or self._shared_http_client.is_closed:
            self._shared_http_client = httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=False,
                headers=self._http_headers,
            )
        return self._shared_http_client


    async def close(self) -> None:
        """Close shared HTTP clients.  Safe to call multiple times."""
        for client in (self._shared_http_client, self._shared_github_client):
            if client is not None and not client.is_closed:
                try:
                    await client.aclose()
                except Exception:
                    pass
        self._shared_http_client = None
        self._shared_github_client = None


    def remember_incoming_media(
        self, conversation_id: str, raw_segments: list[dict[str, Any]] | None
    ) -> None:
        """Record recent media from any incoming message for follow-up image/video operations."""
        self._remember_recent_media(
            conversation_id=conversation_id, raw_segments=raw_segments or []
        )

    async def execute(
        self,
        action: str,
        tool_name: str,
        tool_args: dict[str, Any],
        message_text: str,
        conversation_id: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        raw_segments: list[dict[str, Any]] | None = None,
        bot_id: str = "",
        trace_id: str = "",
    ) -> ToolResult:
        token = _tool_trace_id_ctx.set(normalize_text(trace_id))
        try:
            if action == "search":
                return await self._search(
                    tool_args=tool_args,
                    message_text=message_text,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    user_name=user_name,
                    group_id=group_id,
                    api_call=api_call,
                    raw_segments=raw_segments or [],
                    bot_id=bot_id,
                )
            if action == "generate_image":
                return await self._generate_image(tool_args, message_text)
            if action == "music_search":
                return await self._music_search(tool_args, message_text)
            if action == "music_play":
                return await self._music_play(
                    tool_args, message_text, api_call, group_id
                )
            if action == "music_play_by_id":
                return await self._music_play_by_id(tool_args, api_call, group_id)
            if action == "bilibili_audio_extract":
                return await self._bilibili_audio_extract(
                    tool_args, message_text, api_call, group_id
                )
            if action == "get_group_member_count":
                return await self._group_member_count(group_id, api_call)
            if action == "get_group_member_names":
                return await self._group_member_names(group_id, api_call)
            if action == "plugin_call":
                return await self._plugin_call(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    message_text=message_text,
                    context={
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "user_name": user_name,
                    },
                )
            if action == "send_segment":
                return await self._send_onebot_segment(
                    tool_args=tool_args,
                    group_id=group_id,
                    user_id=user_id,
                    api_call=api_call,
                )
            return ToolResult(
                ok=False, tool_name=action or "unknown", error="unsupported_action"
            )
        finally:
            _tool_trace_id_ctx.reset(token)




    async def _try_direct_image_fetch(
        self, query: str, message_text: str
    ) -> ToolResult | None:
        merged = normalize_text(f"{query}\n{message_text}")
        urls = self._extract_urls(merged)
        if not urls:
            return None

        if self._is_blocked_image_text(merged):
            return ToolResult(
                ok=False,
                tool_name="search_image_direct_url",
                payload={"text": "这类图片我不能发 换一个合规主题我可以继续帮你找"},
                error="blocked_image_request",
            )

        for url in urls:
            if self._is_direct_video_url(url):
                continue
            if self._is_blocked_image_url(url):
                continue
            if await self._is_sendable_image_url(url):
                return ToolResult(
                    ok=True,
                    tool_name="search_image_direct_url",
                    payload={
                        "mode": "image",
                        "query": query,
                        "text": f"收到    ，直接把这张图发你。来源：{url}",
                        "image_url": url,
                        "results": [],
                    },
                )
        return None


    async def _pick_sendable_image(
        self, candidates: list[Any], conversation_id: str
    ) -> Any | None:
        source_history = set(self._recent_image_sources.get(conversation_id, []))

        # 第一轮：优先"可发送 + 没发过"
        for item in candidates[:8]:
            image_url = normalize_text(str(getattr(item, "image_url", "")))
            source_url = (
                normalize_text(str(getattr(item, "source_url", ""))) or image_url
            )
            if not image_url:
                continue
            if self._is_blocked_image_url(image_url) or self._is_blocked_image_url(
                source_url
            ):
                continue
            if source_url in source_history:
                continue
            if await self._is_sendable_image_url(image_url):
                return item

        # 第二轮：可发送即可（允许重复作为兜底）
        for item in candidates[:8]:
            image_url = normalize_text(str(getattr(item, "image_url", "")))
            if not image_url:
                continue
            if self._is_blocked_image_url(image_url):
                continue
            if await self._is_sendable_image_url(image_url):
                return item
        return None

    async def _is_sendable_image_url(self, url: str) -> bool:
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return False
        if not self._is_safe_public_http_url(url):
            return False
        if self._is_blocked_image_url(url):
            return False
        try:
            response = await self._safe_public_http_get(
                self._get_http_client(),
                url,
            )
        except _UnsafeToolUrlError:
            return False
        except Exception:
            return False
        if response.status_code != 200:
            return False
        content_type = str(response.headers.get("content-type", "")).lower()
        if "image/" not in content_type:
            return False
        return bool(response.content)

    def _remember_image_source(self, conversation_id: str, item: Any) -> None:
        source = normalize_text(str(getattr(item, "source_url", ""))) or normalize_text(
            str(getattr(item, "image_url", ""))
        )
        if not source:
            return
        history = self._recent_image_sources.get(conversation_id, [])
        history.append(source)
        if len(history) > 20:
            history = history[-20:]
        self._recent_image_sources[conversation_id] = history

    def _rewrite_query_with_context(
        self, query: str, conversation_id: str, user_id: str
    ) -> str:
        _ = (conversation_id, user_id)
        return normalize_text(query)

    def _rewrite_safe_beauty_query(self, query: str) -> str:
        return normalize_text(query)


    def _pick_best_image_url(self, urls: list[str]) -> str:
        for item in urls:
            url = normalize_text(item)
            if not url:
                continue
            lower = url.lower()
            if re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", lower):
                return url
            if "multimedia.nt.qq.com.cn" in lower:
                return url
        return ""



    async def _fetch_webpage_summary(self, url: str) -> dict[str, Any] | None:
        target = _unwrap_redirect_url(url)
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return None
        if self._is_blocked_video_url(target) or self._is_blocked_image_url(target):
            return None
        if not self._is_safe_public_http_url(target):
            return None

        timeout = httpx.Timeout(
            float(self._web_fetch_timeout_seconds),
            connect=min(8.0, float(self._web_fetch_timeout_seconds)),
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers=self._http_headers,
            ) as client:
                resp = await self._safe_public_http_get(
                    client,
                    target,
                    header_provider=self._build_tool_request_headers,
                )
        except _UnsafeToolUrlError:
            return None
        except Exception:
            return None

        final_url = normalize_text(str(resp.url))
        content_type = normalize_text(str(resp.headers.get("content-type", ""))).lower()
        status_code = int(resp.status_code)
        if status_code <= 0:
            return None

        title = ""
        summary = ""
        paragraphs: list[str] = []

        if "html" in content_type:
            html = resp.text or ""
            title, summary, paragraphs = self._extract_html_summary(html)
        elif "json" in content_type:
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            summary = self._summarize_json_like(data)
        elif content_type.startswith("text/"):
            summary = clip_text(
                normalize_text(resp.text or ""), self._web_fetch_max_chars
            )
        else:
            summary = f"二进制内容，大小约 {len(resp.content)} 字节。"

        summary = normalize_text(summary)
        if not summary and paragraphs:
            summary = clip_text(
                normalize_text(paragraphs[0]), self._web_fetch_max_chars // 2
            )
        if not summary:
            summary = "这个页面内容较少，暂时提取不到有效文本摘要。"
        if self._is_low_signal_web_summary(
            title=title,
            summary=summary,
            paragraphs=paragraphs,
            final_url=final_url or target,
            content_type=content_type,
        ):
            return None

        return {
            "status_code": status_code,
            "final_url": final_url or target,
            "content_type": content_type,
            "title": title,
            "summary": summary,
            "paragraphs": paragraphs[:4],
        }

    def _extract_html_summary(self, html: str) -> tuple[str, str, list[str]]:
        raw = str(html or "")
        if not raw:
            return "", "", []

        title = ""
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        if title_match:
            title = self._clean_html_fragment(title_match.group(1))

        meta_desc = ""
        meta_match = re.search(
            r'(?is)<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
            raw,
        )
        if meta_match:
            meta_desc = self._clean_html_fragment(meta_match.group(1))

        cleaned = re.sub(r"(?is)<!--.*?-->", " ", raw)
        cleaned = re.sub(
            r"(?is)<(script|style|noscript|svg|canvas|iframe)[^>]*>.*?</\1>",
            " ",
            cleaned,
        )
        primary_block = self._extract_primary_html_block(cleaned)
        working = primary_block or cleaned

        paragraphs: list[str] = []
        seen: set[str] = set()
        for match in re.findall(r"(?is)<p[^>]*>(.*?)</p>", working):
            text = self._clean_html_fragment(match)
            if len(text) < 14:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            paragraphs.append(text)
            if len(paragraphs) >= 6:
                break

        if len(paragraphs) < 2:
            for match in re.findall(r"(?is)<li[^>]*>(.*?)</li>", working):
                text = self._clean_html_fragment(match)
                if len(text) < 16:
                    continue
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                paragraphs.append(text)
                if len(paragraphs) >= 6:
                    break

        if not paragraphs:
            whole = self._clean_html_fragment(cleaned)
            paragraphs = self._split_text_to_paragraphs(whole, max_paragraphs=4)

        summary = meta_desc or (paragraphs[0] if paragraphs else "")
        summary = clip_text(normalize_text(summary), self._web_fetch_max_chars // 2)
        return title, summary, paragraphs

    @staticmethod
    def _extract_primary_html_block(cleaned_html: str) -> str:
        if not cleaned_html:
            return ""
        patterns = (
            r"(?is)<article[^>]*>(.*?)</article>",
            r"(?is)<main[^>]*>(.*?)</main>",
            r'(?is)<div[^>]+(?:id|class)=["\'][^"\']*(?:article|content|post|entry|main|正文)[^"\']*["\'][^>]*>(.*?)</div>',
        )
        blocks: list[str] = []
        for pattern in patterns:
            blocks.extend(re.findall(pattern, cleaned_html))
        if not blocks:
            return ""
        blocks = sorted(blocks, key=lambda x: len(x), reverse=True)
        return blocks[0]

    @staticmethod
    def _split_text_to_paragraphs(text: str, max_paragraphs: int = 4) -> list[str]:
        content = normalize_text(text)
        if not content:
            return []
        sentences = [
            s.strip() for s in re.split(r"(?<=[。！？!?])\s+", content) if s.strip()
        ]
        out: list[str] = []
        for sentence in sentences:
            if len(sentence) < 14:
                continue
            out.append(sentence)
            if len(out) >= max(1, int(max_paragraphs)):
                break
        return out

    @staticmethod
    def _clean_html_fragment(fragment: str) -> str:
        value = str(fragment or "")
        if not value:
            return ""
        value = re.sub(r"(?is)<br\s*/?>", " ", value)
        value = re.sub(r"(?is)<[^>]+>", " ", value)
        value = unescape(value)
        value = re.sub(r"\s+", " ", value)
        return normalize_text(value)

    def _summarize_json_like(self, data: Any) -> str:
        if isinstance(data, dict):
            keys = (
                "title",
                "name",
                "description",
                "summary",
                "content",
                "message",
                "result",
            )
            parts: list[str] = []
            for key in keys:
                value = data.get(key)
                text = normalize_text(str(value)) if value is not None else ""
                if text:
                    parts.append(f"{key}: {text}")
                if len(parts) >= 3:
                    break
            if parts:
                return clip_text(" | ".join(parts), self._web_fetch_max_chars)
            return clip_text(normalize_text(str(data)), self._web_fetch_max_chars)
        if isinstance(data, list):
            preview = [normalize_text(str(item)) for item in data[:3]]
            merged = " | ".join([item for item in preview if item])
            return clip_text(merged, self._web_fetch_max_chars)
        return clip_text(normalize_text(str(data)), self._web_fetch_max_chars)

    def _compose_direct_web_summary_text(self, query: str, page: dict[str, Any]) -> str:
        title = normalize_text(str(page.get("title", "")))
        summary = normalize_text(str(page.get("summary", "")))
        url = normalize_text(str(page.get("final_url", "")))
        paragraphs = page.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            paragraphs = []

        lines = [f"我按网页正文看过了“{query}”。"]
        if title:
            lines.append(f"主题：{clip_text(title, 64)}")
        if summary:
            lines.append(f"总结：{clip_text(summary, 220)}")
        for idx, para in enumerate(paragraphs[:2], start=1):
            text = normalize_text(str(para))
            if not text:
                continue
            lines.append(f"依据{idx}：{clip_text(text, 170)}")
        if url:
            lines.append(f"来源：{url}")
        return "\n".join(lines)

    async def _compose_result_web_analysis_text(
        self,
        query: str,
        results: list[SearchResult],
        query_type: str = "general",
    ) -> tuple[str, list[dict[str, str]]]:
        candidates: list[tuple[str, str]] = []
        for item in results:
            url = _unwrap_redirect_url(normalize_text(item.url))
            if not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            if self._is_blocked_video_url(url) or self._is_blocked_image_url(url):
                continue
            title = normalize_text(item.title) or "网页来源"
            if self._is_low_value_web_candidate(
                url=url, title=title, query_type=query_type
            ):
                continue
            candidates.append((title, url))
            if len(candidates) >= self._web_fetch_max_pages:
                break

        if not candidates:
            return "", []

        fetched = await asyncio.gather(
            *[self._fetch_webpage_summary(url) for _, url in candidates],
            return_exceptions=True,
        )

        lines = [f"我按网页正文查了“{query}”，先给你总结："]
        evidences: list[dict[str, str]] = []
        hit = 0
        for (fallback_title, fallback_url), item in zip(candidates, fetched):
            if isinstance(item, Exception) or not isinstance(item, dict):
                continue
            title = normalize_text(str(item.get("title", ""))) or fallback_title
            summary = normalize_text(str(item.get("summary", "")))
            paragraphs = item.get("paragraphs", [])
            if not isinstance(paragraphs, list):
                paragraphs = []
            evidence = normalize_text(str(paragraphs[0])) if paragraphs else ""
            source = normalize_text(str(item.get("final_url", ""))) or fallback_url
            if not summary and not evidence:
                continue
            hit += 1
            lines.append(f"{hit}. {clip_text(title, 64)}")
            if summary:
                lines.append(f"   总结：{clip_text(summary, 180)}")
            if evidence:
                lines.append(f"   依据：{clip_text(evidence, 160)}")
            lines.append(f"   来源：{source}")
            evidences.append(
                {
                    "title": clip_text(title, 64) or "网页来源",
                    "point": clip_text(summary or evidence, 180),
                    "source": source,
                }
            )

        if hit == 0:
            return "", []
        return "\n".join(lines), evidences[:6]

    def _is_low_signal_web_summary(
        self,
        title: str,
        summary: str,
        paragraphs: list[str],
        final_url: str,
        content_type: str,
    ) -> bool:
        text = normalize_text(
            " ".join([title, summary] + [str(p) for p in (paragraphs or [])[:2]])
        ).lower()
        if not text:
            return True
        if "html" in content_type and len(text) < 20:
            return True

        low_signal_cues = (
            "just a moment",
            "enable javascript",
            "verify you are human",
            "cloudflare",
            "captcha",
            "访问受限",
            "访问验证",
            "请开启javascript",
            "请先登录",
            "loading...",
            "下载app",
            "请在app内查看",
        )
        if any(cue in text for cue in low_signal_cues):
            return True

        zh_pref = self._language_preference.startswith("zh")
        has_zh = bool(re.search(r"[\u4e00-\u9fff]", text))
        if zh_pref and not has_zh and len(text) < 64:
            return True

        url = normalize_text(final_url).lower()
        if (
            any(
                token in url
                for token in ("/dy/article/", "tieba.baidu.com/p/", "zhidao.baidu.com")
            )
            and len(text) < 42
        ):
            return True
        return False

    @staticmethod
    def _is_low_value_web_candidate(url: str, title: str, query_type: str) -> bool:
        merged = normalize_text(f"{url} {title}").lower()
        if not merged:
            return True
        hard_noise = (
            "baijiahao.baidu.com",
            "zhidao.baidu.com",
            "tieba.baidu.com/p/",
            "app下载",
            "开户链接",
        )
        if any(token in merged for token in hard_noise):
            return True

        if query_type == "person":
            person_noise = ("开户", "户籍", "户口", "贷款", "银行", "订阅")
            if any(token in merged for token in person_noise):
                return True
        return False





    @staticmethod
    async def _method_browser_resolve_image(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
    ) -> ToolResult:
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(f"{query}\n{message_text}")
            url = urls[0] if urls else ""
        url = _unwrap_redirect_url(url)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给完整图片 URL"},
                error="invalid_url",
            )
        if self._is_blocked_image_text(
            f"{query}\n{message_text}"
        ) or self._is_blocked_image_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这类图片我不能发"},
                error="blocked_image_request",
            )
        if not self._is_safe_public_http_url(url):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "这个图片链接命中了安全限制（内网/本地地址不可访问）"},
                error="unsafe_url",
            )
        if await self._is_sendable_image_url(url):
            evidence = [
                {"title": "图片链接", "point": "链接可直接发送。", "source": url}
            ]
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "mode": "image",
                    "text": f"图片可发送，来源：{url}",
                    "image_url": url,
                    "evidence": evidence,
                },
                evidence=evidence,
            )
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={"text": "这个链接不是可直发图片"},
            error="image_not_sendable",
        )

    async def _method_local_read_text(
        self, method_name: str, method_args: dict[str, Any]
    ) -> ToolResult:
        path_raw = normalize_text(str(method_args.get("path", "")))
        path = self._resolve_local_path(path_raw)
        if path is None:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "本地路径不在允许范围内"},
                error="local_path_not_allowed",
            )
        if not path.exists() or not path.is_file():
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "本地文件不存在"},
                error="local_file_not_found",
            )
        if self._is_sensitive_local_path(path):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "该文件属于敏感配置，默认禁止直接读取。"},
                error="local_sensitive_file_blocked",
            )
        max_chars = method_args.get(
            "max_chars", self._tool_interface_local_read_max_chars
        )
        try:
            max_chars = max(200, min(20000, int(max_chars)))
        except Exception:
            max_chars = self._tool_interface_local_read_max_chars
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "本地文件读取失败"},
                error=f"local_read_failed:{exc}",
            )
        clean = clip_text(normalize_text(text), max_chars)
        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={"text": f"读取成功：{path}\n{clean}", "local_path": str(path)},
        )

    async def _method_local_media_from_path(
        self, method_name: str, method_args: dict[str, Any]
    ) -> ToolResult:
        path_raw = normalize_text(str(method_args.get("path", "")))
        path = self._resolve_local_path(path_raw)
        if path is None:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "本地路径不在允许范围内"},
                error="local_path_not_allowed",
            )
        if not path.exists() or not path.is_file():
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "本地媒体文件不存在"},
                error="local_media_not_found",
            )
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "mode": "image",
                    "text": "本地图片已准备好",
                    "image_url": path.as_uri(),
                },
            )
        if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "mode": "video",
                    "text": "本地视频已准备好",
                    "video_url": path.as_uri(),
                },
            )
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={"text": "这个本地文件不是可发送的图片/视频格式"},
            error="unsupported_local_media_type",
        )

    async def _method_media_pick_image(
        self, method_name: str, raw_segments: list[dict[str, Any]]
    ) -> ToolResult:
        candidates = self._extract_message_media_urls(raw_segments, media_type="image")
        for url in candidates:
            if self._is_blocked_image_url(url):
                continue
            if url.startswith("file://"):
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "mode": "image",
                        "text": "拿到这条消息里的图片，发你看",
                        "image_url": url,
                    },
                )
            if await self._is_sendable_image_url(url):
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "mode": "image",
                        "text": "拿到这条消息里的图片，发你看",
                        "image_url": url,
                    },
                )
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={"text": "这条消息里没拿到可发送图片"},
            error="message_image_not_found",
        )

    async def _method_media_pick_audio(
        self, method_name: str, raw_segments: list[dict[str, Any]]
    ) -> ToolResult:
        candidates = self._extract_message_media_urls(raw_segments, media_type="audio")
        if candidates:
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={"text": f"拿到音频链接了：{candidates[0]}"},
            )
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={"text": "这条消息里没拿到音频链接"},
            error="message_audio_not_found",
        )

    @staticmethod
    async def _generate_image(
        self, tool_args: dict[str, Any], message_text: str
    ) -> ToolResult:
        prompt = normalize_text(str(tool_args.get("prompt", ""))) or normalize_text(
            message_text
        )
        size = normalize_text(str(tool_args.get("size", "")))
        if not prompt:
            return ToolResult(
                ok=False, tool_name="generate_image", error="empty_prompt"
            )

        try:
            result = await self.image_engine.generate(prompt=prompt, size=size or None)
        except Exception as exc:
            return ToolResult(
                ok=False, tool_name="generate_image", error=f"image_failed:{exc}"
            )

        if not result.ok:
            return ToolResult(
                ok=False,
                tool_name="generate_image",
                payload={"text": normalize_text(result.message)},
                error="image_not_ready",
            )

        return ToolResult(
            ok=True,
            tool_name="generate_image",
            payload={
                "text": normalize_text(result.message) or "图片已生成。",
                "image_url": normalize_text(result.url),
            },
        )

    # ── 音乐搜索 & 播放 ──────────────────────────────────────────────




    async def _group_member_count(
        self,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> ToolResult:
        if group_id <= 0 or api_call is None:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_count",
                error="group_api_unavailable",
            )

        try:
            info = await api_call("get_group_info", group_id=group_id, no_cache=True)
            if isinstance(info, dict):
                member_count = self._pick_int(
                    info, ("member_count", "memberCount", "member_num", "memberNum")
                )
                max_member_count = self._pick_int(
                    info, ("max_member_count", "maxMemberCount")
                )
                if member_count > 0 and max_member_count > 0:
                    return ToolResult(
                        ok=True,
                        tool_name="get_group_member_count",
                        payload={
                            "text": f"这个群当前约 {member_count} 人，上限 {max_member_count} 人。"
                        },
                    )
                if member_count > 0:
                    return ToolResult(
                        ok=True,
                        tool_name="get_group_member_count",
                        payload={"text": f"这个群当前约 {member_count} 人。"},
                    )
        except Exception:
            pass

        try:
            members = await api_call("get_group_member_list", group_id=group_id)
            if isinstance(members, list):
                return ToolResult(
                    ok=True,
                    tool_name="get_group_member_count",
                    payload={"text": f"这个群当前约 {len(members)} 人。"},
                )
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_count",
                error=f"group_count_failed:{exc}",
            )

        return ToolResult(
            ok=False,
            tool_name="get_group_member_count",
            payload={},
            error="group_count_unavailable",
        )

    async def _group_member_names(
        self,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> ToolResult:
        if group_id <= 0 or api_call is None:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_names",
                error="group_api_unavailable",
            )

        try:
            members = await api_call("get_group_member_list", group_id=group_id)
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_names",
                error=f"group_names_failed:{exc}",
            )

        if not isinstance(members, list) or not members:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_names",
                payload={"text": "这个群现在拿不到成员名单。"},
                error="group_names_empty",
            )

        names: list[str] = []
        seen: set[str] = set()
        for item in members:
            if not isinstance(item, dict):
                continue
            display = normalize_text(
                str(
                    item.get("card")
                    or item.get("nickname")
                    or item.get("user_id")
                    or ""
                )
            )
            if not display or display in seen:
                continue
            seen.add(display)
            names.append(display)

        if not names:
            return ToolResult(
                ok=False,
                tool_name="get_group_member_names",
                payload={"text": "我拿到了成员列表，但昵称信息是空的。"},
                error="group_names_no_display",
            )

        max_show = 20
        shown = names[:max_show]
        if len(names) > max_show:
            text = (
                f"这个群我先列前 {max_show} 个昵称：{'、'.join(shown)}。\n"
                f"总人数 {len(names)}，要我继续发后面的也可以。"
            )
        else:
            text = f"这个群成员昵称大致有：{'、'.join(shown)}。"

        return ToolResult(
            ok=True, tool_name="get_group_member_names", payload={"text": text}
        )

    async def _plugin_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        message_text: str,
        context: dict[str, Any],
    ) -> ToolResult:
        name = normalize_text(tool_name)
        if not name:
            return ToolResult(
                ok=False, tool_name="plugin_call", error="plugin_name_required"
            )

        plugin_message = (
            normalize_text(str(tool_args.get("message", ""))) or message_text
        )
        extra_context = tool_args.get("context", {})
        if isinstance(extra_context, dict):
            context = {**context, **extra_context}

        try:
            output = await self.plugin_runner(name, plugin_message, context)
        except Exception as exc:
            return ToolResult(
                ok=False, tool_name="plugin_call", error=f"plugin_failed:{exc}"
            )

        reply = normalize_text(output)
        if not reply:
            return ToolResult(
                ok=False, tool_name="plugin_call", error="plugin_empty_reply"
            )

        return ToolResult(ok=True, tool_name="plugin_call", payload={"text": reply})

    async def _send_onebot_segment(
        self,
        tool_args: dict[str, Any],
        group_id: int,
        user_id: str,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> ToolResult:
        """发送 OneBot 消息段。

        tool_args 格式:
          segment_type: 消息段类型
          data: dict — 消息段 data 字段
          text: str — 可选，附带的文本消息（与消息段一起发送）

        支持的 segment_type:
          poke, dice, rps, face, mface,
          image, record, video, file,
          music, json, forward, at, reply
        """
        if not api_call:
            return ToolResult(
                ok=False, tool_name="send_segment", error="api_not_available"
            )

        seg_type = normalize_text(str(tool_args.get("segment_type", ""))).lower()
        seg_data = tool_args.get("data", {})
        if not isinstance(seg_data, dict):
            seg_data = {}

        allowed_types = {
            "poke",
            "dice",
            "rps",
            "face",
            "mface",
            "image",
            "record",
            "video",
            "file",
            "music",
            "json",
            "forward",
            "at",
            "reply",
        }
        if seg_type not in allowed_types:
            return ToolResult(
                ok=False,
                tool_name="send_segment",
                error=f"unsupported_segment_type:{seg_type}, allowed: {','.join(sorted(allowed_types))}",
            )

        # 构建消息数组
        msg: list[dict[str, Any]] = []
        # 可选附带文本
        extra_text = normalize_text(str(tool_args.get("text", "")))
        if extra_text:
            msg.append({"type": "text", "data": {"text": extra_text}})
        msg.append({"type": seg_type, "data": seg_data})

        try:
            if group_id:
                await api_call("send_group_msg", group_id=int(group_id), message=msg)
            else:
                await api_call("send_private_msg", user_id=int(user_id), message=msg)
            return ToolResult(
                ok=True,
                tool_name="send_segment",
                payload={"text": f"已发送 {seg_type} 消息段"},
            )
        except Exception as exc:
            return ToolResult(
                ok=False, tool_name="send_segment", error=f"send_failed:{exc}"
            )

    @staticmethod
    def _pick_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int:
        for key in keys:
            value = payload.get(key)
            try:
                number = int(value)
                if number >= 0:
                    return number
            except (TypeError, ValueError):
                continue
        return -1


    def _filter_and_rank_results(
        self,
        query: str,
        results: list[SearchResult],
        query_type: str = "general",
    ) -> list[SearchResult]:
        if not results:
            return []
        keywords = self._build_query_keywords(query)
        entity_hint = self._extract_entity_hint(query, keywords)
        scored: list[tuple[int, SearchResult]] = []
        for item in results:
            if self._is_obvious_noise_result(item, query_type=query_type):
                continue
            score = self._score_result_relevance(
                item=item,
                keywords=keywords,
                query_type=query_type,
                entity_hint=entity_hint,
            )
            if score <= 0:
                continue
            scored.append((score, item))
        if not scored:
            salvage = [
                item
                for item in results
                if not self._is_obvious_noise_result(item, query_type=query_type)
            ]
            return salvage[:2] if salvage else results[:2]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    @staticmethod
    def _build_evidence_from_results(
        results: list[SearchResult], limit: int = 3
    ) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        for item in results[: max(1, limit)]:
            title = clip_text(normalize_text(item.title) or "来源", 64)
            point = clip_text(normalize_text(item.snippet) or title, 160)
            source = normalize_text(item.url)
            evidence.append({"title": title, "point": point, "source": source})
        return evidence

    @staticmethod
    def _build_page_evidence(page: dict[str, Any]) -> list[dict[str, str]]:
        title = clip_text(normalize_text(str(page.get("title", ""))) or "网页来源", 64)
        summary = clip_text(normalize_text(str(page.get("summary", ""))), 160)
        source = normalize_text(str(page.get("final_url", "")))
        out: list[dict[str, str]] = []
        if summary:
            out.append({"title": title, "point": summary, "source": source})
        paragraphs = page.get("paragraphs", [])
        if isinstance(paragraphs, list):
            for para in paragraphs[:2]:
                text = clip_text(normalize_text(str(para)), 150)
                if not text:
                    continue
                out.append({"title": title, "point": text, "source": source})
        return out[:3]

    @staticmethod
    def _detect_query_type(query: str) -> str:
        content = normalize_text(query).lower()
        if not content:
            return "general"
        if any(
            cue in content
            for cue in ("视频", "b站", "抖音", "快手", "acfun", "video", "clip")
        ):
            return "video"
        if any(
            cue in content
            for cue in (
                "图片",
                "壁纸",
                "头像",
                "image",
                "photo",
                "illustration",
                "封面",
            )
        ):
            return "image"
        if any(
            cue in content
            for cue in (
                "github",
                "代码",
                "api",
                "接口",
                "客户端",
                "技术",
                "部署",
                "报错",
            )
        ):
            return "tech"
        if any(
            cue in content
            for cue in ("是谁", "来历", "什么人", "资料", "人物", "叫什", "哪里人")
        ):
            return "person"
        if any(
            cue in content for cue in ("专辑", "歌曲", "歌手", "discography", "album")
        ):
            return "work"
        return "general"

    @staticmethod
    def _apply_query_type_hints(query: str, query_type: str) -> str:
        content = normalize_text(query)
        if not content:
            return ""
        if query_type == "person" and "人物" not in content and "百科" not in content:
            return f"{content} 人物 资料"
        if query_type == "work" and "专辑" not in content and "作品" not in content:
            return f"{content} 专辑 作品"
        if query_type == "tech" and "文档" not in content and "教程" not in content:
            return f"{content} 文档 教程"
        return content

    @staticmethod
    def _build_query_variants(query: str, query_type: str) -> list[str]:
        base = normalize_text(query)
        if not base:
            return []

        variants: list[str] = [base]
        if query_type == "person":
            variants.extend(
                [
                    f"{base} 是谁 来历",
                    f"{base} 浜虹墿 璧勬枡",
                    f"{base} site:zhihu.com",
                    f"{base} site:baike.baidu.com",
                ]
            )
        elif query_type == "work":
            variants.extend(
                [
                    f"{base} 涓撹緫 鍒楄〃",
                    f"{base} discography",
                    f"{base} site:music.douban.com",
                ]
            )
        elif query_type == "tech":
            variants.extend(
                [
                    f"{base} 官方 文档",
                    f"{base} github",
                    f"{base} 教程",
                ]
            )
        elif query_type == "video":
            variants.extend(
                [
                    f"{base} site:bilibili.com/video",
                    f"{base} site:douyin.com/video",
                ]
            )
        elif query_type == "image":
            variants.extend(
                [
                    f"{base} 楂樻竻 澹佺焊",
                    f"{base} image",
                ]
            )

        uniq: list[str] = []
        seen: set[str] = set()
        for item in variants:
            value = normalize_text(item)
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(value)
        return uniq[:6]

    @staticmethod
    def _extract_entity_hint(query: str, keywords: list[str]) -> str:
        content = normalize_text(query)
        if not content:
            return ""
        # 优先使用显式英文标识（如 facd12）
        en_tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,20}", content)
        if en_tokens:
            return normalize_text(en_tokens[0]).lower()
        # 退化到最长中文关键词
        zh_tokens = [item for item in keywords if re.search(r"[\u4e00-\u9fff]", item)]
        if not zh_tokens:
            return ""
        zh_tokens.sort(key=len, reverse=True)
        return normalize_text(zh_tokens[0]).lower()

    @staticmethod
    def _is_obvious_noise_result(
        item: SearchResult, query_type: str = "general"
    ) -> bool:
        corpus = normalize_text(f"{item.title} {item.snippet} {item.url}").lower()
        if not corpus:
            return True
        common_noise = (
            "广告",
            "推广",
            "下载app",
            "开户链接",
            "开户链接",
            "贷款",
            "开户",
            "户籍",
            "户口",
        )
        if any(cue in corpus for cue in common_noise):
            return True
        if query_type == "person":
            person_noise = ("开户", "户籍所在地", "银行", "办卡", "订阅", "网易订阅")
            if any(cue in corpus for cue in person_noise):
                return True
        return False

    @staticmethod
    def _build_query_keywords(query: str) -> list[str]:
        content = normalize_text(query).lower()
        if not content:
            return []
        # 中文词块
        zh_tokens = re.findall(r"[\u4e00-\u9fff]{2,8}", content)
        # 英文/数字词块
        en_tokens = re.findall(r"[a-z0-9][a-z0-9_\-]{1,24}", content)
        stopwords = {
            "是谁",
            "什么",
            "怎么",
            "哪里",
            "去网上搜",
            "上网搜",
            "搜索",
            "一下",
            "给我",
            "总结",
            "分段",
            "详细",
            "深度",
            "回答",
        }
        merged = [t for t in (zh_tokens + en_tokens) if t and t not in stopwords]
        # 去重保序
        uniq: list[str] = []
        seen: set[str] = set()
        for token in merged:
            if token in seen:
                continue
            seen.add(token)
            uniq.append(token)
        return uniq[:8]

    def _score_result_relevance(
        self,
        item: SearchResult,
        keywords: list[str],
        query_type: str = "general",
        entity_hint: str = "",
    ) -> int:
        title = normalize_text(item.title).lower()
        snippet = normalize_text(item.snippet).lower()
        url = normalize_text(item.url).lower()
        corpus = f"{title} {snippet} {url}"
        if not corpus.strip():
            return 0
        compact_corpus = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", corpus)

        # 明显无关词惩罚
        noise_cues = (
            "户口",
            "开户",
            "辽宁",
            "吉林",
            "贷款",
            "银行",
            "订阅",
            "广告",
        )
        noise_penalty = sum(1 for cue in noise_cues if cue in corpus)

        # 日文页面惩罚（除非关键词本身是日语）
        jp_penalty = 0
        if re.search(r"[\u3040-\u30ff]", corpus):
            if not any(re.search(r"[\u3040-\u30ff]", k) for k in keywords):
                jp_penalty = 2

        # 语言偏好：默认中文优先。
        lang_penalty = 0
        if self._language_preference.startswith("zh"):
            has_zh = bool(re.search(r"[\u4e00-\u9fff]", corpus))
            if not has_zh:
                lang_penalty = 1

        score = 0
        if any(
            domain in url
            for domain in (
                "baike.baidu.com",
                "zh.wikipedia.org",
                "zhihu.com",
                "music.163.com",
                "douban.com",
            )
        ):
            score += 2
        if query_type == "tech" and any(
            domain in url for domain in ("github.com", "stackoverflow.com", "docs.")
        ):
            score += 2
        if query_type == "tech" and (
            "learn.microsoft.com" in url
            or "readthedocs.io" in url
            or "developer." in url
            or "/docs" in url
        ):
            score += 2
        if query_type == "tech" and any(
            domain in url for domain in ("zhihu.com", "csdn.net", "tieba.baidu.com")
        ):
            score -= 2
        if query_type in {"video", "image"} and any(
            domain in url
            for domain in (
                "bilibili.com",
                "douyin.com",
                "kuaishou.com",
                "acfun.cn",
                "pixabay.com",
            )
        ):
            score += 2
            # 视频详情页额外加分（/video/ /short-video/ /v/ac 等）
            if query_type == "video" and re.search(
                r"/(?:video|short-video|photo|note)/", url
            ):
                score += 3
        if query_type == "person" and any(
            domain in url
            for domain in ("baike.baidu.com", "zh.wikipedia.org", "zhihu.com")
        ):
            score += 2

        if not keywords:
            score += 1
        else:
            hit_count = 0
            for key in keywords:
                if key in corpus:
                    hit_count += 1
                    # 标题命中更重要
                    if key in title:
                        score += 3
                    elif key in snippet:
                        score += 2
                    else:
                        score += 1
                    continue
                compact_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", key.lower())
                if compact_key and compact_key in compact_corpus:
                    hit_count += 1
                    score += 1
            # 至少要命中一个关键词，否则视作低相关
            if hit_count == 0:
                return 0

        if entity_hint and entity_hint in corpus:
            score += 3
        elif entity_hint:
            compact_entity = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", entity_hint.lower())
            if compact_entity and compact_entity in compact_corpus:
                score += 2

        score -= noise_penalty * 2
        score -= jp_penalty
        score -= lang_penalty
        return score

    def _resolve_local_roots(self, roots_raw: list[Any]) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for item in roots_raw:
            raw = normalize_text(str(item))
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = (self._project_root / p).resolve()
            else:
                p = p.resolve()
            if (
                p == self._project_root.resolve()
                and not self._tool_interface_local_allow_project_root
            ):
                _tool_log.warning(
                    "local_root_skip_project_root | reason=local_allow_project_root_false"
                )
                continue
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(p)
        if not roots:
            roots = [
                (self._project_root / "storage").resolve(),
                (self._project_root / "config").resolve(),
                (self._project_root / "docs").resolve(),
                (self._project_root / "core").resolve(),
                (self._project_root / "services").resolve(),
                (self._project_root / "plugins").resolve(),
            ]
        return roots

    def _resolve_local_path(self, raw_path: str) -> Path | None:
        raw = normalize_text(str(raw_path))
        if not raw:
            return None
        if raw.startswith("file://"):
            parsed = urlparse(raw)
            file_part = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", file_part):
                file_part = file_part[1:]
            raw = file_part

        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (self._project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()

        for root in self._tool_interface_local_roots:
            try:
                candidate.relative_to(root)
                return candidate
            except Exception:
                continue
        return None

    def _is_sensitive_local_path(self, path: Path) -> bool:
        if self._tool_interface_local_allow_sensitive_files:
            return False
        normalized = str(path).replace("\\", "/").lower()
        file_name = normalize_text(path.name).lower()
        blocked_file_names = {
            ".env",
            ".env.local",
            ".env.production",
            ".env.development",
            "config.yml",
            "config.yaml",
            "auth.json",
            ".secret_key",
            "id_rsa",
            "id_ed25519",
        }
        if file_name in blocked_file_names:
            return True
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            return True
        blocked_fragments = (
            "/.git/",
            "/storage/.secret_key",
            "/storage/admin_state.json",
            "/credentials/",
            "/secrets/",
        )
        return any(fragment in normalized for fragment in blocked_fragments)

    @staticmethod
    def _is_public_ip_obj(
        ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    def _is_safe_public_http_url(self, url: str) -> bool:
        target = normalize_text(url)
        if not target:
            return False
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return False
        if self._tool_interface_allow_private_network:
            return True
        try:
            parsed = urlparse(target)
        except Exception:
            return False
        host = normalize_text(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        if host in {
            "localhost",
            "metadata",
            "metadata.google.internal",
        } or host.endswith(".localhost"):
            return False
        if host.endswith(
            (".local", ".internal", ".localdomain", ".home", ".lan", ".arpa")
        ):
            return False
        try:
            ip_obj = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            ip_obj = None
        if ip_obj is not None:
            return self._is_public_ip_obj(ip_obj)

        cached = self._url_host_safety_cache.get(host)
        if cached is not None:
            return cached

        try:
            # socket.getaddrinfo 是阻塞调用，但此方法被大量同步代码调用
            # 使用缓存减少阻塞频率；首次查询仍会阻塞但结果会被缓存
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except Exception:
            self._url_host_safety_cache[host] = False
            return False

        saw_ip = False
        for info in infos:
            sockaddr = info[4] if len(info) >= 5 else None
            if not sockaddr:
                continue
            address = normalize_text(str(sockaddr[0])).split("%", 1)[0]
            if not address:
                continue
            saw_ip = True
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not self._is_public_ip_obj(resolved_ip):
                self._url_host_safety_cache[host] = False
                return False
        self._url_host_safety_cache[host] = saw_ip
        return saw_ip

    @staticmethod
    def _host_matches_domain(host: str, domain: str) -> bool:
        value = normalize_text(host).strip().lower().rstrip(".")
        token = normalize_text(domain).strip().lower().rstrip(".")
        if not value or not token:
            return False
        return value == token or value.endswith(f".{token}")

    def _build_tool_request_headers(self, url: str) -> dict[str, str]:
        headers = dict(self._http_headers)
        try:
            host = normalize_text(urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return headers
        if (
            (
                self._host_matches_domain(host, "bilibili.com")
                or self._host_matches_domain(host, "b23.tv")
            )
            and self._bilibili_cookie
        ):
            headers["Cookie"] = self._bilibili_cookie
        elif (
            (
                self._host_matches_domain(host, "douyin.com")
                or self._host_matches_domain(host, "iesdouyin.com")
            )
            and self._douyin_cookie
        ):
            headers["Cookie"] = self._douyin_cookie
        elif (
            (
                self._host_matches_domain(host, "kuaishou.com")
                or self._host_matches_domain(host, "chenzhongtech.com")
            )
            and self._kuaishou_cookie
        ):
            headers["Cookie"] = self._kuaishou_cookie
        return headers

    async def _safe_public_http_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        header_provider: Callable[[str], dict[str, str]] | None = None,
        max_redirects: int = 5,
    ) -> httpx.Response:
        current = normalize_text(_unwrap_redirect_url(url))
        if not current:
            raise _UnsafeToolUrlError("empty_url")
        redirects = 0
        while True:
            if not await self._is_safe_public_http_url_async(current):
                raise _UnsafeToolUrlError(f"unsafe_url:{current}")
            request_headers = header_provider(current) if callable(header_provider) else None
            response = await client.get(
                current,
                headers=request_headers,
                follow_redirects=False,
            )
            final_url = normalize_text(str(response.url or current))
            if response.status_code in _HTTP_REDIRECT_STATUS_CODES:
                location = normalize_text(str(response.headers.get("location", "")))
                if not location:
                    return response
                redirects += 1
                if redirects > max_redirects:
                    raise _UnsafeToolUrlError("too_many_redirects")
                next_url = normalize_text(urljoin(final_url or current, location))
                next_url = normalize_text(_unwrap_redirect_url(next_url))
                if not next_url or not await self._is_safe_public_http_url_async(next_url):
                    raise _UnsafeToolUrlError(f"unsafe_redirect_url:{next_url}")
                current = next_url
                continue
            if final_url and not await self._is_safe_public_http_url_async(final_url):
                raise _UnsafeToolUrlError(f"unsafe_final_url:{final_url}")
            return response

    async def _is_safe_public_http_url_async(self, url: str) -> bool:
        """异步版本的 URL 安全检查，避免阻塞事件循环。"""
        target = normalize_text(url)
        if not target:
            return False
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return False
        if self._tool_interface_allow_private_network:
            return True
        try:
            parsed = urlparse(target)
        except Exception:
            return False
        host = normalize_text(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        if host in {
            "localhost",
            "metadata",
            "metadata.google.internal",
        } or host.endswith(".localhost"):
            return False
        if host.endswith(
            (".local", ".internal", ".localdomain", ".home", ".lan", ".arpa")
        ):
            return False
        try:
            ip_obj = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            ip_obj = None
        if ip_obj is not None:
            return self._is_public_ip_obj(ip_obj)

        cached = self._url_host_safety_cache.get(host)
        if cached is not None:
            return cached

        try:
            import asyncio as _aio

            loop = _aio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except Exception:
            self._url_host_safety_cache[host] = False
            return False

        saw_ip = False
        for info in infos:
            sockaddr = info[4] if len(info) >= 5 else None
            if not sockaddr:
                continue
            address = normalize_text(str(sockaddr[0])).split("%", 1)[0]
            if not address:
                continue
            saw_ip = True
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not self._is_public_ip_obj(resolved_ip):
                self._url_host_safety_cache[host] = False
                return False
        self._url_host_safety_cache[host] = saw_ip
        return saw_ip

    @staticmethod
    def _is_known_image_signature(head: bytes) -> bool:
        return (
            head.startswith(b"\x89PNG\r\n\x1a\n")
            or head.startswith(b"\xFF\xD8\xFF")
            or head.startswith(b"GIF87a")
            or head.startswith(b"GIF89a")
            or head.startswith(b"BM")
            or (head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP")
        )

    @staticmethod
    def _clean_markdown_text(text: str) -> str:
        content = str(text or "")
        if not content:
            return ""
        content = re.sub(r"```[\s\S]*?```", " ", content)
        content = re.sub(r"`[^`]+`", " ", content)
        content = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", content)
        content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
        content = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", content)
        content = re.sub(r"(^|\n)\s*[-*+]\s+", r"\1", content)
        content = re.sub(r"(^|\n)\s*\d+\.\s+", r"\1", content)
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return normalize_text(content)



    @staticmethod
    def _looks_like_download_request_text(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        if any(token in plain for token in ("/download", "download=1", "upload=true", "prefer_ext=")):
            return True
        return bool(
            re.search(
                r"\.(?:exe|apk|ipa|msi|zip|rar|7z|dmg|pkg|jar)(?:\b|[?#/])",
                content,
                flags=re.IGNORECASE,
            )
        )

    def _looks_like_software_download_request(self, text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if not self._looks_like_download_request_text(content):
            return False
        software_cues = (
            "hmcl",
            ".exe",
            ".msi",
            ".apk",
            ".jar",
            ".zip",
            "windows",
            "win",
            "安卓",
            "android",
            "官方",
        )
        return any(cue in content for cue in software_cues)

    @staticmethod
    def _guess_download_preferences(
        raw_query: str, message_text: str, repo_name: str = ""
    ) -> tuple[str, str]:
        merged = normalize_text(f"{raw_query}\n{message_text}\n{repo_name}").lower()
        if "hmcl" in merged:
            return ".jar", "HMCL.jar"
        if any(cue in merged for cue in ("android", "安卓")):
            return ".apk", ""
        if any(cue in merged for cue in ("windows", "win", "电脑", "pc")):
            return ".exe", ""
        if any(cue in merged for cue in ("mac", "macos")):
            return ".dmg", ""
        return "", ""


    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        content = normalize_text(text)
        if not content:
            return []
        matches = re.findall(
            r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
            content,
            flags=re.IGNORECASE,
        )
        uniq: list[str] = []
        seen: set[str] = set()
        for raw in matches:
            url = raw.strip().rstrip(".,;)]}")
            if not url or url in seen:
                continue
            seen.add(url)
            uniq.append(url)
        return uniq

    @staticmethod
    def _extract_local_path_candidates(text: str) -> list[str]:
        content = normalize_text(text)
        if not content:
            return []
        patterns = (
            r"[A-Za-z]:\\[^\s\"'<>|?*]+",
            r"(?:\./|\.\./|/)[^\s\"'<>|?*]+",
            r"(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}",
            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",
        )
        out: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for raw in re.findall(pattern, content):
                candidate = (
                    normalize_text(str(raw)).strip().rstrip("锛屻€傦紒锛??,.;:)]}")
                )
                if not candidate:
                    continue
                lower = candidate.lower()
                if lower.startswith("http://") or lower.startswith("https://"):
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                out.append(candidate)
        return out

    @classmethod
    def _pick_local_path_candidate(cls, text: str) -> str:
        rows = cls._extract_local_path_candidates(text)
        if not rows:
            return ""
        scored: list[tuple[int, str]] = []
        for item in rows:
            score = 0
            if re.search(r"\.[A-Za-z0-9]{1,10}$", item):
                score += 4
            if any(
                cue in item
                for cue in (
                    "core/",
                    "core\\",
                    "docs/",
                    "docs\\",
                    "config/",
                    "config\\",
                    "storage/",
                    "storage\\",
                )
            ):
                score += 2
            if item.startswith(
                ("./", "../", "/", "core/", "docs/", "config/", "storage/")
            ):
                score += 1
            if re.match(r"^[A-Za-z]:\\", item):
                score += 2
            if item.startswith("/") and any(
                other != item and other.endswith(item) for other in rows
            ):
                score -= 3
            scored.append((score, item))
        scored.sort(key=lambda it: it[0], reverse=True)
        return scored[0][1] if scored else ""

    @staticmethod
    def _looks_like_local_file_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "本地",
            "文件",
            "路径",
            "读一下",
            "读取",
            "打开",
            "看看这个文件",
            "分析这个文件",
            "学习这个文件",
            "local",
            "read",
            "path",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_local_media_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("local_media_request_cues")
            if normalize_text(cue)
        ]
        if not cues:
            return False
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_media_file_path(path: str) -> bool:
        value = normalize_text(path).lower()
        if not value:
            return False
        return bool(
            re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|webm|mov|m4v)$", value)
        )

    def _extract_message_media_urls(
        self, raw_segments: list[dict[str, Any]], media_type: str
    ) -> list[str]:
        wanted = normalize_text(media_type).lower()
        urls: list[str] = []
        seen: set[str] = set()

        image_types = {"image"}
        video_types = {"video"}
        audio_types = {"record", "audio"}

        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            data = seg.get("data", {}) or {}
            if wanted == "image" and seg_type not in image_types:
                continue
            if wanted == "video" and seg_type not in video_types:
                continue
            if wanted == "audio" and seg_type not in audio_types:
                continue

            candidates: list[str] = []
            for key in ("memory_data_uri", "url", "file", "path"):
                value = normalize_text(str(data.get(key, "")))
                if value:
                    candidates.append(value)

            for raw in candidates:
                value = self._normalize_message_media_value(raw)
                if not value or value in seen:
                    continue
                seen.add(value)
                urls.append(value)

        return urls

    @staticmethod
    def _group_conversation_id_from_any(conversation_id: str) -> str:
        conv = normalize_text(conversation_id)
        if not conv:
            return ""
        match = re.match(r"^group:([^:]+)", conv, flags=re.IGNORECASE)
        if not match:
            return ""
        group_id = normalize_text(match.group(1))
        if not group_id:
            return ""
        return f"group:{group_id}"

    def _recent_media_scope_keys(self, conversation_id: str) -> list[str]:
        conv = normalize_text(conversation_id)
        if not conv:
            return []
        keys = [conv]
        group_conv = self._group_conversation_id_from_any(conv)
        if group_conv and group_conv not in keys:
            keys.append(group_conv)
        return keys

    def _remember_recent_media(
        self, conversation_id: str, raw_segments: list[dict[str, Any]]
    ) -> None:
        target_keys = self._recent_media_scope_keys(conversation_id)
        if not target_keys:
            return
        images = self._extract_message_media_urls(raw_segments, media_type="image")
        videos = self._extract_message_media_urls(raw_segments, media_type="video")
        image_file_map = self._extract_image_url_file_map(raw_segments)
        if not images and not videos:
            self._cleanup_recent_media_cache()
            return

        now = datetime.now(timezone.utc)
        for key in target_keys:
            state = self._recent_media_by_conversation.get(key, {})
            if not isinstance(state, dict):
                state = {}
            state["updated_at"] = now
            image_old = state.get("image", [])
            video_old = state.get("video", [])
            image_file_old = state.get("image_file_map", {})
            if not isinstance(image_old, list):
                image_old = []
            if not isinstance(video_old, list):
                video_old = []
            if not isinstance(image_file_old, dict):
                image_file_old = {}
            if images:
                state["image"] = (images + image_old)[: self._recent_media_cache_limit]
            if videos:
                state["video"] = (videos + video_old)[: self._recent_media_cache_limit]
            if image_file_map:
                merged_image_file_map: dict[str, str] = {}
                map_cap = max(self._recent_media_cache_limit * 6, 32)
                for source_map in (image_file_map, image_file_old):
                    for raw_key, raw_token in source_map.items():
                        map_key = normalize_text(str(raw_key))
                        map_token = normalize_text(str(raw_token))
                        if (
                            not map_key
                            or not map_token
                            or map_key in merged_image_file_map
                        ):
                            continue
                        merged_image_file_map[map_key] = map_token
                        if len(merged_image_file_map) >= map_cap:
                            break
                    if len(merged_image_file_map) >= map_cap:
                        break
                if merged_image_file_map:
                    state["image_file_map"] = merged_image_file_map
            self._recent_media_by_conversation[key] = state
        self._cleanup_recent_media_cache()

    def _get_recent_media(self, conversation_id: str, media_type: str) -> list[str]:
        keys = self._recent_media_scope_keys(conversation_id)
        if not keys:
            return []
        self._cleanup_recent_media_cache()
        field = normalize_text(media_type).lower()
        out: list[str] = []
        seen: set[str] = set()
        for key in keys:
            state = self._recent_media_by_conversation.get(key, {})
            if not isinstance(state, dict):
                continue
            rows = state.get(field, [])
            if not isinstance(rows, list):
                continue
            for item in rows:
                value = normalize_text(str(item))
                if not value or value in seen:
                    continue
                seen.add(value)
                out.append(value)
                if len(out) >= self._recent_media_cache_limit:
                    return out
        return out

    def _get_recent_image_file_map(self, conversation_id: str) -> dict[str, str]:
        keys = self._recent_media_scope_keys(conversation_id)
        if not keys:
            return {}
        self._cleanup_recent_media_cache()
        out: dict[str, str] = {}
        cap = max(self._recent_media_cache_limit * 6, 32)
        for key in keys:
            state = self._recent_media_by_conversation.get(key, {})
            if not isinstance(state, dict):
                continue
            rows = state.get("image_file_map", {})
            if not isinstance(rows, dict):
                continue
            for raw_url, raw_file_token in rows.items():
                url = normalize_text(str(raw_url))
                file_token = normalize_text(str(raw_file_token))
                if not url or not file_token or url in out:
                    continue
                out[url] = file_token
                if len(out) >= cap:
                    return out
        return out

    def _cleanup_recent_media_cache(self) -> None:
        if not self._recent_media_by_conversation:
            return
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=self._recent_media_cache_ttl_seconds)
        stale: list[str] = []
        for key, state in self._recent_media_by_conversation.items():
            ts = state.get("updated_at") if isinstance(state, dict) else None
            if not isinstance(ts, datetime):
                stale.append(key)
                continue
            if now - ts > ttl:
                stale.append(key)
        for key in stale:
            self._recent_media_by_conversation.pop(key, None)

    @staticmethod
    def _normalize_message_media_value(raw: str) -> str:
        value = normalize_text(str(raw or ""))
        if not value:
            return ""
        if value.startswith("data:"):
            return value
        if value.startswith("base64://"):
            return value
        if re.match(r"^[a-zA-Z]:[\\/]", value):
            return Path(value).resolve().as_uri()
        if value.startswith("\\\\"):
            return ""
        if value.startswith("file://"):
            return value
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            return value
        try:
            path = Path(value)
            if path.exists() and path.is_file():
                try:
                    return path.resolve().as_uri()
                except Exception:
                    return str(path.resolve())
        except OSError:
            return ""
        return value

    def _is_blocked_image_text(self, text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        return any(keyword in content for keyword in self._blocked_image_text_keywords)

    def _is_blocked_image_url(self, url: str) -> bool:
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
            for keyword in self._blocked_image_domain_keywords
        )

    @staticmethod
    def _url_contains_keyword(target: str, keyword: str) -> bool:
        content = normalize_text(target).lower()
        token = normalize_text(keyword).lower()
        if not content or not token:
            return False
        if len(token) <= 3:
            return bool(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
                    content,
                    flags=re.IGNORECASE,
                )
            )
        return token in content


    @staticmethod
    def _looks_like_media_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        media_cues = (
            "图",
            "图片",
            "壁纸",
            "头像",
            "视频",
            "发图",
            "发视频",
            "搜图",
            "找图",
            "pixiv",
            "b站",
            "bilibili",
            "抖音",
            "快手",
            "image",
            "video",
        )
        return any(cue in content for cue in media_cues)

    @staticmethod
    def _looks_like_deep_web_analysis_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "根据",
            "总结",
            "概括",
            "分段",
            "详细",
            "来历",
            "是谁",
            "什么来历",
            "这人是谁",
            "文章说了什么",
            "网页说了什么",
            "原文",
            "知乎",
            "上网搜",
            "去网上搜",
            "搜一下",
            "查一下",
            "分析",
        )
        if any(cue in content for cue in cues):
            return True
        return bool(re.search(r"https?://[^\s]+", content))

    @staticmethod
    def _should_auto_web_analysis(
        query: str, query_type: str, intent_text: str
    ) -> bool:
        if query_type in {"video", "image"}:
            return False

        content = normalize_text(f"{query}\n{intent_text}").lower()
        if not content:
            return False

        if query_type in {"person", "work", "tech"}:
            return True

        cues = (
            "是谁",
            "来历",
            "什么人",
            "叫什么",
            "哪里人",
            "总结",
            "概括",
            "分段",
            "详细",
            "深度",
            "证据",
            "根据",
            "原文",
            "来源",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _normalize_multimodal_query(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        content = re.sub(
            r"\bMULTIMODAL_EVENT(?:_AT)?\b", " ", content, flags=re.IGNORECASE
        )
        content = content.replace("用户发送多模态消息：", " ").replace(
            "用户@了你并发送多模态消息：", " "
        )
        content = content.replace("user sent multimodal message:", " ").replace(
            "user mentioned bot and sent multimodal message:",
            " ",
        )
        content = re.sub(
            r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]",
            " ",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(
            r"\b(?:image|video|record|audio|forward)\s*:\s*\S+",
            " ",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(r"\s+", " ", content).strip()
        parts = content.split()
        while parts and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", parts[0]):
            parts.pop(0)
        return " ".join(parts).strip()

    @staticmethod
    def _looks_like_image_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("image_request_cues")
            if normalize_text(cue)
        ]
        if not cues:
            return False
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_image_send_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        send_cues = [
            normalize_text(cue).lower()
            for cue in _pl.get_list("image_send_cues")
            if normalize_text(cue)
        ]
        if not send_cues:
            return False
        return ToolExecutor._looks_like_image_request(content) and any(
            cue in content for cue in send_cues
        )

    @staticmethod
    def _is_passive_multimodal_text(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        if re.fullmatch(
            r"(?:\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]\s*)+",
            content,
            flags=re.IGNORECASE,
        ):
            return True
        return (
            content.startswith("MULTIMODAL_EVENT")
            or content.startswith("用户发送多模态消息：")
            or content.startswith("用户@了你并发送多模态消息：")
            or content.lower().startswith("user sent multimodal message:")
            or content.lower().startswith(
                "user mentioned bot and sent multimodal message:"
            )
        )


    @staticmethod
    def _looks_like_qq_avatar_request(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        if "/avatar" in plain and ("target=self" in plain or "target=me" in plain):
            return True
        avatar_cues = _prompt_cues(
            "qq_avatar_request_cues", ("头像", "avatar", "profile")
        )
        qq_cues = _prompt_cues("qq_identity_cues", ("qq", "q号", "企鹅号", "uin"))
        self_avatar_cues = _prompt_cues(
            "self_avatar_cues",
            ("我的头像", "我头像", "my avatar", "我的qq头像", "我qq头像"),
        )
        other_avatar_cues = _prompt_cues("other_avatar_cues", ("他的头像", "她的头像"))
        return any(cue in content for cue in avatar_cues) and (
            any(cue in content for cue in qq_cues)
            or any(cue in content for cue in self_avatar_cues)
            or any(cue in content for cue in other_avatar_cues)
            or bool(re.search(r"[\u4e00-\u9fffa-z0-9_.-]{2,20}的头像", content))
        )

    @staticmethod
    def _contains_self_avatar_cue(text: str) -> bool:
        content = normalize_text(text)
        cues = _prompt_cues(
            "self_avatar_cues",
            ("我的头像", "我头像", "my avatar", "我的qq头像", "我qq头像"),
            lowercase=False,
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _extract_qq_number(text: str) -> str:
        content = normalize_text(text)
        match = re.search(r"(?<!\d)([1-9]\d{4,11})(?!\d)", content)
        if not match:
            return ""
        return str(match.group(1))

    @staticmethod
    def _normalize_qq_id(value: str) -> str:
        raw = str(value or "").strip()
        if re.fullmatch(r"[1-9]\d{4,11}", raw):
            return raw
        return ""

    @staticmethod
    def _extract_qq_from_at_segments(
        raw_segments: list[dict[str, Any]], bot_id: str
    ) -> str:
        bid = str(bot_id or "").strip()
        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type", "")).lower() != "at":
                continue
            data = seg.get("data", {}) or {}
            qq = str(
                data.get("qq") or data.get("user_id") or data.get("uid") or ""
            ).strip()
            if not qq or qq == "all":
                continue
            if bid and qq == bid:
                continue
            if re.fullmatch(r"[1-9]\d{4,11}", qq):
                return qq
        return ""

    async def _resolve_avatar_target_from_group(
        self,
        merged: str,
        fallback_user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> str:
        if group_id <= 0 or api_call is None:
            return ""

        candidates = self._extract_avatar_name_candidates(merged)
        if not candidates:
            return ""

        try:
            members = await api_call("get_group_member_list", group_id=group_id)
        except Exception:
            return ""
        if not isinstance(members, list):
            return ""

        # 先精确匹配，再包含匹配；命中多个时用第一条稳定返回
        exact_hits: list[str] = []
        fuzzy_hits: list[str] = []
        for item in members:
            if not isinstance(item, dict):
                continue
            uid = self._normalize_qq_id(
                str(
                    item.get("user_id")
                    or item.get("uin")
                    or item.get("uid")
                    or item.get("qq")
                    or ""
                )
            )
            if not uid:
                continue
            card = normalize_text(str(item.get("card", "")))
            nickname = normalize_text(str(item.get("nickname", "")))
            remark = normalize_text(str(item.get("remark", "")))
            display_name = normalize_text(str(item.get("display_name", "")))
            names = [n for n in [card, nickname, remark, display_name] if n]
            keys = [self._normalize_name_key(n) for n in names if n]
            keys = [k for k in keys if k]
            if not keys:
                continue

            for cand in candidates:
                ck = self._normalize_name_key(cand)
                if not ck:
                    continue
                if any(ck == key for key in keys):
                    exact_hits.append(uid)
                    break
                if any(ck in key or key in ck for key in keys):
                    fuzzy_hits.append(uid)
                    break

        if exact_hits:
            return exact_hits[0]
        if fuzzy_hits:
            return fuzzy_hits[0]
        return ""

    @staticmethod
    def _extract_avatar_name_candidates(text: str) -> list[str]:
        content = normalize_text(text)
        if not content:
            return []

        raw_hits: list[str] = []
        cmd_match = re.search(
            r"(?:^|\s)/(?:avatar|qqavatar)\s+([a-z0-9_.-]{2,20})(?:\s|$)",
            content,
            flags=re.IGNORECASE,
        )
        if cmd_match:
            raw_hits.append(cmd_match.group(1))
        patterns = (
            r"([\u4e00-\u9fffA-Za-z0-9_.-]{2,20})的头像",
            r"头像\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9_.-]{2,20})",
            r"发([\u4e00-\u9fffA-Za-z0-9_.-]{2,20})头像",
        )
        for pat in patterns:
            raw_hits.extend(re.findall(pat, content, flags=re.IGNORECASE))

        stopwords = {
            "他的",
            "她的",
            "ta的",
            "这个",
            "那个",
            "群里",
            "群里的",
            "qq群里",
            "qq群里的",
            "头像",
            "我的",
            "我",
        }
        uniq: list[str] = []
        seen: set[str] = set()
        for item in raw_hits:
            cand = normalize_text(str(item)).strip("\"'[]()锛堬級")
            if not cand or cand in stopwords:
                continue
            key = cand.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(cand)
        return uniq[:3]

    @staticmethod
    def _normalize_name_key(name: str) -> str:
        raw = normalize_text(name).lower()
        if not raw:
            return ""
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw)
