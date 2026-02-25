"""Music search and playback engine.

Capabilities:
- Search via Alger API (NeteaseCloudMusicApi wrapper), fallback to Netease official API
- Artist-aware ranking when user provides "artist + song"
- Fetch playable URL and optional lyrics
- Download mp3 and convert to QQ-compatible SILK
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


@dataclass(slots=True)
class MusicSearchResult:
    """Single music search item."""

    song_id: int = 0
    name: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    source: str = "netease"


@dataclass(slots=True)
class MusicPlayResult:
    """Music play result."""

    ok: bool = False
    song: MusicSearchResult | None = None
    audio_path: str = ""
    silk_path: str = ""
    silk_b64: str = ""
    message: str = ""
    error: str = ""


class MusicEngine:
    """Search + play + silk conversion."""

    _DEFAULT_API_BASE = "http://mc.alger.fun/api"
    _NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
    _NETEASE_PLAYER_URL = "https://music.163.com/api/song/enhance/player/url"
    _COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    _DEFAULT_MAX_VOICE_DURATION_S = 0  # 0 means no truncation

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        music_cfg = cfg.get("music", cfg) if isinstance(cfg, dict) else {}

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
        except Exception:
            return False

    async def aclose(self) -> None:
        """Compatibility hook."""
        return None

    async def search(self, keyword: str, limit: int = 5) -> list[MusicSearchResult]:
        """Search songs and prioritize artist+song matching when both are provided."""
        if not self._enable or not keyword.strip():
            return []

        kw = keyword.strip()
        artist_hint, song_hint = self._split_artist_song(kw)
        fetch_limit = max(limit, 12)

        primary = await self._search_alger(kw, fetch_limit)
        fallback: list[MusicSearchResult] = []

        # Alger can be noisy on some Chinese keywords.
        if not primary or not self._looks_like_good_match(primary, artist_hint, song_hint):
            fallback = await self._search_netease(kw, fetch_limit)
            if artist_hint and song_hint:
                fallback_song_only = await self._search_netease(song_hint, fetch_limit)
                fallback = self._merge_unique_results(fallback, fallback_song_only, fetch_limit * 2)

        results = self._merge_unique_results(primary, fallback, fetch_limit * 2)
        if artist_hint:
            results = self._rerank_by_artist(results, artist_hint, song_hint)
        return results[:limit]

    @staticmethod
    def _split_artist_song(keyword: str) -> tuple[str, str]:
        """Try to split a query into (artist_hint, song_hint)."""
        kw = keyword.strip()
        if not kw:
            return "", ""

        # <artist>的<song>
        if "\u7684" in kw:
            left, right = kw.split("\u7684", 1)
            left = left.strip()
            right = right.strip()
            if len(left) >= 2 and len(right) >= 1:
                return left, right

        # <artist> <song>
        parts = kw.split(None, 1)
        if len(parts) == 2 and len(parts[0].strip()) >= 2 and len(parts[1].strip()) >= 1:
            return parts[0].strip(), parts[1].strip()

        # reverse fallback: <song> <artist>
        rev = kw.rsplit(None, 1)
        if len(rev) == 2 and len(rev[0].strip()) >= 1 and len(rev[1].strip()) >= 2:
            return rev[1].strip(), rev[0].strip()

        return "", kw

    @staticmethod
    def _rerank_by_artist(
        results: list[MusicSearchResult],
        artist_hint: str,
        song_hint: str,
    ) -> list[MusicSearchResult]:
        """Rerank by artist+song relevance and penalize covers/remixes."""
        ah = artist_hint.lower()
        sh = song_hint.lower()

        def score(row: MusicSearchResult) -> tuple[int, int, int, int]:
            artist = row.artist.lower()
            name = row.name.lower()

            artist_score = 3 if ah and ah in artist else 0
            song_score = 3 if sh and sh in name else 0
            if not song_score and sh:
                song_score = 1 if any(ch.strip() and ch in name for ch in sh) else 0

            # tolerate swapped query
            if not artist_score and ah in name and sh and sh in artist:
                artist_score = 3
                song_score = max(song_score, 2)

            penalty = 0
            for bad in ("伴奏", "翻唱", "cover", "dj", "remix", "live"):
                if bad in name:
                    penalty += 1

            return (-artist_score, -song_score, penalty, 0)

        return sorted(results, key=score)

    @staticmethod
    def _merge_unique_results(
        first: list[MusicSearchResult],
        second: list[MusicSearchResult],
        max_size: int,
    ) -> list[MusicSearchResult]:
        seen: set[str] = set()
        merged: list[MusicSearchResult] = []
        for row in [*first, *second]:
            key = f"{row.song_id}|{row.name.strip().lower()}|{row.artist.strip().lower()}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= max_size:
                break
        return merged

    @staticmethod
    def _looks_like_good_match(
        rows: list[MusicSearchResult],
        artist_hint: str,
        song_hint: str,
    ) -> bool:
        if not rows:
            return False
        if not artist_hint:
            return True

        ah = artist_hint.lower()
        sh = song_hint.lower()
        for row in rows[:5]:
            artist = row.artist.lower()
            name = row.name.lower()
            if ah in artist and (not sh or sh in name):
                return True
        return False

    async def _search_alger(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        url = f"{self._api_base}/search"
        params = {"keywords": keyword, "limit": limit}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("alger_search_fail | %s", exc)
            return []
        return self._parse_search_songs(data)

    async def _search_netease(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.post(self._NETEASE_SEARCH_URL, data=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("netease_search_fail | %s", exc)
            return []
        return self._parse_search_songs(data)

    @staticmethod
    def _parse_search_songs(data: dict[str, Any]) -> list[MusicSearchResult]:
        songs = data.get("result", {}).get("songs", [])
        if not isinstance(songs, list):
            return []

        results: list[MusicSearchResult] = []
        for item in songs:
            if not isinstance(item, dict):
                continue

            ar_list = item.get("artists") or item.get("ar") or []
            if isinstance(ar_list, list):
                artists = "/".join(
                    str(x.get("name", "")).strip()
                    for x in ar_list
                    if isinstance(x, dict) and str(x.get("name", "")).strip()
                )
            else:
                artists = ""

            al = item.get("album") or item.get("al") or {}
            album = str(al.get("name", "")).strip() if isinstance(al, dict) else ""

            dur = item.get("duration") or item.get("dt") or 0
            try:
                duration_ms = int(dur)
            except Exception:
                duration_ms = 0

            try:
                song_id = int(item.get("id", 0) or 0)
            except Exception:
                song_id = 0

            name = str(item.get("name", "")).strip()
            if not name:
                continue

            results.append(
                MusicSearchResult(
                    song_id=song_id,
                    name=name,
                    artist=artists,
                    album=album,
                    duration_ms=duration_ms,
                    source="netease",
                )
            )

        return results

    async def _get_play_url(self, song_id: int) -> str:
        url = await self._get_alger_url(song_id)
        if not url:
            _log.info("alger_url_empty, fallback to netease | id=%d", song_id)
            url = await self._get_netease_url(song_id)
        return url

    async def _get_alger_url(self, song_id: int) -> str:
        url = f"{self._api_base}/song/url/v1"
        params = {"id": song_id, "level": "higher"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("alger_url_fail | id=%d | %s", song_id, exc)
            return ""

        rows = data.get("data", [])
        if rows and isinstance(rows, list) and isinstance(rows[0], dict) and rows[0].get("url"):
            return str(rows[0]["url"])
        return ""

    async def _get_netease_url(self, song_id: int) -> str:
        params = {"id": song_id, "ids": f"[{song_id}]", "br": 320000}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(self._NETEASE_PLAYER_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("netease_url_fail | id=%d | %s", song_id, exc)
            return ""

        rows = data.get("data", [])
        if rows and isinstance(rows, list) and isinstance(rows[0], dict) and rows[0].get("url"):
            return str(rows[0]["url"])
        return ""

    async def get_lyrics(self, song_id: int) -> str:
        url = f"{self._api_base}/lyric"
        params = {"id": song_id}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("lyric_fail | id=%d | %s", song_id, exc)
            return ""

        return str(data.get("lrc", {}).get("lyric", "") or "")

    async def play(self, keyword: str, as_voice: bool = True) -> MusicPlayResult:
        results = await self.search(keyword, limit=4)
        if not results:
            return MusicPlayResult(ok=False, message="没找到相关歌曲", error="no_results")

        for song in results:
            one = await self._play_song(song, as_voice=as_voice)
            if one.ok:
                return one

        return MusicPlayResult(
            ok=False,
            song=results[0],
            message="歌曲暂时无法播放 可能是区域或版权限制",
            error="play_failed",
        )

    async def _play_song(self, song: MusicSearchResult, as_voice: bool) -> MusicPlayResult:
        play_url = await self._get_play_url(song.song_id)
        if not play_url:
            return MusicPlayResult(ok=False, song=song, error="no_url")

        mp3_path = self._cache_dir / f"netease_{song.song_id}.mp3"
        if mp3_path.exists():
            try:
                if mp3_path.stat().st_size < 64 * 1024:
                    mp3_path.unlink(missing_ok=True)
            except Exception:
                pass

        if not mp3_path.exists():
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(play_url)
                    resp.raise_for_status()
                    mp3_path.write_bytes(resp.content)
            except Exception as exc:
                _log.warning("music_download_fail | id=%d | %s", song.song_id, exc)
                return MusicPlayResult(ok=False, song=song, error="download_failed")

        result = MusicPlayResult(
            ok=True,
            song=song,
            audio_path=str(mp3_path),
            message=f"{song.name} - {song.artist} QWQ",
        )

        if as_voice and self._ffmpeg and self._pilk_available:
            silk_path = await self._convert_to_silk(mp3_path)
            if silk_path:
                try:
                    silk_bytes = silk_path.read_bytes()
                except Exception:
                    silk_bytes = b""
                if len(silk_bytes) >= 256:
                    result.silk_path = str(silk_path)
                    result.silk_b64 = base64.b64encode(silk_bytes).decode("ascii")
                else:
                    _log.warning("silk_file_too_small | path=%s | bytes=%d", silk_path.name, len(silk_bytes))

        self._evict_cache()
        return result

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        """Convert MP3 to QQ-compatible SILK."""
        silk_path = audio_path.with_suffix(".silk")
        pcm_path = audio_path.with_suffix(".pcm")

        try:
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
        except Exception as exc:
            _log.warning("silk_convert_fail | %s", exc)
            return None
        finally:
            if pcm_path.exists():
                pcm_path.unlink(missing_ok=True)

    def _evict_cache(self) -> None:
        """Keep only latest cache files."""
        try:
            files = sorted(self._cache_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
            for file_path in files[self._max_cache_files :]:
                file_path.unlink(missing_ok=True)
        except Exception:
            pass
