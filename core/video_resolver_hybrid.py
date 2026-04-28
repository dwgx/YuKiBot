"""
增强的视频解析器 - 混合架构
结合yt-dlp和专用解析器，针对不同平台使用最优方案
"""
import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

_log = logging.getLogger("yukiko.video_resolver")


class BilixResolver:
    """B站专用解析器 - 使用bilix实现高速异步下载"""

    def __init__(self, cache_dir: Path, ffmpeg_location: str = "", sess_data: str = ""):
        self.cache_dir = cache_dir
        self.ffmpeg_location = ffmpeg_location
        self.sess_data = sess_data
        self._bilix_available = False
        try:
            from bilix.sites.bilibili import DownloaderBilibili
            self._bilix_available = True
            _log.info("bilix_resolver_init | status=available")
        except ImportError:
            _log.warning("bilix_resolver_init | status=unavailable | install: pip install bilix")

    async def download(self, url: str) -> Optional[Path]:
        """使用bilix下载B站视频"""
        if not self._bilix_available:
            return None

        try:
            from bilix.sites.bilibili import DownloaderBilibili

            # 生成输出目录
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            output_dir = self.cache_dir / f"bilix_{digest}"
            output_dir.mkdir(parents=True, exist_ok=True)

            _log.info("bilix_download_start | url=%s | output=%s", url[:60], output_dir.name)

            # 使用bilix下载
            async with DownloaderBilibili(
                sess_data=self.sess_data or None,
                video_concurrency=1,  # 限制并发
            ) as d:
                # 下载视频到指定目录（bilix会自动处理音视频合并）
                await d.get_video(
                    url,
                    path=str(output_dir),
                    quality=720,  # 限制720p
                    image=False,
                    subtitle=False,
                    dm=False,  # 不下载弹幕
                )

            # 查找下载的文件
            video_files = list(output_dir.rglob("*.mp4"))
            if video_files:
                result = video_files[0]
                # 移动到标准缓存目录
                final_path = self.cache_dir / f"{digest}_{result.name}"
                if final_path.exists():
                    final_path.unlink()
                result.rename(final_path)
                # 清理临时目录
                try:
                    output_dir.rmdir()
                except Exception:
                    pass

                size_mb = final_path.stat().st_size / 1024 / 1024
                _log.info("bilix_download_ok | path=%s | size=%.2fMB", final_path.name, size_mb)
                return final_path

            _log.warning("bilix_download_no_file | output_dir=%s", output_dir)
            return None

        except Exception as e:
            _log.warning("bilix_download_error | type=%s | error=%s", type(e).__name__, str(e)[:200])
            return None


class DouyinResolver:
    """抖音专用解析器 - 通过分享页提取无水印视频"""

    _MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    )

    _CDN_HOSTS = [
        "aweme.snssdk.com",
        "v26-web.douyinvod.com",
        "v3-web.douyinvod.com",
        "v9-web.douyinvod.com",
        "v5-web.douyinvod.com",
    ]

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._available = True
        _log.info("douyin_resolver_init | status=available")

    async def download(self, url: str) -> Optional[Path]:
        """下载抖音视频（通过分享页提取 video_id）"""
        try:
            video_id, aweme_id = await self._extract_video_id(url)
        except Exception as exc:
            _log.warning("douyin_resolver: extract failed: %s", str(exc)[:200])
            return None

        if not video_id:
            _log.warning("douyin_resolver: no video_id from %s", url[:80])
            return None

        digest = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        tag = aweme_id or video_id
        out_path = self.cache_dir / f"{digest}_{tag}.mp4"

        for host in self._CDN_HOSTS:
            for ratio in ("720p", "540p"):
                cdn_url = f"https://{host}/aweme/v1/play/?video_id={video_id}&ratio={ratio}&line=0"
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(15.0, connect=8.0),
                        follow_redirects=True,
                        headers={
                            "User-Agent": self._MOBILE_UA,
                            "Referer": "https://www.douyin.com/",
                        },
                    ) as client:
                        resp = await client.get(cdn_url)
                        resp.raise_for_status()
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if "text/" in ctype:
                            break
                        if len(resp.content) < 4096:
                            break
                        out_path.write_bytes(resp.content)
                        size_mb = out_path.stat().st_size / 1024 / 1024
                        _log.info("douyin_resolver_ok | host=%s | ratio=%s | size=%.2fMB", host, ratio, size_mb)
                        return out_path
                except Exception as exc:
                    _log.warning("douyin_resolver: %s/%s failed: %s", host, ratio, str(exc)[:120])
                    continue

        return None

    async def _extract_video_id(self, url: str) -> tuple[str, str]:
        """从抖音 URL 提取 video_id 和 aweme_id。"""
        aweme_id = self._extract_aweme_id(url)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": self._MOBILE_UA, "Referer": "https://www.douyin.com/"},
        ) as client:
            if aweme_id:
                try:
                    share_resp = await client.get(f"https://www.iesdouyin.com/share/video/{aweme_id}/")
                    vid = self._find_video_id_in_text(share_resp.text, str(share_resp.url))
                    if vid:
                        return vid, aweme_id
                except Exception:
                    pass

            resp = await client.get(url)
            final_url = str(resp.url)
            if not aweme_id:
                aweme_id = self._extract_aweme_id(final_url)

            vid = self._find_video_id_in_text(resp.text, final_url)
            if not vid:
                # 尝试从 RENDER_DATA 或 __INITIAL_STATE__ 提取
                m_render = re.search(r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', resp.text, re.DOTALL)
                if m_render:
                    try:
                        import urllib.parse
                        decoded = urllib.parse.unquote(m_render.group(1))
                        vid = self._find_video_id_in_text(decoded, final_url)
                    except Exception:
                        pass
                if not vid:
                    m_state = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', resp.text, re.DOTALL)
                    if m_state:
                        vid = self._find_video_id_in_text(m_state.group(1), final_url)

            if vid:
                return vid, aweme_id

            if aweme_id:
                try:
                    api_resp = await client.get(f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}")
                    if api_resp.is_success:
                        data = api_resp.json()
                        items = data.get("item_list", []) if isinstance(data, dict) else []
                        if items and isinstance(items[0], dict):
                            uri = items[0].get("video", {}).get("play_addr", {}).get("uri", "")
                            if uri and self._is_valid_video_id(uri):
                                return uri, aweme_id
                except Exception:
                    pass

        return "", aweme_id

    @staticmethod
    def _extract_aweme_id(url: str) -> str:
        m = re.search(r"/(?:video|note)/(\d+)", url)
        return m.group(1) if m else ""

    @staticmethod
    def _find_video_id_in_text(text: str, url: str) -> str:
        patterns = (
            r'"play_addr"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
            r'"uri"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
            r'"video_id"\s*:\s*"([A-Za-z0-9_-]{8,80})"',
            r"video_id=([A-Za-z0-9_-]{8,80})",
        )
        for pattern in patterns:
            for raw in re.findall(pattern, text):
                if DouyinResolver._is_valid_video_id(raw):
                    return raw
        return ""

    @staticmethod
    def _is_valid_video_id(value: str) -> bool:
        v = value.strip()
        if not v or len(v) < 8:
            return False
        if v.lower() in {"http", "https", "play", "video"}:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", v):
            return False
        return bool(v[0].lower() == "v" or any(c.isdigit() for c in v))


class HybridVideoResolver:
    """
    混合视频解析器

    策略：
    1. B站优先使用bilix（更快），失败则fallback到yt-dlp
    2. 抖音使用专用分享页解析器，失败则fallback到yt-dlp
    3. 其他平台使用yt-dlp
    """

    def __init__(self, ytdlp_resolver, cache_dir: Path, ffmpeg_location: str = "", bilibili_sessdata: str = ""):
        self.ytdlp_resolver = ytdlp_resolver
        self.bilix_resolver = BilixResolver(cache_dir, ffmpeg_location, sess_data=bilibili_sessdata)
        self.douyin_resolver = DouyinResolver(cache_dir)
        _log.info("hybrid_resolver_init | bilix=%s | douyin=%s",
                    self.bilix_resolver._bilix_available, self.douyin_resolver._available)

    async def download_video(self, url: str) -> Optional[Path]:
        """
        智能下载视频

        根据URL自动选择最优解析器
        """
        host = urlparse(url).netloc.lower()

        # B站：优先bilix
        if "bilibili.com" in host or host.endswith("b23.tv"):
            _log.info("hybrid_resolve | platform=bilibili | method=bilix_first")

            # 尝试bilix
            if self.bilix_resolver._bilix_available:
                result = await self.bilix_resolver.download(url)
                if result:
                    return result
                _log.info("hybrid_resolve | bilix_failed | fallback=ytdlp")

            # fallback到yt-dlp
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 抖音：专用解析器
        elif "douyin.com" in host or "iesdouyin.com" in host:
            _log.info("hybrid_resolve | platform=douyin | method=share_page_first")

            # 尝试分享页解析
            result = await self.douyin_resolver.download(url)
            if result:
                return result
            _log.info("hybrid_resolve | douyin_share_failed | fallback=ytdlp")

            # fallback到yt-dlp
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 腾讯视频：yt-dlp
        elif "v.qq.com" in host or "m.v.qq.com" in host:
            _log.info("hybrid_resolve | platform=tencent | method=ytdlp")
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 优酷：yt-dlp
        elif "youku.com" in host or "v.youku.com" in host:
            _log.info("hybrid_resolve | platform=youku | method=ytdlp")
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 爱奇艺：yt-dlp
        elif "iqiyi.com" in host or "iq.com" in host:
            _log.info("hybrid_resolve | platform=iqiyi | method=ytdlp")
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 芒果TV：yt-dlp
        elif "mgtv.com" in host:
            _log.info("hybrid_resolve | platform=mgtv | method=ytdlp")
            return await asyncio.to_thread(self.ytdlp_resolver, url)

        # 其他平台：yt-dlp
        else:
            _log.info("hybrid_resolve | platform=other | method=ytdlp")
            return await asyncio.to_thread(self.ytdlp_resolver, url)


def create_hybrid_resolver(
    ytdlp_download_func,
    cache_dir: Path,
    ffmpeg_location: str = "",
    bilibili_sessdata: str = "",
) -> HybridVideoResolver:
    """
    创建混合解析器实例

    Args:
        ytdlp_download_func: 原有的yt-dlp下载函数
        cache_dir: 视频缓存目录
        ffmpeg_location: ffmpeg可执行文件路径

    Returns:
        HybridVideoResolver实例
    """
    return HybridVideoResolver(ytdlp_download_func, cache_dir, ffmpeg_location, bilibili_sessdata=bilibili_sessdata)
