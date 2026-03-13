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


def _tool_trace_tag() -> str:
    trace_id = normalize_text(_tool_trace_id_ctx.get(""))
    if not trace_id:
        return ""
    return f" | trace={trace_id}"


def _prompt_cues(
    key: str, defaults: tuple[str, ...], lowercase: bool = True
) -> tuple[str, ...]:
    """Load cue list from prompts.yml.

    In LLM-first mode, keyword/regex cue routing is disabled globally.
    """
    _ = defaults
    if not _TOOLS_HEURISTIC_CUES_ENABLED:
        return ()
    raw = _pl.get_list(key)
    items = raw if isinstance(raw, list) else []
    normalized: list[str] = []
    for item in items:
        value = normalize_text(str(item))
        if not value:
            continue
        normalized.append(value.lower() if lowercase else value)
    return tuple(normalized)


class _SilentYTDLPLogger:
    def debug(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        _ytdlp_log.debug("ytdlp_warn%s: %s", _tool_trace_tag(), msg[:200])

    def error(self, msg: str) -> None:
        # 同一 trace 内重复错误（常见于 cookiesfrombrowser 不可用）只打印一次，避免刷屏
        trace_id = normalize_text(_tool_trace_id_ctx.get(""))
        message = normalize_text(str(msg))[:220]
        lower_message = message.lower()
        # B 站部分 AV 链接会触发 bvid 解析异常，由上层候选重试处理，这里降级为 debug。
        if "keyerror('bvid')" in lower_message or 'keyerror("bvid")' in lower_message:
            _ytdlp_log.debug(
                "ytdlp_bvid_fallback%s: %s", _tool_trace_tag(), message[:220]
            )
            return
        canonical = message
        if "could not find" in lower_message and "cookies database" in lower_message:
            canonical = "missing_browser_cookies_database"
        elif "could not copy" in lower_message and "cookie database" in lower_message:
            canonical = "missing_browser_cookies_database"
        elif "cookiesfrombrowser" in lower_message:
            canonical = "cookiesfrombrowser_error"
        elif "did not get any data blocks" in lower_message:
            canonical = "did_not_get_any_data_blocks"
        key = (trace_id or "-", canonical)
        if key in _ytdlp_error_dedupe:
            return
        if len(_ytdlp_error_dedupe) >= 2000:
            _ytdlp_error_dedupe.clear()
        _ytdlp_error_dedupe.add(key)
        _ytdlp_log.warning("ytdlp_error%s: %s", _tool_trace_tag(), msg[:300])


@dataclass(slots=True)
class ToolResult:
    ok: bool
    tool_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def _find_ffmpeg() -> str:
    """Locate ffmpeg executable, including common winget install locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget 安装的 ffmpeg 可能不在当前 PATH 中
    extra_dirs = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        extra_dirs.append(os.path.join(local_app, "Microsoft", "WinGet", "Links"))
        extra_dirs.append(os.path.join(local_app, "Microsoft", "WinGet", "Packages"))
        extra_dirs.append(os.path.join(local_app, "Programs", "ffmpeg", "bin"))
    program_files = os.environ.get("ProgramFiles", "")
    if program_files:
        extra_dirs.append(os.path.join(program_files, "ffmpeg", "bin"))
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    if program_files_x86:
        extra_dirs.append(os.path.join(program_files_x86, "ffmpeg", "bin"))
    user_profile = os.environ.get("USERPROFILE", "")
    if user_profile:
        extra_dirs.append(
            os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin")
        )
        extra_dirs.append(os.path.join(user_profile, "scoop", "shims"))
    for d in extra_dirs:
        candidate = (
            os.path.join(d, "ffmpeg.exe")
            if os.name == "nt"
            else os.path.join(d, "ffmpeg")
        )
        if os.path.isfile(candidate):
            # 把目录加到 PATH 以便 yt-dlp 也能找到
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return candidate
    # 最后兜底：imageio-ffmpeg 打包的可执行文件
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.isfile(bundled):
            bundled_dir = str(Path(bundled).resolve().parent)
            os.environ["PATH"] = bundled_dir + os.pathsep + os.environ.get("PATH", "")
            return bundled
    except Exception:
        pass
    return ""


def _write_netscape_cookie_file(cookie_str: str, domain: str) -> str:
    """Write cookies into a temporary Netscape-format file and return its path."""
    if not cookie_str.strip():
        return ""
    lines = ["# Netscape HTTP Cookie File"]
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        lines.append(f".{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    if len(lines) <= 1:
        return ""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookie_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class ToolExecutor:
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

        # 初始化混合视频解析器（bilix + yt-dlp）
        self._hybrid_resolver = None
        try:
            from core.video_resolver_hybrid import create_hybrid_resolver

            self._hybrid_resolver = create_hybrid_resolver(
                ytdlp_download_func=self._download_platform_video_sync,
                cache_dir=self._video_cache_dir,
                ffmpeg_location=self._ffmpeg_location,
            )
            _tool_log.info(
                "hybrid_resolver_enabled | bilix_available=%s",
                self._hybrid_resolver.bilix_resolver._bilix_available,
            )
        except Exception as e:
            _tool_log.warning("hybrid_resolver_init_failed | error=%s", str(e)[:100])

        # 平台专属 cookie（抖音/快手）
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
            "v.qq.com",
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

    def get_ai_method_schemas(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._ai_method_schemas]

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
        query = self._normalize_multimodal_query(query)
        query_type = "general"

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
                    "query": f"qq澶村儚 {target}",
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
            async with httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=True,
                headers=self._http_headers,
            ) as client:
                response = await client.get(url)
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
        key = f"{conversation_id}:{user_id}"
        q = normalize_text(query)
        prev = normalize_text(self._last_search_query.get(key, ""))
        followup_cfg = self._raw_config.get("search_followup", {})
        if not isinstance(followup_cfg, dict):
            followup_cfg = {}
        resend_media_cues_raw = followup_cfg.get("resend_media_cues", [])
        if not isinstance(resend_media_cues_raw, list):
            resend_media_cues_raw = []
        resend_media_cues = tuple(
            normalize_text(str(item))
            for item in resend_media_cues_raw
            if normalize_text(str(item))
        )

        short_followup_cues = (
            "人物",
            "二次元人物",
            "再找",
            "你找一个",
            "继续",
            "下载发给我",
            "下载发我",
            "给我下载",
            "帮我下载",
            "下载一下",
            *resend_media_cues,
        )

        merged = q
        if prev:
            # 只在明确“延续上一条搜索”时拼接，避免跨话题串台（例如人物搜索被串成搜图）。
            if any(cue in q for cue in short_followup_cues):
                if q not in prev:
                    merged = f"{prev} {q}".strip()
            elif self._is_generic_search_command(
                q
            ) and not self._looks_like_media_request(prev):
                merged = prev

        # 规范壁纸需求
        if "动漫" in merged and "壁纸" not in merged:
            merged = f"{merged} 壁纸".strip()
        if "二次元" in merged and "壁纸" not in merged:
            merged = f"{merged} 壁纸".strip()

        self._last_search_query[key] = merged
        return merged

    def _rewrite_safe_beauty_query(self, query: str) -> str:
        content = normalize_text(query)
        lower = content.lower()
        if not content:
            return content

        beauty_cues = ("美女", "帅哥", "颜值", "人像", "舞蹈", "小姐姐", "小哥哥")
        adult_cues = (
            "成人",
            "18禁",
            "无码",
            "porn",
            "nsfw",
            "r18",
            "露点",
            "性行为",
            "黄网站",
            "里番",
        )
        if any(cue in lower for cue in adult_cues):
            return content
        if any(cue in lower for cue in beauty_cues):
            if "非成人" not in content and "合规" not in content:
                video_cues = (
                    "视频",
                    "video",
                    "clip",
                    "b站",
                    "bilibili",
                    "抖音",
                    "快手",
                )
                suffix = (
                    " 短视频 非成人 合规"
                    if any(v in lower for v in video_cues)
                    else " 非成人 合规 日常"
                )
                return f"{content}{suffix}"
        return content

    def _build_ai_method_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "browser.fetch_url",
                "scope": "browser",
                "description": "访问网页并返回状态、最终链接、内容类型和文本摘要",
                "args_schema": {"url": "string"},
            },
            {
                "name": "browser.resolve_video",
                "scope": "browser",
                "description": "解析抖音/快手/B站/AcFun 或直链视频，返回可发送 video_url",
                "args_schema": {"url": "string"},
            },
            {
                "name": "browser.resolve_image",
                "scope": "browser",
                "description": "校验图片链接是否可直发，返回可发送 image_url",
                "args_schema": {"url": "string"},
            },
            {
                "name": "douyin.search_video",
                "scope": "douyin",
                "description": "在抖音搜索视频/图文详情链接，优先返回可解析或可发送结果",
                "args_schema": {
                    "query": "string",
                    "limit": "int(optional, default=5)",
                },
            },
            {
                "name": "browser.github_search",
                "scope": "browser",
                "description": "在 GitHub 搜索开源仓库，返回仓库名、星标、简介和链接",
                "args_schema": {
                    "query": "string",
                    "language": "string(optional)",
                    "stars_min": "int(optional)",
                    "sort": "stars|updated(optional)",
                },
            },
            {
                "name": "browser.github_readme",
                "scope": "browser",
                "description": '读取指定 GitHub 仓库 README 摘要，便于"学习这个仓库"',
                "args_schema": {
                    "repo": "owner/repo(optional)",
                    "url": "string(optional)",
                    "max_chars": "int(optional)",
                },
            },
            {
                "name": "local.read_text",
                "scope": "local",
                "description": "读取本地文本文件（受 allowlist 限制）",
                "args_schema": {"path": "string", "max_chars": "int(optional)"},
            },
            {
                "name": "local.media_from_path",
                "scope": "local",
                "description": "从本地路径发送图片或视频（受 allowlist 限制）",
                "args_schema": {"path": "string"},
            },
            {
                "name": "media.qq_avatar",
                "scope": "media",
                "description": "按 QQ 号或消息上下文获取 QQ 头像",
                "args_schema": {"qq": "string(optional)"},
            },
            {
                "name": "media.pick_image_from_message",
                "scope": "media",
                "description": "从当前消息里提取图片并返回可发送 image_url",
                "args_schema": {},
            },
            {
                "name": "media.pick_video_from_message",
                "scope": "media",
                "description": "从当前消息里提取视频并返回可发送 video_url",
                "args_schema": {},
            },
            {
                "name": "media.pick_audio_from_message",
                "scope": "media",
                "description": "从当前消息里提取语音/音频链接并返回文本说明",
                "args_schema": {},
            },
            {
                "name": "media.analyze_image",
                "scope": "media",
                "description": "识别图片内容；可指定 url，未指定时从当前消息里取图",
                "args_schema": {"url": "string(optional)"},
            },
            {
                "name": "video.analyze",
                "scope": "media",
                "description": (
                    "深度分析视频内容：提取关键信息并用视觉 AI 识别画面内容；"
                    "可抓取 B 站标签、弹幕热词、热评或抖音详情数据；"
                    "用户要求分析、评价、解说、总结视频时可使用此方法；"
                    "需要提供视频 URL，返回结构化分析结果。"
                ),
                "args_schema": {
                    "url": "string",
                    "depth": "metadata|rich_metadata|multimodal(optional, default=auto)",
                },
            },
        ]

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

    async def _execute_ai_method(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
        conversation_id: str,
        user_id: str,
        user_name: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
        raw_segments: list[dict[str, Any]],
        bot_id: str,
    ) -> ToolResult | None:
        name = normalize_text(method_name).lower()
        if not name:
            return None
        _tool_log.info(
            "tool_method_call%s | name=%s | args=%s",
            _tool_trace_tag(),
            name,
            clip_text(normalize_text(repr(method_args)), 220),
        )

        if name.startswith("browser.") and not self._tool_interface_browser_enable:
            return ToolResult(
                ok=False,
                tool_name=name,
                payload={"text": "浏览器方法已关闭"},
                error="browser_method_disabled",
            )
        if name.startswith("local.") and not self._tool_interface_local_enable:
            return ToolResult(
                ok=False,
                tool_name=name,
                payload={"text": "本地方法已关闭"},
                error="local_method_disabled",
            )

        if name == "browser.fetch_url":
            return await self._method_browser_fetch_url(name, method_args, query)
        if name == "browser.resolve_video":
            return await self._method_browser_resolve_video(name, method_args, query)
        if name == "browser.resolve_image":
            return await self._method_browser_resolve_image(
                name, method_args, query, message_text
            )
        if name == "douyin.search_video":
            return await self._method_douyin_search_video(name, method_args, query)
        if name == "browser.github_search":
            return await self._method_browser_github_search(
                name,
                method_args,
                query,
                message_text=message_text,
                group_id=group_id,
                api_call=api_call,
            )
        if name == "browser.github_readme":
            return await self._method_browser_github_readme(name, method_args, query)
        if name == "local.read_text":
            return await self._method_local_read_text(name, method_args)
        if name == "local.media_from_path":
            return await self._method_local_media_from_path(name, method_args)
        if name == "media.qq_avatar":
            qq = normalize_text(str(method_args.get("qq", "")))
            avatar_query = f"qq澶村儚 {qq}" if qq else query
            return await self._search_qq_avatar(
                query=avatar_query,
                message_text=message_text,
                user_id=user_id,
                user_name=user_name,
                group_id=group_id,
                api_call=api_call,
                raw_segments=raw_segments,
                bot_id=bot_id,
            )
        if name == "media.pick_image_from_message":
            return await self._method_media_pick_image(name, raw_segments)
        if name == "media.pick_video_from_message":
            return await self._method_media_pick_video(name, raw_segments)
        if name == "media.pick_audio_from_message":
            return await self._method_media_pick_audio(name, raw_segments)
        if name == "media.analyze_image":
            return await self._method_media_analyze_image(
                method_name=name,
                method_args=method_args,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                conversation_id=conversation_id,
                api_call=api_call,
            )
        if name == "video.analyze":
            return await self._method_video_analyze(
                method_name=name,
                method_args=method_args,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                conversation_id=conversation_id,
            )

        return ToolResult(
            ok=False,
            tool_name=name,
            payload={"text": f"方法 {name} 不存在。"},
            error=f"unsupported_ai_method:{name}",
        )

    async def _method_browser_fetch_url(
        self, method_name: str, method_args: dict[str, Any], query: str
    ) -> ToolResult:
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(query)
            url = urls[0] if urls else ""
        url = self._unwrap_redirect_url(url)
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

    async def _fetch_webpage_summary(self, url: str) -> dict[str, Any] | None:
        target = self._unwrap_redirect_url(url)
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            return None
        if self._is_blocked_video_url(target) or self._is_blocked_image_url(target):
            return None
        if not self._is_safe_public_http_url(target):
            return None

        # 为特定平台添加cookie支持
        headers = dict(self._http_headers)
        try:
            host = normalize_text(urlparse(target).netloc).lower()
            if (
                "bilibili.com" in host or host.endswith("b23.tv")
            ) and self._bilibili_cookie:
                headers["Cookie"] = self._bilibili_cookie
            elif (
                "douyin.com" in host or "iesdouyin.com" in host
            ) and self._douyin_cookie:
                headers["Cookie"] = self._douyin_cookie
            elif (
                "kuaishou.com" in host or "chenzhongtech.com" in host
            ) and self._kuaishou_cookie:
                headers["Cookie"] = self._kuaishou_cookie
        except Exception:
            pass

        timeout = httpx.Timeout(
            float(self._web_fetch_timeout_seconds),
            connect=min(8.0, float(self._web_fetch_timeout_seconds)),
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(target)
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
            url = self._unwrap_redirect_url(normalize_text(item.url))
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

    async def _method_browser_github_search(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str = "",
        group_id: int = 0,
        api_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> ToolResult:
        if not self._tool_interface_github_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "GitHub 方法已关闭。"},
                error="github_method_disabled",
            )

        raw_query = normalize_text(str(method_args.get("query", ""))) or normalize_text(
            query
        )
        if not raw_query:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请告诉我你要在 GitHub 搜什么。"},
                error="empty_query",
            )

        search_query = raw_query
        language = normalize_text(str(method_args.get("language", "")))
        if language:
            search_query = f"{search_query} language:{language}"

        stars_min = method_args.get("stars_min", 0)
        try:
            stars_min = max(0, int(stars_min))
        except Exception:
            stars_min = 0
        if stars_min > 0:
            search_query = f"{search_query} stars:>={stars_min}"

        sort = normalize_text(str(method_args.get("sort", ""))).lower()
        if sort not in {"updated", "stars"}:
            sort = "stars"

        params = {
            "q": search_query,
            "sort": sort,
            "order": "desc",
            "per_page": self._github_search_per_page,
        }

        endpoint = f"{self._github_api_base}/search/repositories"
        try:
            async with httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=True,
                headers=self._github_headers,
            ) as client:
                response = await client.get(endpoint, params=params)
        except Exception as exc:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_failed:{exc}",
                human_reason="GitHub API 暂时不可用，已改用网页搜索兜底。",
            )

        if response.status_code == 403:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason="github_rate_limited",
                human_reason="GitHub API 触发限流，已改用网页搜索兜底。",
            )
        if response.status_code >= 400:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_http_{response.status_code}",
                human_reason=f"GitHub API 返回 {response.status_code}，已改用网页搜索兜底。",
            )

        try:
            data = response.json()
        except Exception as exc:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason=f"github_search_parse_failed:{exc}",
                human_reason="GitHub 返回数据解析失败，已改用网页搜索兜底。",
            )

        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list) or not items:
            fallback_terms = re.findall(r"[A-Za-z0-9_.-]{2,}", raw_query)
            alt_query = " ".join(dict.fromkeys(fallback_terms[:3]))
            if alt_query and alt_query.lower() != raw_query.lower():
                alt_params = dict(params)
                alt_params["q"] = alt_query
                try:
                    async with httpx.AsyncClient(
                        timeout=self._http_timeout,
                        follow_redirects=True,
                        headers=self._github_headers,
                    ) as client:
                        alt_resp = await client.get(endpoint, params=alt_params)
                    if alt_resp.status_code < 400:
                        alt_data = alt_resp.json()
                        alt_items = (
                            alt_data.get("items", [])
                            if isinstance(alt_data, dict)
                            else []
                        )
                        if isinstance(alt_items, list) and alt_items:
                            items = alt_items
                except Exception:
                    pass
        if not isinstance(items, list) or not items:
            return await self._github_search_web_fallback(
                method_name=method_name,
                raw_query=raw_query,
                reason="github_search_empty",
                human_reason="GitHub API 未命中，已改用网页搜索兜底。",
            )

        results: list[dict[str, Any]] = []
        evidence: list[dict[str, str]] = []
        lines = [
            f"GitHub 里“{raw_query}”我先给你找了 {min(len(items), self._github_search_per_page)} 个："
        ]
        for idx, item in enumerate(items[: self._github_search_per_page], start=1):
            if not isinstance(item, dict):
                continue
            full_name = normalize_text(str(item.get("full_name", "")))
            html_url = normalize_text(str(item.get("html_url", "")))
            description = normalize_text(str(item.get("description", "")))
            language_name = normalize_text(str(item.get("language", "")))
            stars = item.get("stargazers_count", 0)
            updated = normalize_text(str(item.get("updated_at", "")))
            if not full_name or not html_url:
                continue

            results.append(
                {
                    "full_name": full_name,
                    "url": html_url,
                    "description": description,
                    "language": language_name,
                    "stars": stars,
                    "updated_at": updated,
                }
            )
            star_text = f"{stars}★" if isinstance(stars, int) else "未知★"
            extra = f" | {language_name}" if language_name else ""
            desc_short = clip_text(description, 72) if description else "无简介"
            lines.append(f"{idx}. {full_name} ({star_text}{extra})")
            lines.append(f"   {desc_short}")
            lines.append(f"   {html_url}")
            evidence.append(
                {"title": full_name, "point": desc_short, "source": html_url}
            )

        auto_download_notice = ""
        should_auto_download = (
            bool(api_call)
            and int(group_id or 0) > 0
            and self._looks_like_download_request_text(f"{message_text}\n{raw_query}")
        )
        if should_auto_download and results:
            ok_auto, auto_text, auto_payload = await self._try_auto_upload_github_asset(
                raw_query=raw_query,
                results=results,
                message_text=message_text,
                group_id=int(group_id),
                api_call=api_call,
            )
            if ok_auto:
                payload = {
                    "text": auto_text,
                    "query": raw_query,
                    "results": results,
                    "evidence": evidence,
                }
                payload.update(auto_payload)
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload=payload,
                    evidence=evidence,
                )
            if auto_text:
                auto_download_notice = auto_text

        if len(lines) == 1:
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={"text": f"GitHub 上没拿到可用仓库结果：{raw_query}"},
            )

        if auto_download_notice:
            lines.insert(1, auto_download_notice)

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(lines),
                "query": raw_query,
                "results": results,
                "evidence": evidence,
            },
            evidence=evidence,
        )

    async def _try_auto_upload_github_asset(
        self,
        raw_query: str,
        results: list[dict[str, Any]],
        message_text: str,
        group_id: int,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> tuple[bool, str, dict[str, Any]]:
        if not api_call or group_id <= 0 or not results:
            return False, "", {}
        try:
            # 复用 Agent 里现成的下载/验签/上传链路，避免重复维护两套逻辑。
            from core.agent_tools import _handle_smart_download
        except Exception as exc:
            return (
                False,
                f"自动下载不可用（工具加载失败：{clip_text(str(exc), 80)}）",
                {},
            )

        errors: list[str] = []
        for item in results[:3]:
            repo_url = normalize_text(str(item.get("url", "")))
            repo_name = normalize_text(str(item.get("full_name", "")))
            if not repo_url:
                continue
            prefer_ext, file_name = self._guess_download_preferences(
                raw_query=raw_query,
                message_text=message_text,
                repo_name=repo_name,
            )
            args: dict[str, Any] = {
                "url": repo_url,
                "query": raw_query,
                "kind": "file",
                "upload": True,
                "group_id": int(group_id),
            }
            if prefer_ext:
                args["prefer_ext"] = prefer_ext
            if file_name:
                args["file_name"] = file_name
            try:
                dl_result = await _handle_smart_download(
                    args,
                    {
                        "api_call": api_call,
                        "group_id": int(group_id),
                        "tool_executor": self,
                        "config": self._raw_config,
                    },
                )
            except Exception as exc:
                errors.append(clip_text(f"{repo_name or repo_url}: {exc}", 120))
                continue

            if not bool(getattr(dl_result, "ok", False)):
                err_text = normalize_text(
                    str(getattr(dl_result, "display", ""))
                ) or normalize_text(str(getattr(dl_result, "error", "")))
                if err_text:
                    errors.append(
                        clip_text(f"{repo_name or repo_url}: {err_text}", 120)
                    )
                continue

            data = getattr(dl_result, "data", {}) or {}
            local_file = normalize_text(str(data.get("local_file", "")))
            download_url = normalize_text(str(data.get("download_url", "")))
            source_url = normalize_text(str(data.get("source_url", ""))) or repo_url
            file_label = Path(local_file).name if local_file else (file_name or "文件")
            text = normalize_text(str(getattr(dl_result, "display", "")))
            if not text:
                text = f"已下载并上传群文件：{file_label}"
                if download_url:
                    text += f"\n下载源：{download_url}"
            payload = {
                "downloaded_file": local_file,
                "download_url": download_url,
                "source_url": source_url,
                "uploaded": True,
            }
            return True, text, payload

        if errors:
            return False, f"自动下载尝试失败：{errors[0]}。先给你可靠链接。", {}
        return False, "", {}

    async def _github_search_web_fallback(
        self,
        method_name: str,
        raw_query: str,
        reason: str,
        human_reason: str,
    ) -> ToolResult:
        try:
            rows = await self.search_engine.search(f"site:github.com {raw_query}")
        except Exception:
            rows = []

        picked: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in rows:
            url = self._unwrap_redirect_url(normalize_text(getattr(item, "url", "")))
            title = normalize_text(getattr(item, "title", ""))
            snippet = normalize_text(getattr(item, "snippet", ""))
            if "github.com/" not in url.lower():
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            picked.append({"title": title or "GitHub", "snippet": snippet, "url": url})
            if len(picked) >= self._github_search_per_page:
                break

        if not picked:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={},
                error=reason,
            )

        lines = [f"{human_reason}", f"GitHub 相关结果（{raw_query}）："]
        evidence: list[dict[str, str]] = []
        for idx, item in enumerate(picked, start=1):
            title = clip_text(normalize_text(item.get("title", "")) or "GitHub", 68)
            snippet = clip_text(normalize_text(item.get("snippet", "")) or "无摘要", 88)
            url = normalize_text(item.get("url", ""))
            lines.append(f"{idx}. {title}")
            lines.append(f"   {snippet}")
            lines.append(f"   {url}")
            evidence.append({"title": title, "point": snippet, "source": url})

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(lines),
                "query": raw_query,
                "results": picked,
                "evidence": evidence,
                "fallback": "web_search",
            },
            evidence=evidence,
        )

    async def _method_browser_github_readme(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
    ) -> ToolResult:
        if not self._tool_interface_github_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "GitHub 方法已关闭。"},
                error="github_method_disabled",
            )

        repo = normalize_text(str(method_args.get("repo", "")))
        if not repo:
            url_value = normalize_text(str(method_args.get("url", "")))
            if url_value:
                repo = self._extract_github_repo_from_text(url_value)
        if not repo:
            repo = self._extract_github_repo_from_text(query)
        if not repo:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "请给我仓库名（owner/repo）或 GitHub 仓库链接。"},
                error="repo_required",
            )

        max_chars = method_args.get("max_chars", self._github_readme_max_chars)
        try:
            max_chars = max(200, min(12000, int(max_chars)))
        except Exception:
            max_chars = self._github_readme_max_chars

        repo_endpoint = f"{self._github_api_base}/repos/{repo}"
        readme_endpoint = f"{repo_endpoint}/readme"
        repo_resp = None
        readme_resp = None
        try:
            async with httpx.AsyncClient(
                timeout=self._http_timeout,
                follow_redirects=True,
                headers=self._github_headers,
            ) as client:
                repo_resp = await client.get(repo_endpoint)
                readme_resp = await client.get(readme_endpoint)
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={},
                error=f"github_readme_failed:{exc}",
            )

        if repo_resp is None or repo_resp.status_code >= 400:
            status = repo_resp.status_code if repo_resp is not None else 0
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": f"仓库 {repo} 不存在或不可访问。"},
                error=f"github_repo_http_{status}",
            )

        try:
            repo_data = repo_resp.json()
        except Exception:
            repo_data = {}

        full_name = normalize_text(str(repo_data.get("full_name", ""))) or repo
        html_url = (
            normalize_text(str(repo_data.get("html_url", "")))
            or f"https://github.com/{repo}"
        )
        description = normalize_text(str(repo_data.get("description", "")))
        stars = repo_data.get("stargazers_count", 0)
        language = normalize_text(str(repo_data.get("language", "")))

        readme_text = ""
        if readme_resp is not None and readme_resp.status_code < 400:
            try:
                readme_data = readme_resp.json()
            except Exception:
                readme_data = {}
            content_b64 = normalize_text(str(readme_data.get("content", "")))
            encoding = normalize_text(str(readme_data.get("encoding", ""))).lower()
            if content_b64 and encoding == "base64":
                try:
                    decoded = base64.b64decode(
                        content_b64.encode("utf-8"), validate=False
                    )
                    readme_text = decoded.decode("utf-8", errors="ignore")
                except Exception:
                    readme_text = ""

        cleaned = self._clean_markdown_text(readme_text) if readme_text else ""
        cleaned = clip_text(normalize_text(cleaned), max_chars)

        summary_lines = [f"仓库：{full_name}"]
        if isinstance(stars, int):
            summary_lines.append(f"Stars：{stars}")
        if language:
            summary_lines.append(f"语言：{language}")
        if description:
            summary_lines.append(f"简介：{description}")
        summary_lines.append(f"链接：{html_url}")
        if cleaned:
            summary_lines.append(f"README 摘要：{cleaned}")
        else:
            summary_lines.append("README 摘要：这个仓库没有拿到可读 README。")
        evidence = [
            {
                "title": full_name,
                "point": clip_text(cleaned or description or "仓库元数据已获取。", 180),
                "source": html_url,
            }
        ]

        return ToolResult(
            ok=True,
            tool_name=method_name,
            payload={
                "text": "\n".join(summary_lines),
                "repo": full_name,
                "repo_url": html_url,
                "readme_excerpt": cleaned,
                "evidence": evidence,
            },
            evidence=evidence,
        )

    async def _method_browser_resolve_video(
        self, method_name: str, method_args: dict[str, Any], query: str
    ) -> ToolResult:
        url = normalize_text(str(method_args.get("url", "")))
        if not url:
            urls = self._extract_urls(query)
            url = urls[0] if urls else ""
        url = self._unwrap_redirect_url(url)
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
        keyword = self._normalize_multimodal_query(keyword)
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
            candidate = self._unwrap_redirect_url(normalize_text(item.url))
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
                candidate = self._unwrap_redirect_url(normalize_text(row.url))
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
                candidate = self._unwrap_redirect_url(normalize_text(row.url))
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
        target = self._unwrap_redirect_url(url)
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
        target = self._unwrap_redirect_url(source_url)
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
        url = self._unwrap_redirect_url(url)
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
        candidates: list[str] = []
        candidate_meta: list[dict[str, str]] = []
        if explicit_url:
            resolved_explicit = self._unwrap_redirect_url(explicit_url)
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
        if not candidates:
            message_candidates = self._extract_message_media_urls(
                raw_segments, media_type="image"
            )
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
            source = self._unwrap_redirect_url(url)
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
                else "这张图我已经尝试识别了，但内容太模糊或信息不足，结果不稳定 你可以发更清晰截图或告诉我要重点看哪一块"
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
                        else "这张图这次没识别出来，请发更清晰的图片，或告诉我要重点看哪一块。"
                    )
                )
            },
            error="vision_analyze_failed",
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
        url = self._unwrap_redirect_url(url)
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
                candidate = self._unwrap_redirect_url(normalize_text(raw_candidate))
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

    async def _vision_uncertain_web_fallback(
        self, query: str, message_text: str
    ) -> ToolResult | None:
        merged = self._normalize_multimodal_query(f"{query}\n{message_text}")
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
            prompt_hint = self._normalize_multimodal_query(f"{query}\n{message_text}")
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
        merged = self._normalize_multimodal_query(f"{query}\n{message_text}")
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
        merged = self._normalize_multimodal_query(f"{query}\n{message_text}")
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
                resolved = ToolExecutor._unwrap_redirect_url(url)
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
        if not ToolExecutor._is_known_image_signature(head):
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
            provider_hint = normalize_text(self._vision_provider).lower()
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
            model_name or "-",
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

        payload = {
            "model": model_name,
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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        candidates = self._candidate_openai_bases(
            base_url=base_url, prefer_v1=prefer_v1
        )

        for base in candidates:
            url = f"{base}/chat/completions"
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                    resp = await client_http.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue

            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                continue
            message = (
                choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            )
            content = message.get("content", "")
            if isinstance(content, str):
                text = normalize_text(content)
                if text:
                    return text
                continue
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        parts.append(normalize_text(str(item.get("text", ""))))
                text = normalize_text("".join(parts))
                if text:
                    return text

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

    async def _inspect_platform_video_metadata(self, source_url: str) -> dict[str, Any]:
        if YoutubeDL is None:
            return {}
        url = self._unwrap_redirect_url(source_url)
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
        urls = [self._unwrap_redirect_url(url) for url in self._extract_urls(query)]
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
            candidate = self._unwrap_redirect_url(normalize_text(item.url))
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
                candidate = self._unwrap_redirect_url(normalize_text(item.url))
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
            candidate = self._unwrap_redirect_url(candidate)
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
            r"(?:^|\s)platform\s*=\s*(bilibili|douyin|kuaishou|acfun)(?:\s|$)", lower
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

        if explicit_platform == "bilibili":
            out.append(f"{content} site:bilibili.com/video")
        elif explicit_platform == "douyin":
            out.append(f"{content} site:douyin.com/video")
        elif explicit_platform == "kuaishou":
            out.append(f"{content} site:kuaishou.com/short-video")
        elif explicit_platform == "acfun":
            out.append(f"{content} site:acfun.cn/v/ac")

        # 未显式指定平台时给一组通用详情页搜索
        if not out:
            out.extend(
                [
                    f"{content} site:bilibili.com/video",
                    f"{content} site:douyin.com/video",
                    f"{content} site:kuaishou.com/short-video",
                    f"{content} site:acfun.cn/v/ac",
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

    async def _music_search(
        self, tool_args: dict[str, Any], message_text: str
    ) -> ToolResult:
        keyword = normalize_matching_text(str(tool_args.get("keyword", "")))
        title = normalize_matching_text(str(tool_args.get("title", "")))
        artist = normalize_matching_text(str(tool_args.get("artist", "")))
        if not keyword and title:
            keyword = f"{title} {artist}".strip()
        if not keyword:
            keyword = normalize_matching_text(message_text)
        if not keyword:
            return ToolResult(ok=False, tool_name="music_search", error="empty_keyword")
        # 去掉常见前缀
        for prefix in (
            "点歌",
            "听歌",
            "放歌",
            "搜歌",
            "播放",
            "来首",
            "来一首",
            "唱",
            "/music",
            "/song",
        ):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix) :].strip()
        if not keyword:
            return ToolResult(ok=False, tool_name="music_search", error="empty_keyword")

        results = await self._music_engine.search(
            keyword, limit=5, title=title, artist=artist
        )
        if not results:
            return ToolResult(
                ok=False,
                tool_name="music_search",
                payload={"text": f"没找到「{keyword}」相关的歌曲。"},
                error="no_results",
            )

        filtered_results = results
        if title:
            intent = self._music_engine._build_keyword_intent(
                keyword=keyword, title=title, artist=artist
            )
            exact: list[MusicSearchResult] = []
            title_hits: list[MusicSearchResult] = []
            for row in results:
                if (
                    self._music_engine._title_match_level(intent.title_hint, row.name)
                    < 2
                ):
                    continue
                if self._music_engine._should_avoid_version(
                    row.name, intent.title_hint
                ):
                    continue
                title_hits.append(row)
                if artist and not self._music_engine._artist_matches_intent(
                    row.artist, intent
                ):
                    continue
                exact.append(row)

            if exact:
                filtered_results = exact
            else:
                if artist and title_hits:
                    return ToolResult(
                        ok=False,
                        tool_name="music_search",
                        payload={
                            "text": f"找到同名候选，但和歌手「{artist}」不一致，不能直接替你播。",
                            "results": [],
                        },
                        error="artist_mismatch",
                    )
                return ToolResult(
                    ok=False,
                    tool_name="music_search",
                    payload={
                        "text": f"没找到和《{title}》明确匹配的歌曲，近似名称不能直接当成同一首播。",
                        "results": [],
                    },
                    error="no_exact_match",
                )

        lines = [f"🎵 搜索「{keyword}」找到 {len(filtered_results)} 首歌："]
        for i, s in enumerate(filtered_results, 1):
            dur = (
                f" ({s.duration_ms // 1000 // 60}:{s.duration_ms // 1000 % 60:02d})"
                if s.duration_ms
                else ""
            )
            lines.append(f"{i}. {s.name} - {s.artist}{dur}")
        lines.append("\n发送「点歌 歌名」可以直接播放。")
        return ToolResult(
            ok=True,
            tool_name="music_search",
            payload={
                "text": "\n".join(lines),
                "results": [
                    {"id": s.song_id, "name": s.name, "artist": s.artist}
                    for s in filtered_results
                ],
            },
        )

    async def _music_play_by_id(
        self,
        tool_args: dict[str, Any],
        api_call: Callable[..., Awaitable[Any]] | None,
        group_id: int,
    ) -> ToolResult:
        song_id = int(tool_args.get("song_id", 0) or 0)
        if song_id <= 0:
            return ToolResult(
                ok=False, tool_name="music_play_by_id", error="invalid_song_id"
            )

        # 从 tool_args 获取歌曲信息
        song_name = normalize_matching_text(str(tool_args.get("song_name", "")))
        artist = normalize_matching_text(str(tool_args.get("artist", "")))
        keyword = normalize_matching_text(str(tool_args.get("keyword", "")))
        require_checker = getattr(
            self._music_engine, "_requires_verified_original", None
        )
        if callable(require_checker):
            require_verified_original = bool(require_checker(keyword))
        else:
            require_verified_original = bool(
                MusicEngine._requires_verified_original(keyword)
            )

        matched_row: MusicSearchResult | None = None
        if keyword or song_name:
            query = keyword or f"{song_name} {artist}".strip()
            try:
                candidates = await self._music_engine.search(
                    query, limit=12, title=song_name, artist=artist
                )
            except Exception:
                candidates = []
            for row in candidates:
                if int(getattr(row, "song_id", 0) or 0) != song_id:
                    continue
                matched_row = row
                if normalize_text(getattr(row, "name", "")):
                    song_name = normalize_matching_text(str(row.name))
                if normalize_text(getattr(row, "artist", "")):
                    artist = normalize_matching_text(str(row.artist))
                break

        # 直接根据 ID 播放
        from core.music import MusicSearchResult

        song = MusicSearchResult(
            song_id=song_id,
            name=song_name,
            artist=artist,
            album="",
            duration_ms=0,
            source="netease",
        )
        if matched_row is not None:
            song.name = (
                normalize_text(str(getattr(matched_row, "name", song.name)))
                or song.name
            )
            song.artist = (
                normalize_text(str(getattr(matched_row, "artist", song.artist)))
                or song.artist
            )
            song.duration_ms = int(getattr(matched_row, "duration_ms", 0) or 0)
            song.source = (
                normalize_text(str(getattr(matched_row, "source", song.source)))
                or song.source
            )
            song.source_url = normalize_text(
                str(getattr(matched_row, "source_url", ""))
            )

        result = await self._music_engine._play_song(
            song,
            as_voice=True,
            require_verified_original=require_verified_original,
        )

        if not result.ok:
            return ToolResult(
                ok=False,
                tool_name="music_play_by_id",
                payload={"text": result.message or "播放失败。"},
                error=result.error,
            )

        payload: dict[str, Any] = {"text": result.message}

        # 优先给完整音频文件
        if result.audio_path and api_call:
            payload["audio_file"] = result.audio_path
            if result.silk_path:
                payload["audio_file_silk"] = result.silk_path
        elif result.silk_path and api_call:
            payload["audio_file"] = result.silk_path
        elif result.silk_b64 and api_call:
            payload["record_b64"] = result.silk_b64

        return ToolResult(ok=True, tool_name="music_play_by_id", payload=payload)

    async def _music_play(
        self,
        tool_args: dict[str, Any],
        message_text: str,
        api_call: Callable[..., Awaitable[Any]] | None,
        group_id: int,
    ) -> ToolResult:
        keyword = normalize_matching_text(str(tool_args.get("keyword", "")))
        title = normalize_matching_text(str(tool_args.get("title", "")))
        artist = normalize_matching_text(str(tool_args.get("artist", "")))
        if not keyword and title:
            keyword = f"{title} {artist}".strip()
        if not keyword:
            keyword = normalize_matching_text(message_text)
        if not keyword:
            return ToolResult(ok=False, tool_name="music_play", error="empty_keyword")
        for prefix in (
            "点歌",
            "听歌",
            "放歌",
            "搜歌",
            "播放",
            "来首",
            "来一首",
            "唱",
            "/music",
            "/song",
        ):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix) :].strip()
        if not keyword:
            return ToolResult(ok=False, tool_name="music_play", error="empty_keyword")

        result = await self._music_engine.play(
            keyword, as_voice=True, title=title, artist=artist
        )
        if not result.ok:
            return ToolResult(
                ok=False,
                tool_name="music_play",
                payload={"text": result.message or "播放失败。"},
                error=result.error,
            )

        payload: dict[str, Any] = {"text": result.message}

        # 优先给完整音频文件，发送层可按策略决定“整段发 / 分段发 / 回退 silk”。
        if result.audio_path and api_call:
            payload["audio_file"] = result.audio_path
            if result.silk_path:
                payload["audio_file_silk"] = result.silk_path
        elif result.silk_path and api_call:
            payload["audio_file"] = result.silk_path
        elif result.silk_b64 and api_call:
            # 仅在无本地文件时才用 base64
            payload["record_b64"] = result.silk_b64

        return ToolResult(ok=True, tool_name="music_play", payload=payload)

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
                    self._unwrap_redirect_url(raw_url),
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

    @classmethod
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

    @classmethod
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
    def _unwrap_redirect_url(url: str) -> str:
        raw = normalize_text(url)
        if not raw:
            return ""
        try:
            parsed = urlparse(raw)
        except Exception:
            return raw
        host = normalize_text(parsed.netloc).lower()
        if not host:
            return raw
        if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
            query_map = parse_qs(parsed.query)
            uddg = query_map.get("uddg", [])
            if uddg:
                return normalize_text(unquote(str(uddg[0])))

        # 抖音精选页常见链接（jingxuan?modal_id=xxxx），转换成视频详情页提高解析成功率
        if "douyin.com" in host:
            query_map = parse_qs(parsed.query)
            for key in ("modal_id", "aweme_id", "item_id"):
                values = query_map.get(key, [])
                if values:
                    vid = normalize_text(str(values[0]))
                    if vid.isdigit():
                        return f"https://www.douyin.com/video/{vid}"
        # B站链接规范化：BVxxxx -> /video/BVxxxx；去掉常见追踪参数，保留 p/t
        if "bilibili.com" in host:
            path = normalize_text(unquote(parsed.path or ""))
            match_bv = re.search(r"/(BV[0-9A-Za-z]{10})", path, flags=re.IGNORECASE)
            match_av = re.search(r"/(av\d+)", path, flags=re.IGNORECASE)
            if match_bv:
                base = f"https://www.bilibili.com/video/{match_bv.group(1)}"
                q = parse_qs(parsed.query)
                kept: dict[str, list[str]] = {}
                for key in ("p", "t"):
                    vals = q.get(key, [])
                    if vals:
                        kept[key] = vals
                if kept:
                    return f"{base}?{urlencode({k: v[0] for k, v in kept.items()})}"
                return base
            if match_av:
                base = f"https://www.bilibili.com/video/{match_av.group(1)}"
                q = parse_qs(parsed.query)
                kept: dict[str, list[str]] = {}
                for key in ("p", "t"):
                    vals = q.get(key, [])
                    if vals:
                        kept[key] = vals
                if kept:
                    return f"{base}?{urlencode({k: v[0] for k, v in kept.items()})}"
                return base
        return raw

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
            blocked_cues = ("/search", "/rank", "/bangumi")
            if any(cue in path for cue in blocked_cues):
                return False
            if re.search(r"/v/ac\d+", path, flags=re.IGNORECASE):
                return True
            if "ac=" in query:
                return True
            short_path = path.strip("/")
            return bool(short_path and len(short_path) >= 6)

        if "youku.com" in host:
            # 优酷视频详情页: /v_show/id_xxx.html
            if "/v_show/" in path:
                return True
            if re.search(r"/id_[a-zA-Z0-9]+", path):
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
        url = self._unwrap_redirect_url(source_url)
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
        url = self._unwrap_redirect_url(source_url)
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
            url = self._unwrap_redirect_url(normalize_text(value))
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
                url = self._unwrap_redirect_url(normalize_text(value))
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
        if ToolExecutor._is_known_image_signature(head):
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

    def _looks_like_github_request(self, text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        if _shared_github_request(content, config=self._raw_config):
            return True
        return bool(
            re.search(
                r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                content,
                flags=re.IGNORECASE,
            )
        )

    def _looks_like_repo_readme_request(self, text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        if _shared_repo_readme_request(content, config=self._raw_config):
            return True
        plain = re.sub(r"\s+", "", content.lower())
        if "/readme" in plain:
            return True
        return bool(
            re.search(
                r"(?:^|\s)readme\s+[a-z0-9_.-]+/[a-z0-9_.-]+(?:\s|$)",
                content,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _looks_like_download_request_text(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        if "/download" in plain:
            return True
        cues = (
            "下载",
            "发给我",
            "发我",
            "安装包",
            "启动器",
            "客户端",
            "release",
            "releases",
            "installer",
            "setup",
        )
        return any(cue in content for cue in cues)

    def _looks_like_software_download_request(self, text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if not self._looks_like_download_request_text(content):
            return False
        # 避免把“下载视频/图片”误判成软件安装包下载。
        media_cues = ("视频", "图", "图片", "壁纸", "抖音", "快手", "b站", "bilibili")
        if any(cue in content for cue in media_cues):
            return False
        software_cues = (
            "hmcl",
            "启动器",
            "安装包",
            "客户端",
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
    def _extract_github_repo_from_text(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""

        url_match = re.search(
            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
            content,
            flags=re.IGNORECASE,
        )
        if url_match:
            owner = url_match.group(1)
            repo = url_match.group(2)
            repo = re.sub(r"\.git$", "", repo, flags=re.IGNORECASE)
            return f"{owner}/{repo}"

        token_match = re.search(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b", content)
        if token_match:
            owner = token_match.group(1)
            repo = re.sub(r"\.git$", "", token_match.group(2), flags=re.IGNORECASE)
            if owner.lower() not in {"http", "https"}:
                return f"{owner}/{repo}"
        return ""

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
            if not isinstance(image_old, list):
                image_old = []
            if not isinstance(video_old, list):
                video_old = []
            if images:
                state["image"] = (images + image_old)[: self._recent_media_cache_limit]
            if videos:
                state["video"] = (videos + video_old)[: self._recent_media_cache_limit]
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
    def _looks_like_vision_web_lookup_request(text: str) -> bool:
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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
    def _looks_like_image_request(text: str) -> bool:
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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

    def _looks_like_video_send_request(self, text: str) -> bool:
        content = self._normalize_multimodal_query(text).lower()
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

    @staticmethod
    def _looks_like_image_analysis_request(text: str) -> bool:
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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
    def _looks_like_music_request(text: str) -> bool:
        content = ToolExecutor._normalize_multimodal_query(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        explicit_tokens = (
            "/music",
            "/song",
            "action=music",
            "tool=music",
            "intent=music",
            "music=1",
        )
        if any(token in plain for token in explicit_tokens):
            return True
        explicit_patterns = (
            r"(?:^|\s)/(?:music|song)(?:\s|$)",
            r"(?:^|\s)(?:action|tool|intent)\s*=\s*music(?:\s|$)",
        )
        return any(re.search(pattern, content) for pattern in explicit_patterns)

    def _looks_like_video_request(self, text: str) -> bool:
        content = self._normalize_multimodal_query(text)
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
        content = self._normalize_multimodal_query(text).lower()
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
        content = ToolExecutor._normalize_multimodal_query(text).lower()
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
