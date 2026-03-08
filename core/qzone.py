"""QQ空间(QZone)数据获取模块 — 个人资料 / 说说 / 相册列表。

需要 QQ 登录 cookie (p_skey, uin, skey) 才能访问大部分内容。
Cookie 可通过浏览器自动提取或手动配置。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from utils.text import normalize_text, tokenize

_log = logging.getLogger("yukiko.qzone")
_QZONE_KEYWORD_STOPWORDS = {
    "这个",
    "那个",
    "然后",
    "还是",
    "已经",
    "就是",
    "真的",
    "感觉",
    "今天",
    "昨天",
    "最近",
    "因为",
    "所以",
    "一个",
    "一下",
    "我们",
    "你们",
    "他们",
    "自己",
    "什么",
    "怎么",
    "为什么",
}

# ---------------------------------------------------------------------------
# g_tk 令牌计算
# ---------------------------------------------------------------------------

def compute_g_tk(skey: str) -> int:
    """根据 p_skey / skey 计算 QZone API 所需的 g_tk 令牌。"""
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


def strip_jsonp(text: str) -> str:
    """剥离 JSONP 回调包装，返回纯 JSON 字符串。"""
    m = re.match(r"^[^(]*\((.+)\);?\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """将 'k1=v1; k2=v2' 格式的 cookie 字符串解析为 dict。"""
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class QZoneProfile:
    uin: str = ""
    nickname: str = ""
    gender: str = ""          # 男/女/未知
    location: str = ""        # 省份 城市
    level: int = 0
    vip_info: str = ""
    signature: str = ""
    avatar_url: str = ""
    birthday: str = ""
    age: int = 0
    constellation: str = ""   # 星座


@dataclass(slots=True)
class QZoneMood:
    tid: str = ""
    content: str = ""
    create_time: str = ""
    comment_count: int = 0
    like_count: int = 0
    pic_urls: list[str] = field(default_factory=list)
    video_url: str = ""
    video_cover_url: str = ""


@dataclass(slots=True)
class QZoneAlbum:
    album_id: str = ""
    name: str = ""
    desc: str = ""
    photo_count: int = 0
    create_time: str = ""


@dataclass(slots=True)
class QZonePhoto:
    """相册中的单张照片。"""
    photo_id: str = ""
    album_id: str = ""
    url: str = ""           # 原图 URL
    thumb_url: str = ""     # 缩略图 URL
    name: str = ""
    desc: str = ""
    create_time: str = ""
    width: int = 0
    height: int = 0


@dataclass(slots=True)
class QZoneAnalysis:
    target_uin: str = ""
    profile: QZoneProfile = field(default_factory=QZoneProfile)
    moods: list[QZoneMood] = field(default_factory=list)
    albums: list[QZoneAlbum] = field(default_factory=list)
    mood_keywords: list[str] = field(default_factory=list)
    image_post_ratio: float = 0.0
    avg_mood_length: int = 0
    total_album_photos: int = 0
    latest_mood_time: str = ""


# ---------------------------------------------------------------------------
# QZone API 客户端
# ---------------------------------------------------------------------------

class QZoneClient:
    """QZone API 客户端，需要有效的 QQ 登录 cookie。"""

    _PROFILE_URL = (
        "https://h5.qzone.qq.com/proxy/domain/base.qzone.qq.com"
        "/cgi-bin/user/cgi_userinfo_get_all"
    )
    _MOOD_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
        "/cgi-bin/emotion_cgi_msglist_v6"
    )
    _ALBUM_URL = (
        "https://h5.qzone.qq.com/proxy/domain/photo.qzone.qq.com"
        "/fcgi-bin/fcg_list_album_v3"
    )
    _PHOTO_LIST_URL = (
        "https://h5.qzone.qq.com/proxy/domain/photo.qzone.qq.com"
        "/fcgi-bin/cgi_list_photo"
    )

    def __init__(self, cookies: dict[str, str], *, timeout: float = 12.0):
        self._cookies = cookies
        self._timeout = timeout
        self._g_tk = compute_g_tk(cookies.get("p_skey", "") or cookies.get("skey", ""))
        raw_uin = cookies.get("uin", "") or cookies.get("p_uin", "")
        self._self_uin = raw_uin.lstrip("o") if raw_uin.startswith("o") else raw_uin

    @property
    def self_uin(self) -> str:
        return self._self_uin

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _headers(self, referer_uin: str = "") -> dict[str, str]:
        ref = f"https://user.qzone.qq.com/{referer_uin or self._self_uin}"
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            ),
            "Referer": ref,
            "Cookie": self._cookie_header(),
        }

    async def _get_json(self, url: str, params: dict[str, Any], uin: str) -> dict[str, Any]:
        """发起 GET 请求，解析 JSONP 响应。"""
        params["g_tk"] = self._g_tk
        async with httpx.AsyncClient(timeout=self._timeout, verify=False) as client:
            resp = await client.get(url, params=params, headers=self._headers(uin))
            resp.raise_for_status()
            text = resp.text.strip()
        raw_json = strip_jsonp(text)
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            _log.warning("qzone_jsonp_parse_fail | url=%s | text=%s", url, text[:200])
            return {}

    async def get_profile(self, target_uin: str) -> QZoneProfile:
        """获取目标用户的 QZone 资料。"""
        data = await self._get_json(self._PROFILE_URL, {
            "uin": target_uin, "fupdate": "1", "outCharset": "utf-8",
        }, target_uin)

        code = data.get("code")
        if code == -3000:
            raise PermissionError("QZone cookie 已过期，请重新配置")
        if code == -4009:
            raise PermissionError("对方空间设置了访问权限，无法查看")
        if code not in (0, None):
            _log.warning("qzone_profile_error | uin=%s | code=%s", target_uin, code)

        info = data.get("data", data)
        gender_map = {0: "未知", 1: "男", 2: "女"}
        profile = QZoneProfile(
            uin=target_uin,
            nickname=str(info.get("nickname", "") or info.get("nick", "") or ""),
            gender=gender_map.get(info.get("sex", 0), "未知"),
            location=self._build_location(info),
            level=int(info.get("level", 0) or 0),
            vip_info=self._parse_vip(info),
            signature=str(info.get("signature", "") or info.get("desc", "") or ""),
            avatar_url=str(info.get("avatar", "") or info.get("avatarUrl", "") or ""),
            birthday=self._parse_birthday(info),
            age=int(info.get("age", 0) or 0),
            constellation=str(info.get("constellation", "") or ""),
        )
        return profile

    async def get_moods(self, target_uin: str, count: int = 10, offset: int = 0) -> list[QZoneMood]:
        """获取目标用户的说说列表。"""
        count = min(max(1, count), 20)
        data = await self._get_json(self._MOOD_URL, {
            "uin": target_uin, "num": str(count), "pos": str(offset),
            "format": "jsonp", "need_private_comment": "1",
            "outCharset": "utf-8",
        }, target_uin)

        code = data.get("code")
        if code == -3000:
            raise PermissionError("QZone cookie 已过期，请重新配置")
        if code == -4009:
            raise PermissionError("对方空间设置了访问权限，无法查看说说")

        moods: list[QZoneMood] = []
        for item in (data.get("msglist") or []):
            pics: list[str] = []
            for pic in (item.get("pic") or []):
                url = pic.get("url3") or pic.get("url2") or pic.get("url1") or ""
                if url:
                    pics.append(url)
            # 提取视频 URL
            video_url = ""
            video_cover_url = ""
            video_info = item.get("video") or {}
            if isinstance(video_info, dict):
                video_url = str(video_info.get("url3") or video_info.get("url2") or video_info.get("url1") or "")
                video_cover_url = str(video_info.get("cover_url") or video_info.get("pic_url") or "")
            # 某些说说的视频在 richinfo 中
            if not video_url:
                for rich in (item.get("richinfo") or []):
                    if isinstance(rich, dict) and rich.get("busitype") in (1, "1"):
                        video_url = str(rich.get("playurl") or rich.get("url") or "")
                        if not video_cover_url:
                            video_cover_url = str(rich.get("coverurl") or "")
                        if video_url:
                            break
            ts = int(item.get("created_time", 0) or 0)
            moods.append(QZoneMood(
                tid=str(item.get("tid", "")),
                content=str(item.get("content", "") or ""),
                create_time=time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
                comment_count=int(item.get("cmtnum", 0) or 0),
                like_count=int(item.get("fwdnum", 0) or 0),
                pic_urls=pics,
                video_url=video_url,
                video_cover_url=video_cover_url,
            ))
        return moods

    async def get_albums(self, target_uin: str) -> list[QZoneAlbum]:
        """获取目标用户的相册列表。"""
        data = await self._get_json(self._ALBUM_URL, {
            "uin": target_uin, "outCharset": "utf-8",
        }, target_uin)

        code = data.get("code")
        if code == -3000:
            raise PermissionError("QZone cookie 已过期，请重新配置")
        if code == -4009:
            raise PermissionError("对方空间设置了访问权限，无法查看相册")

        albums: list[QZoneAlbum] = []
        album_list = data.get("data", {}).get("albumListModeSort") or data.get("data", {}).get("albumList") or []
        for item in album_list:
            ts = int(item.get("createtime", 0) or 0)
            albums.append(QZoneAlbum(
                album_id=str(item.get("id", "")),
                name=str(item.get("name", "") or ""),
                desc=str(item.get("desc", "") or ""),
                photo_count=int(item.get("total", 0) or 0),
                create_time=time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
            ))
        return albums

    async def get_photos(
        self,
        target_uin: str,
        album_id: str,
        count: int = 30,
        offset: int = 0,
    ) -> list[QZonePhoto]:
        """获取指定相册中的照片列表（含原图 URL）。"""
        count = min(max(1, count), 100)
        data = await self._get_json(self._PHOTO_LIST_URL, {
            "uin": target_uin,
            "topicId": album_id,
            "num": str(count),
            "start": str(offset),
            "outCharset": "utf-8",
            "mode": "0",
            "noTopic": "0",
        }, target_uin)

        code = data.get("code")
        if code == -3000:
            raise PermissionError("QZone cookie 已过期，请重新配置")
        if code == -4009:
            raise PermissionError("对方空间设置了访问权限，无法查看照片")

        photos: list[QZonePhoto] = []
        photo_list = data.get("data", {}).get("photoList") or data.get("photoList") or []
        for item in photo_list:
            # 优先取原图
            url = (
                str(item.get("raw") or item.get("url") or item.get("origin_url") or "")
            )
            if not url:
                # 回退到大图
                url = str(item.get("url3") or item.get("url2") or item.get("url1") or "")
            thumb = str(item.get("url1") or item.get("pre") or "")
            ts = int(item.get("uploadtime", 0) or item.get("modifytime", 0) or 0)
            photos.append(QZonePhoto(
                photo_id=str(item.get("lloc", "") or item.get("picKey", "")),
                album_id=album_id,
                url=url,
                thumb_url=thumb,
                name=str(item.get("name", "") or ""),
                desc=str(item.get("desc", "") or ""),
                create_time=time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
                width=int(item.get("width", 0) or 0),
                height=int(item.get("height", 0) or 0),
            ))
        return photos

    async def download_photos(
        self,
        photos: list[QZonePhoto],
        output_dir: str,
        *,
        max_concurrent: int = 3,
    ) -> list[str]:
        """批量下载照片到指定目录，返回成功下载的文件路径列表。"""
        import asyncio
        from pathlib import Path

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        sem = asyncio.Semaphore(max_concurrent)
        results: list[str] = []

        async def _dl(photo: QZonePhoto) -> str | None:
            url = photo.url or photo.thumb_url
            if not url:
                return None
            ext = ".jpg"
            if ".png" in url.lower():
                ext = ".png"
            elif ".gif" in url.lower():
                ext = ".gif"
            fname = f"{photo.photo_id or photo.album_id}_{len(results)}{ext}"
            path = out / fname
            async with sem:
                try:
                    async with httpx.AsyncClient(
                        timeout=15.0, follow_redirects=True, verify=False
                    ) as client:
                        resp = await client.get(url, headers=self._headers(photo.album_id))
                        resp.raise_for_status()
                        path.write_bytes(resp.content)
                        return str(path)
                except Exception as exc:
                    _log.warning("qzone_photo_download_fail | %s | %s", fname, exc)
                    return None

        tasks = [_dl(p) for p in photos]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, str):
                results.append(result)
        return results

    async def analyze_space(
        self,
        target_uin: str,
        *,
        mood_count: int = 8,
        include_moods: bool = True,
        include_albums: bool = True,
    ) -> QZoneAnalysis:
        """聚合 QQ 空间资料，返回可直接用于展示/工具输出的结构化分析。"""
        profile = await self.get_profile(target_uin)
        moods: list[QZoneMood] = []
        albums: list[QZoneAlbum] = []

        if include_moods:
            moods = await self.get_moods(target_uin, count=min(max(1, mood_count), 20))
        if include_albums:
            albums = await self.get_albums(target_uin)

        image_post_ratio = 0.0
        avg_mood_length = 0
        latest_mood_time = ""
        mood_keywords: list[str] = []
        if moods:
            image_posts = sum(1 for item in moods if item.pic_urls)
            image_post_ratio = image_posts / len(moods)
            avg_mood_length = int(
                sum(len(normalize_text(item.content)) for item in moods) / max(1, len(moods))
            )
            latest_mood_time = moods[0].create_time or ""
            mood_keywords = self._extract_mood_keywords(moods, top_n=6)

        total_album_photos = sum(max(0, item.photo_count) for item in albums)
        return QZoneAnalysis(
            target_uin=target_uin,
            profile=profile,
            moods=moods,
            albums=albums,
            mood_keywords=mood_keywords,
            image_post_ratio=image_post_ratio,
            avg_mood_length=avg_mood_length,
            total_album_photos=total_album_photos,
            latest_mood_time=latest_mood_time,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_location(info: dict[str, Any]) -> str:
        province = str(info.get("province", "") or "")
        city = str(info.get("city", "") or "")
        country = str(info.get("country", "") or "")
        parts = [p for p in (country, province, city) if p]
        return " ".join(parts)

    @staticmethod
    def _parse_vip(info: dict[str, Any]) -> str:
        parts: list[str] = []
        if info.get("is_yellow_year_vip"):
            parts.append("年费黄钻")
        elif info.get("yellow_vip_level"):
            parts.append(f"黄钻LV{info['yellow_vip_level']}")
        if info.get("vip"):
            parts.append(f"VIP{info.get('vip_level', '')}")
        return " ".join(parts) if parts else ""

    @staticmethod
    def _parse_birthday(info: dict[str, Any]) -> str:
        y = info.get("birthday_y") or info.get("birthyear") or 0
        m = info.get("birthday_m") or info.get("birthmonth") or 0
        d = info.get("birthday_d") or info.get("birthday") or 0
        if y and m and d:
            return f"{y}-{int(m):02d}-{int(d):02d}"
        if m and d:
            return f"{int(m):02d}-{int(d):02d}"
        return ""

    @staticmethod
    def _extract_mood_keywords(moods: list[QZoneMood], top_n: int = 6) -> list[str]:
        freq: dict[str, int] = {}
        for mood in moods:
            for token in tokenize(mood.content):
                if token in _QZONE_KEYWORD_STOPWORDS:
                    continue
                if len(token) <= 1 or token.isdigit():
                    continue
                freq[token] = freq.get(token, 0) + 1
        if not freq:
            return []
        sorted_items = sorted(freq.items(), key=lambda item: (-item[1], item[0]))
        return [token for token, _ in sorted_items[: max(1, top_n)]]
