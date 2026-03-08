"""Music search and playback engine.

Capabilities:
- Search via Alger API (NeteaseCloudMusicApi wrapper), fallback to Netease official API
- Respect the query keyword from upper-layer agent without local version preference reranking
- Fetch playable URL and optional lyrics
- Download mp3 and convert to QQ-compatible SILK
- Local alternative source matching (QQ Music, Kuwo, Kugou, Migu)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from utils.text import normalize_text

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


@dataclass(slots=True)
class MusicPlayUrl:
    """Resolved playable URL metadata."""

    url: str = ""
    duration_ms: int = 0
    is_trial: bool = False
    source: str = ""
    level: str = ""


@dataclass(slots=True)
class MusicKeywordIntent:
    title_hint: str = ""
    artist_hint: str = ""
    artist_tokens: tuple[str, ...] = ()


class MusicEngine:
    """Search + play + silk conversion."""

    _DEFAULT_API_BASE = "http://mc.alger.fun/api"
    _NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
    _NETEASE_PLAYER_URL = "https://music.163.com/api/song/enhance/player/url"
    _ALGER_PLAYER_URL_V1 = "/song/url/v1"
    _ALGER_PLAYER_URL = "/song/url"
    _ALGER_SEARCH_URL = "/search"
    _COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    _DEFAULT_MAX_VOICE_DURATION_S = 0  # 0 means no truncation
    _DEFAULT_TRIAL_MAX_DURATION_MS = 35_000
    _BREAK_LIMIT_MIN_FULL_MS = 90_000

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        music_cfg = cfg.get("music", cfg) if isinstance(cfg, dict) else {}

        self._enable = bool(music_cfg.get("enable", True))
        self._api_base = str(music_cfg.get("api_base", self._DEFAULT_API_BASE)).rstrip("/")
        self._cache_dir = Path(music_cfg.get("cache_dir", "storage/cache/music"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._timeout = float(music_cfg.get("timeout_seconds", 15))
        self._ffmpeg = shutil.which("ffmpeg") or self._find_bundled_ffmpeg() or ""
        self._pilk_available = self._check_pilk()

        self._max_voice_duration_s = max(
            0,
            int(music_cfg.get("max_voice_duration_seconds", self._DEFAULT_MAX_VOICE_DURATION_S)),
        )
        self._silk_encode_timeout_s = max(30, int(music_cfg.get("silk_encode_timeout_seconds", 180)))
        self._silk_bit_rate = max(8000, int(music_cfg.get("silk_bit_rate", 32000)))
        self._max_cache_files = int(music_cfg.get("cache_keep_files", 50))
        self._break_limit_enable = bool(music_cfg.get("break_limit_enable", True))
        trial_raw = music_cfg.get("trial_max_duration_ms", self._DEFAULT_TRIAL_MAX_DURATION_MS)
        try:
            trial_cfg_raw = int(trial_raw or 0)
        except Exception:
            trial_cfg_raw = self._DEFAULT_TRIAL_MAX_DURATION_MS
        self._trial_max_duration_ms = max(0, trial_cfg_raw)
        self._artist_guard_enable = bool(music_cfg.get("artist_guard_enable", True))
        self._artist_guard_allow_mismatch_fallback = bool(
            music_cfg.get("artist_guard_allow_mismatch_fallback", False)
        )

        # UnblockNeteaseMusic 配置
        self._unblock_enable = bool(music_cfg.get("unblock_enable", False))
        self._unblock_api_base = str(music_cfg.get("unblock_api_base", "")).rstrip("/")
        self._unblock_sources = str(music_cfg.get("unblock_sources", "qq,kuwo,migu")).strip()

        # 本地音源匹配器
        self._local_source_enable = bool(music_cfg.get("local_source_enable", True))
        self._source_matcher = None
        if self._local_source_enable:
            try:
                from core.music_sources import MusicSourceMatcher
                self._source_matcher = MusicSourceMatcher(timeout=self._timeout)
            except Exception as exc:
                _log.warning("music_source_matcher_init_fail | %s", exc)
                self._source_matcher = None

        self._alger_web_base = self._derive_alger_web_base(self._api_base)
        self._alger_discovered_api_bases: list[str] = []

    @staticmethod
    def _find_bundled_ffmpeg() -> str:
        """Try to find ffmpeg bundled by imageio-ffmpeg."""
        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path:
                return str(path)
        except Exception:
            pass
        return ""

    @staticmethod
    def _check_pilk() -> bool:
        try:
            import pilk  # noqa: F401

            return True
        except Exception:
            pass
        try:
            import pysilk  # noqa: F401

            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        """Compatibility hook."""
        return None

    async def search(self, keyword: str, limit: int = 5) -> list[MusicSearchResult]:
        """Search songs with minimal local preference logic."""
        if not self._enable or not keyword.strip():
            return []

        kw = keyword.strip()
        intent = self._parse_keyword_intent(kw)
        fetch_limit = max(limit * 3, 20)  # 获取更多结果用于过滤

        # Always search with original keyword first
        primary = await self._search_alger(kw, fetch_limit)
        fallback: list[MusicSearchResult] = []
        title_only_fallback: list[MusicSearchResult] = []
        if not primary:
            title_hint = intent.title_hint
            if title_hint and title_hint != kw.lower():
                primary = await self._search_alger(title_hint, fetch_limit)

        if not primary:
            fallback = await self._search_netease(kw, fetch_limit)
            if not fallback:
                title_hint = intent.title_hint
                if title_hint and title_hint != kw.lower():
                    title_only_fallback = await self._search_netease(title_hint, fetch_limit)

        results = self._merge_unique_results(primary, fallback, fetch_limit * 2)
        if title_only_fallback:
            results = self._merge_unique_results(results, title_only_fallback, fetch_limit * 2)

        # 结构化排序：按歌名/歌手提示与关键词命中排序（不使用本地词典特判）。
        results = self._rank_search_results(results, kw, intent=intent)

        return results[:limit]

    @staticmethod
    def _parse_keyword_intent(keyword: str) -> MusicKeywordIntent:
        raw = normalize_text(keyword)
        lower = raw.lower()
        parts = [x for x in re.split(r"[\s,，;；/|]+", lower) if x]
        artist_tokens: tuple[str, ...] = ()
        if len(parts) >= 2:
            title_hint = parts[0]
            artist_hint = " ".join(parts[1:])
            artist_tokens = tuple(dict.fromkeys(x for x in parts[1:] if x))
            return MusicKeywordIntent(
                title_hint=normalize_text(title_hint),
                artist_hint=normalize_text(artist_hint),
                artist_tokens=artist_tokens,
            )
        return MusicKeywordIntent(
            title_hint=normalize_text(lower),
            artist_hint="",
            artist_tokens=(),
        )

    @staticmethod
    def _compact_text(text: str) -> str:
        return re.sub(r"[\s\-\_·•./|\\,，;；:&()（）\[\]{}]+", "", normalize_text(text).lower())

    @classmethod
    def _artist_matches_intent(cls, artist: str, intent: MusicKeywordIntent) -> bool:
        if not intent.artist_hint:
            return True
        artist_norm = normalize_text(artist).lower()
        artist_compact = cls._compact_text(artist_norm)
        hint_compact = cls._compact_text(intent.artist_hint)
        if hint_compact and (hint_compact in artist_compact or artist_compact in hint_compact):
            return True
        if intent.artist_tokens:
            token_hits = 0
            for token in intent.artist_tokens:
                token_norm = normalize_text(token).lower()
                token_compact = cls._compact_text(token_norm)
                if not token_compact:
                    continue
                if token_compact in artist_compact:
                    token_hits += 1
            if token_hits > 0:
                return True
        return False

    @staticmethod
    def _rank_search_results(
        results: list[MusicSearchResult],
        keyword: str,
        *,
        intent: MusicKeywordIntent | None = None,
    ) -> list[MusicSearchResult]:
        """对搜索结果进行智能排序，优先关键词命中且非改编版本。"""
        keyword_lower = normalize_text(keyword).lower()
        raw_tokens = [x for x in re.split(r"[\s,，;；/|]+", keyword_lower) if x]
        if not raw_tokens and keyword_lower:
            raw_tokens = [keyword_lower]
        # 去重但保持顺序，避免重复词影响评分。
        tokens = list(dict.fromkeys(raw_tokens))
        intent_obj = intent or MusicKeywordIntent(title_hint=keyword_lower, artist_hint="", artist_tokens=())

        compact_keyword = re.sub(r"\s+", "", keyword_lower)
        compact_title_hint = re.sub(r"\s+", "", normalize_text(intent_obj.title_hint).lower())
        artist_hint = normalize_text(intent_obj.artist_hint).lower()

        def score_result(item: MusicSearchResult) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int]:
            """关键词命中优先，其次标题/歌手约束，再其次时长。"""
            name_lower = item.name.lower()
            artist_lower = item.artist.lower()
            compact_name = re.sub(r"\s+", "", name_lower)

            exact_name_match = 1 if keyword_lower and keyword_lower in name_lower else 0
            exact_compact_match = 1 if compact_keyword and compact_name == compact_keyword else 0
            starts_with_keyword = 1 if compact_keyword and compact_name.startswith(compact_keyword) else 0
            title_exact_match = 1 if compact_title_hint and compact_name == compact_title_hint else 0
            title_contains = 1 if compact_title_hint and compact_title_hint in compact_name else 0
            artist_hint_match = 1 if MusicEngine._artist_matches_intent(artist_lower, intent_obj) else 0
            keyword_pos_score = 0
            if compact_keyword:
                pos = compact_name.find(compact_keyword)
                if pos >= 0:
                    keyword_pos_score = max(1, 1000 - pos)
            length_gap_score = 0
            if compact_keyword:
                length_gap_score = max(1, 1000 - abs(len(compact_name) - len(compact_keyword)) * 10)

            name_token_hits = 0
            artist_token_hits = 0
            all_token_hits = 0
            for token in tokens:
                if token in name_lower:
                    name_token_hits += 1
                if token in artist_lower:
                    artist_token_hits += 1
                if token in name_lower or token in artist_lower:
                    all_token_hits += 1

            duration_score = int(item.duration_ms or 0)

            return (
                title_exact_match,
                title_contains,
                artist_hint_match,
                exact_compact_match,
                exact_name_match,
                starts_with_keyword,
                keyword_pos_score,
                length_gap_score,
                name_token_hits,
                all_token_hits,
                artist_token_hits,
                duration_score,
            )

        # 按得分排序（降序）
        return sorted(results, key=score_result, reverse=True)

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
    def _derive_alger_web_base(api_base: str) -> str:
        base = normalize_text(api_base).rstrip("/")
        if not base:
            return ""
        parsed = urlparse(base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        lowered = base.lower()
        if lowered.endswith("/api"):
            return base[:-4]
        return base

    @staticmethod
    def _normalize_alger_api_base(candidate: str) -> str:
        raw = normalize_text(candidate).rstrip("/")
        if not raw:
            return ""
        if raw.lower().endswith("/api"):
            return raw
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/api"
        return ""

    async def _discover_alger_api_bases_via_crawl(self) -> list[str]:
        """从 Alger 前端页面/JS 中提取 API 基址，作为 API 不可用时的爬虫兜底。"""
        web_base = normalize_text(self._alger_web_base).rstrip("/")
        if not web_base:
            return []

        script_urls: list[str] = []
        discovered: list[str] = []
        seen: set[str] = set()
        web_host = normalize_text(urlparse(web_base).netloc).lower()

        def _add_api_base(raw_base: str) -> None:
            normalized = self._normalize_alger_api_base(raw_base)
            if not normalized:
                return
            host = normalize_text(urlparse(normalized).netloc).lower()
            # 只保留 Alger 同域（或显式含 alger 标识）的 API，避免拉入无关第三方接口。
            if web_host and host and host != web_host and "alger" not in host:
                return
            if normalized in seen:
                return
            seen.add(normalized)
            discovered.append(normalized)

        _add_api_base(self._api_base)

        try:
            async with httpx.AsyncClient(timeout=max(10.0, self._timeout), headers=self._COMMON_HEADERS) as client:
                index_resp = await client.get(web_base)
                index_resp.raise_for_status()
                html = normalize_text(index_resp.text)
                if not html:
                    return discovered

                for raw in re.findall(r"""<(?:script|link)[^>]+(?:src|href)=["']([^"']+assets/[^"']+\.js[^"']*)["']""", html, flags=re.IGNORECASE):
                    full = normalize_text(urljoin(f"{web_base}/", raw))
                    if full and full not in script_urls:
                        script_urls.append(full)
                    if len(script_urls) >= 4:
                        break

                for js_url in script_urls:
                    try:
                        js_resp = await client.get(js_url)
                        js_resp.raise_for_status()
                        js_text = normalize_text(js_resp.text)
                    except Exception:
                        continue
                    if not js_text:
                        continue
                    for match in re.findall(r"""https?://[^"'\s]+?/api""", js_text):
                        _add_api_base(match)
                    # 前端常见 request.get("/song/url/v1")，至少可反推出同域 /api。
                    if ("/song/url/v1" in js_text) or ("/song/url" in js_text and "/search" in js_text):
                        _add_api_base(f"{web_base}/api")
        except Exception as exc:
            _log.warning("alger_crawl_discover_fail | %s", exc)
            return discovered

        if len(discovered) > 1:
            _log.info(
                "alger_crawl_discover_ok | web=%s | candidates=%s",
                web_base,
                ",".join(discovered[:5]),
            )
        return discovered

    async def _candidate_alger_api_bases(self) -> list[str]:
        base = self._normalize_alger_api_base(self._api_base)
        out: list[str] = []
        if base:
            out.append(base)
        for item in self._alger_discovered_api_bases:
            norm = self._normalize_alger_api_base(item)
            if norm and norm not in out:
                out.append(norm)
        return out

    async def _search_alger(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        params = {"keywords": keyword, "limit": limit}
        tried: set[str] = set()

        async def _try_search(base: str, source: str) -> list[MusicSearchResult]:
            endpoint = f"{base}{self._ALGER_SEARCH_URL}"
            key = f"{source}:{endpoint}"
            if key in tried:
                return []
            tried.add(key)
            # 带重试的请求（Alger 偶发 502）
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                        resp = await client.get(endpoint, params=params)
                        if resp.status_code == 502 and attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    _log.warning("alger_search_fail | source=%s | %s", source, exc)
                    return []
                rows = self._parse_search_songs(data)
                if rows:
                    return rows
                _log.warning("alger_search_empty | source=%s | endpoint=%s", source, endpoint)
                return []
            if last_exc:
                _log.warning("alger_search_fail | source=%s | %s", source, last_exc)
            return []

        for api_base in await self._candidate_alger_api_bases():
            rows = await _try_search(api_base, source="api")
            if rows:
                return rows

        discovered = await self._discover_alger_api_bases_via_crawl()
        for api_base in discovered:
            if api_base not in self._alger_discovered_api_bases:
                self._alger_discovered_api_bases.append(api_base)
            rows = await _try_search(api_base, source="crawler")
            if rows:
                return rows
        return []

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

    @classmethod
    def _pick_better_url(cls, current: MusicPlayUrl, incoming: MusicPlayUrl) -> MusicPlayUrl:
        if not incoming.url:
            return current
        if not current.url:
            return incoming
        level_rank = {
            "jymaster": 5,
            "sky": 4,
            "lossless": 3,
            "hires": 3,
            "exhigh": 2,
            "higher": 1,
            "standard": 0,
        }

        def _score(info: MusicPlayUrl) -> tuple[int, int, int]:
            quality = level_rank.get(normalize_text(info.level).lower(), 0)
            return (0 if info.is_trial else 1, int(info.duration_ms or 0), quality)

        return incoming if _score(incoming) > _score(current) else current

    def _extract_play_url_meta(self, rows: Any, *, source: str) -> MusicPlayUrl:
        best = MusicPlayUrl()
        if not isinstance(rows, list):
            return best

        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_url = row.get("url")
            if not isinstance(raw_url, str):
                continue
            url = normalize_text(raw_url)
            if not url or not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            try:
                duration_ms = int(row.get("time", 0) or 0)
            except Exception:
                duration_ms = 0
            free_trial_info = row.get("freeTrialInfo")
            is_trial = bool(free_trial_info)
            if not is_trial:
                free_trial_priv = row.get("freeTrialPrivilege")
                if isinstance(free_trial_priv, dict):
                    try:
                        cannot_reason = int(free_trial_priv.get("cannotListenReason") or 0)
                    except Exception:
                        cannot_reason = 0
                    if cannot_reason == 1:
                        # 版权受限时经常下发试听片段；再结合时长阈值减少误判。
                        if self._break_limit_enable:
                            # 破限模式：只把“明显短片段”判为试听，避免把完整音源误判。
                            is_trial = duration_ms <= 0 or duration_ms < self._BREAK_LIMIT_MIN_FULL_MS
                        else:
                            threshold = self._trial_max_duration_ms
                            is_trial = duration_ms <= 0 or (threshold > 0 and duration_ms <= threshold)
            level = normalize_text(str(row.get("level", "")))
            candidate = MusicPlayUrl(
                url=url,
                duration_ms=duration_ms,
                is_trial=is_trial,
                source=source,
                level=level,
            )
            best = self._pick_better_url(best, candidate)
        return best

    async def _get_play_url(self, song_id: int) -> MusicPlayUrl:
        alger = await self._get_alger_url(song_id)
        if alger.url and not alger.is_trial:
            return alger

        # 尝试 UnblockNeteaseMusic 服务
        if self._unblock_enable and self._unblock_api_base:
            unblock = await self._get_unblock_url(song_id)
            if unblock.url and not unblock.is_trial:
                _log.info(
                    "unblock_success | id=%d | src=%s | level=%s",
                    song_id,
                    unblock.source or "-",
                    unblock.level or "-",
                )
                return unblock
            # 如果 unblock 也是试听，选择更好的
            if unblock.url:
                alger = self._pick_better_url(alger, unblock)

        if not alger.url:
            _log.info("alger_url_empty, fallback to netease | id=%d", song_id)
        elif alger.is_trial:
            _log.info(
                "alger_url_trial_only | id=%d | src=%s | level=%s | time=%dms",
                song_id,
                alger.source or "-",
                alger.level or "-",
                alger.duration_ms,
            )

        netease = await self._get_netease_url(song_id)
        chosen = self._pick_better_url(alger, netease)
        return chosen

    async def _get_play_url_with_alternative(
        self,
        song: MusicSearchResult,
    ) -> MusicPlayUrl:
        """获取播放链接，支持本地音源替换。"""
        # 先尝试网易云音源
        netease_url = await self._get_play_url(song.song_id)
        if netease_url.url and not netease_url.is_trial:
            return netease_url

        # 如果网易云失败，尝试本地音源匹配
        if self._source_matcher and song.name and song.artist:
            _log.info(
                "trying_alternative_source | id=%d | song=%s | artist=%s",
                song.song_id, song.name, song.artist,
            )
            sources = self._unblock_sources.split(",") if self._unblock_sources else None
            alternative = await self._source_matcher.find_alternative(
                song.name,
                song.artist,
                song.duration_ms,
                sources,
            )
            if alternative and alternative.url:
                return MusicPlayUrl(
                    url=alternative.url,
                    duration_ms=alternative.duration_ms,
                    is_trial=False,
                    source=alternative.source,
                    level=alternative.quality,
                )

        return netease_url

    async def _get_alger_url(self, song_id: int) -> MusicPlayUrl:
        best = MusicPlayUrl()
        tried: set[str] = set()

        async def _try_base(api_base: str, source_tag: str) -> MusicPlayUrl:
            local_best = MusicPlayUrl()
            endpoint_rows = [
                (f"{api_base}{self._ALGER_PLAYER_URL_V1}", {"id": song_id, "level": "exhigh"}, "alger_v1"),
                (f"{api_base}{self._ALGER_PLAYER_URL}", {"id": song_id, "br": 320000}, "alger"),
            ]
            for endpoint, params, source in endpoint_rows:
                key = f"{source_tag}:{endpoint}"
                if key in tried:
                    continue
                tried.add(key)
                # 带重试（Alger 偶发 502）
                for attempt in range(2):
                    try:
                        async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                            resp = await client.get(endpoint, params=params)
                            if resp.status_code == 502 and attempt < 1:
                                await asyncio.sleep(0.5)
                                continue
                            resp.raise_for_status()
                            data = resp.json()
                    except Exception as exc:
                        if attempt < 1:
                            await asyncio.sleep(0.5)
                            continue
                        _log.warning("alger_url_fail | id=%d | source=%s | %s", song_id, f"{source_tag}:{source}", exc)
                        break
                    rows = data.get("data", [])
                    candidate = self._extract_play_url_meta(rows, source=f"{source_tag}:{source}")
                    local_best = self._pick_better_url(local_best, candidate)
                    break
            return local_best

        for api_base in await self._candidate_alger_api_bases():
            candidate = await _try_base(api_base, source_tag="api")
            best = self._pick_better_url(best, candidate)
            if best.url and not best.is_trial:
                return best

        discovered = await self._discover_alger_api_bases_via_crawl()
        for api_base in discovered:
            if api_base not in self._alger_discovered_api_bases:
                self._alger_discovered_api_bases.append(api_base)
            candidate = await _try_base(api_base, source_tag="crawler")
            best = self._pick_better_url(best, candidate)
            if best.url and not best.is_trial:
                return best
        return best

    async def _get_netease_url(self, song_id: int) -> MusicPlayUrl:
        params = {"id": song_id, "ids": f"[{song_id}]", "br": 320000}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(self._NETEASE_PLAYER_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("netease_url_fail | id=%d | %s", song_id, exc)
            return MusicPlayUrl()

        rows = data.get("data", [])
        return self._extract_play_url_meta(rows, source="netease")

    async def _get_unblock_url(self, song_id: int) -> MusicPlayUrl:
        """通过 UnblockNeteaseMusic 服务获取音源。"""
        if not self._unblock_api_base:
            return MusicPlayUrl()

        # UnblockNeteaseMusic 的 API 格式：GET /song/url?id=xxx&source=qq,kuwo
        url = f"{self._unblock_api_base}/song/url"
        params = {"id": song_id}
        if self._unblock_sources:
            params["source"] = self._unblock_sources

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("unblock_url_fail | id=%d | %s", song_id, exc)
            return MusicPlayUrl()

        # UnblockNeteaseMusic 返回格式类似 Netease API
        rows = data.get("data", [])
        return self._extract_play_url_meta(rows, source="unblock")

    @staticmethod
    def _order_play_candidates(results: list[MusicSearchResult], *, intent: MusicKeywordIntent) -> list[MusicSearchResult]:
        if not results:
            return []
        title_hint = normalize_text(intent.title_hint).lower()

        strict_title: list[MusicSearchResult] = []
        title_only: list[MusicSearchResult] = []
        artist_only: list[MusicSearchResult] = []
        rest: list[MusicSearchResult] = []

        for row in results:
            name_l = normalize_text(row.name).lower()
            title_hit = bool(title_hint and title_hint in name_l)
            artist_hit = bool(intent.artist_hint and MusicEngine._artist_matches_intent(row.artist, intent))
            if title_hit and artist_hit:
                strict_title.append(row)
            elif title_hit:
                title_only.append(row)
            elif artist_hit:
                artist_only.append(row)
            else:
                rest.append(row)
        merged = [*strict_title, *title_only, *artist_only, *rest]

        seen_ids: set[int] = set()
        out: list[MusicSearchResult] = []
        for row in merged:
            if row.song_id in seen_ids:
                continue
            seen_ids.add(row.song_id)
            out.append(row)
        return out

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
        results = await self.search(keyword, limit=12)
        if not results:
            return MusicPlayResult(ok=False, message="没找到相关歌曲", error="no_results")

        intent = self._parse_keyword_intent(keyword)
        ordered = self._order_play_candidates(results, intent=intent)
        has_artist_hint = bool(intent.artist_hint)
        artist_matched: list[MusicSearchResult] = []
        artist_mismatched: list[MusicSearchResult] = []
        if has_artist_hint:
            for row in ordered:
                if self._artist_matches_intent(row.artist, intent):
                    artist_matched.append(row)
                else:
                    artist_mismatched.append(row)
            if artist_matched:
                ordered = [*artist_matched, *artist_mismatched]
            elif self._artist_guard_enable and not self._artist_guard_allow_mismatch_fallback:
                top = results[0]
                return MusicPlayResult(
                    ok=False,
                    song=top,
                    message=f"没找到与歌手「{intent.artist_hint}」匹配的可播版本，请换个关键词或指定歌曲ID。",
                    error="artist_mismatch",
                )
        first = results[0]
        first_preview: MusicPlayResult | None = None
        last_error: MusicPlayResult | None = None
        strict_last_error: MusicPlayResult | None = None
        strict_attempted = 0

        # 依次尝试多个候选，尽量拿到可下载完整音频。
        for idx, song in enumerate(ordered[:8], start=1):
            strict_mode_for_song = bool(has_artist_hint and self._artist_matches_intent(song.artist, intent))
            if has_artist_hint and strict_mode_for_song:
                strict_attempted += 1
            one = await self._play_song(song, as_voice=as_voice)
            if one.ok:
                if idx > 1:
                    _log.info(
                        "music_play_fallback_hit | keyword=%s | picked=%d/%d | id=%d | song=%s - %s",
                        normalize_text(keyword)[:80],
                        idx,
                        len(ordered[:8]),
                        song.song_id,
                        song.name,
                        song.artist,
                    )
                return one
            if one.error == "preview_only" and first_preview is None:
                first_preview = one
            if has_artist_hint and strict_mode_for_song:
                strict_last_error = one
            last_error = one
            if (
                has_artist_hint
                and self._artist_guard_enable
                and not self._artist_guard_allow_mismatch_fallback
                and strict_attempted > 0
                and idx >= len(artist_matched)
            ):
                break

        if (
            has_artist_hint
            and self._artist_guard_enable
            and not self._artist_guard_allow_mismatch_fallback
            and strict_attempted > 0
        ):
            if strict_last_error is not None and strict_last_error.error == "preview_only":
                strict_song = strict_last_error.song or first
                return MusicPlayResult(
                    ok=False,
                    song=strict_song,
                    message=strict_last_error.message or "命中了仅试听音源，没有可用完整音源。",
                    error="preview_only",
                )
            strict_song = (strict_last_error.song if strict_last_error else first) or first
            return MusicPlayResult(
                ok=False,
                song=strict_song,
                message=f"找到歌手「{intent.artist_hint}」的候选但都不可播，请换源或改为指定歌曲ID。",
                error=(strict_last_error.error if strict_last_error else "artist_play_failed") or "artist_play_failed",
            )

        if first_preview is not None:
            song = first_preview.song or first
            return MusicPlayResult(
                ok=False,
                song=song,
                message=first_preview.message or "命中了仅试听音源（约 20~30 秒），没有可用完整音源。",
                error="preview_only",
            )

        return MusicPlayResult(
            ok=False,
            song=first,
            message="歌曲暂时无法播放 可能是区域或版权限制",
            error=(last_error.error if last_error else "") or "play_failed",
        )

    async def _play_song(self, song: MusicSearchResult, as_voice: bool) -> MusicPlayResult:
        play_url = await self._get_play_url_with_alternative(song)
        if not play_url.url:
            return MusicPlayResult(ok=False, song=song, error="no_url")
        if play_url.is_trial:
            preview_s = max(20, int(round(play_url.duration_ms / 1000.0))) if play_url.duration_ms > 0 else 30
            _log.info(
                "music_play_preview_only | id=%d | song=%s - %s | source=%s | level=%s | time=%dms",
                song.song_id,
                song.name,
                song.artist,
                play_url.source or "-",
                play_url.level or "-",
                play_url.duration_ms,
            )
            return MusicPlayResult(
                ok=False,
                song=song,
                message=f"「{song.name} - {song.artist}」当前只能拿到试听片段（约 {preview_s} 秒），没有可用完整音源。",
                error="preview_only",
            )

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
                    resp = await client.get(play_url.url)
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

    @staticmethod
    def _encode_silk_with_fallback(
        encoder_mod: Any,
        pcm_path: Path,
        silk_path: Path,
        *,
        prefer_keywords: bool,
        bit_rate: int,
        prefer_file_io: bool = False,
    ) -> tuple[Any, str]:
        """Try multiple signatures so pilk/pysilk version differences won't break encoding."""

        def _path_kw_with_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                sample_rate=24000,
                bit_rate=int(bit_rate),
                tencent=True,
            )

        def _path_kw_no_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                sample_rate=24000,
                tencent=True,
            )

        def _path_pos_with_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                24000,
                int(bit_rate),
            )

        def _path_pos_legacy() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                24000,
                True,
            )

        def _file_kw_with_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    sample_rate=24000,
                    bit_rate=int(bit_rate),
                    tencent=True,
                )

        def _file_kw_no_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    sample_rate=24000,
                    tencent=True,
                )

        def _file_pos_with_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    24000,
                    int(bit_rate),
                )

        def _file_pos_legacy() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    24000,
                    True,
                )

        attempts: list[tuple[str, Any]] = []
        if prefer_keywords:
            ordered = [
                ("kw_with_bitrate", _path_kw_with_bitrate, _file_kw_with_bitrate),
                ("kw_no_bitrate", _path_kw_no_bitrate, _file_kw_no_bitrate),
                ("pos_with_bitrate", _path_pos_with_bitrate, _file_pos_with_bitrate),
                ("pos_legacy", _path_pos_legacy, _file_pos_legacy),
            ]
        else:
            ordered = [
                ("pos_with_bitrate", _path_pos_with_bitrate, _file_pos_with_bitrate),
                ("pos_legacy", _path_pos_legacy, _file_pos_legacy),
                ("kw_with_bitrate", _path_kw_with_bitrate, _file_kw_with_bitrate),
                ("kw_no_bitrate", _path_kw_no_bitrate, _file_kw_no_bitrate),
            ]
        if prefer_file_io:
            for name, path_fn, file_fn in ordered:
                attempts.append((f"file_{name}", file_fn))
                attempts.append((f"path_{name}", path_fn))
        else:
            for name, path_fn, file_fn in ordered:
                attempts.append((f"path_{name}", path_fn))
                attempts.append((f"file_{name}", file_fn))

        last_exc: Exception | None = None
        for name, fn in attempts:
            try:
                return fn(), name
            except TypeError as exc:
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no_silk_encode_attempt")

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

            try:
                import pilk
                _silk_mod = "pilk"
                encoder_mod = pilk
                encode_ret, encode_sig = self._encode_silk_with_fallback(
                    encoder_mod,
                    pcm_path,
                    silk_path,
                    prefer_keywords=True,
                    bit_rate=self._silk_bit_rate,
                )
            except ImportError:
                import pysilk as pilk  # type: ignore[no-redef]
                _silk_mod = "pysilk"
                encoder_mod = pilk
                encode_ret, encode_sig = self._encode_silk_with_fallback(
                    encoder_mod,
                    pcm_path,
                    silk_path,
                    prefer_keywords=False,
                    bit_rate=self._silk_bit_rate,
                    prefer_file_io=True,
                )
            if not silk_path.exists():
                return None

            size = silk_path.stat().st_size
            duration_ms = -1  # unknown
            try:
                duration_ms = int(encoder_mod.get_duration(str(silk_path)))
            except Exception:
                pass

            _log.info(
                "silk_encode_ok | mod=%s | sig=%s | ret=%s | dur=%dms | size=%d | path=%s",
                _silk_mod,
                encode_sig,
                encode_ret,
                duration_ms,
                size,
                silk_path.name,
            )
            if size < 256 or (duration_ms == 0):
                # duration_ms == -1 means get_duration unavailable (pysilk), allow it
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
