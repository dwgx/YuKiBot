"""Persist recalled chat messages so WebUI can keep them visible after deletion."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from utils.text import normalize_text

_LOCK = threading.Lock()
_ROOT_DIR = Path(__file__).resolve().parents[1]
_DB_PATH = _ROOT_DIR / "storage" / "chat_recall.db"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recalled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            bot_id TEXT DEFAULT '',
            message_id TEXT NOT NULL,
            seq TEXT DEFAULT '',
            timestamp INTEGER DEFAULT 0,
            sender_id TEXT DEFAULT '',
            sender_name TEXT DEFAULT '',
            sender_role TEXT DEFAULT '',
            is_self INTEGER DEFAULT 0,
            text TEXT DEFAULT '',
            segments_json TEXT DEFAULT '[]',
            operator_id TEXT DEFAULT '',
            operator_name TEXT DEFAULT '',
            source TEXT DEFAULT '',
            note TEXT DEFAULT '',
            recalled_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_recalled_messages_conv_mid
        ON recalled_messages (conversation_id, message_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recalled_messages_conv_ts
        ON recalled_messages (conversation_id, timestamp, recalled_at)
        """
    )
    return conn


def _safe_segments(segments: Any) -> list[dict[str, Any]]:
    if not isinstance(segments, list):
        return []
    out: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        raw_data = seg.get("data", {}) or {}
        data = raw_data if isinstance(raw_data, dict) else {}
        if seg_type:
            out.append({"type": seg_type, "data": data})
    return out


def build_conversation_id(chat_type: str, peer_id: Any) -> str:
    resolved_type = normalize_text(str(chat_type)).lower()
    resolved_peer = normalize_text(str(peer_id))
    if not resolved_type or not resolved_peer:
        return ""
    return f"{resolved_type}:{resolved_peer}"


def record_recalled_message(payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = normalize_text(str(payload.get("conversation_id", "")))
    chat_type = normalize_text(str(payload.get("chat_type", ""))).lower()
    peer_id = normalize_text(str(payload.get("peer_id", "")))
    message_id = normalize_text(str(payload.get("message_id", "")))
    if not message_id:
        raise ValueError("message_id is required")
    if not conversation_id and chat_type and peer_id:
        conversation_id = build_conversation_id(chat_type, peer_id)
    if not conversation_id:
        raise ValueError("conversation_id is required")

    row = {
        "conversation_id": conversation_id,
        "chat_type": chat_type or "group",
        "peer_id": peer_id,
        "bot_id": normalize_text(str(payload.get("bot_id", ""))),
        "message_id": message_id,
        "seq": normalize_text(str(payload.get("seq", ""))),
        "timestamp": int(payload.get("timestamp", 0) or 0),
        "sender_id": normalize_text(str(payload.get("sender_id", ""))),
        "sender_name": normalize_text(str(payload.get("sender_name", ""))) or "未知用户",
        "sender_role": normalize_text(str(payload.get("sender_role", ""))).lower(),
        "is_self": 1 if bool(payload.get("is_self")) else 0,
        "text": str(payload.get("text", "") or ""),
        "segments_json": json.dumps(_safe_segments(payload.get("segments", [])), ensure_ascii=False),
        "operator_id": normalize_text(str(payload.get("operator_id", ""))),
        "operator_name": normalize_text(str(payload.get("operator_name", ""))),
        "source": normalize_text(str(payload.get("source", ""))),
        "note": normalize_text(str(payload.get("note", ""))),
        "recalled_at": int(payload.get("recalled_at", 0) or time.time()),
    }

    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO recalled_messages (
                    conversation_id, chat_type, peer_id, bot_id, message_id, seq, timestamp,
                    sender_id, sender_name, sender_role, is_self, text, segments_json,
                    operator_id, operator_name, source, note, recalled_at
                )
                VALUES (
                    :conversation_id, :chat_type, :peer_id, :bot_id, :message_id, :seq, :timestamp,
                    :sender_id, :sender_name, :sender_role, :is_self, :text, :segments_json,
                    :operator_id, :operator_name, :source, :note, :recalled_at
                )
                ON CONFLICT(conversation_id, message_id) DO UPDATE SET
                    seq=excluded.seq,
                    timestamp=excluded.timestamp,
                    sender_id=excluded.sender_id,
                    sender_name=excluded.sender_name,
                    sender_role=excluded.sender_role,
                    is_self=excluded.is_self,
                    text=excluded.text,
                    segments_json=excluded.segments_json,
                    operator_id=excluded.operator_id,
                    operator_name=excluded.operator_name,
                    source=excluded.source,
                    note=excluded.note,
                    recalled_at=excluded.recalled_at
                """,
                row,
            )
            conn.commit()
        finally:
            conn.close()
    return row


def list_recalled_messages(conversation_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    conv = normalize_text(conversation_id)
    if not conv:
        return []
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM recalled_messages
                WHERE conversation_id = ?
                ORDER BY timestamp ASC, recalled_at ASC, id ASC
                LIMIT ?
                """,
                (conv, max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            segments = json.loads(str(row["segments_json"] or "[]"))
        except Exception:
            segments = []
        items.append(
            {
                "conversation_id": str(row["conversation_id"] or ""),
                "chat_type": str(row["chat_type"] or ""),
                "peer_id": str(row["peer_id"] or ""),
                "bot_id": str(row["bot_id"] or ""),
                "message_id": str(row["message_id"] or ""),
                "seq": str(row["seq"] or ""),
                "timestamp": int(row["timestamp"] or 0),
                "sender_id": str(row["sender_id"] or ""),
                "sender_name": str(row["sender_name"] or "未知用户"),
                "sender_role": str(row["sender_role"] or ""),
                "is_self": bool(row["is_self"]),
                "text": str(row["text"] or ""),
                "segments": segments if isinstance(segments, list) else [],
                "operator_id": str(row["operator_id"] or ""),
                "operator_name": str(row["operator_name"] or ""),
                "source": str(row["source"] or ""),
                "note": str(row["note"] or ""),
                "recalled_at": int(row["recalled_at"] or 0),
                "is_recalled": True,
            }
        )
    return items
