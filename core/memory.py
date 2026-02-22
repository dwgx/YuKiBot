from __future__ import annotations

import hashlib
import json
import math
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

        self.memory_dir = memory_dir
        self.daily_dir = memory_dir / "daily"
        self.vector_dir = memory_dir / "vector"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)

        self._history: dict[str, deque[MemoryMessage]] = defaultdict(
            lambda: deque(maxlen=self.max_context_messages)
        )
        self._daily_records: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._daily_keywords: dict[str, Counter[str]] = defaultdict(Counter)
        self._daily_emotions: dict[str, Counter[str]] = defaultdict(Counter)
        self._message_counter = 0

        self.db_path = self.vector_dir / "memory.db"
        if self.enable_vector_memory:
            self._init_vector_db()

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
    ) -> None:
        text = normalize_text(content)
        if not text:
            return

        ts = timestamp or datetime.now(timezone.utc)
        self._history[conversation_id].append(
            MemoryMessage(role=role, user_id=user_id, content=text, timestamp=ts)
        )
        self._store_vector(conversation_id, user_id, role, text, ts)

        day_key = ts.astimezone().date().isoformat()
        self._daily_records[day_key].append((role, text))
        for token in tokenize(text):
            if token in STOP_WORDS:
                continue
            self._daily_keywords[day_key][token] += 1
        if role == "user":
            emotion = self.detect_emotion(text)
            self._daily_emotions[day_key][emotion] += 1

        self._message_counter += 1
        if self.enable_daily_log and self._message_counter % self.summary_every_n_messages == 0:
            self.write_daily_snapshot(day_key)

    def get_recent_messages(self, conversation_id: str, limit: int | None = None) -> list[MemoryMessage]:
        records = list(self._history.get(conversation_id, []))
        if limit is None:
            return records
        return records[-limit:]

    def get_recent_texts(self, conversation_id: str, limit: int | None = None) -> list[str]:
        return [item.content for item in self.get_recent_messages(conversation_id, limit=limit)]

    def search_related(self, conversation_id: str, query: str, top_k: int | None = None) -> list[str]:
        if not self.enable_vector_memory or not query.strip():
            return []

        query_vec = self._embed(query)
        k = top_k or self.retrieve_top_k
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT content, embedding
                FROM embeddings
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 300;
                """,
                (conversation_id,),
            ).fetchall()

        scored: list[tuple[float, str]] = []
        for content, emb_json in rows:
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
        negative = ("难受", "伤心", "烦", "崩溃", "痛苦", "累", "失望", "sad")
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

        user_messages = [text for role, text in records if role == "user"]
        important_candidates = [msg for msg in user_messages if len(msg) >= 10]
        important = important_candidates[-8:] if important_candidates else user_messages[-5:]
        top_keywords = keyword_counter.most_common(10)
        dominant_emotion = "中性"
        if emotion_counter:
            dominant_emotion = emotion_counter.most_common(1)[0][0]

        if top_keywords:
            keyword_text = "、".join(word for word, _ in top_keywords[:3])
            summary = f"今天围绕 {keyword_text} 的对话较多，整体情绪以“{dominant_emotion}”为主。"
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
        lines.append("## 出现频率最高关键词")
        if top_keywords:
            lines.extend(f"- {word}: {count}" for word, count in top_keywords)
        else:
            lines.append("- 暂无关键词。")

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
        lines.append("- 下一步建议：保持上下文连续性，在关键话题中主动提供结构化帮助。")

        output = "\n".join(lines).rstrip() + "\n"
        file_path = self.daily_dir / f"{key}.md"
        file_path.write_text(output, encoding="utf-8")

