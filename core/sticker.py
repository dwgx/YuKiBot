"""Sticker & emoji manager for YuKiKo bot.

表情包来源:
1. QQ 经典表情 (face_config.json, 181个)
2. 本地自定义表情 (storage/emoji/custom/, 用户手动放入)
3. 用户教学表情 (storage/emoji/add/<qq号>/, 群聊中学习)

学习流程: 用户在群里发/引用表情包 + 说"学习表情包"
-> 机器人下载图片 -> LLM 判断合法性+生成描述 -> 存入 add/<qq号>/
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_log = logging.getLogger("yukiko.sticker")

_QQ_DATA_ROOTS = [
    Path(os.path.expanduser("~/OneDrive/Documents/Tencent Files")),
    Path(os.path.expanduser("~/Documents/Tencent Files")),
]

KNOWLEDGE_FILE = "sticker_knowledge.json"


@dataclass
class FaceInfo:
    face_id: int
    desc: str
    keywords: list[str] = field(default_factory=list)
    hidden: bool = False


@dataclass
class EmojiInfo:
    """本地表情包 (custom 或 add)."""
    file_path: str
    description: str = ""
    emotions: list[str] = field(default_factory=list)
    category: str = ""         # 搞笑/可爱/嘲讽/日常/动漫/反应/文字/其他
    tags: list[str] = field(default_factory=list)
    source: str = ""           # "custom" | qq号
    learned: bool = False
    registered: bool = False   # 是否有真实描述 (区分 hash 假学习)
    manual_override: bool = False  # 手动注册表覆盖


# ── Emotion → face_id mapping for classic QQ faces ──
_EMOTION_MAP: dict[str, list[int]] = {
    "开心": [13, 4, 178, 21], "高兴": [13, 4, 178], "快乐": [13, 4],
    "笑": [13, 14, 20, 28, 178, 182, 283], "大笑": [13, 283], "微笑": [14],
    "偷笑": [20], "憨笑": [28], "斜眼笑": [178], "笑哭": [182], "狂笑": [283],
    "哭": [5, 9, 182, 173], "大哭": [9], "流泪": [5], "泪奔": [173],
    "难过": [15, 107, 106], "伤心": [5, 15, 9], "委屈": [106],
    "生气": [11, 86], "愤怒": [11, 86], "怄火": [86], "发怒": [11],
    "害怕": [26, 110], "惊恐": [26], "吓": [110],
    "惊讶": [0], "震惊": [0], "惊喜": [0],
    "尴尬": [10, 96, 97, 100], "冷汗": [96], "擦汗": [97],
    "无语": [22, 174, 284], "无奈": [174, 22], "白眼": [22],
    "面无表情": [284], "呵呵": [272],
    "鄙视": [105, 77], "嘲讽": [230], "踩": [77],
    "得意": [4, 23], "傲慢": [23], "酷": [16],
    "害羞": [6, 109], "脸红": [6],
    "可爱": [21, 175], "卖萌": [175],
    "困": [8, 25, 104], "睡": [8], "哈欠": [104],
    "疑问": [32, 268], "问号": [268, 32],
    "赞": [76, 201], "好": [76, 124, 201], "点赞": [201], "ok": [124],
    "不好": [77, 121], "差": [77, 121], "差劲": [121],
    "爱": [66, 42, 122], "喜欢": [66, 2, 42], "爱心": [66], "心碎": [67],
    "再见": [39], "拜拜": [39],
    "你好": [78, 14], "握手": [78],
    "谢谢": [78, 297], "拜谢": [297], "感谢": [297],
    "抱歉": [106, 96], "对不起": [106],
    "加油": [30, 146], "奋斗": [30], "鼓掌": [99],
    "吃瓜": [271], "摸鱼": [285], "敬礼": [282], "期待": [294],
    "doge": [179], "狗": [179, 277], "汪汪": [277],
    "emm": [270], "捂脸": [264], "辣眼睛": [265],
    "头秃": [267], "沧桑": [263], "我酸了": [273],
    "暗中观察": [269], "让我看看": [292],
    "抱抱": [49], "拥抱": [49],
    "飞吻": [85], "亲亲": [109],
    "调皮": [12], "呲牙": [13],
    "发呆": [3], "色": [2],
    "闭嘴": [7], "嘘": [33],
    "抓狂": [18], "吐": [19],
    "骷髅": [37], "猪头": [46],
    "便便": [59], "炸弹": [55],
    "咖啡": [60], "茶": [171],
    "玫瑰": [63], "凋谢": [64],
    "太阳": [74], "月亮": [75],
    "西瓜": [89], "蛋糕": [53],
    "胜利": [79], "拳头": [120],
    "勾引": [119], "抱拳": [118],
    "坏笑": [101], "阴险": [108],
    "左哼哼": [102], "右哼哼": [103],
    "可怜": [111], "快哭了": [107],
    "眨眼": [172], "斜眼": [178],
    "喷血": [177], "小纠结": [176],
    "脑阔疼": [262], "哦哟": [266],
    "无眼笑": [281], "睁眼": [289],
    "摸锦鲤": [293], "拿到红包": [295],
    "牛啊": [299], "胖三斤": [300],
}

# 不合法表情关键词 (LLM 判断后二次过滤)
_BANNED_KEYWORDS = {"色情", "裸体", "暴力", "血腥", "政治", "涉政", "赌博"}

# 有效分类列表
_VALID_CATEGORIES = {"搞笑", "可爱", "嘲讽", "日常", "动漫", "反应", "文字", "其他"}

# 用于检测 hash 文件名 (非真实描述)
_HEX_PATTERN = re.compile(r"^[0-9A-Fa-f]{16,}$")


def _detect_image_mime(data: bytes) -> str:
    """根据图片字节头判断 MIME，避免错误声明为 PNG。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/png"


class StickerManager:
    """Manages QQ faces and local emoji images."""

    def __init__(self, storage_dir: str | Path, config: dict[str, Any] | None = None):
        self._storage = Path(storage_dir)
        self._storage.mkdir(parents=True, exist_ok=True)
        self._config = config or {}
        self._faces: dict[int, FaceInfo] = {}
        self._emojis: dict[str, EmojiInfo] = {}  # key = relative path from emoji root
        self._knowledge_path = self._storage / KNOWLEDGE_FILE

        # 目录结构
        self._emoji_root = self._storage.parent / "emoji"
        self._custom_dir = self._emoji_root / "custom"
        self._add_dir = self._emoji_root / "add"
        self._custom_dir.mkdir(parents=True, exist_ok=True)
        self._add_dir.mkdir(parents=True, exist_ok=True)
        # 运行时最近学习缓存，用于“发送刚刚学习的表情包”精确召回。
        self._last_learned_by_user: dict[str, str] = {}
        self._last_learned_global: str = ""

        self._load_knowledge()

    def _normalize_knowledge_file_path(self, key: str, raw_path: str) -> str:
        """Persist relative paths only; migrate old absolute paths."""
        value = str(raw_path or "").strip().replace("\\", "/")
        if not value:
            return key
        path_obj = Path(value)
        if not path_obj.is_absolute():
            return value
        try:
            rel = path_obj.resolve().relative_to(self._emoji_root.resolve()).as_posix()
            return rel
        except Exception:
            # 非 emoji 根目录下的历史绝对路径，保留 key 避免不可移植。
            return key

    def _resolve_emoji_path(self, key: str, info: EmojiInfo) -> Path:
        raw = str(info.file_path or "").strip().replace("\\", "/")
        candidate = Path(raw) if raw else Path(key)
        if candidate.is_absolute():
            return candidate
        return (self._emoji_root / candidate).resolve()

    @staticmethod
    def _extract_json_blob(text: str) -> str:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if raw.startswith("{") and raw.endswith("}"):
            return raw
        left = raw.find("{")
        right = raw.rfind("}")
        if left >= 0 and right > left:
            return raw[left:right + 1]
        return raw

    @staticmethod
    def _repair_json_blob(text: str) -> str:
        repaired = str(text or "").strip()
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        repaired = repaired.replace("\u201c", '"').replace("\u201d", '"')
        repaired = repaired.replace("\u2018", "'").replace("\u2019", "'")
        repaired = re.sub(r"\bTrue\b", "true", repaired)
        repaired = re.sub(r"\bFalse\b", "false", repaired)
        repaired = re.sub(r"\bNone\b", "null", repaired)
        # 简单兜底：很多模型会输出单引号 JSON。
        if "'" in repaired and '"' not in repaired:
            repaired = repaired.replace("'", '"')
        return repaired

    def _parse_json_tolerant(self, raw_text: str) -> dict[str, Any] | None:
        blob = self._extract_json_blob(raw_text)
        candidates = [blob, self._repair_json_blob(blob)]
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
        return None

    def _save_chat_emoji(
        self,
        user_id: str,
        img_data: bytes,
        description: str,
        emotions: list[str] | None = None,
        category: str = "",
        tags: list[str] | None = None,
        registered: bool = True,
    ) -> str:
        user_dir = self._add_dir / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        img_hash = hashlib.md5(img_data).hexdigest()[:12]
        ext = ".png"
        if img_data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif img_data[:4] == b"GIF8":
            ext = ".gif"
        filename = f"{img_hash}{ext}"
        filepath = user_dir / filename
        filepath.write_bytes(img_data)

        key = f"add/{user_id}/{filename}"
        self._emojis[key] = EmojiInfo(
            file_path=key,
            description=description,
            emotions=emotions if isinstance(emotions, list) else [],
            category=category if category in _VALID_CATEGORIES else "其他",
            tags=tags if isinstance(tags, list) else [],
            source=str(user_id),
            learned=True,
            registered=bool(registered),
        )
        source_user = str(user_id or "").strip()
        if source_user:
            self._last_learned_by_user[source_user] = key
        self._last_learned_global = key
        self._save_knowledge()
        return key

    # ── Registry (手动注册表) ──

    def load_registry(self, path: Path | None = None) -> int:
        """加载 emoji_registry.yml 手动注册表，返回覆盖条目数。"""
        registry_path = path or (self._storage / "emoji_registry.yml")
        if not registry_path.exists():
            return 0
        try:
            import yaml
        except ImportError:
            _log.warning("registry_skip | pyyaml not installed")
            return 0
        try:
            data = yaml.safe_load(registry_path.read_text("utf-8")) or {}
        except Exception as e:
            _log.error("registry_load_fail | %s", e)
            return 0
        return self._apply_registry(data)

    def _apply_registry(self, data: dict[str, Any]) -> int:
        """将手动注册表合并到 _emojis，返回覆盖条目数。"""
        stickers = data.get("stickers", {})
        if not isinstance(stickers, dict):
            return 0
        count = 0
        for key, info in stickers.items():
            if not isinstance(info, dict):
                continue
            # 排除标记
            if info.get("exclude"):
                if key in self._emojis:
                    del self._emojis[key]
                    count += 1
                continue
            e = self._emojis.get(key)
            if not e:
                continue
            if info.get("description"):
                e.description = str(info["description"])
            if info.get("category"):
                cat = str(info["category"])
                e.category = cat if cat in _VALID_CATEGORIES else "其他"
            if info.get("tags"):
                e.tags = list(info["tags"]) if isinstance(info["tags"], list) else []
            if info.get("emotions"):
                e.emotions = list(info["emotions"]) if isinstance(info["emotions"], list) else []
            e.registered = True
            e.manual_override = True
            e.learned = True
            count += 1
        if count:
            _log.info("registry_applied | overrides=%d", count)
        return count

    # ── Public API ──

    def scan(self, qq_data_path: str | Path | None = None) -> dict[str, int]:
        """Scan classic faces + local emoji directories + load registry."""
        fc = self._scan_classic_faces(qq_data_path)
        ec = self._scan_local_emojis()
        rc = self.load_registry()
        self._save_knowledge()
        _log.info("scan_done | faces=%d emojis=%d registry=%d", fc, ec, rc)
        return {"faces": fc, "emojis": ec, "registry": rc}

    def find_face(self, query: str) -> list[FaceInfo]:
        """Find classic QQ faces matching a query."""
        q = query.lower().strip().lstrip("/")
        results: list[FaceInfo] = []
        seen: set[int] = set()
        for kw, ids in _EMOTION_MAP.items():
            if q in kw or kw in q:
                for fid in ids:
                    if fid not in seen and fid in self._faces:
                        results.append(self._faces[fid])
                        seen.add(fid)
        for face in self._faces.values():
            if face.face_id in seen:
                continue
            if q in face.desc.lstrip("/").lower():
                results.append(face)
                seen.add(face.face_id)
        return results[:10]

    def find_emoji(self, query: str, strict: bool = False) -> list[EmojiInfo]:
        """Find learned local emojis matching a query.

        支持:
        - 关键词搜索 (匹配 description / emotions / tags / category)
        - "#标签" → 按 tag 精确匹配
        - "random" / "随机" → 随机返回几张
        - 空 query → 随机返回
        """
        q = query.lower().strip()
        pool = [e for e in self._emojis.values() if e.learned]
        if not pool:
            return []

        # 随机模式
        if not q or q in ("random", "随机", "随便", "任意"):
            return random.sample(pool, min(5, len(pool)))

        # tag 搜索: #猫
        if q.startswith("#"):
            tag_q = q[1:]
            return self.find_emoji_by_tags([tag_q])

        # 多字段匹配，按相关度排序
        scored: list[tuple[int, EmojiInfo]] = []
        for e in pool:
            score = 0
            # category 精确匹配 (最高优先)
            if e.category and q == e.category.lower():
                score += 10
            # description 包含
            if e.description and q in e.description.lower():
                score += 5
            # emotions 匹配
            if any(q in em.lower() or em.lower() in q for em in e.emotions):
                score += 4
            # tags 匹配
            if any(q in t.lower() or t.lower() in q for t in e.tags):
                score += 3
            # category 包含
            if e.category and q in e.category.lower():
                score += 2
            if score > 0:
                scored.append((score, e))

        if not scored:
            if strict:
                return []
            # 兼容旧行为：没匹配到时回落随机一张
            registered = [e for e in pool if e.registered]
            fallback = registered if registered else pool
            return [random.choice(fallback)]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:10]]

    def find_emoji_by_category(self, category: str) -> list[EmojiInfo]:
        """按分类筛选已注册的表情。"""
        cat = category.lower().strip()
        return [e for e in self._emojis.values()
                if e.registered and e.category and e.category.lower() == cat]

    def find_emoji_by_tags(self, tags: list[str]) -> list[EmojiInfo]:
        """按标签筛选 (OR 逻辑)。"""
        tag_set = {t.lower().strip() for t in tags}
        results = []
        for e in self._emojis.values():
            if not e.learned:
                continue
            e_tags = {t.lower() for t in e.tags}
            if tag_set & e_tags:
                results.append(e)
        return results[:10]

    def emoji_key(self, emoji: EmojiInfo) -> str | None:
        """查找 EmojiInfo 对应的 key。"""
        for k, v in self._emojis.items():
            if v is emoji:
                return k
        return None

    def random_emoji(self, count: int = 1) -> list[tuple[str, EmojiInfo]]:
        """Return random (key, emoji) pairs from learned emojis."""
        learned = [(k, e) for k, e in self._emojis.items() if e.learned]
        if not learned:
            return []
        return random.sample(learned, min(count, len(learned)))

    def latest_emoji(self, source_user: str = "", count: int = 1) -> list[tuple[str, EmojiInfo]]:
        """按文件修改时间返回最近学习的表情包。"""
        limit = max(1, int(count))
        scored: list[tuple[float, str, EmojiInfo]] = []
        source = str(source_user or "").strip()
        for key, emoji in self._emojis.items():
            if not emoji.learned:
                continue
            if source and str(emoji.source or "").strip() != source:
                continue
            try:
                mtime = self._resolve_emoji_path(key, emoji).stat().st_mtime
            except Exception:
                mtime = 0.0
            scored.append((mtime, key, emoji))
        if not scored:
            return []
        scored.sort(key=lambda row: row[0], reverse=True)
        return [(key, emoji) for _, key, emoji in scored[:limit]]

    def last_learned_emoji(self, source_user: str = "") -> tuple[str, EmojiInfo] | None:
        """返回最近学习的一张表情（优先指定用户）。"""
        source = str(source_user or "").strip()
        candidates: list[str] = []
        if source:
            recent_key = self._last_learned_by_user.get(source, "")
            if recent_key:
                candidates.append(recent_key)
        if self._last_learned_global:
            candidates.append(self._last_learned_global)

        for key in candidates:
            emoji = self._emojis.get(key)
            if emoji and emoji.learned:
                return key, emoji
        return None

    async def learn_from_chat(
        self,
        image_url: str,
        user_id: str,
        llm_call: Any,
        image_file: str = "",
        image_sub_type: str = "",
        api_call: Any | None = None,
    ) -> tuple[bool, str]:
        """用户在群里教学表情包: 下载图片 -> LLM审核+描述 -> 存入 add/<qq号>/。

        Returns (success, message).
        """
        import httpx

        # 1. 下载图片（先尝试 URL；失败后尝试 OneBot/NapCat get_image/download_file 兜底）
        _ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        img_data: bytes | None = None
        last_err: str = ""
        source_hint = "url"

        def _extract_api_data(result: Any) -> dict[str, Any]:
            if not isinstance(result, dict):
                return {}
            data_part = result.get("data")
            if isinstance(data_part, dict):
                merged = dict(data_part)
                for k, v in result.items():
                    if k not in merged:
                        merged[k] = v
                return merged
            return result

        def _resolve_local_path(raw_path: Any) -> Path | None:
            value = str(raw_path or "").strip()
            if not value:
                return None
            if value.startswith("file://"):
                parsed = urlparse(value)
                local_raw = unquote(parsed.path or "")
                if os.name == "nt" and local_raw.startswith("/") and re.match(r"^/[A-Za-z]:", local_raw):
                    local_raw = local_raw[1:]
                value = local_raw
            candidate = Path(value).expanduser()
            try:
                candidate = candidate.resolve()
            except Exception:
                pass
            return candidate

        def _read_local_file(raw_path: Any) -> tuple[bytes | None, str]:
            p = _resolve_local_path(raw_path)
            if not p:
                return None, "missing_local_path"
            if not p.exists() or not p.is_file():
                return None, f"local_file_not_found:{p}"
            try:
                return p.read_bytes(), ""
            except Exception as exc:
                return None, str(exc)

        async def _download_via_http(url: str) -> tuple[bytes | None, str]:
            target = str(url or "").strip()
            if not target:
                return None, "empty_url"
            err = ""
            for _attempt in range(3):
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(20.0, connect=10.0),
                        follow_redirects=True,
                        headers={"User-Agent": _ua},
                    ) as client:
                        resp = await client.get(target)
                    if resp.status_code != 200:
                        err = f"HTTP {resp.status_code}"
                        continue
                    return resp.content, ""
                except Exception as exc:
                    err = str(exc)
                    if _attempt < 2:
                        import asyncio as _aio
                        await _aio.sleep(1.0 * (_attempt + 1))
            return None, err

        # 1.1 先尝试直接下载 URL
        img_data, last_err = await _download_via_http(image_url)

        # 1.2 URL 失败时，尝试 get_image(file=...) 读取本地缓存文件
        if img_data is None and api_call and image_file:
            get_image_result: Any = None
            get_image_err: str = ""
            for kwargs in (
                {"file": image_file},
                {"file_id": image_file},
                {"id": image_file},
            ):
                try:
                    get_image_result = await api_call("get_image", **kwargs)
                    get_image_err = ""
                    break
                except Exception as exc:
                    get_image_err = str(exc)
            if get_image_result is not None:
                payload = _extract_api_data(get_image_result)
                for key in ("file", "file_path", "path", "local_path", "filename"):
                    img_data, last_err = _read_local_file(payload.get(key))
                    if img_data is not None:
                        source_hint = f"get_image:{key}"
                        break
                if img_data is None:
                    for key in ("url", "download_url", "src"):
                        candidate = str(payload.get(key, "")).strip()
                        if not candidate:
                            continue
                        img_data, last_err = await _download_via_http(candidate)
                        if img_data is not None:
                            source_hint = f"get_image:{key}"
                            break
            elif get_image_err:
                last_err = f"get_image:{get_image_err}"

        # 1.3 再兜底 download_file(url=...)
        if img_data is None and api_call and image_url:
            try:
                dl_result = await api_call("download_file", url=image_url, thread_count=1)
                payload = _extract_api_data(dl_result)
                for key in ("file", "file_path", "path", "local_path", "filename"):
                    img_data, last_err = _read_local_file(payload.get(key))
                    if img_data is not None:
                        source_hint = f"download_file:{key}"
                        break
            except Exception as exc:
                last_err = f"download_file:{exc}"

        if img_data is None:
            hint = "QQ 图片链接可能已失效"
            if str(image_sub_type).strip() == "1":
                hint = "QQ 动画表情链接受限，建议改发静态图或原图文件"
            _log.warning(
                "learn_chat_download_fail | user=%s | file=%s | sub_type=%s | err=%s",
                user_id,
                image_file or "-",
                image_sub_type or "-",
                last_err or "-",
            )
            return False, f"下载失败: {last_err or 'unknown'}，{hint}"
        _log.info(
            "learn_chat_download_ok | user=%s | source=%s | bytes=%d",
            user_id,
            source_hint,
            len(img_data),
        )
        if len(img_data) < 100:
            return False, "图片太小，跳过"
        if len(img_data) > 5 * 1024 * 1024:
            return False, "图片超过5MB，太大了"

        # 2. LLM 审核 + 描述
        mime = _detect_image_mime(img_data)
        b64 = base64.b64encode(img_data).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {
                        "type": "text",
                        "text": (
                            "这是一个用户想让机器人学习的表情包。请判断:\n"
                            "1. 是否合法(不含色情/暴力/政治等违规内容)\n"
                            "2. 如果合法，描述内容和适用情绪\n"
                            '回复JSON: {"legal": true/false, "reason": "不合法原因(合法则空)", '
                            '"description": "简短描述(20字以内)", '
                            '"emotions": ["情绪1", "情绪2"], '
                            '"category": "搞笑|可爱|嘲讽|日常|动漫|反应|文字|其他", '
                            '"tags": ["标签1", "标签2", "标签3"]}\n'
                            "只回复JSON。"
                        ),
                    },
                ],
            }
        ]

        try:
            resp_text = await llm_call(messages)
            obj = self._parse_json_tolerant(resp_text)
            if obj is None:
                retry_messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                            {
                                "type": "text",
                                "text": (
                                    "你上一条输出不是可解析 JSON。"
                                    "现在只返回一个 JSON 对象，不要代码块、不要解释。\n"
                                    '{"legal": true/false, "reason": "", "description": "", "emotions": [], "category": "其他", "tags": []}'
                                ),
                            },
                        ],
                    }
                ]
                retry_text = await llm_call(retry_messages)
                obj = self._parse_json_tolerant(retry_text)
                if obj is None:
                    _log.warning("learn_chat_parse_fail | user=%s | reason=invalid_json", user_id)
                    # 保底策略：不丢图，先入库待补全
                    key = self._save_chat_emoji(
                        user_id=user_id,
                        img_data=img_data,
                        description="待补全描述",
                        emotions=[],
                        category="其他",
                        tags=["待补全"],
                        registered=False,
                    )
                    _log.info("learn_chat_fallback_saved | user=%s | key=%s", user_id, key)
                    return True, "已收下这张表情，描述待补全（稍后自动补全）"
        except Exception as e:
            _log.warning("learn_chat_llm_fail | %s", e)
            key = self._save_chat_emoji(
                user_id=user_id,
                img_data=img_data,
                description="待补全描述",
                emotions=[],
                category="其他",
                tags=["待补全"],
                registered=False,
            )
            _log.info("learn_chat_fallback_saved | user=%s | key=%s | reason=llm_error", user_id, key)
            return True, "已收下这张表情，描述待补全（模型暂时不稳定）"

        if not obj.get("legal", False):
            reason = obj.get("reason", "内容不合规")
            _log.warning("learn_chat_review_reject | user=%s | reason=%s", user_id, reason)
            return False, f"不合法: {reason}"

        desc = str(obj.get("description", "")).strip()
        emotions = obj.get("emotions", [])
        category = str(obj.get("category", "")).strip()
        tags = obj.get("tags", [])
        if not desc:
            return False, "LLM 未返回描述"

        # 二次过滤
        combined = desc + " ".join(emotions)
        for bw in _BANNED_KEYWORDS:
            if bw in combined:
                return False, f"内容包含违规关键词"

        # 3. 保存 + 注册（只存相对路径）
        key = self._save_chat_emoji(
            user_id=user_id,
            img_data=img_data,
            description=desc,
            emotions=emotions if isinstance(emotions, list) else [],
            category=category,
            tags=tags if isinstance(tags, list) else [],
            registered=True,
        )
        _log.info("learn_chat_ok | user=%s key=%s desc=%s", user_id, key, desc)
        # 返回空 display，让 Agent 根据 ok=True 自己组织回复
        return True, ""

    def get_face_segment(self, face_id: int) -> dict[str, Any]:
        return {"type": "face", "data": {"id": str(face_id)}}

    def get_emoji_segment(self, key: str) -> dict[str, Any] | None:
        """Return image segment for a local emoji."""
        e = self._emojis.get(key)
        if not e:
            return None
        p = self._resolve_emoji_path(key, e)
        if not p.exists():
            return None
        # 用 file:// 本地路径发送
        return {"type": "image", "data": {"file": f"file:///{p.resolve()}"}}

    def emoji_image_b64(self, key: str) -> str | None:
        """Read emoji as base64 for LLM vision."""
        e = self._emojis.get(key)
        if not e:
            return None
        p = self._resolve_emoji_path(key, e)
        if not p.exists() or p.stat().st_size < 100:
            return None
        try:
            return base64.b64encode(p.read_bytes()).decode()
        except Exception:
            return None

    def get_unlearned(self) -> list[EmojiInfo]:
        return [e for e in self._emojis.values() if not e.learned]

    @property
    def face_count(self) -> int:
        return len(self._faces)

    @property
    def emoji_count(self) -> int:
        return len(self._emojis)

    @property
    def learned_count(self) -> int:
        return sum(1 for e in self._emojis.values() if e.learned)

    @property
    def registered_count(self) -> int:
        return sum(1 for e in self._emojis.values() if e.registered)

    def get_unregistered(self) -> list[tuple[str, EmojiInfo]]:
        """获取需要 LLM 分析的表情 (description 是 hash 或空，且非手动覆盖)。"""
        result = []
        for key, e in self._emojis.items():
            if e.manual_override or e.registered:
                continue
            if not e.description or bool(_HEX_PATTERN.match(e.description)):
                result.append((key, e))
        return result

    def category_stats(self) -> dict[str, int]:
        """返回各分类的表情数量统计。"""
        stats: dict[str, int] = {}
        for e in self._emojis.values():
            if e.registered and e.category:
                stats[e.category] = stats.get(e.category, 0) + 1
        return stats

    def face_list_for_prompt(self) -> str:
        parts = []
        for fid in sorted(self._faces):
            f = self._faces[fid]
            if not f.hidden:
                parts.append(f"{fid}:{f.desc.lstrip('/')}")
        return ", ".join(parts)

    def status_text(self) -> str:
        unreg = len(self.get_unregistered())
        custom_count = sum(1 for e in self._emojis.values() if e.source == "custom")
        add_count = sum(1 for e in self._emojis.values() if e.source != "custom")
        return (
            f"表情系统: {self.face_count} 经典表情, "
            f"{custom_count} 自定义表情, {add_count} 用户教学表情 "
            f"(已注册 {self.registered_count}, 待注册 {unreg})"
        )

    # ── Internal: scan ──

    def _find_qq_root(self, qq_data_path: str | Path | None = None) -> Path | None:
        if qq_data_path:
            p = Path(qq_data_path)
            if p.exists():
                return p
        for root in _QQ_DATA_ROOTS:
            if root.exists():
                return root
        return None

    def _scan_classic_faces(self, qq_data_path: str | Path | None = None) -> int:
        root = self._find_qq_root(qq_data_path)
        if not root:
            _log.debug("qq_data_root_not_found")
            return 0
        cfg = root / "nt_qq" / "global" / "nt_data" / "Emoji" / "emoji-resource" / "face_config.json"
        if not cfg.exists():
            _log.warning("face_config_not_found | path=%s", cfg)
            return 0
        try:
            data = json.loads(cfg.read_text("utf-8"))
        except Exception as e:
            _log.error("face_config_parse | %s", e)
            return 0
        count = 0
        for item in data.get("sysface", []):
            sid = item.get("QSid", "")
            if not sid.isdigit():
                continue
            fid = int(sid)
            if fid > 300:
                continue
            self._faces[fid] = FaceInfo(
                face_id=fid,
                desc=item.get("QDes", ""),
                hidden=item.get("QHide", "0") == "1",
            )
            count += 1
        _log.info("classic_faces | count=%d", count)
        return count

    def _scan_local_emojis(self) -> int:
        """Scan storage/emoji/custom/ and storage/emoji/add/<qq>/ for images."""
        existing = set(self._emojis.keys())
        count = 0
        img_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

        # custom/ — 用户手动放入的图片
        if self._custom_dir.exists():
            for f in self._custom_dir.iterdir():
                if not f.is_file() or f.suffix.lower() not in img_exts:
                    continue
                key = f"custom/{f.name}"
                if key in existing:
                    e = self._emojis.get(key)
                    if e and not e.learned:
                        e.learned = True
                        if not e.description:
                            e.description = f.stem
                        e.file_path = key
                        count += 1
                    continue
                is_hash = bool(_HEX_PATTERN.match(f.stem))
                self._emojis[key] = EmojiInfo(
                    file_path=key, source="custom",
                    description=f.stem,
                    learned=True,
                    registered=not is_hash,  # hash 文件名 = 未真正注册
                )
                count += 1

        # add/<qq号>/
        if self._add_dir.exists():
            for user_dir in self._add_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                qq = user_dir.name
                for f in user_dir.iterdir():
                    if not f.is_file() or f.suffix.lower() not in img_exts:
                        continue
                    key = f"add/{qq}/{f.name}"
                    if key in existing:
                        e = self._emojis.get(key)
                        if e:
                            e.file_path = key
                        continue
                    self._emojis[key] = EmojiInfo(
                        file_path=key, source=qq,
                    )
                    count += 1

        _log.info("local_emojis | new=%d total=%d", count, len(self._emojis))
        return count

    # ── Knowledge persistence ──

    def _load_knowledge(self) -> None:
        if not self._knowledge_path.exists():
            return
        try:
            data = json.loads(self._knowledge_path.read_text("utf-8"))
            migrated = 0
            for key, info in data.get("emojis", {}).items():
                desc = info.get("description", "")
                is_hash = bool(_HEX_PATTERN.match(desc)) if desc else True
                relative_path = self._normalize_knowledge_file_path(
                    key=str(key),
                    raw_path=str(info.get("file_path", "")),
                )
                if relative_path != str(info.get("file_path", "")):
                    migrated += 1
                self._emojis[key] = EmojiInfo(
                    file_path=relative_path,
                    description=desc,
                    emotions=info.get("emotions", []),
                    category=info.get("category", ""),
                    tags=info.get("tags", []),
                    source=info.get("source", ""),
                    learned=info.get("learned", False),
                    registered=info.get("registered", not is_hash),
                    manual_override=info.get("manual_override", False),
                )
            _log.info("knowledge_loaded | emojis=%d learned=%d registered=%d",
                      len(self._emojis), self.learned_count, self.registered_count)
            if migrated > 0:
                self._save_knowledge()
                _log.info("knowledge_path_migrated | migrated=%d", migrated)
        except Exception as e:
            _log.error("knowledge_load | %s", e)

    def _save_knowledge(self) -> None:
        data: dict[str, Any] = {
            "version": 4,
            "last_scan": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "emojis": {},
        }
        for key, e in self._emojis.items():
            data["emojis"][key] = {
                "file_path": self._normalize_knowledge_file_path(key=key, raw_path=e.file_path),
                "description": e.description,
                "emotions": e.emotions,
                "category": e.category,
                "tags": e.tags,
                "source": e.source,
                "learned": e.learned,
                "registered": e.registered,
                "manual_override": e.manual_override,
            }
        try:
            self._knowledge_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            _log.error("knowledge_save | %s", e)

    # ── Batch learn for custom dir images ──

    async def learn_batch(self, llm_call: Any, batch_size: int = 5) -> int:
        """Learn unlearned/unregistered local emojis via multimodal LLM."""
        unregistered = self.get_unregistered()
        if not unregistered:
            return 0
        batch = unregistered[:batch_size]
        learned = 0
        for key, emoji in batch:
            b64 = self.emoji_image_b64(key)
            if not b64:
                _log.warning("learn_skip_no_image | key=%s", key)
                continue
            mime = "image/png"
            try:
                raw = self._resolve_emoji_path(key, emoji).read_bytes()
                if raw:
                    mime = _detect_image_mime(raw)
            except Exception:
                mime = "image/png"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {
                            "type": "text",
                            "text": (
                                "这是一个表情包图片。请用JSON回复:\n"
                                '{"description": "简短描述(20字以内)", '
                                '"emotions": ["情绪1", "情绪2"], '
                                '"category": "搞笑|可爱|嘲讽|日常|动漫|反应|文字|其他", '
                                '"tags": ["标签1", "标签2", "标签3"]}\n'
                                "只回复JSON。"
                            ),
                        },
                    ],
                }
            ]
            try:
                resp = await llm_call(messages)
                text = (resp or "").strip()
                if not text:
                    _log.debug("learn_skip | key=%s | empty_response", key)
                    continue
                obj = self._parse_json_tolerant(text)
                if obj is None:
                    _log.debug("learn_skip | key=%s | no_json_in_response", key)
                    continue
                desc = obj.get("description", "")
                if desc:
                    emoji.description = desc
                    emoji.emotions = obj.get("emotions", []) if isinstance(obj.get("emotions"), list) else []
                    cat = str(obj.get("category", ""))
                    emoji.category = cat if cat in _VALID_CATEGORIES else "其他"
                    emoji.tags = obj.get("tags", []) if isinstance(obj.get("tags"), list) else []
                    emoji.learned = True
                    emoji.registered = True
                    learned += 1
                    _log.info("learn_ok | key=%s desc=%s cat=%s", key, desc, emoji.category)
            except Exception as e:
                _log.warning("learn_fail | key=%s | %s", key, e)
        if learned:
            self._save_knowledge()
        return learned

    async def auto_register(self, llm_call: Any, batch_size: int = 10) -> dict[str, int]:
        """启动时自动注册: 批量 LLM 分析未注册的表情包。

        Returns: {"total": N, "registered": M, "remaining": R}
        """
        unregistered = self.get_unregistered()
        total = len(unregistered)
        if total == 0:
            return {"total": 0, "registered": 0, "remaining": 0}
        _log.info("auto_register_start | unregistered=%d batch=%d", total, batch_size)
        registered = await self.learn_batch(llm_call=llm_call, batch_size=batch_size)
        remaining = total - registered
        _log.info("auto_register_done | registered=%d remaining=%d", registered, remaining)
        return {"total": total, "registered": registered, "remaining": remaining}
