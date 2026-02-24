from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.filter import STOP_WORDS
from utils.text import normalize_text, tokenize


@dataclass(slots=True)
class MemoryMessage:
    role: str
    user_id: str
    user_name: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryEngine:
    def __init__(self, config: dict[str, Any], memory_dir: Path):
        self.enable_daily_log = bool(config.get("enable_daily_log", True))
        self.enable_vector_memory = bool(config.get("enable_vector_memory", True))
        self.max_context_messages = int(config.get("max_context_messages", 50))
        self.summary_every_n_messages = max(1, int(config.get("summary_every_n_messages", 20)))
        self.vector_dim = max(16, int(config.get("vector_dim", 64)))
        self.retrieve_top_k = max(1, int(config.get("retrieve_top_k", 5)))
        self.privacy_filter = bool(config.get("privacy_filter", False))

        self.memory_dir = memory_dir
        self.daily_dir = memory_dir / "daily"
        self.vector_dir = memory_dir / "vector"
        self.user_dir = memory_dir / "users"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._history: dict[str, deque[MemoryMessage]] = defaultdict(
            lambda: deque(maxlen=self.max_context_messages)
        )
        # (role, user_id, user_name, content)
        self._daily_records: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        self._daily_keywords: dict[str, Counter[str]] = defaultdict(Counter)
        self._daily_emotions: dict[str, Counter[str]] = defaultdict(Counter)
        self._daily_user_message_count: dict[str, Counter[str]] = defaultdict(Counter)
        self._daily_user_keywords: dict[str, dict[str, Counter[str]]] = defaultdict(
            lambda: defaultdict(Counter)
        )
        self._daily_topic_traces: dict[str, list[str]] = defaultdict(list)
        self._daily_user_intents: dict[str, dict[str, Counter[str]]] = defaultdict(
            lambda: defaultdict(Counter)
        )
        self._user_display_names: dict[str, str] = {}
        self._message_counter = 0

        self.user_profiles_path = self.user_dir / "profiles.json"
        self._user_profiles: dict[str, dict[str, Any]] = self._load_user_profiles()
        for user_id, profile in self._user_profiles.items():
            name = normalize_text(str(profile.get("display_name", "")))
            if name:
                self._user_display_names[user_id] = name
        self.thread_state_path = self.user_dir / "thread_state.json"
        self._thread_state: dict[str, dict[str, Any]] = self._load_thread_state()

        self.db_path = self.vector_dir / "memory.db"
        if self.enable_vector_memory:
            self._init_vector_db()

    def _load_user_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.user_profiles_path.exists():
            return {}
        try:
            data = json.loads(self.user_profiles_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for user_id, profile in data.items():
            if isinstance(profile, dict):
                parsed[str(user_id)] = profile
        return parsed

    def _save_user_profiles(self) -> None:
        self.user_profiles_path.write_text(
            json.dumps(self._user_profiles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_thread_state(self) -> dict[str, dict[str, Any]]:
        if not self.thread_state_path.exists():
            return {}
        try:
            data = json.loads(self.thread_state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        parsed: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                parsed[str(key)] = value
        return parsed

    def _save_thread_state(self) -> None:
        self.thread_state_path.write_text(
            json.dumps(self._thread_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_vector_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_conversation_id ON embeddings(conversation_id);"
            )

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.vector_dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).hexdigest()
            idx = int(digest, 16) % self.vector_dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))

    def _store_vector(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        ts: datetime,
    ) -> None:
        if not self.enable_vector_memory:
            return
        embedding = self._embed(content)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embeddings (conversation_id, user_id, role, content, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    conversation_id,
                    user_id,
                    role,
                    content,
                    json.dumps(embedding, ensure_ascii=False),
                    ts.isoformat(),
                ),
            )

    def add_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        timestamp: datetime | None = None,
        user_name: str = "",
    ) -> None:
        text = normalize_text(content)
        if not text:
            return
        if self.privacy_filter:
            text = self._redact_sensitive_content(text)
            if not text:
                return

        ts = timestamp or datetime.now(timezone.utc)
        clean_user_name = normalize_text(user_name)
        self._history[conversation_id].append(
            MemoryMessage(role=role, user_id=user_id, user_name=clean_user_name, content=text, timestamp=ts)
        )
        self._store_vector(conversation_id, user_id, role, text, ts)

        day_key = ts.astimezone().date().isoformat()
        self._daily_records[day_key].append((role, user_id, clean_user_name, text))
        for token in tokenize(text):
            if token in STOP_WORDS:
                continue
            self._daily_keywords[day_key][token] += 1

        if role == "user":
            if clean_user_name:
                self._user_display_names[user_id] = clean_user_name
            emotion = self.detect_emotion(text)
            self._daily_emotions[day_key][emotion] += 1
            self._daily_user_message_count[day_key][user_id] += 1
            for token in tokenize(text):
                if token in STOP_WORDS:
                    continue
                self._daily_user_keywords[day_key][user_id][token] += 1
            self._update_user_profile(
                user_id=user_id,
                user_name=clean_user_name,
                text=text,
                ts=ts,
                conversation_id=conversation_id,
            )

        self._message_counter += 1
        if self.enable_daily_log and self._message_counter % self.summary_every_n_messages == 0:
            self.write_daily_snapshot(day_key)

    @staticmethod
    def _redact_sensitive_content(text: str) -> str:
        redacted = text
        redacted = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[已隐藏邮箱]", redacted)
        redacted = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[已隐藏手机号]", redacted)
        redacted = re.sub(r"\b(sk-[A-Za-z0-9_-]{12,})\b", "[已隐藏密钥]", redacted)
        return normalize_text(redacted)

    # ── 语言风格检测 ──

    _SLANG_PATTERNS = re.compile(
        r"(hhh|233|awsl|xswl|yyds|绝绝子|无语子|笑死|6{3,}|牛[逼批]|卧槽|wc|nb|"
        r"草|寄|芜湖|蚌埠|典|乐|绷|急了|破防|麻了|真下头|离谱|逆天|抽象|"
        r"bro|lol|omg|wtf|bruh|dude|ngl|fr|ong|lowkey|highkey|"
        r"[QqAaOoTt][WwVv][QqAaOo]|orz|ovo|qwq|uwu|awa)",
        re.IGNORECASE,
    )
    _FORMAL_PATTERNS = re.compile(
        r"(请问|您好|麻烦|感谢|谢谢您|请教|劳驾|打扰|不好意思|"
        r"能否|是否|可否|建议|认为|个人觉得|综上|总结来说)",
    )
    _EMOJI_PATTERN = re.compile(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
        r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0001F1E0-\U0001F1FF]"
    )

    @classmethod
    def _detect_language_style(cls, text: str) -> str:
        """检测单条消息的语言风格：slang / casual / formal。"""
        slang_hits = len(cls._SLANG_PATTERNS.findall(text))
        formal_hits = len(cls._FORMAL_PATTERNS.findall(text))
        emoji_hits = len(cls._EMOJI_PATTERN.findall(text))

        if slang_hits >= 2 or (slang_hits >= 1 and emoji_hits >= 2):
            return "slang"
        if formal_hits >= 2:
            return "formal"
        if slang_hits >= 1 or emoji_hits >= 1:
            return "casual"
        if formal_hits >= 1:
            return "formal"
        return "casual"

    @staticmethod
    def _detect_topic_category(text: str) -> str:
        """粗粒度话题分类。"""
        lower = text.lower()
        tech_kw = ("代码", "bug", "api", "python", "java", "linux", "git", "docker",
                    "数据库", "服务器", "编程", "开发", "框架", "npm", "pip", "debug")
        game_kw = ("游戏", "原神", "lol", "mc", "steam", "ps5", "xbox", "switch",
                    "副本", "抽卡", "氪金", "段位", "rank", "fps", "moba")
        anime_kw = ("动漫", "番剧", "漫画", "二次元", "cos", "声优", "新番",
                     "轻小说", "bilibili", "b站", "mad", "amv")
        life_kw = ("吃饭", "睡觉", "上班", "下班", "周末", "天气", "外卖",
                    "快递", "出门", "回家", "累了", "休息")
        music_kw = ("歌", "音乐", "专辑", "歌手", "演唱会", "网易云", "qq音乐")

        for kw_set, label in [
            (tech_kw, "tech"), (game_kw, "game"), (anime_kw, "anime"),
            (life_kw, "life"), (music_kw, "music"),
        ]:
            if any(kw in lower for kw in kw_set):
                return label
        return "general"

    def _update_user_profile(
        self,
        user_id: str,
        user_name: str,
        text: str,
        ts: datetime,
        conversation_id: str,
    ) -> None:
        profile = self._user_profiles.get(user_id, {})
        message_count = int(profile.get("message_count", 0)) + 1
        total_chars = int(profile.get("total_chars", 0)) + len(text)
        question_count = int(profile.get("question_count", 0))
        if "?" in text or "？" in text:
            question_count += 1

        hour = ts.astimezone().hour
        hours = profile.get("active_hours", {})
        if not isinstance(hours, dict):
            hours = {}
        hour_key = f"{hour:02d}"
        hours[hour_key] = int(hours.get(hour_key, 0)) + 1

        keywords = profile.get("keywords", {})
        if not isinstance(keywords, dict):
            keywords = {}
        for token in tokenize(text):
            if token in STOP_WORDS:
                continue
            keywords[token] = int(keywords.get(token, 0)) + 1

        # 语言风格统计
        style_counts = profile.get("style_counts", {})
        if not isinstance(style_counts, dict):
            style_counts = {}
        style = self._detect_language_style(text)
        style_counts[style] = int(style_counts.get(style, 0)) + 1

        # 话题分类统计
        topic_counts = profile.get("topic_counts", {})
        if not isinstance(topic_counts, dict):
            topic_counts = {}
        topic = self._detect_topic_category(text)
        topic_counts[topic] = int(topic_counts.get(topic, 0)) + 1

        # 回复长度偏好追踪（最近 20 条的平均长度）
        recent_lengths = profile.get("recent_lengths", [])
        if not isinstance(recent_lengths, list):
            recent_lengths = []
        recent_lengths.append(len(text))
        recent_lengths = recent_lengths[-20:]

        # 情绪统计
        emotion_counts = profile.get("emotion_counts", {})
        if not isinstance(emotion_counts, dict):
            emotion_counts = {}
        emotion = self.detect_emotion(text)
        emotion_counts[emotion] = int(emotion_counts.get(emotion, 0)) + 1

        display_name = user_name or str(profile.get("display_name", "")).strip()
        updated = {
            "user_id": user_id,
            "display_name": display_name,
            "message_count": message_count,
            "total_chars": total_chars,
            "question_count": question_count,
            "last_seen": ts.isoformat(),
            "last_conversation_id": conversation_id,
            "active_hours": hours,
            "keywords": keywords,
            "style_counts": style_counts,
            "topic_counts": topic_counts,
            "recent_lengths": recent_lengths,
            "emotion_counts": emotion_counts,
        }
        self._user_profiles[user_id] = updated
        if display_name:
            self._user_display_names[user_id] = display_name
        self._save_user_profiles()

    def get_user_profile_summary(self, user_id: str) -> str:
        profile = self._user_profiles.get(user_id)
        if not profile:
            return ""

        message_count = int(profile.get("message_count", 0))
        if message_count <= 0:
            return ""

        display_name = (
            normalize_text(str(profile.get("display_name", "")))
            or self._user_display_names.get(user_id, user_id)
        )
        total_chars = int(profile.get("total_chars", 0))
        question_count = int(profile.get("question_count", 0))

        avg_len = total_chars / max(1, message_count)
        question_ratio = question_count / max(1, message_count)

        keywords = profile.get("keywords", {})
        keyword_items: list[tuple[str, int]] = []
        if isinstance(keywords, dict):
            keyword_items = sorted(
                ((str(k), int(v)) for k, v in keywords.items()),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
        keyword_text = "、".join(item[0] for item in keyword_items) if keyword_items else "暂无明显主题"

        style_hints: list[str] = []

        # 语言风格
        style_counts = profile.get("style_counts", {})
        if isinstance(style_counts, dict) and style_counts:
            dominant_style = max(style_counts.items(), key=lambda x: int(x[1]))[0]
            style_map = {"slang": "网络用语多", "formal": "表达偏正式", "casual": "日常口语"}
            style_hints.append(style_map.get(dominant_style, "日常口语"))

        # 消息长度
        if avg_len <= 10:
            style_hints.append("偏短句")
        elif avg_len >= 35:
            style_hints.append("描述偏详细")

        if question_ratio >= 0.35:
            style_hints.append("常追问细节")

        # 话题偏好
        topic_counts = profile.get("topic_counts", {})
        if isinstance(topic_counts, dict) and topic_counts:
            top_topics = sorted(topic_counts.items(), key=lambda x: int(x[1]), reverse=True)[:2]
            topic_map = {
                "tech": "技术", "game": "游戏", "anime": "动漫/二次元",
                "life": "日常生活", "music": "音乐", "general": "综合",
            }
            topic_labels = [topic_map.get(t, t) for t, _ in top_topics if t != "general"]
            if topic_labels:
                style_hints.append(f"常聊{'、'.join(topic_labels)}")

        # 情绪倾向
        emotion_counts = profile.get("emotion_counts", {})
        if isinstance(emotion_counts, dict) and emotion_counts:
            dominant_emotion = max(emotion_counts.items(), key=lambda x: int(x[1]))[0]
            if dominant_emotion != "中性":
                style_hints.append(f"情绪偏{dominant_emotion}")

        # 活跃时段
        active_hours = profile.get("active_hours", {})
        if isinstance(active_hours, dict) and active_hours:
            peak_hour = max(active_hours.items(), key=lambda item: int(item[1]))[0]
            style_hints.append(f"活跃时段约 {peak_hour}:00")

        return (
            f"{display_name}（{user_id}）累计消息 {message_count} 条，"
            f"常聊关键词：{keyword_text}。习惯：{'、'.join(style_hints)}。"
        )
    def get_recent_messages(self, conversation_id: str, limit: int | None = None) -> list[MemoryMessage]:
        records = list(self._history.get(conversation_id, []))
        if limit is None:
            return records
        return records[-limit:]

    def get_recent_texts(self, conversation_id: str, limit: int | None = None) -> list[str]:
        return [item.content for item in self.get_recent_messages(conversation_id, limit=limit)]

    def get_conversation_keyword_hints(self, conversation_id: str, limit: int = 8) -> list[str]:
        """从会话近期用户消息提取高频关键词（仅作 AI 语义提示，不做硬触发）。"""
        window = max(20, min(160, self.max_context_messages))
        records = self.get_recent_messages(conversation_id, limit=window)
        counter: Counter[str] = Counter()
        for item in records:
            if str(getattr(item, "role", "")) != "user":
                continue
            content = normalize_text(str(getattr(item, "content", "")))
            if not content:
                continue
            for token in tokenize(content):
                word = normalize_text(token)
                if not word or word in STOP_WORDS:
                    continue
                # 过滤噪声 token，保留中文词和长度>=2 的英文词。
                if len(word) <= 1 and re.fullmatch(r"[A-Za-z0-9_]+", word):
                    continue
                counter[word] += 1

        top_n = max(1, min(20, int(limit)))
        return [word for word, _ in counter.most_common(top_n)]

    def record_decision(
        self,
        conversation_id: str,
        user_id: str,
        action: str,
        reason: str,
        text: str,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or datetime.now(timezone.utc)
        day_key = ts.astimezone().date().isoformat()
        intent = normalize_text(action or "unknown").lower() or "unknown"
        self._daily_user_intents[day_key][user_id][intent] += 1

        topic = self._topic_from_text(text)
        if topic:
            traces = self._daily_topic_traces[day_key]
            if not traces or traces[-1] != topic:
                traces.append(topic)
            if len(traces) > 30:
                del traces[:-30]

        state = self._thread_state.get(conversation_id, {})
        state.update(
            {
                "last_user_id": user_id,
                "last_action": intent,
                "last_reason": normalize_text(reason)[:80],
                "last_topic": topic,
                "updated_at": ts.isoformat(),
            }
        )
        self._thread_state[conversation_id] = state
        self._save_thread_state()

    def get_thread_state(self, conversation_id: str) -> dict[str, Any]:
        state = self._thread_state.get(conversation_id, {})
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _topic_from_text(text: str) -> str:
        tokens = [token for token in tokenize(text or "") if token not in STOP_WORDS]
        if not tokens:
            return ""
        return " ".join(tokens[:3])

    def search_related(
        self,
        conversation_id: str,
        query: str,
        top_k: int | None = None,
        roles: tuple[str, ...] | None = None,
        user_id: str = "",
    ) -> list[str]:
        if not self.enable_vector_memory or not query.strip():
            return []

        query_vec = self._embed(query)
        k = top_k or self.retrieve_top_k
        allowed_roles = roles or ("user", "assistant")
        user_filter = normalize_text(user_id)
        with self._connect() as conn:
            if user_filter:
                rows = conn.execute(
                    """
                    SELECT role, content, embedding, user_id
                    FROM embeddings
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT 500;
                    """,
                    (conversation_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT role, content, embedding, user_id
                    FROM embeddings
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT 300;
                    """,
                    (conversation_id,),
                ).fetchall()

        scored: list[tuple[float, str]] = []
        for role, content, emb_json, row_user_id in rows:
            if str(role) not in allowed_roles:
                continue
            if user_filter and str(role) == "user" and normalize_text(str(row_user_id)) != user_filter:
                continue
            try:
                emb = json.loads(emb_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(emb, list):
                continue
            vector = [float(x) for x in emb]
            score = self._cosine(query_vec, vector)
            scored.append((score, str(content)))

        scored.sort(key=lambda item: item[0], reverse=True)
        seen: set[str] = set()
        results: list[str] = []
        for _, content in scored:
            if content in seen:
                continue
            seen.add(content)
            results.append(content)
            if len(results) >= k:
                break
        return results

    @staticmethod
    def detect_emotion(text: str) -> str:
        lower = text.lower()
        anxiety = ("焦虑", "担心", "害怕", "恐慌", "紧张", "anxious")
        negative = ("难受", "伤心", "累", "崩溃", "痛苦", "失望", "sad")
        positive = ("开心", "高兴", "喜欢", "太棒", "哈哈", "happy", "great")
        cold = ("随便", "行吧", "哦", "嗯", "...", "无所谓")

        if any(word in lower for word in anxiety):
            return "焦虑"
        if any(word in lower for word in negative):
            return "消极"
        if any(word in lower for word in positive):
            return "开心"
        if any(word in lower for word in cold):
            return "冷淡"
        return "中性"
    def write_daily_snapshot(self, day_key: str | None = None) -> None:
        if not self.enable_daily_log:
            return

        key = day_key or datetime.now().date().isoformat()
        records = self._daily_records.get(key, [])
        keyword_counter = self._daily_keywords.get(key, Counter())
        emotion_counter = self._daily_emotions.get(key, Counter())

        user_messages = [text for role, _, _, text in records if role == "user"]
        important_candidates = [msg for msg in user_messages if len(msg) >= 10]
        important = important_candidates[-8:] if important_candidates else user_messages[-5:]
        top_keywords = keyword_counter.most_common(10)

        dominant_emotion = "中性"
        if emotion_counter:
            dominant_emotion = emotion_counter.most_common(1)[0][0]

        if top_keywords:
            keyword_text = "、".join(word for word, _ in top_keywords[:3])
            summary = f'今天围绕 {keyword_text} 的对话较多，整体情绪以“{dominant_emotion}”为主。'
        else:
            summary = "今天对话量较少，后续可继续积累语料。"

        lines: list[str] = [
            f"# {key}",
            "",
            "## 当天重要聊天摘要",
        ]
        if important:
            lines.extend(f"- {item}" for item in important)
        else:
            lines.append("- 暂无足够对话数据。")

        lines.append("")
        lines.append("## 当日主题")
        if top_keywords:
            lines.append("- 今日高频主题词：" + "、".join(word for word, _ in top_keywords[:5]))
        else:
            lines.append("- 今日主题词不足，建议继续观察更多样本。")

        lines.append("")
        lines.append("## 出现频率最高关键词")
        if top_keywords:
            lines.extend(f"- {word}: {count}" for word, count in top_keywords)
        else:
            lines.append("- 暂无关键词。")

        lines.append("")
        lines.append("## 群内主题轨迹")
        topic_traces = self._daily_topic_traces.get(key, [])
        if topic_traces:
            for idx, topic in enumerate(topic_traces[-15:], start=1):
                lines.append(f"- {idx}. {topic}")
        else:
            lines.append("- 今日暂无明显主题轨迹。")

        lines.append("")
        lines.append("## 用户习惯与活跃度")
        daily_user_counter = self._daily_user_message_count.get(key, Counter())
        if daily_user_counter:
            for user_id, count in daily_user_counter.most_common(8):
                profile = self._user_profiles.get(user_id, {})
                display_name = (
                    normalize_text(str(profile.get("display_name", "")))
                    or self._user_display_names.get(user_id, user_id)
                )
                user_kw_counter = self._daily_user_keywords.get(key, {}).get(user_id, Counter())
                kw_text = "、".join(word for word, _ in user_kw_counter.most_common(3)) or "暂无明显偏好"

                msg_count = int(profile.get("message_count", 0))
                total_chars = int(profile.get("total_chars", 0))
                avg_len = total_chars / max(1, msg_count)
                question_ratio = int(profile.get("question_count", 0)) / max(1, msg_count)

                style_hint = "交流节奏稳定"
                if avg_len <= 10:
                    style_hint = "偏短句"
                elif avg_len >= 35:
                    style_hint = "偏详细"
                if question_ratio >= 0.35:
                    style_hint += "、常追问细节"

                lines.append(
                    f"- {display_name}({user_id})：今日 {count} 条；常聊 {kw_text}；习惯 {style_hint}"
                )
        else:
            lines.append("- 今日暂无用户习惯数据。")

        lines.append("")
        lines.append("## 用户常见触发意图分布")
        intent_map = self._daily_user_intents.get(key, {})
        if intent_map:
            for user_id, counter in sorted(
                intent_map.items(),
                key=lambda item: sum(item[1].values()),
                reverse=True,
            )[:8]:
                profile = self._user_profiles.get(user_id, {})
                display_name = (
                    normalize_text(str(profile.get("display_name", "")))
                    or self._user_display_names.get(user_id, user_id)
                )
                intents = "、".join(f"{name}:{count}" for name, count in counter.most_common(4)) or "暂无"
                lines.append(f"- {display_name}({user_id})：{intents}")
        else:
            lines.append("- 今日暂无触发意图数据。")

        lines.append("")
        lines.append("## 用户情绪趋势")
        if emotion_counter:
            for label in ("开心", "消极", "焦虑", "冷淡", "中性"):
                lines.append(f"- {label}: {emotion_counter.get(label, 0)}")
        else:
            lines.append("- 暂无可识别情绪。")

        lines.append("")
        lines.append("## Yukiko 自己的总结")
        lines.append(f"- {summary}")
        lines.append("- 下一步建议：延续上下文连续性，优先给可执行答案，同时保持陪聊温度。")

        output = "\n".join(lines).rstrip() + "\n"
        file_path = self.daily_dir / f"{key}.md"
        file_path.write_text(output, encoding="utf-8")

