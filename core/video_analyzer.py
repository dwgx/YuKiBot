from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from core.prompt_policy import PromptPolicy
from core.system_prompts import SystemPromptRelay
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.video_analyzer")


@dataclass(slots=True)
class VideoAnalysisResult:
    """统一的视频分析结果。"""

    source_url: str = ""
    platform: str = ""  # bilibili | douyin | kuaishou | acfun | direct | unknown

    # 基础元数据
    title: str = ""
    uploader: str = ""
    duration: int = 0
    description: str = ""
    webpage_url: str = ""
    thumbnail_url: str = ""
    post_type: str = "video"  # video | image_text
    image_urls: list[str] = field(default_factory=list)

    # 富元数据（B站/抖音专属）
    tags: list[str] = field(default_factory=list)
    danmaku_keywords: list[str] = field(default_factory=list)
    hot_comments: list[str] = field(default_factory=list)
    view_count: int = 0
    like_count: int = 0
    coin_count: int = 0
    share_count: int = 0

    # 多模态分析（关键帧 → Vision API）
    keyframe_descriptions: list[str] = field(default_factory=list)
    subtitle_text: str = ""
    subtitle_lang: str = ""
    subtitle_source: str = ""

    # 下载结果
    local_video_path: str = ""

    # 状态
    analysis_depth: str = "metadata"  # metadata | rich_metadata | multimodal
    errors: list[str] = field(default_factory=list)

    def to_context_block(self) -> str:
        """格式化为结构化上下文，供 ThinkingEngine 使用。"""
        blocks: list[str] = []
        if self.title:
            blocks.append(f"标题: {self.title}")
        if self.uploader:
            blocks.append(f"UP主/作者: {self.uploader}")
        if self.duration > 0:
            blocks.append(f"时长: {self._fmt_duration()}")
        if self.platform:
            blocks.append(f"平台: {self.platform}")
        if self.post_type and self.post_type != "video":
            blocks.append("类型: 图文作品")
        if self.tags:
            blocks.append(f"标签: {', '.join(self.tags[:10])}")
        if self.view_count:
            blocks.append(f"播放量: {self.view_count}")
        if self.like_count:
            blocks.append(f"点赞: {self.like_count}")
        if self.coin_count:
            blocks.append(f"投币: {self.coin_count}")
        if self.share_count:
            blocks.append(f"分享: {self.share_count}")
        if self.description:
            blocks.append(f"简介: {clip_text(self.description, 200)}")
        if self.danmaku_keywords:
            blocks.append(f"弹幕热词: {', '.join(self.danmaku_keywords[:8])}")
        if self.hot_comments:
            blocks.append("热门评论:")
            for i, c in enumerate(self.hot_comments[:3], 1):
                blocks.append(f"  {i}. {clip_text(c, 80)}")
        if self.image_urls:
            blocks.append(f"图文图片数: {len(self.image_urls)}")
            for idx, image_url in enumerate(self.image_urls[:3], 1):
                blocks.append(f"  图{idx}: {clip_text(image_url, 140)}")
        if self.keyframe_descriptions:
            blocks.append("关键帧内容描述:")
            for i, desc in enumerate(self.keyframe_descriptions, 1):
                blocks.append(f"  帧{i}: {clip_text(desc, 120)}")
        if self.subtitle_text:
            blocks.append(f"字幕证据: 已提取（{self.subtitle_lang or 'unknown'}）")
            lines = [normalize_text(item) for item in re.split(r"[\n\r]+", self.subtitle_text) if normalize_text(item)]
            if not lines:
                lines = [
                    normalize_text(item)
                    for item in re.split(r"(?<=[。！？.!?])", self.subtitle_text)
                    if normalize_text(item)
                ]
            if lines:
                blocks.append("字幕摘录:")
                for i, line in enumerate(lines[:6], 1):
                    blocks.append(f"  {i}. {clip_text(line, 88)}")
            if self.subtitle_source:
                blocks.append(f"字幕来源: {clip_text(self.subtitle_source, 120)}")
        blocks.append(f"分析深度: {self.analysis_depth}")
        if self.webpage_url:
            blocks.append(f"来源: {self.webpage_url}")
        return "\n".join(blocks)

    def _fmt_duration(self) -> str:
        s = self.duration
        if s <= 0:
            return ""
        h, remainder = divmod(s, 3600)
        m, sec = divmod(remainder, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _find_ffmpeg_path(name: str = "ffmpeg") -> str:
    """查找 ffmpeg/ffprobe，兼容 winget 安装路径。"""
    found = shutil.which(name)
    if found:
        return found
    extra_dirs: list[str] = []
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
        extra_dirs.append(os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin"))
        extra_dirs.append(os.path.join(user_profile, "scoop", "shims"))

    for d in extra_dirs:
        candidate = os.path.join(d, f"{name}.exe") if os.name == "nt" else os.path.join(d, name)
        if os.path.isfile(candidate):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return candidate

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
        ffmpeg_path = _find_ffmpeg_path("ffmpeg")
        if ffmpeg_path:
            ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            sibling = str(Path(ffmpeg_path).resolve().parent / ffprobe_name)
            if os.path.isfile(sibling):
                os.environ["PATH"] = str(Path(sibling).resolve().parent) + os.pathsep + os.environ.get("PATH", "")
                return sibling
    return ""


class VideoAnalyzer:
    """专用视频分析引擎：关键帧提取 + Vision API + B站/抖音富元数据。"""

    def __init__(self, config: dict[str, Any]):
        va_cfg = config.get("video_analysis", {}) or {}
        vision_cfg = config.get("vision", {}) or {}
        search_cfg = config.get("search", {}) or {}
        video_cfg = search_cfg.get("video_resolver", {}) or {}

        # 关键帧提取配置
        self._keyframe_count = max(1, min(8, int(va_cfg.get("keyframe_count", 4))))
        self._keyframe_max_dim = max(256, int(va_cfg.get("keyframe_max_dimension", 720)))
        self._keyframe_quality = max(1, min(31, int(va_cfg.get("keyframe_quality", 5))))

        # Vision API 配置（复用 vision 段，为空时回退到主 API 配置）
        api_cfg = config.get("api", {}) or {}
        self._vision_enable = bool(vision_cfg.get("enable", True))
        self._vision_provider = normalize_text(str(vision_cfg.get("provider", ""))).lower()
        self._vision_base_url = normalize_text(str(
            vision_cfg.get("base_url", "") or api_cfg.get("base_url", "")
        )).rstrip("/")
        self._vision_api_key = normalize_text(str(
            vision_cfg.get("api_key", "") or api_cfg.get("api_key", "")
        ))
        self._vision_model = normalize_text(str(
            vision_cfg.get("model", "") or api_cfg.get("model", "")
        ))
        self._vision_timeout = max(8, int(vision_cfg.get("timeout_seconds", 35)))
        self._vision_max_tokens = max(200, int(vision_cfg.get("max_tokens", 1200)))

        # B站配置
        bili_cfg = va_cfg.get("bilibili", {}) or {}
        self._bili_enable = bool(bili_cfg.get("enable", True))
        self._bili_sessdata = normalize_text(str(bili_cfg.get("sessdata", "")))
        self._bili_jct = normalize_text(str(bili_cfg.get("bili_jct", "")))
        self._bili_danmaku_top_n = max(3, int(bili_cfg.get("danmaku_top_n", 8)))
        self._bili_comments_top_n = max(1, int(bili_cfg.get("comments_top_n", 3)))

        # 抖音配置
        douyin_cfg = va_cfg.get("douyin", {}) or {}
        self._douyin_enable = bool(douyin_cfg.get("enable", True))
        self._douyin_cookie = normalize_text(str(douyin_cfg.get("cookie", "")))

        # 快手配置
        ks_cfg = va_cfg.get("kuaishou", {}) or {}
        self._kuaishou_enable = bool(ks_cfg.get("enable", True))
        self._kuaishou_cookie = normalize_text(str(ks_cfg.get("cookie", "")))

        # AcFun 配置
        acfun_cfg = va_cfg.get("acfun", {}) or {}
        self._acfun_enable = bool(acfun_cfg.get("enable", True))
        self._acfun_cookie = normalize_text(str(acfun_cfg.get("cookie", "")))
        self._acfun_timeout = max(6, int(acfun_cfg.get("timeout_seconds", 12)))

        # 缓存目录
        cache_raw = str(video_cfg.get("cache_dir", "storage/cache/videos"))
        self._cache_dir = Path(cache_raw)
        if not self._cache_dir.is_absolute():
            self._cache_dir = (Path(__file__).resolve().parents[1] / self._cache_dir).resolve()
        self._keyframe_dir = self._cache_dir / "keyframes"
        self._keyframe_dir.mkdir(parents=True, exist_ok=True)

        self._ffmpeg_available = bool(_find_ffmpeg_path("ffmpeg"))
        self._ffprobe_available = bool(_find_ffmpeg_path("ffprobe"))
        self._prompt_policy = PromptPolicy.from_config(config)

    # ── 平台检测 ──

    @staticmethod
    def detect_platform(url: str) -> str:
        host = normalize_text(urlparse(url).netloc).lower()
        if "bilibili.com" in host or host.endswith("b23.tv"):
            return "bilibili"
        if "douyin.com" in host or "iesdouyin.com" in host:
            return "douyin"
        if "kuaishou.com" in host or "chenzhongtech.com" in host:
            return "kuaishou"
        if "acfun.cn" in host or "acfun.com" in host:
            return "acfun"
        if "youku.com" in host:
            return "youku"
        if "v.qq.com" in host or "qq.com" in host:
            return "tencent"
        return "unknown"

    # ── 主入口 ──

    async def analyze(
        self,
        source_url: str,
        local_video_path: str = "",
        depth: str = "auto",
        yt_dlp_meta: dict[str, Any] | None = None,
    ) -> VideoAnalysisResult:
        """
        主分析入口。由 tools.py 在视频下载/解析后调用。
        depth="auto" 时：B站/抖音自动走 rich_metadata，有本地文件+ffmpeg 时升级到 multimodal。
        """
        result = VideoAnalysisResult(source_url=source_url)
        platform = self.detect_platform(source_url)
        result.platform = platform

        # Step 1: yt-dlp 基础元数据
        if yt_dlp_meta:
            self._fill_from_ytdlp(result, yt_dlp_meta)

        # Step 2: 平台专属富元数据
        if depth in ("auto", "rich_metadata", "multimodal"):
            if platform == "bilibili" and self._bili_enable:
                await self._enrich_bilibili(result, source_url)
                if result.analysis_depth == "metadata":
                    result.analysis_depth = "rich_metadata"
            elif platform == "douyin" and self._douyin_enable:
                await self._enrich_douyin(result, source_url)
                if result.analysis_depth == "metadata":
                    result.analysis_depth = "rich_metadata"
            elif platform == "kuaishou" and self._kuaishou_enable:
                await self._enrich_kuaishou(result, source_url)
                if result.analysis_depth == "metadata":
                    result.analysis_depth = "rich_metadata"
            elif platform == "acfun" and self._acfun_enable:
                await self._enrich_acfun(result, source_url)
                if result.analysis_depth == "metadata":
                    result.analysis_depth = "rich_metadata"

        # Step 3: 多模态关键帧分析
        if local_video_path:
            result.local_video_path = local_video_path
        should_multimodal = depth in ("auto", "multimodal")
        has_local = bool(result.local_video_path) and Path(result.local_video_path).exists()
        if should_multimodal and has_local and self._ffmpeg_available and self._vision_enable:
            keyframe_paths = await self._extract_keyframes(result.local_video_path)
            if keyframe_paths:
                descriptions = await self._describe_keyframes(keyframe_paths, result)
                result.keyframe_descriptions = descriptions
                result.analysis_depth = "multimodal"
                for p in keyframe_paths:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass

        # Step 4: 尝试从本地视频提取内嵌字幕（ffmpeg）
        if has_local and self._ffmpeg_available and not result.subtitle_text:
            try:
                extracted_sub = await self._extract_embedded_subtitles(result.local_video_path)
                if extracted_sub:
                    result.subtitle_text = extracted_sub[:8000]
                    result.subtitle_source = "ffmpeg_embedded"
                    result.subtitle_lang = "auto"
            except Exception as exc:
                _log.debug("subtitle_extract_fail | err=%s", str(exc)[:120])

        return result

    # ── Step 1: yt-dlp 元数据填充 ──

    @staticmethod
    def _fill_from_ytdlp(result: VideoAnalysisResult, meta: dict[str, Any]) -> None:
        result.title = normalize_text(str(meta.get("title", "")))
        result.uploader = normalize_text(str(meta.get("uploader", "") or meta.get("channel", "")))
        dur = meta.get("duration")
        result.duration = int(dur) if isinstance(dur, (int, float)) else 0
        result.description = clip_text(normalize_text(str(meta.get("description", ""))), 300)
        result.webpage_url = normalize_text(str(meta.get("webpage_url", "")))
        result.thumbnail_url = normalize_text(str(meta.get("thumbnail", "")))
        result.view_count = int(meta.get("view_count", 0) or 0)
        result.like_count = int(meta.get("like_count", 0) or 0)
        subtitle_text = normalize_text(str(meta.get("subtitle_text", "")))
        if subtitle_text and not result.subtitle_text:
            result.subtitle_text = subtitle_text
            result.subtitle_lang = normalize_text(str(meta.get("subtitle_lang", "")))
            result.subtitle_source = normalize_text(str(meta.get("subtitle_source", "")))

    # ── Step 2a: B站富元数据 ──

    async def _enrich_bilibili(self, result: VideoAnalysisResult, url: str) -> None:
        """使用 bilibili-api-python 获取标签、弹幕热词、热评。"""
        try:
            from bilibili_api import video as bili_video, Credential
        except ImportError:
            result.errors.append("bilibili-api-python not installed")
            return

        bvid = self._extract_bvid(url)
        if not bvid:
            result.errors.append("无法从URL提取BV号")
            return

        credential = None
        if self._bili_sessdata:
            credential = Credential(
                sessdata=self._bili_sessdata,
                bili_jct=self._bili_jct or None,
            )

        v = bili_video.Video(bvid=bvid, credential=credential)

        # 并行获取 info 和 tags
        info: dict[str, Any] = {}
        tags_data: list[Any] = []
        try:
            info = await asyncio.wait_for(v.get_info(), timeout=10)
        except Exception as e:
            result.errors.append(f"bilibili_info: {e}")
        try:
            tags_data = await asyncio.wait_for(v.get_tags(), timeout=10)
        except Exception as e:
            result.errors.append(f"bilibili_tags: {e}")

        # 解析 info
        if isinstance(info, dict):
            stat = info.get("stat", {})
            if isinstance(stat, dict):
                result.view_count = int(stat.get("view", 0) or 0)
                result.like_count = int(stat.get("like", 0) or 0)
                result.coin_count = int(stat.get("coin", 0) or 0)
                result.share_count = int(stat.get("share", 0) or 0)
            if not result.title:
                result.title = normalize_text(str(info.get("title", "")))
            if not result.uploader:
                owner = info.get("owner", {})
                if isinstance(owner, dict):
                    result.uploader = normalize_text(str(owner.get("name", "")))
            if not result.description:
                result.description = clip_text(normalize_text(str(info.get("desc", ""))), 300)
            if not result.thumbnail_url:
                result.thumbnail_url = normalize_text(str(info.get("pic", "")))
            dur = info.get("duration")
            if isinstance(dur, (int, float)) and dur > 0 and result.duration <= 0:
                result.duration = int(dur)

        # 解析 tags
        if isinstance(tags_data, list):
            result.tags = [
                normalize_text(str(t.get("tag_name", "")))
                for t in tags_data
                if isinstance(t, dict) and t.get("tag_name")
            ][:10]

        # 获取弹幕热词
        await self._fetch_bilibili_danmaku(v, result)

        # 获取热评
        await self._fetch_bilibili_comments(info, credential, result)
        # 获取字幕（优先作为视频总结证据）
        await self._fetch_bilibili_subtitle(v, result)

    async def _fetch_bilibili_danmaku(self, v: Any, result: VideoAnalysisResult) -> None:
        try:
            danmaku_list = await asyncio.wait_for(v.get_danmakus(page_index=0), timeout=10)
            if danmaku_list:
                word_freq: Counter[str] = Counter()
                for dm in danmaku_list:
                    text = normalize_text(str(getattr(dm, "text", "")))
                    if text and len(text) >= 2:
                        word_freq[text] += 1
                result.danmaku_keywords = [w for w, _ in word_freq.most_common(self._bili_danmaku_top_n)]
        except Exception as e:
            result.errors.append(f"bilibili_danmaku: {e}")

    async def _fetch_bilibili_comments(
        self, info: dict[str, Any], credential: Any, result: VideoAnalysisResult
    ) -> None:
        try:
            from bilibili_api import comment as bili_comment
            from bilibili_api import ResourceType

            aid = int(info.get("aid", 0) or 0)
            if aid <= 0:
                return
            comments_data = await asyncio.wait_for(
                bili_comment.get_comments(
                    oid=aid,
                    type_=ResourceType.VIDEO,
                    order=bili_comment.OrderType.LIKE,
                    credential=credential,
                ),
                timeout=10,
            )
            if isinstance(comments_data, dict):
                replies = comments_data.get("replies", [])
                if isinstance(replies, list):
                    for reply in replies[: self._bili_comments_top_n]:
                        if not isinstance(reply, dict):
                            continue
                        content = reply.get("content", {})
                        msg = normalize_text(str(content.get("message", ""))) if isinstance(content, dict) else ""
                        if msg:
                            result.hot_comments.append(clip_text(msg, 100))
        except Exception as e:
            result.errors.append(f"bilibili_comments: {e}")

    async def _fetch_bilibili_subtitle(self, v: Any, result: VideoAnalysisResult) -> None:
        if result.subtitle_text:
            return
        try:
            cid = await asyncio.wait_for(v.get_cid(page_index=0), timeout=10)
        except Exception as e:
            result.errors.append(f"bilibili_subtitle_cid: {e}")
            return
        if not cid:
            return
        try:
            subtitle_info = await asyncio.wait_for(v.get_subtitle(cid=cid), timeout=12)
        except Exception as e:
            result.errors.append(f"bilibili_subtitle_info: {e}")
            return

        rows: list[dict[str, str]] = []
        if isinstance(subtitle_info, dict):
            subtitles = subtitle_info.get("subtitles", [])
            if not isinstance(subtitles, list):
                subtitle_obj = subtitle_info.get("subtitle", {})
                if isinstance(subtitle_obj, dict):
                    subtitles = subtitle_obj.get("subtitles", [])
            if isinstance(subtitles, list):
                for item in subtitles:
                    if not isinstance(item, dict):
                        continue
                    lang = normalize_text(str(item.get("lan") or item.get("lang") or item.get("lan_doc")))
                    sub_url = normalize_text(str(item.get("subtitle_url") or item.get("url")))
                    if not sub_url:
                        continue
                    if sub_url.startswith("//"):
                        sub_url = f"https:{sub_url}"
                    rows.append({"lang": lang, "url": sub_url})
        if not rows:
            return

        def _score(x: dict[str, str]) -> int:
            lang = normalize_text(str(x.get("lang", ""))).lower()
            score = 0
            if any(token in lang for token in ("zh", "cn", "中文", "汉")):
                score += 10
            if "auto" in lang:
                score -= 1
            return score

        rows.sort(key=_score, reverse=True)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": result.webpage_url or result.source_url or "https://www.bilibili.com/",
        }
        for row in rows[:4]:
            sub_url = normalize_text(str(row.get("url", "")))
            if not sub_url:
                continue
            try:
                async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
                    resp = await client.get(sub_url)
                if resp.status_code != 200:
                    continue
                payload = resp.json() if "json" in str(resp.headers.get("content-type", "")).lower() else {}
                text = self._extract_subtitle_text_from_bili_payload(payload)
                if len(text) < 20:
                    continue
                result.subtitle_text = clip_text(text, 8000)
                result.subtitle_lang = normalize_text(str(row.get("lang", "")))
                result.subtitle_source = sub_url
                return
            except Exception:
                continue

    @staticmethod
    def _extract_subtitle_text_from_bili_payload(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        body = payload.get("body", [])
        if not isinstance(body, list):
            return ""
        lines: list[str] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            content = normalize_text(str(item.get("content", "")))
            if content:
                lines.append(content)
        return normalize_text("\n".join(lines))

    @staticmethod
    def _extract_bvid(url: str) -> str:
        match = re.search(r"(BV[a-zA-Z0-9]+)", url, re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _normalize_url_list(value: Any, limit: int = 18) -> list[str]:
        rows: list[str] = []
        seen: set[str] = set()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        for raw in value:
            item = normalize_text(str(raw))
            if not item or item in seen:
                continue
            if not re.match(r"^https?://", item, flags=re.IGNORECASE):
                continue
            rows.append(item)
            seen.add(item)
            if len(rows) >= max(1, limit):
                break
        return rows

    # ── Step 2b: 抖音富元数据 ──

    async def _enrich_douyin(self, result: VideoAnalysisResult, url: str) -> None:
        """使用 f2 库获取抖音视频详细信息，失败时回退到分享页提取。"""
        # 先尝试 F2 库
        f2_ok = await self._enrich_douyin_via_f2(result, url)
        if f2_ok:
            return

        # F2 失败时回退到分享页 API
        await self._enrich_douyin_via_share_api(result, url)

    async def _enrich_douyin_via_f2(self, result: VideoAnalysisResult, url: str) -> bool:
        """通过 F2 库获取抖音元数据，成功返回 True。"""
        try:
            from f2.apps.douyin.handler import DouyinHandler
            from f2.apps.douyin.utils import AwemeIdFetcher
        except ImportError:
            result.errors.append("f2 not installed")
            return False

        try:
            aweme_id = await asyncio.wait_for(
                AwemeIdFetcher.get_aweme_id(url), timeout=10
            )
            if not aweme_id:
                result.errors.append("douyin: 无法提取 aweme_id")
                return False

            kwargs: dict[str, Any] = {
                "headers": {},
                "cookie": self._douyin_cookie or "",
            }
            handler = DouyinHandler(kwargs=kwargs)

            aweme_data = await asyncio.wait_for(
                handler.fetch_one_video(aweme_id), timeout=15
            )
            if not aweme_data:
                return False

            title = normalize_text(str(getattr(aweme_data, "desc", "")))
            if not result.title:
                result.title = title
            if not result.description and title:
                result.description = clip_text(title, 220)
            if not result.uploader:
                uploader = normalize_text(str(getattr(aweme_data, "nickname", "")))
                if uploader:
                    result.uploader = uploader

            image_urls = self._normalize_url_list(getattr(aweme_data, "images", []), limit=20)
            if image_urls:
                result.post_type = "image_text"
                result.image_urls = image_urls
                if not result.thumbnail_url:
                    result.thumbnail_url = image_urls[0]
            else:
                result.post_type = "video"

            play_count = int(getattr(aweme_data, "play_count", 0) or 0)
            digg_count = int(getattr(aweme_data, "digg_count", 0) or 0)
            share_count = int(getattr(aweme_data, "share_count", 0) or 0)
            if play_count > 0:
                result.view_count = play_count
            if digg_count > 0:
                result.like_count = digg_count
            if share_count > 0:
                result.share_count = share_count

            hashtags = getattr(aweme_data, "hashtag_names", None)
            if isinstance(hashtags, list):
                tags = [normalize_text(str(item)) for item in hashtags if normalize_text(str(item))]
                if tags:
                    result.tags = tags[:12]

            dur = getattr(aweme_data, "duration", 0)
            if (
                result.post_type != "image_text"
                and isinstance(dur, (int, float))
                and dur > 0
                and result.duration <= 0
            ):
                result.duration = max(1, int(dur / 1000))

            if not result.webpage_url:
                aweme_id_text = normalize_text(str(getattr(aweme_data, "aweme_id", ""))) or aweme_id
                if aweme_id_text:
                    page_type = "note" if result.post_type == "image_text" else "video"
                    result.webpage_url = f"https://www.douyin.com/{page_type}/{aweme_id_text}"

            return True
        except Exception as e:
            result.errors.append(f"douyin_f2: {e}")
            return False

    async def _enrich_douyin_via_share_api(self, result: VideoAnalysisResult, url: str) -> None:
        """通过 iesdouyin 分享页 API 获取抖音元数据（F2 失败时的回退）。"""
        aweme_id = ""
        m = re.search(r"/(?:video|note)/(\d+)", url)
        if m:
            aweme_id = m.group(1)

        if not aweme_id:
            # 尝试从短链接解析
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    follow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                    },
                ) as client:
                    resp = await client.get(url)
                    final_url = str(resp.url)
                    m = re.search(r"/(?:video|note)/(\d+)", final_url)
                    if m:
                        aweme_id = m.group(1)
            except Exception:
                pass

        if not aweme_id:
            return

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                },
            ) as client:
                api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
                resp = await client.get(api_url)
                if not resp.is_success:
                    return
                data = resp.json()

            items = data.get("item_list", []) if isinstance(data, dict) else []
            if not items:
                return
            item = items[0] if isinstance(items, list) else {}
            if not isinstance(item, dict):
                return

            desc = normalize_text(str(item.get("desc", "")))
            if not result.title and desc:
                result.title = desc
            if not result.description and desc:
                result.description = clip_text(desc, 220)

            author = item.get("author", {}) or {}
            if isinstance(author, dict) and not result.uploader:
                result.uploader = normalize_text(str(author.get("nickname", "")))

            stats = item.get("statistics", {}) or {}
            if isinstance(stats, dict):
                if not result.view_count:
                    result.view_count = int(stats.get("play_count", 0) or 0)
                if not result.like_count:
                    result.like_count = int(stats.get("digg_count", 0) or 0)
                if not result.share_count:
                    result.share_count = int(stats.get("share_count", 0) or 0)

            if not result.webpage_url:
                result.webpage_url = f"https://www.douyin.com/video/{aweme_id}"

        except Exception as e:
            result.errors.append(f"douyin_share_api: {e}")

    # ── Step 2c: 快手富元数据 ──

    async def _enrich_kuaishou(self, result: VideoAnalysisResult, url: str) -> None:
        """通过快手网页 GraphQL API 获取视频详细信息。"""
        photo_id = self._extract_kuaishou_photo_id(url)
        if not photo_id:
            result.errors.append("kuaishou: 无法提取 photoId")
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.kuaishou.com/",
            "Content-Type": "application/json",
            "Origin": "https://www.kuaishou.com",
        }
        if self._kuaishou_cookie:
            headers["Cookie"] = self._kuaishou_cookie

        gql_payload = {
            "operationName": "visionVideoDetail",
            "variables": {"photoId": photo_id, "page": "detail"},
            "query": (
                "query visionVideoDetail($photoId: String, $page: String) {"
                "  visionVideoDetail(photoId: $photoId, page: $page) {"
                "    status type photo {"
                "      id caption userExt { name id } "
                "      animatedCoverUrl coverUrl photoUrl "
                "      timestamp duration "
                "      realLikeCount viewCount commentCount shareCount "
                "      tags { name } "
                "    }"
                "  }"
                "}"
            ),
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0)) as client:
                resp = await client.post(
                    "https://www.kuaishou.com/graphql",
                    headers=headers,
                    json=gql_payload,
                )
            if resp.status_code >= 400:
                result.errors.append(f"kuaishou_api: HTTP {resp.status_code}")
                return

            data = resp.json()
            detail = (data.get("data") or {}).get("visionVideoDetail") or {}
            photo = detail.get("photo") or {}
            if not photo:
                result.errors.append("kuaishou_api: empty photo data")
                return

            if not result.title:
                result.title = normalize_text(str(photo.get("caption", "")))
            if not result.uploader:
                user_ext = photo.get("userExt") or {}
                result.uploader = normalize_text(str(user_ext.get("name", "")))
            if not result.thumbnail_url:
                result.thumbnail_url = normalize_text(
                    str(photo.get("coverUrl", "") or photo.get("photoUrl", ""))
                )

            result.view_count = int(photo.get("viewCount", 0) or 0)
            result.like_count = int(photo.get("realLikeCount", 0) or 0)
            result.share_count = int(photo.get("shareCount", 0) or 0)

            dur = photo.get("duration")
            if isinstance(dur, (int, float)) and dur > 0 and result.duration <= 0:
                # 快手 duration 单位是毫秒，统一转为秒
                result.duration = max(1, int(dur / 1000))

            tags_raw = photo.get("tags")
            if isinstance(tags_raw, list) and not result.tags:
                result.tags = [
                    normalize_text(str(t.get("name", "")))
                    for t in tags_raw
                    if isinstance(t, dict) and t.get("name")
                ][:10]

        except Exception as e:
            result.errors.append(f"kuaishou_gql: {e}")

    @staticmethod
    def _extract_kuaishou_photo_id(url: str) -> str:
        """从快手 URL 中提取 photoId。"""
        # https://www.kuaishou.com/short-video/xxxxx
        # https://v.kuaishou.com/xxxxx
        m = re.search(r"/short-video/([a-zA-Z0-9_-]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"photoId=([a-zA-Z0-9_-]+)", url)
        if m:
            return m.group(1)
        # 短链接需要先 resolve，这里只做基本提取
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts and len(parts[-1]) >= 8:
            return parts[-1]
        return ""

    # ── Step 2d: AcFun 富元数据 ──

    async def _enrich_acfun(self, result: VideoAnalysisResult, url: str) -> None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.acfun.cn/",
        }
        if self._acfun_cookie:
            headers["Cookie"] = self._acfun_cookie

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(self._acfun_timeout), connect=6.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(url)
        except Exception as exc:
            result.errors.append(f"acfun_fetch: {exc}")
            return

        if resp.status_code >= 400:
            result.errors.append(f"acfun_fetch: HTTP {resp.status_code}")
            return

        final_url = normalize_text(str(resp.url))
        if final_url and not result.webpage_url:
            result.webpage_url = final_url
        html = resp.text or ""

        title = self._extract_html_title(html)
        if title and not result.title:
            result.title = clip_text(title, 220)
        if not result.description:
            desc = (
                self._extract_meta_content(html, "description")
                or self._extract_meta_content(html, "og:description")
            )
            if desc:
                result.description = clip_text(desc, 320)
        if not result.thumbnail_url:
            cover = (
                self._extract_meta_content(html, "og:image")
                or self._extract_meta_content(html, "twitter:image")
            )
            if cover and re.match(r"^https?://", cover, flags=re.IGNORECASE):
                result.thumbnail_url = cover

        if not result.tags:
            keywords = self._extract_meta_content(html, "keywords")
            if keywords:
                rows = re.split(r"[，,|/#\s]+", keywords)
                tags = [normalize_text(item) for item in rows if normalize_text(item)]
                if tags:
                    result.tags = list(dict.fromkeys(tags))[:12]

        for marker in (
            "window.videoInfo",
            "window.pageInfo",
            "window.__INITIAL_STATE__",
            "window.__initialState__",
        ):
            payload = self._extract_json_object_after_marker(html, marker)
            if payload is not None:
                self._apply_acfun_payload(result, payload)

        for raw in re.findall(
            r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            payload = self._safe_json_load(raw)
            if payload is None:
                continue
            self._apply_acfun_payload(result, payload)

    @staticmethod
    def _extract_html_title(html: str) -> str:
        if not html:
            return ""
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        title = normalize_text(unescape(match.group(1)))
        if not title:
            return ""
        title = re.sub(r"\s*[-_｜|]\s*AcFun.*$", "", title, flags=re.IGNORECASE).strip()
        return title

    @staticmethod
    def _extract_meta_content(html: str, key: str) -> str:
        if not html:
            return ""
        escaped = re.escape(normalize_text(key))
        patterns = (
            rf"<meta[^>]+(?:name|property)=[\"']{escaped}[\"'][^>]+content=[\"'](.*?)[\"'][^>]*>",
            rf"<meta[^>]+content=[\"'](.*?)[\"'][^>]+(?:name|property)=[\"']{escaped}[\"'][^>]*>",
        )
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            value = normalize_text(unescape(match.group(1)))
            if value:
                return value
        return ""

    @classmethod
    def _extract_json_object_after_marker(cls, html: str, marker: str) -> Any:
        if not html or not marker:
            return None
        start_pos = 0
        while True:
            idx = html.find(marker, start_pos)
            if idx < 0:
                return None
            brace_start = html.find("{", idx)
            if brace_start < 0:
                return None
            depth = 0
            in_string = False
            escaped = False
            for cursor in range(brace_start, len(html)):
                ch = html[cursor]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                    continue
                if ch == "{":
                    depth += 1
                    continue
                if ch == "}":
                    depth -= 1
                    if depth == 0:
                        payload = cls._safe_json_load(html[brace_start : cursor + 1])
                        if payload is not None:
                            return payload
                        break
            start_pos = idx + len(marker)

    @staticmethod
    def _safe_json_load(raw: Any) -> Any:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        try:
            return json.loads(unescape(text))
        except Exception:
            return None

    @classmethod
    def _iter_json_nodes(cls, value: Any, *, max_depth: int = 6, depth: int = 0):
        if depth > max_depth:
            return
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._iter_json_nodes(child, max_depth=max_depth, depth=depth + 1)
            return
        if isinstance(value, list):
            for child in value[:40]:
                yield from cls._iter_json_nodes(child, max_depth=max_depth, depth=depth + 1)

    @classmethod
    def _pick_first_text_from_nodes(cls, nodes: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
        for node in nodes:
            for key in keys:
                if key not in node:
                    continue
                raw = node.get(key)
                if isinstance(raw, (dict, list)):
                    continue
                text = normalize_text(str(raw))
                if text and not re.fullmatch(r"[1-9]\d{5,15}", text):
                    return text
        return ""

    @staticmethod
    def _to_int(raw: Any) -> int:
        try:
            if isinstance(raw, str):
                normalized = normalize_text(raw).replace(",", "")
                if re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
                    return int(float(normalized))
                return 0
            if isinstance(raw, (int, float)):
                return int(raw)
        except Exception:
            return 0
        return 0

    @staticmethod
    def _parse_iso8601_duration_seconds(raw: str) -> int:
        text = normalize_text(raw).upper()
        if not text:
            return 0
        match = re.fullmatch(
            r"P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
            text,
        )
        if not match:
            return 0
        h = int(match.group(1) or 0)
        m = int(match.group(2) or 0)
        s = int(match.group(3) or 0)
        return h * 3600 + m * 60 + s

    @classmethod
    def _extract_image_urls_from_value(cls, raw: Any, limit: int = 20) -> list[str]:
        rows: list[str] = []
        seen: set[str] = set()

        def _walk(item: Any) -> None:
            if len(rows) >= max(1, limit):
                return
            if isinstance(item, str):
                url = normalize_text(str(item))
                if not url or not re.match(r"^https?://", url, flags=re.IGNORECASE):
                    return
                if not re.search(r"\.(?:jpe?g|png|webp|gif|bmp)(?:\?|$)", url, flags=re.IGNORECASE):
                    return
                if url in seen:
                    return
                seen.add(url)
                rows.append(url)
                return
            if isinstance(item, dict):
                for key in ("url", "src", "image", "cover", "img", "value"):
                    if key in item:
                        _walk(item.get(key))
                for child in item.values():
                    if isinstance(child, (dict, list)):
                        _walk(child)
                return
            if isinstance(item, list):
                for child in item[:40]:
                    _walk(child)

        _walk(raw)
        return rows

    @classmethod
    def _apply_acfun_payload(cls, result: VideoAnalysisResult, payload: Any) -> None:
        nodes = list(cls._iter_json_nodes(payload))
        if not nodes:
            return

        if not result.title:
            title = cls._pick_first_text_from_nodes(
                nodes,
                ("title", "videoTitle", "dougaTitle", "name", "headline"),
            )
            if title:
                result.title = clip_text(title, 220)

        if not result.uploader:
            author_name = ""
            for node in nodes:
                author = node.get("author")
                if isinstance(author, dict):
                    author_name = normalize_text(
                        str(
                            author.get("name")
                            or author.get("nickname")
                            or author.get("userName")
                            or ""
                        )
                    )
                    if author_name:
                        break
                for key in ("userName", "upName", "authorName", "ownerName", "creatorName"):
                    value = normalize_text(str(node.get(key, "")))
                    if value:
                        author_name = value
                        break
                if author_name:
                    break
            if author_name:
                result.uploader = clip_text(author_name, 80)

        if not result.description:
            desc = cls._pick_first_text_from_nodes(
                nodes, ("description", "desc", "summary", "content", "intro")
            )
            if desc:
                result.description = clip_text(desc, 320)

        if not result.thumbnail_url:
            for node in nodes:
                for key in (
                    "thumbnailUrl",
                    "thumbnailURL",
                    "coverUrl",
                    "cover",
                    "coverImage",
                    "pic",
                    "poster",
                ):
                    if key not in node:
                        continue
                    candidates = cls._extract_image_urls_from_value(node.get(key), limit=1)
                    if candidates:
                        result.thumbnail_url = candidates[0]
                        break
                if result.thumbnail_url:
                    break

        if result.duration <= 0:
            duration_raw = ""
            for node in nodes:
                for key in ("durationMillis", "duration_ms", "videoDuration", "duration"):
                    if key in node:
                        duration_raw = normalize_text(str(node.get(key, "")))
                        if duration_raw:
                            break
                if duration_raw:
                    break
            if duration_raw:
                duration_int = cls._to_int(duration_raw)
                if duration_int > 0:
                    if duration_int >= 1000:
                        result.duration = max(1, int(duration_int / 1000))
                    else:
                        result.duration = duration_int
                else:
                    parsed_seconds = cls._parse_iso8601_duration_seconds(duration_raw)
                    if parsed_seconds > 0:
                        result.duration = parsed_seconds

        for node in nodes:
            view = cls._to_int(node.get("viewCount"))
            if view <= 0:
                view = cls._to_int(node.get("playCount"))
            if view > result.view_count:
                result.view_count = view

            like = cls._to_int(node.get("likeCount"))
            if like <= 0:
                like = cls._to_int(node.get("diggCount"))
            if like > result.like_count:
                result.like_count = like

            share = cls._to_int(node.get("shareCount"))
            if share <= 0:
                share = cls._to_int(node.get("forwardCount"))
            if share > result.share_count:
                result.share_count = share

            interact = node.get("interactionStatistic")
            if isinstance(interact, dict):
                count = cls._to_int(interact.get("userInteractionCount"))
                i_type = normalize_text(str(interact.get("interactionType", ""))).lower()
                if "watch" in i_type and count > result.view_count:
                    result.view_count = count
                if "like" in i_type and count > result.like_count:
                    result.like_count = count
            elif isinstance(interact, list):
                for row in interact:
                    if not isinstance(row, dict):
                        continue
                    count = cls._to_int(row.get("userInteractionCount"))
                    i_type = normalize_text(str(row.get("interactionType", ""))).lower()
                    if "watch" in i_type and count > result.view_count:
                        result.view_count = count
                    if "like" in i_type and count > result.like_count:
                        result.like_count = count

        if not result.tags:
            tags: list[str] = []
            for node in nodes:
                for key in ("tags", "tagList", "tagNameList", "videoTagList"):
                    if key not in node:
                        continue
                    value = node.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                name = normalize_text(
                                    str(
                                        item.get("name")
                                        or item.get("tagName")
                                        or item.get("title")
                                        or ""
                                    )
                                )
                            else:
                                name = normalize_text(str(item))
                            if name and name not in tags:
                                tags.append(name)
                    elif isinstance(value, str):
                        for item in re.split(r"[，,|/#\s]+", value):
                            name = normalize_text(item)
                            if name and name not in tags:
                                tags.append(name)
                if len(tags) >= 12:
                    break
            if tags:
                result.tags = tags[:12]

        if not result.image_urls:
            image_urls: list[str] = []
            for node in nodes:
                for key in ("imageUrls", "images", "imgs", "pictures", "picList"):
                    if key not in node:
                        continue
                    rows = cls._extract_image_urls_from_value(node.get(key), limit=20)
                    for row in rows:
                        if row not in image_urls:
                            image_urls.append(row)
                if len(image_urls) >= 20:
                    break
            if image_urls:
                result.post_type = "image_text"
                result.image_urls = image_urls[:20]
                if not result.thumbnail_url:
                    result.thumbnail_url = result.image_urls[0]

    async def _extract_keyframes(self, video_path: str) -> list[Path]:
        """用 ffmpeg 提取关键帧：优先场景变化检测，不足时补充均匀采样。"""
        path = Path(video_path)
        if not path.exists():
            return []

        digest = hashlib.sha1(str(path).encode()).hexdigest()[:10]
        duration = await self._get_video_duration(str(path))
        if duration <= 0:
            duration = 60

        scale_filter = (
            f"scale='min({self._keyframe_max_dim},iw)':"
            f"'min({self._keyframe_max_dim},ih)':"
            f"force_original_aspect_ratio=decrease"
        )

        # 1) 场景变化检测
        scene_frames = await self._extract_scene_change_frames(
            path, digest, scale_filter
        )

        # 2) 如果场景帧不足，用均匀采样补充
        if len(scene_frames) < self._keyframe_count:
            uniform_frames = await self._extract_uniform_frames(
                path, digest, duration, scale_filter
            )
            # 合并去重（按文件名排序）
            existing = {f.name for f in scene_frames}
            for uf in uniform_frames:
                if uf.name not in existing and len(scene_frames) < self._keyframe_count:
                    scene_frames.append(uf)

        return scene_frames[: self._keyframe_count]

    async def _extract_scene_change_frames(
        self, path: Path, digest: str, scale_filter: str
    ) -> list[Path]:
        """用 ffmpeg select='gt(scene,0.3)' 提取场景切换帧。"""
        pattern = str(self._keyframe_dir / f"{digest}_sc_%02d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(path),
            "-vf", f"select='gt(scene,0.3)',{scale_filter}",
            "-vsync", "vfr",
            "-frames:v", str(self._keyframe_count),
            "-q:v", str(self._keyframe_quality),
            pattern,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except Exception:
            return []
        return sorted(self._keyframe_dir.glob(f"{digest}_sc_*.jpg"))

    async def _extract_uniform_frames(
        self, path: Path, digest: str, duration: int, scale_filter: str
    ) -> list[Path]:
        """均匀采样 N 帧作为兜底。"""
        pattern = str(self._keyframe_dir / f"{digest}_kf_%02d.jpg")
        interval = max(1, duration // (self._keyframe_count + 1))
        cmd = [
            "ffmpeg", "-y",
            "-i", str(path),
            "-vf", f"fps=1/{interval},{scale_filter}",
            "-frames:v", str(self._keyframe_count),
            "-q:v", str(self._keyframe_quality),
            pattern,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except Exception:
            return []
        return sorted(self._keyframe_dir.glob(f"{digest}_kf_*.jpg"))

    async def _get_video_duration(self, video_path: str) -> int:
        """通过 ffprobe 获取视频时长（秒）。"""
        if not self._ffprobe_available:
            return 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return int(float(stdout.decode().strip()))
        except Exception:
            return 0

    # ── Step 4: 从本地视频提取内嵌字幕 ──

    async def _extract_embedded_subtitles(self, video_path: str) -> str:
        """用 ffmpeg 提取视频内嵌字幕流（SRT/ASS/text），超时 10s。"""
        try:
            # 先用 ffprobe 检查是否有字幕流
            probe_proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-select_streams", "s",
                "-show_entries", "stream=index,codec_name",
                "-of", "csv=p=0",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            probe_out, _ = await asyncio.wait_for(probe_proc.communicate(), timeout=8)
            probe_text = probe_out.decode("utf-8", errors="replace").strip()
            if not probe_text:
                return ""

            # 提取第一条字幕流为 SRT 格式
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", video_path,
                "-map", "0:s:0",
                "-f", "srt",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            raw_srt = out.decode("utf-8", errors="replace").strip()
            if not raw_srt:
                return ""

            # 解析 SRT：只提取文本行，去掉时间码和序号
            import re as _re
            lines = []
            for line in raw_srt.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if _re.match(r"^\d+$", line):
                    continue
                if _re.match(r"\d{2}:\d{2}:\d{2}", line):
                    continue
                # 去掉 ASS/SRT 标签
                clean = _re.sub(r"<[^>]+>", "", line)
                clean = _re.sub(r"\{[^}]+\}", "", clean)
                clean = clean.strip()
                if clean and clean not in lines[-1:]:
                    lines.append(clean)

            return "\n".join(lines[:200])
        except (asyncio.TimeoutError, Exception) as exc:
            _log.debug("subtitle_extract_error | err=%s", str(exc)[:120])
            return ""

    # ── Step 3b: Vision API 分析关键帧 ──

    async def _describe_keyframes(
        self, keyframe_paths: list[Path], result: VideoAnalysisResult
    ) -> list[str]:
        """批量发送多帧给 Vision API，一次调用获取所有帧描述。"""
        context_hint = ""
        if result.title:
            context_hint = f"视频标题：{result.title}。"
        if result.tags:
            context_hint += f"标签：{', '.join(result.tags[:5])}。"
        if result.uploader:
            context_hint += f"作者：{result.uploader}。"

        # 尝试批量分析（一次 API 调用）
        batch_result = await self._vision_describe_batch(keyframe_paths, context_hint)
        if batch_result and len(batch_result) >= len(keyframe_paths) // 2:
            return batch_result

        # 批量失败时回退到逐帧分析
        descriptions: list[str] = []
        for i, path in enumerate(keyframe_paths, 1):
            base_prompt = SystemPromptRelay.video_single_user_prompt(
                context_hint=context_hint,
                frame_index=i,
                total_frames=len(keyframe_paths),
            )
            prompt = self._prompt_policy.compose_prompt(
                channel="video",
                base_prompt=base_prompt,
                tool_name="video.analyze",
            )
            desc = await self._vision_describe_image(path, prompt)
            if desc:
                descriptions.append(desc)
        return descriptions

    async def _vision_describe_batch(
        self, keyframe_paths: list[Path], context_hint: str
    ) -> list[str]:
        """将所有关键帧打包到一次 Vision API 调用中。"""
        if not self._vision_api_key or not self._vision_base_url or not self._vision_model:
            return []

        content_parts: list[dict[str, Any]] = []
        base_user_prompt = SystemPromptRelay.video_batch_user_prompt(
            context_hint=context_hint,
            total_frames=len(keyframe_paths),
        )
        prompt_text = self._prompt_policy.compose_prompt(
            channel="video",
            base_prompt=base_user_prompt,
            tool_name="video.analyze",
        )
        content_parts.append({"type": "text", "text": prompt_text})

        for path in keyframe_paths:
            try:
                image_bytes = path.read_bytes()
                b64 = base64.b64encode(image_bytes).decode("ascii")
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            except Exception:
                continue

        if len(content_parts) <= 1:
            return []

        base_system_prompt = SystemPromptRelay.video_batch_system_prompt()
        system_prompt = self._prompt_policy.compose_prompt(
            channel="video",
            base_prompt=base_system_prompt,
            tool_name="video.analyze",
        )
        payload = {
            "model": self._vision_model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0.2,
            "max_tokens": self._vision_max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._vision_api_key}",
            "Content-Type": "application/json",
        }

        base = self._vision_base_url.rstrip("/")
        endpoints = (
            [f"{base}/v1/chat/completions", f"{base}/chat/completions"]
            if not base.endswith("/v1")
            else [f"{base}/chat/completions", f"{base[:-3]}/chat/completions"]
        )

        for endpoint in endpoints:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(float(self._vision_timeout) * 1.5, connect=8.0)
                ) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    continue
                raw = normalize_text(str(choices[0].get("message", {}).get("content", "")))
                if not raw:
                    continue
                # 解析 "帧N: xxx" 格式
                return self._parse_batch_descriptions(raw, len(keyframe_paths))
            except Exception:
                continue
        return []

    @staticmethod
    def _parse_batch_descriptions(raw: str, expected: int) -> list[str]:
        """解析批量描述结果，提取每帧描述。"""
        descriptions: list[str] = []
        # 尝试按 "帧N:" 或 "帧 N:" 分割
        parts = re.split(r"帧\s*\d+\s*[:：]", raw)
        for part in parts:
            text = part.strip()
            if text and len(text) >= 5:
                descriptions.append(clip_text(text, 150))
        if descriptions:
            return descriptions[:expected]
        # 回退：按换行分割
        for line in raw.split("\n"):
            line = line.strip()
            if line and len(line) >= 5:
                # 去掉可能的序号前缀
                cleaned = re.sub(r"^\d+[.、)\]]\s*", "", line).strip()
                if cleaned:
                    descriptions.append(clip_text(cleaned, 150))
        return descriptions[:expected]

    async def _vision_describe_image(self, image_path: Path, prompt: str) -> str:
        """调用 Vision API 分析单张图片。"""
        if not self._vision_api_key or not self._vision_base_url or not self._vision_model:
            return ""

        try:
            image_bytes = image_path.read_bytes()
            b64 = base64.b64encode(image_bytes).decode("ascii")
            data_url = f"data:image/jpeg;base64,{b64}"
        except Exception:
            return ""

        base_system_prompt = SystemPromptRelay.video_single_system_prompt()
        system_prompt = self._prompt_policy.compose_prompt(
            channel="video",
            base_prompt=base_system_prompt,
            tool_name="video.analyze",
        )
        payload = {
            "model": self._vision_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": self._vision_max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._vision_api_key}",
            "Content-Type": "application/json",
        }

        base = self._vision_base_url.rstrip("/")
        endpoints = (
            [f"{base}/v1/chat/completions", f"{base}/chat/completions"]
            if not base.endswith("/v1")
            else [f"{base}/chat/completions", f"{base[:-3]}/chat/completions"]
        )

        for endpoint in endpoints:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(float(self._vision_timeout), connect=8.0)
                ) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    text = normalize_text(str(content))
                    if text:
                        return text
            except Exception:
                continue
        return ""
