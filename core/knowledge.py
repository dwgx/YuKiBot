"""独立知识库 — 与记忆库分离的持久化知识存储。

记忆库 (memory.py): 对话历史、用户画像、短期上下文
知识库 (knowledge.py): 事实知识、热梗、百科、学习到的概念

特性:
- SQLite + FTS5 全文检索
- 分类与 TTL
- category+title 维度去重/upsert
- 冲突更新版本表 knowledge_versions
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.knowledge")


# ── 分类常量 ──
CATEGORY_FACT = "fact"        # 事实知识 (永久)
CATEGORY_MEME = "meme"        # 热梗/流行语 (30天TTL)
CATEGORY_WIKI = "wiki"        # 百科知识 (永久)
CATEGORY_TREND = "trend"      # 热搜快照 (1天TTL)
CATEGORY_LEARNED = "learned"  # 从对话中学到的 (永久)

_DEFAULT_TTL = {
    CATEGORY_FACT: 0,       # 永久
    CATEGORY_MEME: 2592000, # 30天
    CATEGORY_WIKI: 0,       # 永久
    CATEGORY_TREND: 86400,  # 1天
    CATEGORY_LEARNED: 0,    # 永久
}


@dataclass(slots=True)
class KnowledgeEntry:
    """知识条目。"""
    id: int = 0
    category: str = ""
    title: str = ""
    content: str = ""
    source: str = ""       # zhihu / baike / wikipedia / weibo / chat / manual
    tags: list[str] = field(default_factory=list)
    created_at: float = 0
    expires_at: float = 0  # 0 = 永不过期
    extra: dict[str, Any] = field(default_factory=dict)


class KnowledgeBase:
    """独立知识库，SQLite + FTS5 全文搜索。"""

    def __init__(self, db_path: str = "storage/knowledge/knowledge.db"):
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            self._db_path = (Path(__file__).resolve().parents[1] / self._db_path).resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                extra TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
            CREATE INDEX IF NOT EXISTS idx_knowledge_title ON knowledge(title);
            CREATE INDEX IF NOT EXISTS idx_knowledge_expires ON knowledge(expires_at);
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                extra TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                replaced_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_versions_kid ON knowledge_versions(knowledge_id);
        """)
        # FTS5 全文搜索索引
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    title, content, tags,
                    content='knowledge',
                    content_rowid='id',
                    tokenize='unicode61'
                )
            """)
        except sqlite3.OperationalError:
            _log.warning("FTS5 not available, falling back to LIKE search")
        conn.commit()

    def add(
        self,
        category: str,
        title: str,
        content: str = "",
        source: str = "",
        tags: list[str] | None = None,
        ttl: int | None = None,
        extra: dict[str, Any] | None = None,
        upsert: bool = True,
    ) -> int:
        """添加知识条目。upsert=True 时同 category+title 会更新。"""
        conn = self._get_conn()
        now = time.time()
        if ttl is None:
            ttl = _DEFAULT_TTL.get(category, 0)
        expires_at = (now + ttl) if ttl > 0 else 0
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        extra_json = json.dumps(extra or {}, ensure_ascii=False)

        if upsert:
            # 检查是否已存在
            row = conn.execute(
                "SELECT id, content, source, tags, extra, created_at FROM knowledge WHERE category=? AND title=?",
                (category, title),
            ).fetchone()
            if row:
                previous_content = normalize_text(str(row[1] or ""))
                next_content = normalize_text(str(content or ""))
                if previous_content != next_content:
                    self._record_version_snapshot(
                        conn=conn,
                        knowledge_id=int(row[0]),
                        category=category,
                        title=title,
                        content=str(row[1] or ""),
                        source=str(row[2] or ""),
                        tags=str(row[3] or "[]"),
                        extra=str(row[4] or "{}"),
                        created_at=float(row[5] or now),
                        replaced_at=now,
                    )
                conn.execute(
                    "UPDATE knowledge SET content=?, source=?, tags=?, expires_at=?, extra=? WHERE id=?",
                    (
                        content,
                        source,
                        tags_json,
                        expires_at,
                        json.dumps(
                            self._merge_extra(self._safe_json_dict(str(row[4] or "{}")), self._safe_json_dict(extra_json)),
                            ensure_ascii=False,
                        ),
                        row[0],
                    ),
                )
                self._update_fts(conn, row[0], title, content, tags_json)
                conn.commit()
                return row[0]

        cursor = conn.execute(
            "INSERT INTO knowledge (category, title, content, source, tags, created_at, expires_at, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category, title, content, source, tags_json, now, expires_at, extra_json),
        )
        row_id = cursor.lastrowid or 0
        self._insert_fts(conn, row_id, title, content, tags_json)
        conn.commit()
        return row_id

    def upsert_conflict_checked(
        self,
        category: str,
        title: str,
        content: str,
        source: str = "",
        tags: list[str] | None = None,
        extra: dict[str, Any] | None = None,
        confidence: float = 0.7,
        update_mode: str = "auto",
        mark_correction: bool = False,
    ) -> dict[str, Any]:
        """Upsert with conflict detection + versioning metadata."""
        conn = self._get_conn()
        now = time.time()
        title_n = normalize_text(title)
        content_n = normalize_text(content)
        if not title_n or not content_n:
            return {"ok": False, "action": "skipped", "reason": "empty"}

        row = conn.execute(
            "SELECT id, content, source, tags, extra, created_at FROM knowledge WHERE category=? AND title=?",
            (category, title_n),
        ).fetchone()

        payload_extra = dict(extra or {})
        payload_extra["confidence"] = max(0.0, min(1.0, float(confidence)))
        payload_extra["update_mode"] = normalize_text(update_mode) or "auto"
        payload_extra["is_correction"] = bool(mark_correction)
        payload_extra["updated_at"] = now

        tags_json = json.dumps(tags or [], ensure_ascii=False)
        extra_json = json.dumps(payload_extra, ensure_ascii=False)

        if row is None:
            new_id = self.add(
                category=category,
                title=title_n,
                content=content_n,
                source=source,
                tags=tags or [],
                extra=payload_extra,
                upsert=False,
            )
            return {"ok": True, "action": "inserted", "id": new_id, "updated": False}

        kid = int(row[0])
        old_content = normalize_text(str(row[1] or ""))
        old_extra_raw = str(row[4] or "{}")
        try:
            old_extra = json.loads(old_extra_raw)
        except Exception:
            _log.warning("knowledge_parse_extra_error | kid=%s", row[0], exc_info=True)
            old_extra = {}

        merged_extra = self._merge_extra(old_extra, payload_extra)
        action = "noop"
        if old_content != content_n:
            self._record_version_snapshot(
                conn=conn,
                knowledge_id=kid,
                category=category,
                title=title_n,
                content=str(row[1] or ""),
                source=str(row[2] or ""),
                tags=str(row[3] or "[]"),
                extra=old_extra_raw,
                created_at=float(row[5] or now),
                replaced_at=now,
            )
            conn.execute(
                "UPDATE knowledge SET content=?, source=?, tags=?, extra=?, expires_at=? WHERE id=?",
                (
                    content_n,
                    source or str(row[2] or ""),
                    tags_json if tags is not None else str(row[3] or "[]"),
                    json.dumps(merged_extra, ensure_ascii=False),
                    0,
                    kid,
                ),
            )
            self._update_fts(conn, kid, title_n, content_n, tags_json if tags is not None else str(row[3] or "[]"))
            action = "updated"
        else:
            # 内容未变化也更新元信息（置信度/来源等）。
            conn.execute(
                "UPDATE knowledge SET source=?, tags=?, extra=? WHERE id=?",
                (
                    source or str(row[2] or ""),
                    tags_json if tags is not None else str(row[3] or "[]"),
                    json.dumps(merged_extra, ensure_ascii=False),
                    kid,
                ),
            )
            action = "noop"
        conn.commit()
        return {"ok": True, "action": action, "id": kid, "updated": action == "updated"}

    def search(self, query: str, category: str = "", limit: int = 10) -> list[KnowledgeEntry]:
        """搜索知识库 (FTS5 优先，LIKE 兜底)。"""
        conn = self._get_conn()
        now = time.time()

        # 尝试 FTS5
        try:
            if category:
                sql = (
                    "SELECT k.* FROM knowledge k "
                    "JOIN knowledge_fts f ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? AND k.category=? "
                    "AND (k.expires_at=0 OR k.expires_at>?) "
                    "ORDER BY rank LIMIT ?"
                )
                rows = conn.execute(sql, (query, category, now, limit)).fetchall()
            else:
                sql = (
                    "SELECT k.* FROM knowledge k "
                    "JOIN knowledge_fts f ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? "
                    "AND (k.expires_at=0 OR k.expires_at>?) "
                    "ORDER BY rank LIMIT ?"
                )
                rows = conn.execute(sql, (query, now, limit)).fetchall()
            return self._rerank_entries([self._row_to_entry(r) for r in rows], limit=limit)
        except sqlite3.OperationalError:
            pass

        # LIKE 兜底
        like_q = f"%{query}%"
        if category:
            sql = (
                "SELECT * FROM knowledge WHERE category=? "
                "AND (title LIKE ? OR content LIKE ? OR tags LIKE ?) "
                "AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (category, like_q, like_q, like_q, now, limit)).fetchall()
        else:
            sql = (
                "SELECT * FROM knowledge "
                "WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?) "
                "AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (like_q, like_q, like_q, now, limit)).fetchall()
        return self._rerank_entries([self._row_to_entry(r) for r in rows], limit=limit)

    def get_by_category(self, category: str, limit: int = 20) -> list[KnowledgeEntry]:
        """按分类获取最新条目。"""
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM knowledge WHERE category=? AND (expires_at=0 OR expires_at>?) "
            "ORDER BY created_at DESC LIMIT ?",
            (category, now, limit),
        ).fetchall()
        return self._rerank_entries([self._row_to_entry(r) for r in rows], limit=limit)

    def count(self, category: str = "") -> int:
        """统计条目数。"""
        conn = self._get_conn()
        if category:
            row = conn.execute("SELECT COUNT(*) FROM knowledge WHERE category=?", (category,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()
        return row[0] if row else 0

    def cleanup_expired(self) -> int:
        """清理过期条目。"""
        conn = self._get_conn()
        now = time.time()
        cursor = conn.execute(
            "DELETE FROM knowledge WHERE expires_at>0 AND expires_at<?", (now,)
        )
        deleted = cursor.rowcount
        if deleted > 0:
            conn.commit()
            _log.info("knowledge_cleanup | deleted=%d", deleted)
        return deleted

    def stats(self) -> dict[str, int]:
        """各分类统计。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM knowledge GROUP BY category"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def batch_add_trends(self, trends: list[dict[str, Any]], source: str = "") -> int:
        """批量添加热搜条目。"""
        added = 0
        for item in trends:
            title = normalize_text(str(item.get("title", "")))
            if not title:
                continue
            self.add(
                category=CATEGORY_TREND,
                title=title,
                content=normalize_text(str(item.get("snippet", ""))),
                source=source or normalize_text(str(item.get("source", "trend"))),
                tags=[source] if source else [],
                extra={"heat": str(item.get("heat", "")), "url": str(item.get("url", ""))},
            )
            added += 1
        return added

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 内部方法 ──

    @staticmethod
    def _row_to_entry(row: tuple) -> KnowledgeEntry:
        try:
            tags_val = json.loads(row[5]) if row[5] else []
        except Exception:
            _log.warning("knowledge_parse_tags_error | row_id=%s", row[0], exc_info=True)
            tags_val = []
        try:
            extra_val = json.loads(row[8]) if row[8] else {}
        except Exception:
            _log.warning("knowledge_parse_extra_json_error | row_id=%s", row[0], exc_info=True)
            extra_val = {}
        return KnowledgeEntry(
            id=row[0],
            category=row[1],
            title=row[2],
            content=row[3],
            source=row[4],
            tags=tags_val if isinstance(tags_val, list) else [],
            created_at=row[6],
            expires_at=row[7],
            extra=extra_val if isinstance(extra_val, dict) else {},
        )

    @staticmethod
    def _insert_fts(conn: sqlite3.Connection, row_id: int, title: str, content: str, tags: str) -> None:
        try:
            conn.execute(
                "INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (row_id, title, content, tags),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _update_fts(conn: sqlite3.Connection, row_id: int, title: str, content: str, tags: str) -> None:
        try:
            conn.execute("DELETE FROM knowledge_fts WHERE rowid=?", (row_id,))
            conn.execute(
                "INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (row_id, title, content, tags),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _merge_extra(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = dict(old or {})
        for k, v in (new or {}).items():
            merged[k] = v
        # 追踪纠错次数，便于检索优先级排序。
        if bool(new.get("is_correction")):
            merged["correction_count"] = int(merged.get("correction_count", 0) or 0) + 1
        return merged

    def _record_version_snapshot(
        self,
        conn: sqlite3.Connection,
        knowledge_id: int,
        category: str,
        title: str,
        content: str,
        source: str,
        tags: str,
        extra: str,
        created_at: float,
        replaced_at: float,
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) FROM knowledge_versions WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        next_ver = int((row[0] if row else 0) or 0) + 1
        conn.execute(
            "INSERT INTO knowledge_versions (knowledge_id, version_no, category, title, content, source, tags, extra, created_at, replaced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                knowledge_id,
                next_ver,
                category,
                title,
                content,
                source,
                tags,
                extra,
                created_at,
                replaced_at,
            ),
        )

    @staticmethod
    def _entry_rank(entry: KnowledgeEntry) -> tuple[int, float, float]:
        extra = entry.extra if isinstance(entry.extra, dict) else {}
        correction_bias = 0
        if normalize_text(str(extra.get("source_type", ""))).lower() == "user_correction":
            correction_bias += 2
        if bool(extra.get("is_correction")):
            correction_bias += 1
        correction_bias += int(extra.get("correction_count", 0) or 0)
        confidence = float(extra.get("confidence", 0.0) or 0.0)
        freshness = float(extra.get("updated_at", entry.created_at) or entry.created_at)
        return correction_bias, confidence, freshness

    def _rerank_entries(self, entries: list[KnowledgeEntry], limit: int) -> list[KnowledgeEntry]:
        if not entries:
            return []
        ranked = sorted(entries, key=self._entry_rank, reverse=True)
        return ranked[: max(1, int(limit))]

    @staticmethod
    def _safe_json_dict(text: str) -> dict[str, Any]:
        try:
            raw = json.loads(text or "{}")
            if isinstance(raw, dict):
                return raw
        except Exception:
            _log.warning("knowledge_safe_json_dict_error", exc_info=True)
        return {}
