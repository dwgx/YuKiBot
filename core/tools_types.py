"""tools.py 公共类型和辅助函数。

从 core/tools.py 拆分。包含:
- ToolResult: 工具执行结果
- _SilentYTDLPLogger: yt-dlp 静默日志
- _find_ffmpeg: FFmpeg 定位
- _write_netscape_cookie_file: cookie 文件写入
- _tool_trace_tag / _prompt_cues: 工具追踪辅助
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx
from utils.intent import (
    looks_like_github_request as _shared_github_request,
)
from utils.intent import (
    looks_like_repo_readme_request as _shared_repo_readme_request,
)
from utils.intent import (
    looks_like_video_request as _shared_video_request,
)
from utils.text import clip_text, normalize_matching_text, normalize_text

from core import prompt_loader as _pl
from core.image import ImageEngine
from core.music import MusicEngine, MusicPlayResult
from core.prompt_policy import PromptPolicy
from core.search import SearchEngine, SearchResult
from core.system_prompts import SystemPromptRelay
from core.video_analyzer import VideoAnalysisResult, VideoAnalyzer

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


def _find_ffmpeg(name: str = "ffmpeg") -> str:
    """Locate ffmpeg/ffprobe executable, including common winget install locations."""
    found = shutil.which(name)
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
    exe_name = f"{name}.exe" if os.name == "nt" else name
    for d in extra_dirs:
        candidate = os.path.join(d, exe_name)
        if os.path.isfile(candidate):
            # 把目录加到 PATH 以便 yt-dlp 也能找到
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return candidate
    # 最后兜底：imageio-ffmpeg 打包的可执行文件
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and os.path.isfile(bundled):
                bundled_dir = str(Path(bundled).resolve().parent)
                os.environ["PATH"] = bundled_dir + os.pathsep + os.environ.get("PATH", "")
                return bundled
        except Exception:
            pass
    elif name == "ffprobe":
        # ffprobe 通常与 ffmpeg 同目录
        ffmpeg_path = _find_ffmpeg("ffmpeg")
        if ffmpeg_path:
            sibling = str(Path(ffmpeg_path).resolve().parent / exe_name)
            if os.path.isfile(sibling):
                return sibling
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


def _normalize_multimodal_query(text: str) -> str:
    """Strip multimodal event markers and CQ-style tokens from user text."""
    from utils.text import normalize_text as _nt
    content = _nt(text)
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
        r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?]",
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


def _is_known_image_signature(head: bytes) -> bool:
    """Return True if *head* starts with a recognized image magic number."""
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xFF\xD8\xFF")
        or head.startswith(b"GIF87a")
        or head.startswith(b"GIF89a")
        or head.startswith(b"BM")
        or (head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP")
    )


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
