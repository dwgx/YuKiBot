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

SYSTEM_NOISE_KEYWORDS = frozenset(
    {
        "multimodal_event",
        "multimodal_event_at",
        "user",
        "sent",
        "multimodal",
        "message",
        "image",
        "video",
        "record",
        "audio",
        "https",
        "http",
        "com",
        "qq",
        "multimedia",
        "nt",
        "cn",
        "download",
        "appid",
        "fileid",
        "file",
        "url",
        "mentioned",
        "bot",
        "forward",
        "and",
    }
)
INVALID_PREFERRED_NAMES = frozenset({"你", "我", "他", "她", "它", "ta"})


@dataclass(slots=True)
class MemoryMessage:
    role: str
    user_id: str
    user_name: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryEngine:
    def __init__(self, config: dict[str, Any], memory_dir: Path, global_config: dict[str, Any] | None = None):
        control_cfg = {}
        if isinstance(global_config, dict):
            control_cfg = global_config.get("control", {})
            if not isinstance(control_cfg, dict):
                control_cfg = {}
        self.heuristic_rules_enable = bool(control_cfg.get("heuristic_rules_enable", False))
        self.enable_daily_log = bool(config.get("enable_daily_log", True))
        self.enable_vector_memory = bool(config.get("enable_vector_memory", True))
        self.max_context_messages = int(config.get("max_context_messages", 50))
        self.summary_every_n_messages = max(1, int(config.get("summary_every_n_messages", 20)))
        self.vector_dim = max(16, int(config.get("vector_dim", 64)))
        self.retrieve_top_k = max(1, int(config.get("retrieve_top_k", 5)))
        self.privacy_filter = bool(config.get("privacy_filter", False))
        if self.heuristic_rules_enable:
            self.preferred_name_patterns = self._compile_regex_list(config.get("preferred_name_patterns", []))
            self.preferred_name_invalid_parts = tuple(self._normalize_text_list(config.get("preferred_name_invalid_parts", [])))
            self.preferred_name_blocklist = tuple(self._normalize_text_list(config.get("preferred_name_blocklist", [])))
            self.preferred_name_block_patterns = self._compile_regex_list(config.get("preferred_name_block_patterns", []))
            self.high_risk_confirm_enable_patterns = self._compile_regex_list(
                config.get("high_risk_confirm_enable_patterns", [])
            )
            self.high_risk_confirm_disable_patterns = self._compile_regex_list(
                config.get("high_risk_confirm_disable_patterns", [])
            )
            self.agent_directive_cues = tuple(
                self._normalize_text_list(
                    config.get("agent_directive_cues", [])
                )
            )
            self.agent_directive_target_cues = tuple(
                self._normalize_text_list(
                    config.get("agent_directive_target_cues", [])
                )
            )
        else:
            self.preferred_name_patterns = ()
            self.preferred_name_invalid_parts = ()
            self.preferred_name_blocklist = ()
            self.preferred_name_block_patterns = ()
            self.high_risk_confirm_enable_patterns = ()
            self.high_risk_confirm_disable_patterns = ()
            self.agent_directive_cues = ()
            self.agent_directive_target_cues = ()

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
        self._sanitize_loaded_profiles()
        for user_id, profile in self._user_profiles.items():
            name = normalize_text(str(profile.get("display_name", "")))
            if name:
                self._user_display_names[user_id] = name
        self.thread_state_path = self.user_dir / "thread_state.json"
        self._thread_state: dict[str, dict[str, Any]] = self._load_thread_state()

        self.db_path = self.vector_dir / "memory.db"
        self._vector_buffer: list[tuple[str, str, str, str, str, str]] = []
        self._vector_buffer_limit = 10  # 攒 10 条再批量写入
        if self.enable_vector_memory:
            self._init_vector_db()

    @staticmethod
    def _normalize_text_list(values: Any) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            text = normalize_text(str(item))
            if text:
                out.append(text)
        return out

    @classmethod
    def _compile_regex_list(cls, values: Any) -> tuple[re.Pattern[str], ...]:
        patterns: list[re.Pattern[str]] = []
        for raw in cls._normalize_text_list(values):
            try:
                patterns.append(re.compile(raw))
            except re.error:
                continue
        return tuple(patterns)

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
        """延迟写入用户画像，避免每条消息都写磁盘。"""
        self._user_profiles_dirty = True

    def _sanitize_loaded_profiles(self) -> None:
        """启动时清理历史关键词噪声，避免旧画像长期污染。"""
        dirty = False
        for user_id, profile in self._user_profiles.items():
            if not isinstance(profile, dict):
                continue
            raw_keywords = profile.get("keywords", {})
            if not isinstance(raw_keywords, dict):
                continue
            cleaned: dict[str, int] = {}
            for raw_word, raw_count in raw_keywords.items():
                word = normalize_text(str(raw_word)).lower()
                if self._is_system_noise_keyword(word):
                    continue
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                cleaned[word] = cleaned.get(word, 0) + count
            if len(cleaned) > 80:
                top_items = sorted(cleaned.items(), key=lambda x: x[1], reverse=True)[:50]
                cleaned = dict(top_items)
            if cleaned != raw_keywords:
                profile["keywords"] = cleaned
                self._user_profiles[user_id] = profile
                dirty = True
        if dirty:
            self._save_user_profiles()

    def _save_user_profiles_immediate(self) -> None:
        """显式记忆场景下立即刷盘，避免重启丢失。"""
        self._user_profiles_dirty = True
        self._flush_user_profiles()

    def _flush_user_profiles(self) -> None:
        """实际写入用户画像到磁盘（由 write_daily_snapshot 或外部定时调用）。"""
        if not getattr(self, "_user_profiles_dirty", False):
            return
        try:
            import tempfile
            tmp_path = self.user_profiles_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(self._user_profiles, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.user_profiles_path)
            self._user_profiles_dirty = False
        except Exception:
            pass

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
        """延迟写入线程状态。"""
        self._thread_state_dirty = True

    def _flush_thread_state(self) -> None:
        """实际写入线程状态到磁盘。"""
        if not getattr(self, "_thread_state_dirty", False):
            return
        try:
            import tempfile
            tmp_path = self.thread_state_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(self._thread_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.thread_state_path)
            self._thread_state_dirty = False
        except Exception:
            pass

    def flush(self) -> None:
        """手动刷盘内存数据（用于退出前保底持久化）。"""
        self._flush_vector_buffer()
        self._flush_user_profiles()
        self._flush_thread_state()

    def close(self) -> None:
        """关闭 memory 引擎（刷盘并释放 SQLite 连接）。"""
        self.flush()
        conn = getattr(self, "_db_conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        self._db_conn = None

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self, "_db_conn") or self._db_conn is None:
            self._db_conn = sqlite3.connect(self.db_path)
            self._db_conn.execute("PRAGMA journal_mode=WAL;")
        return self._db_conn

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT,
                    reason TEXT,
                    before_content TEXT,
                    after_content TEXT,
                    conversation_id TEXT,
                    user_id TEXT,
                    role TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_audit_record_id ON memory_audit_log(record_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_audit_created_at ON memory_audit_log(created_at);"
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
        self._vector_buffer.append((
            conversation_id,
            user_id,
            role,
            content,
            json.dumps(embedding, ensure_ascii=False),
            ts.isoformat(),
        ))
        if len(self._vector_buffer) >= self._vector_buffer_limit:
            self._flush_vector_buffer()

    def _flush_vector_buffer(self) -> None:
        """批量写入 embedding 缓冲区到 SQLite。"""
        if not self._vector_buffer:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO embeddings (conversation_id, user_id, role, content, embedding, created_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    self._vector_buffer,
                )
        except Exception:
            pass
        self._vector_buffer.clear()

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
        keyword_tokens = self._extract_profile_keywords(text)
        for token in keyword_tokens:
            self._daily_keywords[day_key][token] += 1

        if role == "user":
            if clean_user_name:
                self._user_display_names[user_id] = clean_user_name
            emotion = self.detect_emotion(text)
            self._daily_emotions[day_key][emotion] += 1
            self._daily_user_message_count[day_key][user_id] += 1
            for token in keyword_tokens:
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

    @staticmethod
    def _normalize_profile_text(text: str) -> str:
        """清理系统事件包装文本，尽量保留用户真实输入。"""
        content = normalize_text(text)
        if not content:
            return ""
        content = re.sub(r"\bMULTIMODAL_EVENT(?:_AT)?\b", " ", content, flags=re.IGNORECASE)
        content = content.replace("用户发送多模态消息：", " ").replace("用户@了你并发送多模态消息：", " ")
        content = content.replace("user sent multimodal message:", " ").replace(
            "user mentioned bot and sent multimodal message:",
            " ",
        )
        content = re.sub(
            r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]",
            " ",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(r"\b(?:image|video|record|audio|forward)\s*:\s*\S+", " ", content, flags=re.IGNORECASE)
        content = re.sub(r"https?://\S+", " ", content, flags=re.IGNORECASE)
        content = re.sub(r"\s+", " ", content).strip()
        return content

    @classmethod
    def _is_system_noise_keyword(cls, token: str) -> bool:
        word = normalize_text(token).lower()
        if not word:
            return True
        if word in STOP_WORDS or word in SYSTEM_NOISE_KEYWORDS:
            return True
        if len(word) < 2:
            return True
        if word.startswith("eh") and len(word) > 20:
            return True
        if re.fullmatch(r"[a-f0-9]{16,}", word):
            return True
        if re.fullmatch(r"[a-z0-9_-]{24,}", word):
            return True
        return False

    @classmethod
    def _extract_profile_keywords(cls, text: str) -> list[str]:
        source = cls._normalize_profile_text(text)
        if not source:
            return []
        output: list[str] = []
        for token in tokenize(source):
            if cls._is_system_noise_keyword(token):
                continue
            output.append(token)
        return output

    # ── 语言风格检测 ──

    _EMOJI_PATTERN = re.compile(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
        r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0001F1E0-\U0001F1FF]"
    )
    _ASCII_EMOTICON_PATTERN = re.compile(
        r"(?:[:;=8xX][\-^']?[)D(PpOo/\\|]|[\^><][_\-xX]?[\^><])"
    )
    _REPEAT_PUNCT_PATTERN = re.compile(r"([!?！？~～])\1+")
    _LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z]{2,}")
    _FORMAL_PUNCT_PATTERN = re.compile(r"[。；：]")

    @classmethod
    def _detect_language_style(cls, text: str) -> str:
        """检测单条消息的语言风格：slang / casual / formal。"""
        content = normalize_text(text)
        if not content:
            return "casual"

        compact = re.sub(r"\s+", "", content)
        char_count = len(compact)

        emoji_hits = len(cls._EMOJI_PATTERN.findall(content))
        emoticon_hits = len(cls._ASCII_EMOTICON_PATTERN.findall(content))
        repeat_punct_hits = len(cls._REPEAT_PUNCT_PATTERN.findall(content))
        formal_punct_hits = len(cls._FORMAL_PUNCT_PATTERN.findall(content))
        exclaim_hits = content.count("!") + content.count("！")
        latin_tokens = len(cls._LATIN_TOKEN_PATTERN.findall(content))
        latin_ratio = latin_tokens / max(1, char_count)

        informal_score = 0
        formal_score = 0

        if emoji_hits >= 1:
            informal_score += 1
        if emoticon_hits >= 1:
            informal_score += 2
        if repeat_punct_hits >= 1:
            informal_score += 2
        if exclaim_hits >= 2:
            informal_score += 1
        if latin_ratio >= 0.12 and char_count <= 64:
            informal_score += 1
        if char_count <= 8:
            informal_score += 1

        if formal_punct_hits >= 1:
            formal_score += 1
        if char_count >= 24:
            formal_score += 1
        if emoji_hits == 0 and emoticon_hits == 0 and repeat_punct_hits == 0:
            formal_score += 1
        if exclaim_hits == 0:
            formal_score += 1

        if informal_score >= 3 and informal_score >= formal_score:
            return "slang"
        if formal_score >= 4 and informal_score <= 1:
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
        profile_text = self._normalize_profile_text(text)
        message_count = int(profile.get("message_count", 0)) + 1
        total_chars = int(profile.get("total_chars", 0)) + len(profile_text)
        question_count = int(profile.get("question_count", 0))
        if "?" in profile_text or "？" in profile_text:
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
        for token in self._extract_profile_keywords(profile_text):
            keywords[token] = int(keywords.get(token, 0)) + 1
        # 裁剪 keywords，只保留 top 50 防止无限膨胀
        if len(keywords) > 80:
            top_items = sorted(keywords.items(), key=lambda x: x[1], reverse=True)[:50]
            keywords = dict(top_items)

        # 语言风格统计
        style_counts = profile.get("style_counts", {})
        if not isinstance(style_counts, dict):
            style_counts = {}
        if profile_text:
            style = self._detect_language_style(profile_text)
            style_counts[style] = int(style_counts.get(style, 0)) + 1

        # 话题分类统计
        topic_counts = profile.get("topic_counts", {})
        if not isinstance(topic_counts, dict):
            topic_counts = {}
        if profile_text:
            topic = self._detect_topic_category(profile_text)
            topic_counts[topic] = int(topic_counts.get(topic, 0)) + 1

        # 回复长度偏好追踪（最近 20 条的平均长度）
        recent_lengths = profile.get("recent_lengths", [])
        if not isinstance(recent_lengths, list):
            recent_lengths = []
        if profile_text:
            recent_lengths.append(len(profile_text))
        recent_lengths = recent_lengths[-20:]

        # 情绪统计
        emotion_counts = profile.get("emotion_counts", {})
        if not isinstance(emotion_counts, dict):
            emotion_counts = {}
        if profile_text:
            emotion = self.detect_emotion(profile_text)
            emotion_counts[emotion] = int(emotion_counts.get(emotion, 0)) + 1

        preferred_name = normalize_text(str(profile.get("preferred_name", "")))
        preferred_name_updated_at = normalize_text(str(profile.get("preferred_name_updated_at", "")))
        detected_preferred_name = self._extract_preferred_name(text) if self.heuristic_rules_enable else ""
        if detected_preferred_name:
            preferred_name = detected_preferred_name
            preferred_name_updated_at = ts.isoformat()
        agent_policies = profile.get("agent_policies", {})
        if not isinstance(agent_policies, dict):
            agent_policies = {}
        detected_high_risk_confirm = self._extract_high_risk_confirm_policy(text) if self.heuristic_rules_enable else None
        if detected_high_risk_confirm is not None:
            agent_policies["high_risk_confirmation_required"] = bool(detected_high_risk_confirm)
            agent_policies["high_risk_confirmation_updated_at"] = ts.isoformat()
        agent_directives = profile.get("agent_directives", [])
        if not isinstance(agent_directives, list):
            agent_directives = []
        detected_directive = self._extract_agent_directive(text) if self.heuristic_rules_enable else ""
        if detected_directive:
            dedup_directives = [normalize_text(str(row)) for row in agent_directives if normalize_text(str(row))]
            if detected_directive in dedup_directives:
                dedup_directives.remove(detected_directive)
            dedup_directives.append(detected_directive)
            agent_directives = dedup_directives[-12:]
        explicit_facts = profile.get("explicit_facts", [])
        if not isinstance(explicit_facts, list):
            explicit_facts = []
        detected_fact = self._extract_explicit_fact(text) if self.heuristic_rules_enable else ""
        if detected_fact:
            dedup_facts: list[dict[str, Any]] = []
            fact_key = normalize_text(detected_fact).lower()
            for row in explicit_facts:
                if isinstance(row, dict):
                    fact_text = normalize_text(str(row.get("fact", "")))
                    updated_at = normalize_text(str(row.get("updated_at", "")))
                    fact_conversation = normalize_text(str(row.get("conversation_id", "")))
                else:
                    fact_text = normalize_text(str(row))
                    updated_at = ""
                    fact_conversation = ""
                if not fact_text:
                    continue
                if fact_text.lower() == fact_key:
                    continue
                dedup_facts.append(
                    {
                        "fact": fact_text,
                        "updated_at": updated_at,
                        "conversation_id": fact_conversation,
                    }
                )
            dedup_facts.append(
                {
                    "fact": detected_fact,
                    "updated_at": ts.isoformat(),
                    "conversation_id": conversation_id,
                }
            )
            explicit_facts = dedup_facts[-30:]

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
            "preferred_name": preferred_name,
            "preferred_name_updated_at": preferred_name_updated_at,
            "agent_policies": agent_policies,
            "agent_directives": agent_directives,
            "explicit_facts": explicit_facts,
        }
        self._user_profiles[user_id] = updated
        if display_name:
            self._user_display_names[user_id] = display_name
        if detected_fact:
            self._save_user_profiles_immediate()
        else:
            self._save_user_profiles()

    def _extract_preferred_name(self, text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        for pattern in self.preferred_name_patterns:
            match = pattern.search(content)
            if not match:
                continue
            candidate = ""
            group_names = getattr(match.re, "groupindex", {})
            if isinstance(group_names, dict) and "name" in group_names:
                try:
                    candidate = match.group("name") or ""
                except (IndexError, KeyError):
                    candidate = ""
            if not candidate and (match.lastindex or 0) >= 1:
                # Fallback: use the first capture group when no named "name" group is available.
                try:
                    candidate = match.group(1) or ""
                except IndexError:
                    candidate = ""
            candidate = normalize_text(candidate) if candidate else ""
            if not candidate:
                continue
            if any(part in candidate for part in self.preferred_name_invalid_parts):
                continue
            if any(part and part in candidate for part in self.preferred_name_blocklist):
                continue
            if any(p.search(candidate) for p in self.preferred_name_block_patterns):
                continue
            if len(candidate) > 24:
                continue
            return candidate
        return ""

    def _extract_high_risk_confirm_policy(self, text: str) -> bool | None:
        content = normalize_text(text)
        if not content:
            return None
        for pattern in self.high_risk_confirm_disable_patterns:
            if pattern.search(content):
                return False
        for pattern in self.high_risk_confirm_enable_patterns:
            if pattern.search(content):
                return True
        return None

    def _extract_agent_directive(self, text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        if len(content) < 6 or len(content) > 160:
            return ""
        if "?" in content or "？" in content:
            return ""
        if not any(cue in content for cue in self.agent_directive_cues):
            return ""
        if self.agent_directive_target_cues and not any(cue in content.lower() for cue in self.agent_directive_target_cues):
            return ""
        # 过滤明显闲聊口水句。
        if content in {"记住了", "知道了", "明白了"}:
            return ""
        return content

    @staticmethod
    def _extract_explicit_fact(text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""
        if "?" in content or "？" in content:
            return ""

        # 典型表达：记住了奥 1+1+1=阴叁儿 / 记住：xxx
        patterns = (
            r"^(?:你?给?我?记住|你记住|记住|记好了|记一下|记下来|记得)(?:了)?(?:吧|哈|啊|呀|哦|奥|噢)?[：:，,\s]*(.{2,160})$",
            r"^(?:从现在开始|以后)\s*(?:记住|按这个记|都按这个)(?:吧|哈|啊|呀|哦|奥|噢)?[：:，,\s]*(.{2,160})$",
        )
        for raw in patterns:
            matched = re.match(raw, content, flags=re.IGNORECASE)
            if not matched:
                continue
            candidate = normalize_text(matched.group(1))
            candidate = re.sub(r"^(?:了|吧|哈|啊|呀|哦|奥|噢)[\s，,。：:]*", "", candidate)
            if not candidate:
                continue
            if len(candidate) > 160:
                candidate = candidate[:160]
            if candidate in {"记住了", "知道了", "明白了"}:
                continue
            return candidate

        # 宽松兜底：包含“记住”且存在等式表达，按事实保存
        if "记住" in content and ("=" in content or "等于" in content):
            idx = content.find("记住")
            candidate = normalize_text(content[idx + len("记住"):])
            candidate = re.sub(r"^(?:了|吧|哈|啊|呀|哦|奥|噢)[\s，,。：:]*", "", candidate)
            if candidate and len(candidate) <= 160:
                return candidate
        return ""

    @staticmethod
    def _normalize_fact_key(text: str) -> str:
        content = normalize_text(text).lower()
        if not content:
            return ""
        content = re.sub(r"[，。！？!?,：:；;、\s\"'“”‘’（）()\[\]【】<>《》]+", "", content)
        return content

    @staticmethod
    def _split_fact_pair(fact: str) -> tuple[str, str] | None:
        content = normalize_text(fact)
        if not content:
            return None
        separators = ("=", "＝", "等于", "是")
        for sep in separators:
            if sep not in content:
                continue
            left, right = content.split(sep, 1)
            lhs = normalize_text(left)
            rhs = normalize_text(right)
            if lhs and rhs:
                return lhs, rhs
        return None

    @classmethod
    def _extract_query_key(cls, text: str) -> str:
        content = normalize_text(text)
        if not content:
            return ""

        query_patterns = (
            r"^\s*(.+?)\s*(?:等于几|等于多少|等于啥|等于什么)\s*$",
            r"^\s*(.+?)\s*(?:是什么|是啥|啥意思|什么意思)\s*$",
            r"^\s*(.+?)\s*(?:\?|？)\s*$",
        )
        for raw in query_patterns:
            matched = re.match(raw, content, flags=re.IGNORECASE)
            if not matched:
                continue
            candidate = normalize_text(matched.group(1))
            key = cls._normalize_fact_key(candidate)
            if key:
                return key
        return cls._normalize_fact_key(content)

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
        preferred_name = normalize_text(str(profile.get("preferred_name", "")))
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

        if preferred_name and preferred_name != display_name:
            style_hints.insert(0, f"偏好称呼“{preferred_name}”")

        explicit_facts = self.get_explicit_facts(user_id, limit=3)
        facts_text = ""
        if explicit_facts:
            clipped = [row[:60] for row in explicit_facts]
            facts_text = f" 用户明确记住：{'；'.join(clipped)}。"

        return (
            f"{display_name}（{user_id}）累计消息 {message_count} 条，"
            f"常聊关键词：{keyword_text}。习惯：{'、'.join(style_hints)}。"
            f"{facts_text}"
        )

    def get_preferred_name(self, user_id: str, fallback_name: str = "") -> str:
        """获取稳定称呼：preferred_name > user_id 短标识 > display_name > fallback。"""
        uid = normalize_text(str(user_id))
        profile = self._user_profiles.get(uid, {})
        if isinstance(profile, dict):
            preferred = normalize_text(str(profile.get("preferred_name", "")))
            if preferred and preferred.lower() not in INVALID_PREFERRED_NAMES:
                return preferred
        if uid and re.fullmatch(r"\d{4,20}", uid):
            return f"用户{uid[-4:]}"
        if isinstance(profile, dict):
            display = normalize_text(str(profile.get("display_name", "")))
            if display:
                return display
        fallback = normalize_text(fallback_name)
        if fallback:
            return fallback
        return "某人"

    def get_display_name(self, user_id: str) -> str:
        """获取用户显示名称（优先 preferred_name，其次 display_name）。"""
        uid = str(user_id)
        profile = self._user_profiles.get(uid, {})
        if isinstance(profile, dict):
            preferred = normalize_text(str(profile.get("preferred_name", "")))
            if preferred:
                return preferred
            display = normalize_text(str(profile.get("display_name", "")))
            if display:
                return display
        return self._user_display_names.get(uid, "")

    def normalize_preferred_name_candidate(self, value: str) -> str:
        """规范化称呼候选值，返回可落库文本；非法则返回空。"""
        candidate = normalize_text(value)
        if not candidate:
            return ""
        candidate = re.sub(r"^[\s\"'“”‘’《》〈〉【】\[\]\(\)（）、,，。:：;；!！?？~～]+", "", candidate)
        candidate = re.sub(r"[\s\"'“”‘’《》〈〉【】\[\]\(\)（）、,，。:：;；!！?？~～]+$", "", candidate)
        candidate = normalize_text(candidate)
        if not candidate:
            return ""
        if len(candidate) > 24:
            return ""
        if any(part in candidate for part in self.preferred_name_invalid_parts):
            return ""
        if any(part and part in candidate for part in self.preferred_name_blocklist):
            return ""
        if any(p.search(candidate) for p in self.preferred_name_block_patterns):
            return ""
        return candidate

    def set_preferred_name(
        self,
        *,
        target_user_id: str,
        preferred_name: str,
        actor: str = "system",
        conversation_id: str = "",
        note: str = "",
        reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """手动设置用户偏好称呼（支持审计）。"""
        uid = normalize_text(target_user_id)
        if not uid:
            return False, "缺少 target_user_id", {}
        name = self.normalize_preferred_name_candidate(preferred_name)
        if not name:
            return False, "称呼不合法或为空", {}

        profile = self._user_profiles.get(uid, {})
        if not isinstance(profile, dict):
            profile = {"user_id": uid}
        before_name = normalize_text(str(profile.get("preferred_name", "")))
        if before_name == name:
            payload = {
                "user_id": uid,
                "display_name": normalize_text(str(profile.get("display_name", ""))) or self._user_display_names.get(uid, uid),
                "preferred_name": before_name,
                "preferred_name_updated_at": normalize_text(str(profile.get("preferred_name_updated_at", ""))),
            }
            return True, "称呼未变化", payload

        now = datetime.now(timezone.utc).isoformat()
        if not normalize_text(str(profile.get("display_name", ""))):
            profile["display_name"] = self._user_display_names.get(uid, "")
        profile["preferred_name"] = name
        profile["preferred_name_updated_at"] = now
        self._user_profiles[uid] = profile
        if normalize_text(str(profile.get("display_name", ""))):
            self._user_display_names[uid] = normalize_text(str(profile.get("display_name", "")))
        self._save_user_profiles_immediate()
        self._write_memory_audit(
            record_id=None,
            action="set_preferred_name",
            actor=normalize_text(actor) or "system",
            note=normalize_text(note) or "更新偏好称呼",
            reason=normalize_text(reason) or "manual_preferred_name_update",
            before_content=before_name,
            after_content=name,
            conversation_id=normalize_text(conversation_id),
            user_id=uid,
            role="profile",
        )

        payload = {
            "user_id": uid,
            "display_name": normalize_text(str(profile.get("display_name", ""))) or self._user_display_names.get(uid, uid),
            "preferred_name": name,
            "preferred_name_updated_at": now,
        }
        return True, "称呼已更新", payload

    def get_agent_policies(self, user_id: str) -> dict[str, Any]:
        profile = self._user_profiles.get(str(user_id), {})
        if not isinstance(profile, dict):
            return {}
        policies = profile.get("agent_policies", {})
        if not isinstance(policies, dict):
            return {}
        return dict(policies)

    def get_agent_directives(self, user_id: str) -> list[str]:
        profile = self._user_profiles.get(str(user_id), {})
        if not isinstance(profile, dict):
            return []
        directives = profile.get("agent_directives", [])
        if not isinstance(directives, list):
            directives = []
        clean_directives = [normalize_text(str(row)) for row in directives if normalize_text(str(row))]
        facts = self.get_explicit_facts(user_id, limit=8)
        for fact in facts:
            clean_directives.append(f"用户明确记忆: {fact}")
        return clean_directives[-20:]

    def get_explicit_facts(self, user_id: str, limit: int = 8) -> list[str]:
        profile = self._user_profiles.get(str(user_id), {})
        if not isinstance(profile, dict):
            return []
        rows = profile.get("explicit_facts", [])
        if not isinstance(rows, list):
            return []
        facts: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                fact = normalize_text(str(row.get("fact", "")))
            else:
                fact = normalize_text(str(row))
            if fact:
                facts.append(fact)
        if limit <= 0:
            return facts
        return facts[-max(1, int(limit)) :]

    def match_explicit_fact_query(self, user_id: str, text: str) -> dict[str, str] | None:
        query_key = self._extract_query_key(text)
        if not query_key:
            return None
        facts = self.get_explicit_facts(user_id, limit=30)
        if not facts:
            return None

        for fact in reversed(facts):
            pair = self._split_fact_pair(fact)
            if not pair:
                continue
            lhs, rhs = pair
            lhs_key = self._normalize_fact_key(lhs)
            if not lhs_key:
                continue
            if query_key == lhs_key or query_key in lhs_key or lhs_key in query_key:
                return {"fact": fact, "lhs": lhs, "rhs": rhs}
        return None

    def get_recent_messages(self, conversation_id: str, limit: int | None = None) -> list[MemoryMessage]:
        records = list(self._history.get(conversation_id, []))
        if limit is None:
            return records
        return records[-limit:]

    def get_recent_texts(self, conversation_id: str, limit: int | None = None) -> list[str]:
        items = self.get_recent_messages(conversation_id, limit=limit)
        result: list[str] = []
        for item in items:
            role = str(getattr(item, "role", "user"))
            name = str(getattr(item, "user_name", "")) or str(getattr(item, "user_id", ""))
            content = str(getattr(item, "content", ""))
            if role == "assistant":
                result.append(f"[bot] {content}")
            else:
                result.append(f"[{name}] {content}")
        return result

    def get_recent_speakers(
        self,
        conversation_id: str,
        limit: int = 10,
    ) -> list[tuple[str, str, str]]:
        """获取近期活跃用户（user_id, stable_name, 最近一句话预览）。"""
        window = max(1, int(limit))
        history = self.get_recent_messages(conversation_id, limit=window)
        speakers: dict[str, tuple[str, str]] = {}
        for msg in reversed(history):
            if normalize_text(str(msg.role)).lower() != "user":
                continue
            uid = normalize_text(str(msg.user_id))
            if not uid or uid in speakers:
                continue
            preview_source = self._normalize_profile_text(str(msg.content)) or normalize_text(str(msg.content))
            preview = f"{preview_source[:30]}..." if len(preview_source) > 30 else preview_source
            speakers[uid] = (
                self.get_preferred_name(uid, fallback_name=normalize_text(str(msg.user_name))),
                preview or "(无文本)",
            )
        return [(uid, name, msg) for uid, (name, msg) in speakers.items()]

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
            for token in self._extract_profile_keywords(content):
                word = normalize_text(token)
                if not word:
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

    def _fetch_embedding_record(self, record_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, user_id, role, content, created_at
                FROM embeddings
                WHERE id = ?;
                """,
                (int(record_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "conversation_id": str(row[1] or ""),
            "user_id": str(row[2] or ""),
            "role": str(row[3] or ""),
            "content": str(row[4] or ""),
            "created_at": str(row[5] or ""),
        }

    def _append_history_entry(self, conversation_id: str, user_id: str, role: str, content: str, timestamp: datetime | None = None) -> None:
        text = normalize_text(content)
        if not text:
            return
        uid = normalize_text(user_id)
        conv = normalize_text(conversation_id)
        if not conv:
            return
        user_name = self.get_display_name(uid) if uid else ""
        self._history[conv].append(
            MemoryMessage(
                role=normalize_text(role) or "user",
                user_id=uid,
                user_name=user_name,
                content=text,
                timestamp=timestamp or datetime.now(timezone.utc),
            )
        )

    def _update_history_entry(
        self,
        *,
        conversation_id: str,
        user_id: str,
        role: str,
        old_content: str,
        new_content: str,
    ) -> None:
        rows = self._history.get(conversation_id)
        if not rows:
            return
        target_uid = normalize_text(user_id)
        target_role = normalize_text(role)
        before = normalize_text(old_content)
        after = normalize_text(new_content)
        if not before or not after:
            return
        for idx in range(len(rows) - 1, -1, -1):
            item = rows[idx]
            if normalize_text(str(item.user_id)) != target_uid:
                continue
            if normalize_text(str(item.role)) != target_role:
                continue
            if normalize_text(str(item.content)) != before:
                continue
            item.content = after
            rows[idx] = item
            break

    def _remove_history_entry(self, *, conversation_id: str, user_id: str, role: str, content: str) -> None:
        rows = self._history.get(conversation_id)
        if not rows:
            return
        target_uid = normalize_text(user_id)
        target_role = normalize_text(role)
        target_content = normalize_text(content)
        if not target_content:
            return
        for idx in range(len(rows) - 1, -1, -1):
            item = rows[idx]
            if normalize_text(str(item.user_id)) != target_uid:
                continue
            if normalize_text(str(item.role)) != target_role:
                continue
            if normalize_text(str(item.content)) != target_content:
                continue
            del rows[idx]
            break

    def _write_memory_audit(
        self,
        *,
        record_id: int | None,
        action: str,
        actor: str,
        note: str = "",
        reason: str = "",
        before_content: str = "",
        after_content: str = "",
        conversation_id: str = "",
        user_id: str = "",
        role: str = "",
    ) -> None:
        if not self.enable_vector_memory:
            return
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_audit_log (
                    record_id, action, actor, note, reason, before_content, after_content,
                    conversation_id, user_id, role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    int(record_id) if record_id is not None else None,
                    normalize_text(action) or "unknown",
                    normalize_text(actor) or "system",
                    normalize_text(note),
                    normalize_text(reason),
                    normalize_text(before_content),
                    normalize_text(after_content),
                    normalize_text(conversation_id),
                    normalize_text(user_id),
                    normalize_text(role),
                    ts,
                ),
            )

    @staticmethod
    def _format_conversation_label(conversation_id: str) -> str:
        conv = normalize_text(conversation_id)
        if not conv:
            return "-"
        if conv.startswith("group:"):
            parts = conv.split(":")
            if len(parts) >= 4 and parts[2] == "user":
                return f"群聊 {parts[1]}（按用户隔离）"
            if len(parts) >= 2:
                return f"群聊 {parts[1]}"
            return "群聊"
        if conv.startswith("private:"):
            return "私聊"
        return conv

    def list_memory_records(
        self,
        *,
        conversation_id: str = "",
        user_id: str = "",
        role: str = "",
        keyword: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.enable_vector_memory:
            return [], 0

        limit = max(1, min(200, int(limit or 50)))
        offset = max(0, int(offset or 0))
        clauses: list[str] = []
        params: list[Any] = []

        conv = normalize_text(conversation_id)
        uid = normalize_text(user_id)
        role_value = normalize_text(role).lower()
        kw = normalize_text(keyword)

        if conv:
            clauses.append("conversation_id = ?")
            params.append(conv)
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)
        if role_value:
            clauses.append("role = ?")
            params.append(role_value)
        if kw:
            clauses.append("content LIKE ?")
            params.append(f"%{kw}%")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM embeddings {where_sql};",
                tuple(params),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            rows = conn.execute(
                f"""
                SELECT id, conversation_id, user_id, role, content, created_at
                FROM embeddings
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?;
                """,
                tuple(params + [limit, offset]),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            row_conv = str(row[1] or "")
            row_uid = str(row[2] or "")
            profile = self._user_profiles.get(row_uid, {})
            display_name = (
                normalize_text(str(profile.get("display_name", "")))
                or self._user_display_names.get(row_uid, row_uid)
            )
            out.append(
                {
                    "id": int(row[0]),
                    "conversation_id": row_conv,
                    "conversation_label": self._format_conversation_label(row_conv),
                    "user_id": row_uid,
                    "display_name": display_name,
                    "role": str(row[3] or ""),
                    "content": str(row[4] or ""),
                    "created_at": str(row[5] or ""),
                }
            )
        return out, total

    def add_memory_record(
        self,
        *,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        actor: str = "system",
        note: str = "",
        reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        if not self.enable_vector_memory:
            return False, "memory_disabled", {}

        conv = normalize_text(conversation_id)
        uid = normalize_text(user_id)
        role_value = normalize_text(role).lower() or "user"
        text = normalize_text(content)
        if not conv:
            return False, "conversation_id 不能为空", {}
        if not uid:
            return False, "user_id 不能为空", {}
        if role_value not in {"user", "assistant", "system"}:
            return False, "role 必须是 user/assistant/system", {}
        if not text:
            return False, "content 不能为空", {}

        created_at = datetime.now(timezone.utc).isoformat()
        emb = json.dumps(self._embed(text), ensure_ascii=False)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO embeddings (conversation_id, user_id, role, content, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (conv, uid, role_value, text, emb, created_at),
            )
            record_id = int(cursor.lastrowid or 0)

        self._append_history_entry(conversation_id=conv, user_id=uid, role=role_value, content=text)
        self._write_memory_audit(
            record_id=record_id,
            action="add",
            actor=actor,
            note=note,
            reason=reason,
            after_content=text,
            conversation_id=conv,
            user_id=uid,
            role=role_value,
        )

        profile = self._user_profiles.get(uid, {})
        display_name = (
            normalize_text(str(profile.get("display_name", "")))
            or self._user_display_names.get(uid, uid)
        )
        payload = {
            "id": record_id,
            "conversation_id": conv,
            "conversation_label": self._format_conversation_label(conv),
            "user_id": uid,
            "display_name": display_name,
            "role": role_value,
            "content": text,
            "created_at": created_at,
        }
        return True, "memory_added", payload

    def update_memory_record(
        self,
        *,
        record_id: int,
        content: str,
        actor: str = "system",
        note: str = "",
        reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        if not self.enable_vector_memory:
            return False, "memory_disabled", {}
        note_text = normalize_text(note)
        if not note_text:
            return False, "修改记忆必须填写备注 note", {}

        before = self._fetch_embedding_record(record_id)
        if not before:
            return False, "memory_not_found", {}
        new_text = normalize_text(content)
        if not new_text:
            return False, "content 不能为空", {}
        if new_text == normalize_text(before.get("content", "")):
            return False, "内容未变化", before

        emb = json.dumps(self._embed(new_text), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE embeddings
                SET content = ?, embedding = ?
                WHERE id = ?;
                """,
                (new_text, emb, int(record_id)),
            )

        self._update_history_entry(
            conversation_id=str(before.get("conversation_id", "")),
            user_id=str(before.get("user_id", "")),
            role=str(before.get("role", "")),
            old_content=str(before.get("content", "")),
            new_content=new_text,
        )
        self._write_memory_audit(
            record_id=int(record_id),
            action="update",
            actor=actor,
            note=note_text,
            reason=reason,
            before_content=str(before.get("content", "")),
            after_content=new_text,
            conversation_id=str(before.get("conversation_id", "")),
            user_id=str(before.get("user_id", "")),
            role=str(before.get("role", "")),
        )

        after = self._fetch_embedding_record(record_id) or {}
        conv = normalize_text(str(after.get("conversation_id", "")))
        uid = normalize_text(str(after.get("user_id", "")))
        profile = self._user_profiles.get(uid, {})
        display_name = (
            normalize_text(str(profile.get("display_name", "")))
            or self._user_display_names.get(uid, uid)
        )
        after["conversation_label"] = self._format_conversation_label(conv)
        after["display_name"] = display_name
        return True, "memory_updated", after

    def delete_memory_record(
        self,
        *,
        record_id: int,
        actor: str = "system",
        note: str = "",
        reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        if not self.enable_vector_memory:
            return False, "memory_disabled", {}
        note_text = normalize_text(note)
        if not note_text:
            return False, "删除记忆必须填写备注 note", {}

        before = self._fetch_embedding_record(record_id)
        if not before:
            return False, "memory_not_found", {}

        with self._connect() as conn:
            conn.execute("DELETE FROM embeddings WHERE id = ?;", (int(record_id),))

        self._remove_history_entry(
            conversation_id=str(before.get("conversation_id", "")),
            user_id=str(before.get("user_id", "")),
            role=str(before.get("role", "")),
            content=str(before.get("content", "")),
        )
        self._write_memory_audit(
            record_id=int(record_id),
            action="delete",
            actor=actor,
            note=note_text,
            reason=reason,
            before_content=str(before.get("content", "")),
            after_content="",
            conversation_id=str(before.get("conversation_id", "")),
            user_id=str(before.get("user_id", "")),
            role=str(before.get("role", "")),
        )
        conv = normalize_text(str(before.get("conversation_id", "")))
        uid = normalize_text(str(before.get("user_id", "")))
        profile = self._user_profiles.get(uid, {})
        before["conversation_label"] = self._format_conversation_label(conv)
        before["display_name"] = (
            normalize_text(str(profile.get("display_name", "")))
            or self._user_display_names.get(uid, uid)
        )
        return True, "memory_deleted", before

    def list_memory_audit_logs(
        self,
        *,
        record_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.enable_vector_memory:
            return [], 0
        limit = max(1, min(500, int(limit or 100)))
        offset = max(0, int(offset or 0))
        params: list[Any] = []
        where_sql = ""
        if record_id is not None:
            where_sql = "WHERE record_id = ?"
            params.append(int(record_id))

        with self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM memory_audit_log {where_sql};",
                tuple(params),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0
            rows = conn.execute(
                f"""
                SELECT id, record_id, action, actor, note, reason, before_content, after_content,
                       conversation_id, user_id, role, created_at
                FROM memory_audit_log
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?;
                """,
                tuple(params + [limit, offset]),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": int(row[0]),
                    "record_id": int(row[1]) if row[1] is not None else None,
                    "action": str(row[2] or ""),
                    "actor": str(row[3] or ""),
                    "note": str(row[4] or ""),
                    "reason": str(row[5] or ""),
                    "before_content": str(row[6] or ""),
                    "after_content": str(row[7] or ""),
                    "conversation_id": str(row[8] or ""),
                    "user_id": str(row[9] or ""),
                    "role": str(row[10] or ""),
                    "created_at": str(row[11] or ""),
                }
            )
        return out, total

    def compact_memory_records(
        self,
        *,
        conversation_id: str = "",
        user_id: str = "",
        role: str = "",
        actor: str = "system",
        note: str = "",
        reason: str = "",
        dry_run: bool = True,
        keep_latest: int = 1,
    ) -> tuple[bool, str, dict[str, Any]]:
        """按“相同会话+用户+角色+归一化内容”去重记忆记录。"""
        if not self.enable_vector_memory:
            return False, "memory_disabled", {}

        keep_latest = max(1, int(keep_latest or 1))
        conv = normalize_text(conversation_id)
        uid = normalize_text(user_id)
        role_value = normalize_text(role).lower()
        note_text = normalize_text(note)
        reason_text = normalize_text(reason)
        if not dry_run and not note_text:
            return False, "执行整理必须填写备注 note", {}

        clauses: list[str] = []
        params: list[Any] = []
        if conv:
            clauses.append("conversation_id = ?")
            params.append(conv)
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)
        if role_value:
            clauses.append("role = ?")
            params.append(role_value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, conversation_id, user_id, role, content
                FROM embeddings
                {where_sql}
                ORDER BY id DESC;
                """,
                tuple(params),
            ).fetchall()

        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            item = {
                "id": int(row[0]),
                "conversation_id": str(row[1] or ""),
                "user_id": str(row[2] or ""),
                "role": str(row[3] or ""),
                "content": str(row[4] or ""),
            }
            key = (
                normalize_text(item["conversation_id"]),
                normalize_text(item["user_id"]),
                normalize_text(item["role"]).lower(),
                normalize_text(item["content"]),
            )
            groups[key].append(item)

        delete_items: list[dict[str, Any]] = []
        for dup_rows in groups.values():
            if len(dup_rows) <= keep_latest:
                continue
            # rows 已按 id DESC，保留最新 keep_latest 条。
            delete_items.extend(dup_rows[keep_latest:])
        delete_items.sort(key=lambda item: int(item["id"]))

        payload = {
            "dry_run": bool(dry_run),
            "scanned": len(rows),
            "duplicates": len(delete_items),
            "keep_latest": keep_latest,
            "filters": {
                "conversation_id": conv,
                "user_id": uid,
                "role": role_value,
            },
            "deleted_ids": [int(item["id"]) for item in delete_items],
        }

        if dry_run or not delete_items:
            return True, "memory_compact_preview", payload

        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM embeddings WHERE id = ?;",
                [(int(item["id"]),) for item in delete_items],
            )

        actor_text = normalize_text(actor) or "system"
        for item in delete_items:
            self._remove_history_entry(
                conversation_id=str(item.get("conversation_id", "")),
                user_id=str(item.get("user_id", "")),
                role=str(item.get("role", "")),
                content=str(item.get("content", "")),
            )
            self._write_memory_audit(
                record_id=int(item["id"]),
                action="compact_delete",
                actor=actor_text,
                note=note_text,
                reason=reason_text or "memory_compact",
                before_content=str(item.get("content", "")),
                after_content="",
                conversation_id=str(item.get("conversation_id", "")),
                user_id=str(item.get("user_id", "")),
                role=str(item.get("role", "")),
            )
        return True, "memory_compacted", payload

    @staticmethod
    def _topic_from_text(text: str) -> str:
        tokens = MemoryEngine._extract_profile_keywords(text or "")
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

        # 先刷缓冲区，确保最新数据可查
        self._flush_vector_buffer()

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
            # 群聊隔离：当指定 user_id 时，user/assistant 均要求同一用户域，避免跨用户记忆串台。
            if user_filter and normalize_text(str(row_user_id)) != user_filter:
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

    @classmethod
    def _is_effective_daily_summary_item(cls, text: str) -> bool:
        content = cls._normalize_profile_text(text)
        if not content:
            return False
        if len(content) < 2:
            return False
        if cls._is_system_noise_keyword(content):
            return False
        if re.fullmatch(r"[\W_]+", content):
            return False
        if re.fullmatch(r"\d+", content):
            return False
        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))
        latin = len(re.findall(r"[A-Za-z]", content))
        if (cjk + latin) < 2:
            return False
        return True

    @classmethod
    def _select_daily_important_messages(
        cls,
        user_messages: list[str],
        limit: int = 8,
    ) -> list[str]:
        filtered: list[str] = []
        for raw in user_messages:
            content = cls._normalize_profile_text(raw)
            if not cls._is_effective_daily_summary_item(content):
                continue
            if filtered and filtered[-1] == content:
                continue
            filtered.append(content)
        return filtered[-max(1, limit):]

    def write_daily_snapshot(self, day_key: str | None = None) -> None:
        if not self.enable_daily_log:
            return

        key = day_key or datetime.now().date().isoformat()
        records = self._daily_records.get(key, [])
        keyword_counter = self._daily_keywords.get(key, Counter())
        emotion_counter = self._daily_emotions.get(key, Counter())

        user_messages = [text for role, _, _, text in records if role == "user"]
        important = self._select_daily_important_messages(user_messages, limit=8)
        raw_top_keywords = keyword_counter.most_common(20)
        top_keywords: list[tuple[str, int]] = []
        for word, count in raw_top_keywords:
            token = normalize_text(str(word))
            if not token:
                continue
            if self._is_system_noise_keyword(token):
                continue
            if re.fullmatch(r"\d+", token):
                continue
            top_keywords.append((token, int(count)))
            if len(top_keywords) >= 10:
                break
        if top_keywords and max((c for _, c in top_keywords), default=0) <= 1 and len(user_messages) < 5:
            top_keywords = []

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
            lines.append("- 暂无足够有效摘要。")

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
                    f"- {display_name}：今日 {count} 条；常聊 {kw_text}；习惯 {style_hint}"
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
                lines.append(f"- {display_name}：{intents}")
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

        # 趁快照时机刷盘延迟写入的数据
        self._flush_vector_buffer()
        self._flush_user_profiles()
        self._flush_thread_state()

        # 清理过期的 daily 数据（只保留最近 3 天）
        self._cleanup_daily_data(key)

    def _cleanup_daily_data(self, current_day_key: str) -> None:
        """清理超过 3 天的 daily 内存数据，防止 OOM。"""
        from datetime import date, timedelta
        try:
            current_date = date.fromisoformat(current_day_key)
        except (ValueError, TypeError):
            return
        cutoff = (current_date - timedelta(days=3)).isoformat()
        for store in (
            self._daily_records,
            self._daily_keywords,
            self._daily_emotions,
            self._daily_user_message_count,
            self._daily_user_keywords,
            self._daily_topic_traces,
            self._daily_user_intents,
        ):
            expired = [k for k in store if k < cutoff]
            for k in expired:
                store.pop(k, None)

        # 清理 SQLite 中超过 7 天的 embedding 数据
        if self.enable_vector_memory:
            embedding_cutoff = (current_date - timedelta(days=7)).isoformat()
            try:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM embeddings WHERE created_at < ?;",
                        (embedding_cutoff,),
                    )
            except Exception:
                pass

