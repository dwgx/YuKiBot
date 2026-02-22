from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.filter import contains_any_keyword, matched_keywords
from utils.randomizer import hit
from utils.text import extract_sentence_starts, normalize_text


@dataclass(slots=True)
class TriggerInput:
    conversation_id: str
    text: str
    mentioned: bool
    is_private: bool
    timestamp: datetime


@dataclass(slots=True)
class TriggerResult:
    should_handle: bool
    reason: str
    sensitive_context: str = ""
    sensitive_keywords: tuple[str, ...] = ()


class TriggerEngine:
    def __init__(
        self,
        trigger_config: dict[str, Any],
        triggers_file_config: dict[str, Any],
        sensitive_config: dict[str, Any],
        bot_config: dict[str, Any],
    ):
        self.require_at = bool(trigger_config.get("require_at", True))
        self.allow_name_trigger = bool(trigger_config.get("allow_name_trigger", True))
        self.allow_keyword_trigger = bool(trigger_config.get("allow_keyword_trigger", True))
        self.auto_reply_probability = float(trigger_config.get("auto_reply_probability", 0.0))
        self.sensitive_window = int(trigger_config.get("sensitive_analysis_window", 6))
        self.session_timeout = timedelta(
            minutes=float(trigger_config.get("active_session_timeout_minutes", 8))
        )

        self.bot_name = str(bot_config.get("name", "yukiko"))
        nicknames = bot_config.get("nicknames", [])
        self.nicknames = [self.bot_name] + [str(item) for item in nicknames]
        self.keywords = [str(item) for item in triggers_file_config.get("keywords", [])]
        self.command_prefixes = [str(item) for item in triggers_file_config.get("command_prefixes", ["/"])]
        self.sensitive_keywords = [str(item) for item in sensitive_config.get("keywords", [])]
        self._active_sessions: dict[str, datetime] = {}

    def _cleanup_sessions(self, now: datetime) -> None:
        expired = [
            conversation_id
            for conversation_id, last_ts in self._active_sessions.items()
            if now - last_ts > self.session_timeout
        ]
        for conversation_id in expired:
            self._active_sessions.pop(conversation_id, None)

    def is_active_session(self, conversation_id: str, now: datetime) -> bool:
        self._cleanup_sessions(now)
        ts = self._active_sessions.get(conversation_id)
        if not ts:
            return False
        if now - ts > self.session_timeout:
            self._active_sessions.pop(conversation_id, None)
            return False
        return True

    def activate_session(self, conversation_id: str, now: datetime | None = None) -> None:
        ts = now or datetime.now(timezone.utc)
        self._active_sessions[conversation_id] = ts

    def close_session(self, conversation_id: str) -> None:
        self._active_sessions.pop(conversation_id, None)

    def evaluate(self, payload: TriggerInput, recent_messages: list[str]) -> TriggerResult:
        text = normalize_text(payload.text)
        now = payload.timestamp
        self._cleanup_sessions(now)

        if self.is_active_session(payload.conversation_id, now):
            self.activate_session(payload.conversation_id, now)
            return TriggerResult(should_handle=True, reason="active_session")

        if any(text.startswith(prefix) for prefix in self.command_prefixes):
            return TriggerResult(should_handle=True, reason="command")

        if payload.mentioned:
            return TriggerResult(should_handle=True, reason="at_trigger")

        if self.allow_name_trigger and contains_any_keyword(text, self.nicknames):
            return TriggerResult(should_handle=True, reason="name_trigger")

        if self.allow_keyword_trigger and contains_any_keyword(text, self.keywords):
            return TriggerResult(should_handle=True, reason="keyword_trigger")

        sensitive_hits = matched_keywords(text, self.sensitive_keywords)
        if sensitive_hits:
            context_source = (recent_messages + [text])[-self.sensitive_window :]
            heads: list[str] = []
            for msg in context_source:
                heads.extend(extract_sentence_starts(msg, max_sentences=1, max_chars=16))
            sensitive_context = " / ".join(heads[: self.sensitive_window])
            return TriggerResult(
                should_handle=True,
                reason="sensitive_trigger",
                sensitive_context=sensitive_context,
                sensitive_keywords=tuple(sensitive_hits),
            )

        if hit(self.auto_reply_probability):
            return TriggerResult(should_handle=True, reason="random_trigger")

        return TriggerResult(should_handle=False, reason="ignore")

