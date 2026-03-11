from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from utils.text import normalize_text

_log = logging.getLogger("yukiko.soundcloud")


@dataclass(slots=True)
class SoundCloudAudio:
    track_id: int = 0
    title: str = ""
    artist: str = ""
    page_url: str = ""
    artwork_url: str = ""
    audio_url: str = ""
    protocol: str = ""
    mime_type: str = ""
    playlist_title: str = ""
    source: str = "soundcloud"


class SoundCloudClient:
    _API_BASE = "https://api-v2.soundcloud.com"
    _DISCOVER_URL = "https://soundcloud.com/discover"
    _CLIENT_ID_PATTERNS = (
        r'client_id:"([A-Za-z0-9]{16,64})"',
        r"client_id:'([A-Za-z0-9]{16,64})'",
        r'"client_id":"([A-Za-z0-9]{16,64})"',
        r"client_id=([A-Za-z0-9]{16,64})",
    )
    _ASSET_URL_RE = re.compile(r"https://a-v2\.sndcdn\.com/assets/[^\"']+\.js", flags=re.IGNORECASE)
    _CLIENT_ID_TTL_SECONDS = 6 * 60 * 60

    def __init__(self, timeout: float = 10.0):
        self._timeout = max(6.0, float(timeout))
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._client_id = ""
        self._client_id_expire_at = 0.0
        self._client_id_lock = asyncio.Lock()

    @staticmethod
    def is_soundcloud_url(url: str) -> bool:
        target = normalize_text(url)
        if not target.startswith("http"):
            return False
        try:
            host = normalize_text(urlparse(target).netloc).lower()
        except Exception:
            return False
        return host == "soundcloud.com" or host.endswith(".soundcloud.com")

    @staticmethod
    def is_discover_url(url: str) -> bool:
        target = normalize_text(url)
        if not SoundCloudClient.is_soundcloud_url(target):
            return False
        try:
            path = normalize_text(urlparse(target).path).lower().rstrip("/")
        except Exception:
            return False
        return path == "/discover" or path.startswith("/discover/")

    async def get_client_id(self, *, force_refresh: bool = False) -> str:
        now = time.time()
        if not force_refresh and self._client_id and self._client_id_expire_at > now:
            return self._client_id

        async with self._client_id_lock:
            now = time.time()
            if not force_refresh and self._client_id and self._client_id_expire_at > now:
                return self._client_id
            client_id = await self._discover_client_id()
            self._client_id = client_id
            self._client_id_expire_at = now + self._CLIENT_ID_TTL_SECONDS
            _log.info("soundcloud_client_id_ok | value=%s", client_id[:8])
            return client_id

    async def search_tracks(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        keyword = normalize_text(query)
        if not keyword:
            return []
        payload = await self._api_get_json(
            "/search/tracks",
            params={"q": keyword, "limit": max(1, min(20, int(limit)))},
        )
        rows = payload.get("collection", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    async def resolve(self, page_url: str) -> dict[str, Any]:
        target = normalize_text(page_url)
        if not target:
            return {}
        payload = await self._api_get_json("/resolve", params={"url": target})
        return payload if isinstance(payload, dict) else {}

    async def get_track_audio(self, track: dict[str, Any], *, playlist_title: str = "") -> SoundCloudAudio | None:
        if not isinstance(track, dict):
            return None

        full_track = track
        transcodings = self._get_track_transcodings(track)
        if not transcodings:
            track_id = int(track.get("id", 0) or 0)
            if track_id > 0:
                try:
                    fetched = await self._api_get_json(f"/tracks/{track_id}")
                except Exception:
                    fetched = {}
                if isinstance(fetched, dict) and fetched.get("id"):
                    full_track = fetched
                    transcodings = self._get_track_transcodings(full_track)
        if not transcodings:
            return None

        title = normalize_text(str(full_track.get("title", "")))
        artist = self._extract_track_artist(full_track)
        page_url = normalize_text(str(full_track.get("permalink_url", "")))
        artwork_url = normalize_text(str(full_track.get("artwork_url", "")))
        track_id = int(full_track.get("id", 0) or 0)
        track_authorization = normalize_text(str(full_track.get("track_authorization", "")))
        best_hls: SoundCloudAudio | None = None

        for transcoding in self._sort_transcodings(transcodings):
            transcoding_url = normalize_text(str(transcoding.get("url", "")))
            if not transcoding_url:
                continue
            params: dict[str, Any] = {}
            if track_authorization:
                params["track_authorization"] = track_authorization
            try:
                payload = await self._api_get_json(transcoding_url, params=params)
            except Exception:
                continue
            audio_url = normalize_text(str(payload.get("url", ""))) if isinstance(payload, dict) else ""
            if not audio_url.startswith("http"):
                continue
            fmt = transcoding.get("format", {}) if isinstance(transcoding, dict) else {}
            protocol = normalize_text(str(fmt.get("protocol", ""))).lower()
            mime_type = normalize_text(str(fmt.get("mime_type", ""))).lower()
            candidate = SoundCloudAudio(
                track_id=track_id,
                title=title,
                artist=artist,
                page_url=page_url,
                artwork_url=artwork_url,
                audio_url=audio_url,
                protocol=protocol,
                mime_type=mime_type,
                playlist_title=normalize_text(playlist_title),
            )
            if protocol == "progressive":
                return candidate
            if best_hls is None:
                best_hls = candidate
        return best_hls

    async def resolve_page_audio(self, page_url: str) -> SoundCloudAudio | None:
        target = normalize_text(page_url)
        if not target:
            return None
        if self.is_discover_url(target):
            return await self.resolve_discover_audio()

        payload = await self.resolve(target)
        return await self._pick_audio_from_resolved(payload)

    async def resolve_discover_audio(self) -> SoundCloudAudio | None:
        payload = await self._api_get_json("/mixed-selections", params={"limit": 12, "offset": 0})
        if not isinstance(payload, dict):
            return None

        playlist_refs: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for section in payload.get("collection", []) or []:
            if not isinstance(section, dict):
                continue
            section_title = normalize_text(str(section.get("title", "")))
            rows = ((section.get("items") or {}).get("collection") or [])
            for item in rows:
                if not isinstance(item, dict):
                    continue
                if normalize_text(str(item.get("kind", ""))).lower() != "playlist":
                    continue
                playlist_url = normalize_text(str(item.get("permalink_url", "")))
                if not playlist_url or playlist_url in seen_urls:
                    continue
                seen_urls.add(playlist_url)
                playlist_title = normalize_text(str(item.get("title", ""))) or section_title
                playlist_refs.append((playlist_title, playlist_url))
                if len(playlist_refs) >= 8:
                    break
            if len(playlist_refs) >= 8:
                break

        for playlist_title, playlist_url in playlist_refs:
            try:
                resolved = await self.resolve(playlist_url)
            except Exception as exc:
                _log.warning("soundcloud_discover_playlist_fail | url=%s | %s", playlist_url[:120], exc)
                continue
            audio = await self._pick_audio_from_resolved(resolved, playlist_title=playlist_title)
            if audio and audio.audio_url:
                return audio
        return None

    async def _pick_audio_from_resolved(
        self,
        payload: dict[str, Any],
        *,
        playlist_title: str = "",
    ) -> SoundCloudAudio | None:
        if not isinstance(payload, dict):
            return None
        kind = normalize_text(str(payload.get("kind", ""))).lower()
        if kind == "track":
            return await self.get_track_audio(payload, playlist_title=playlist_title)
        if kind != "playlist":
            return None

        resolved_playlist_title = normalize_text(str(payload.get("title", ""))) or normalize_text(playlist_title)
        tracks = payload.get("tracks", [])
        if not isinstance(tracks, list):
            return None
        for track in tracks[:16]:
            if not isinstance(track, dict):
                continue
            if not bool(track.get("streamable", True)):
                continue
            audio = await self.get_track_audio(track, playlist_title=resolved_playlist_title)
            if audio and audio.audio_url:
                return audio
        return None

    async def _api_get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = dict(params or {})
        retried = False

        while True:
            client_id = await self.get_client_id(force_refresh=retried)
            request_params["client_id"] = client_id
            url = path_or_url if path_or_url.startswith("http") else f"{self._API_BASE}{path_or_url}"
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=min(6.0, self._timeout)),
                follow_redirects=True,
                headers=self._headers,
            ) as client:
                resp = await client.get(url, params=request_params)
            if resp.status_code == 401 and not retried:
                retried = True
                continue
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}

    async def _discover_client_id(self) -> str:
        pages = [self._DISCOVER_URL, "https://soundcloud.com/"]
        last_error = ""
        for page_url in pages:
            try:
                asset_urls = await self._extract_asset_urls(page_url)
                if not asset_urls:
                    continue
                client_id = await self._scan_assets_for_client_id(asset_urls)
                if client_id:
                    return client_id
            except Exception as exc:
                last_error = str(exc)
                _log.warning("soundcloud_client_id_page_fail | url=%s | %s", page_url, exc)
        raise RuntimeError(f"soundcloud_client_id_not_found:{last_error or 'no_assets'}")

    async def _extract_asset_urls(self, page_url: str) -> list[str]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=min(6.0, self._timeout)),
            follow_redirects=True,
            headers=self._headers,
        ) as client:
            resp = await client.get(page_url)
        resp.raise_for_status()
        html = resp.text
        return list(dict.fromkeys(self._ASSET_URL_RE.findall(html)))

    async def _scan_assets_for_client_id(self, asset_urls: list[str]) -> str:
        ordered = sorted(
            list(dict.fromkeys(asset_urls)),
            key=lambda item: ("53-" not in item and "54-" not in item, len(item)),
        )
        for asset_url in ordered:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=min(6.0, self._timeout)),
                follow_redirects=True,
                headers=self._headers,
            ) as client:
                resp = await client.get(asset_url)
            resp.raise_for_status()
            text = resp.text
            for pattern in self._CLIENT_ID_PATTERNS:
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
        return ""

    @staticmethod
    def _get_track_transcodings(track: dict[str, Any]) -> list[dict[str, Any]]:
        media = track.get("media", {})
        if not isinstance(media, dict):
            return []
        rows = media.get("transcodings", [])
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _extract_track_artist(track: dict[str, Any]) -> str:
        publisher = track.get("publisher_metadata", {})
        if isinstance(publisher, dict):
            artist = normalize_text(str(publisher.get("artist", "")))
            if artist:
                return artist
        user = track.get("user", {})
        if isinstance(user, dict):
            artist = normalize_text(str(user.get("username", "")))
            if artist:
                return artist
        return ""

    @staticmethod
    def _sort_transcodings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def _score(item: dict[str, Any]) -> tuple[int, int]:
            fmt = item.get("format", {}) if isinstance(item, dict) else {}
            protocol = normalize_text(str(fmt.get("protocol", ""))).lower()
            mime_type = normalize_text(str(fmt.get("mime_type", ""))).lower()
            preset = normalize_text(str(item.get("preset", ""))).lower()
            if protocol == "progressive" and "audio/mpeg" in mime_type:
                return (0, 0)
            if protocol == "progressive":
                return (1, 0)
            # 对当前这套发送/下载链路，HLS MP3 比 AAC HLS 更容易直接复用。
            if protocol == "hls" and "audio/mpeg" in mime_type:
                return (2, 0)
            if protocol == "hls" and "audio/mp4" in mime_type:
                return (3, 0)
            if protocol == "hls":
                return (4, 0)
            if "aac" in preset:
                return (5, 0)
            return (6, 0)

        return sorted(rows, key=_score)
