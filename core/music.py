"""音乐搜索与提取引擎 — AlgerMusic API (NeteaseCloudMusicApi) + SILK 语音转换。

支持功能:
- AlgerMusic API 搜索 & 播放链接获取（优先，支持 VIP 解锁）
- 网易云官方 API 作为回退
- 歌词获取
- ffmpeg + pilk 转换为 QQ SILK 语音格式
- 缓存管理
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger("yukiko.music")

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MusicSearchResult:
    """单条音乐搜索结果。"""
    song_id: int = 0
    name: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    source: str = "netease"  # netease | qqmusic | ytdlp

@dataclass(slots=True)
class MusicPlayResult:
    """音乐播放结果。"""
    ok: bool = False
    song: MusicSearchResult | None = None
    audio_path: str = ""       # MP3/OGG 本地路径
    silk_path: str = ""        # SILK 语音路径（可直接发 QQ 语音条）
    silk_b64: str = ""         # SILK base64（用于 record segment）
    message: str = ""
    error: str = ""

# ---------------------------------------------------------------------------
# MusicEngine
# ---------------------------------------------------------------------------

class MusicEngine:
    """音乐搜索与提取引擎。"""

    _DEFAULT_API_BASE = "http://mc.alger.fun/api"
    _NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
    _NETEASE_PLAYER_URL = "https://music.163.com/api/song/enhance/player/url"
    _COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    _DEFAULT_MAX_VOICE_DURATION_S = 0  # 0=不截断，按歌曲原时长转换

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        music_cfg = cfg.get("music", {}) or {}
        self._enable = bool(music_cfg.get("enable", True))
        self._api_base = str(music_cfg.get("api_base", self._DEFAULT_API_BASE)).rstrip("/")
        self._cache_dir = Path(music_cfg.get("cache_dir", "storage/cache/music"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = float(music_cfg.get("timeout_seconds", 15))
        self._ffmpeg = shutil.which("ffmpeg") or ""
        self._pilk_available = self._check_pilk()
        self._max_voice_duration_s = max(
            0,
            int(music_cfg.get("max_voice_duration_seconds", self._DEFAULT_MAX_VOICE_DURATION_S)),
        )
        self._silk_encode_timeout_s = max(30, int(music_cfg.get("silk_encode_timeout_seconds", 180)))
        self._max_cache_files = int(music_cfg.get("cache_keep_files", 50))

    @staticmethod
    def _check_pilk() -> bool:
        try:
            import pilk  # noqa: F401
            return True
        except ImportError:
            return False

    # ── 搜索 ──────────────────────────────────────────────────────────

    async def search(self, keyword: str, limit: int = 5) -> list[MusicSearchResult]:
        """搜索音乐，优先 AlgerMusic API，回退网易云官方。"""
        if not self._enable or not keyword.strip():
            return []
        kw = keyword.strip()
        # 优先 AlgerMusic API（NeteaseCloudMusicApi，支持 VIP 解锁）
        results = await self._search_alger(kw, limit)
        if not results:
            _log.info("alger_search_empty, fallback to netease | kw=%s", kw)
            results = await self._search_netease(kw, limit)
        return results

    async def _search_alger(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        """通过 AlgerMusic API 搜索。"""
        url = f"{self._api_base}/search"
        params = {"keywords": keyword, "limit": limit}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("alger_search_fail | %s", e)
            return []
        return self._parse_search_songs(data)

    async def _search_netease(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        """直接调用网易云官方 API（回退）。"""
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.post(self._NETEASE_SEARCH_URL, data=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("netease_search_fail | %s", e)
            return []
        return self._parse_search_songs(data)

    @staticmethod
    def _parse_search_songs(data: dict) -> list[MusicSearchResult]:
        """解析搜索结果 JSON（AlgerMusic 和网易云格式兼容）。"""
        songs = data.get("result", {}).get("songs", [])
        results: list[MusicSearchResult] = []
        for s in songs:
            if not isinstance(s, dict):
                continue
            # artists 字段兼容 "artists" 和 "ar"
            ar_list = s.get("artists") or s.get("ar") or []
            artists = "/".join(a.get("name", "") for a in ar_list if isinstance(a, dict))
            # album 字段兼容 "album" 和 "al"
            al = s.get("album") or s.get("al") or {}
            album = al.get("name", "") if isinstance(al, dict) else ""
            # duration 字段兼容 "duration" 和 "dt"
            dur = s.get("duration") or s.get("dt") or 0
            results.append(MusicSearchResult(
                song_id=int(s.get("id", 0)),
                name=str(s.get("name", "")).strip(),
                artist=artists,
                album=album,
                duration_ms=int(dur),
                source="netease",
            ))
        return results

    # ── 获取播放链接 ──────────────────────────────────────────────────

    async def _get_play_url(self, song_id: int) -> str:
        """获取播放链接，优先 AlgerMusic API（VIP 解锁），回退网易云官方。"""
        url = await self._get_alger_url(song_id)
        if not url:
            _log.info("alger_url_empty, fallback to netease | id=%d", song_id)
            url = await self._get_netease_url(song_id)
        return url

    async def _get_alger_url(self, song_id: int) -> str:
        """通过 AlgerMusic API 获取播放链接（支持 VIP 解锁）。"""
        url = f"{self._api_base}/song/url/v1"
        params = {"id": song_id, "level": "higher"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("alger_url_fail | id=%d | %s", song_id, e)
            return ""
        songs = data.get("data", [])
        if songs and isinstance(songs[0], dict) and songs[0].get("url"):
            return str(songs[0]["url"])
        return ""

    async def _get_netease_url(self, song_id: int) -> str:
        """直接调用网易云官方 API 获取播放链接（回退）。"""
        params = {"id": song_id, "ids": f"[{song_id}]", "br": 320000}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(self._NETEASE_PLAYER_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("netease_url_fail | id=%d | %s", song_id, e)
            return ""
        songs = data.get("data", [])
        if songs and isinstance(songs[0], dict) and songs[0].get("url"):
            return str(songs[0]["url"])
        return ""

    # ── 歌词 ──────────────────────────────────────────────────────────

    async def get_lyrics(self, song_id: int) -> str:
        """通过 AlgerMusic API 获取歌词文本。"""
        url = f"{self._api_base}/lyric"
        params = {"id": song_id}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("lyric_fail | id=%d | %s", song_id, e)
            return ""
        lrc = data.get("lrc", {}).get("lyric", "")
        return lrc

    # ── 播放（搜索 + 下载 + 转 SILK）──────────────────────────────────

    async def play(self, keyword: str, as_voice: bool = True) -> MusicPlayResult:
        """搜索并获取第一首歌的音频，可选转为 SILK 语音。"""
        results = await self.search(keyword, limit=3)
        if not results:
            return MusicPlayResult(ok=False, message="没找到相关歌曲。", error="no_results")

        for song in results:
            result = await self._play_song(song, as_voice=as_voice)
            if result.ok:
                return result
        return MusicPlayResult(ok=False, song=results[0], message="歌曲暂时无法播放（可能是 VIP 专属）。", error="play_failed")

    async def _play_song(self, song: MusicSearchResult, as_voice: bool) -> MusicPlayResult:
        url = await self._get_play_url(song.song_id)
        if not url:
            return MusicPlayResult(ok=False, song=song, error="no_url")

        # 下载 MP3
        mp3_path = self._cache_dir / f"netease_{song.song_id}.mp3"
        # 过小缓存文件大概率是异常下载，删除后重拉，避免后续语音 0s。
        if mp3_path.exists():
            try:
                if mp3_path.stat().st_size < 64 * 1024:
                    mp3_path.unlink(missing_ok=True)
            except Exception:
                pass
        if not mp3_path.exists():
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    mp3_path.write_bytes(resp.content)
            except Exception as e:
                _log.warning("music_download_fail | id=%d | %s", song.song_id, e)
                return MusicPlayResult(ok=False, song=song, error="download_failed")

        result = MusicPlayResult(
            ok=True, song=song, audio_path=str(mp3_path),
            message=f"🎵 {song.name} - {song.artist}",
        )

        # 转 SILK 语音
        if as_voice and self._ffmpeg and self._pilk_available:
            silk = await self._convert_to_silk(mp3_path)
            if silk:
                try:
                    silk_bytes = silk.read_bytes()
                except Exception:
                    silk_bytes = b""
                if len(silk_bytes) >= 256:
                    result.silk_path = str(silk)
                    result.silk_b64 = base64.b64encode(silk_bytes).decode("ascii")
                else:
                    _log.warning("silk_file_too_small | path=%s | bytes=%d", silk.name, len(silk_bytes))

        self._evict_cache()
        return result

    # ── SILK 转换 ─────────────────────────────────────────────────────

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        """Convert MP3 to QQ-compatible SILK."""
        silk_path = audio_path.with_suffix(".silk")
        pcm_path = audio_path.with_suffix(".pcm")
        try:
            # Always re-encode to avoid reusing stale/invalid silk cache.
            if silk_path.exists():
                silk_path.unlink(missing_ok=True)

            cmd = [
                self._ffmpeg,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-f",
                "s16le",
            ]
            if self._max_voice_duration_s > 0:
                cmd.extend(["-t", str(self._max_voice_duration_s)])
            cmd.append(str(pcm_path))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=self._silk_encode_timeout_s)
            if proc.returncode != 0 or not pcm_path.exists():
                return None

            import pilk

            encode_ret = pilk.encode(
                str(pcm_path),
                str(silk_path),
                pcm_rate=24000,
                tencent=True,
            )
            if not silk_path.exists():
                return None

            size = silk_path.stat().st_size
            try:
                duration_ms = int(pilk.get_duration(str(silk_path)))
            except Exception:
                duration_ms = 0

            _log.info(
                "silk_encode_ok | ret=%s | dur=%dms | size=%d | path=%s",
                encode_ret,
                duration_ms,
                size,
                silk_path.name,
            )
            if size < 256 or duration_ms <= 0:
                _log.warning(
                    "silk_invalid | dur=%dms | size=%d | path=%s",
                    duration_ms,
                    size,
                    silk_path.name,
                )
                silk_path.unlink(missing_ok=True)
                return None
            return silk_path
        except Exception as e:
            _log.warning("silk_convert_fail | %s", e)
            return None
        finally:
            if pcm_path.exists():
                pcm_path.unlink(missing_ok=True)
    def _evict_cache(self) -> None:
        """保留最近的缓存文件，删除多余的。"""
        try:
            files = sorted(self._cache_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[self._max_cache_files:]:
                f.unlink(missing_ok=True)
        except Exception:
            pass
