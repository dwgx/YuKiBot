from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from utils.text import normalize_text


@dataclass(slots=True)
class TriggerInput:
    conversation_id: str
    user_id: str
    text: str
    mentioned: bool
    is_private: bool
    timestamp: datetime


@dataclass(slots=True)
class TriggerResult:
    should_handle: bool
    reason: str
    active_session: bool = False
    followup_candidate: bool = False
    listen_probe: bool = False
    overload_active: bool = False
    busy_messages: int = 0
    busy_users: int = 0
    scene_hint: str = "chat"
    proactive: bool = False
    ai_gate: bool = True
    priority: int = 0


class TriggerEngine:
    """负责会话状态、节流与轻量触发语义判定。"""

    def __init__(
        self,
        trigger_config: dict[str, Any],
        bot_config: dict[str, Any],
        triggers_file_config: dict[str, Any] | None = None,
        sensitive_config: dict[str, Any] | None = None,
    ):
        _ = (triggers_file_config, sensitive_config)  # 兼容旧调用
        aliases = {normalize_text(str(bot_config.get("name", ""))).lower()}
        for item in bot_config.get("nicknames", []) or []:
            aliases.add(normalize_text(str(item)).lower())
        # 常用默认别名兜底，避免配置缺省时喊不醒。
        aliases.update({"yuki", "yukiko", "雪"})
        aliases.discard("")
        self.bot_aliases = aliases

        self.session_timeout = timedelta(minutes=float(trigger_config.get("active_session_timeout_minutes", 8)))
        self.followup_reply_window = timedelta(
            seconds=max(5, int(trigger_config.get("followup_reply_window_seconds", 20)))
        )
        self.followup_max_turns = max(1, int(trigger_config.get("followup_max_turns", 2)))

        self.busy_window = timedelta(seconds=max(15, int(trigger_config.get("busy_window_seconds", 60))))

        # 默认开启轻度“旁听探测”，配合后续 self_check 高阈值，减少误接话同时保留自然接话能力。
        self.ai_listen_enable = bool(trigger_config.get("ai_listen_enable", True))
        self.ai_listen_interval = timedelta(
            seconds=max(15, int(trigger_config.get("ai_listen_interval_seconds", 45)))
        )
        self.ai_listen_min_messages = max(1, int(trigger_config.get("ai_listen_min_messages", 8)))
        self.ai_listen_min_unique_users = max(1, int(trigger_config.get("ai_listen_min_unique_users", 3)))
        self.ai_listen_keyword_enable = bool(trigger_config.get("ai_listen_keyword_enable", True))
        keywords_raw = trigger_config.get("ai_listen_keywords", [])
        if not isinstance(keywords_raw, list):
            keywords_raw = []
        self.ai_listen_keywords = [
            normalize_text(str(item)).lower()
            for item in keywords_raw
            if normalize_text(str(item))
        ]
        explicit_request_cues_raw = trigger_config.get("explicit_request_cues", [])
        if not isinstance(explicit_request_cues_raw, list):
            explicit_request_cues_raw = []
        self.explicit_request_cues = tuple(
            normalize_text(str(item)).lower()
            for item in explicit_request_cues_raw
            if normalize_text(str(item))
        )
        self.ai_listen_min_keyword_hits = max(1, int(trigger_config.get("ai_listen_min_keyword_hits", 1)))
        self.ai_listen_min_score = max(0.5, float(trigger_config.get("ai_listen_min_score", 1.2)))
        self.delegate_undirected_to_ai = bool(trigger_config.get("delegate_undirected_to_ai", True))

        self.overload_enable = bool(trigger_config.get("overload_enable", True))
        self.overload_min_messages = max(1, int(trigger_config.get("overload_min_messages", 20)))
        self.overload_min_unique_users = max(1, int(trigger_config.get("overload_min_unique_users", 3)))
        self.overload_pause = timedelta(seconds=max(10, int(trigger_config.get("overload_pause_seconds", 45))))
        self.overload_notice_cooldown = timedelta(
            seconds=max(10, int(trigger_config.get("overload_notice_cooldown_seconds", 90)))
        )

        self._active_sessions: dict[str, datetime] = {}
        self._recent_group_messages: dict[str, deque[tuple[datetime, str]]] = defaultdict(deque)
        self._last_reply_targets: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_proactive_reply_at: dict[str, datetime] = {}
        self._overload_until: dict[str, datetime] = {}
        self._last_overload_notice_at: dict[str, datetime] = {}
        self._last_ai_probe_at: dict[str, datetime] = {}

    def _session_key(self, conversation_id: str, user_id: str, is_private: bool) -> str:
        if is_private:
            return conversation_id
        return f"{conversation_id}:{user_id}"

    def activate_session(
        self,
        conversation_id: str,
        user_id: str,
        is_private: bool,
        now: datetime | None = None,
    ) -> None:
        ts = now or datetime.now(timezone.utc)
        self._active_sessions[self._session_key(conversation_id, user_id, is_private)] = ts

    def close_session(self, conversation_id: str, user_id: str, is_private: bool) -> None:
        self._active_sessions.pop(self._session_key(conversation_id, user_id, is_private), None)
        targets = self._last_reply_targets.get(conversation_id)
        if isinstance(targets, dict):
            targets.pop(str(user_id), None)
            if not targets:
                self._last_reply_targets.pop(conversation_id, None)
        self._last_proactive_reply_at.pop(conversation_id, None)
        self._overload_until.pop(conversation_id, None)
        self._last_overload_notice_at.pop(conversation_id, None)
        self._last_ai_probe_at.pop(conversation_id, None)

    def mark_reply_target(self, conversation_id: str, user_id: str, now: datetime | None = None) -> None:
        ts = now or datetime.now(timezone.utc)
        targets = self._last_reply_targets.setdefault(conversation_id, {})
        targets[str(user_id)] = {
            "ts": ts,
            "remaining_turns": self.followup_max_turns,
        }

    def mark_proactive_reply(self, conversation_id: str, now: datetime | None = None) -> None:
        self._last_proactive_reply_at[conversation_id] = now or datetime.now(timezone.utc)

    def evaluate(self, payload: TriggerInput, recent_messages: list[str]) -> TriggerResult:
        _ = recent_messages
        now = payload.timestamp
        self._cleanup(now)

        active_session = self._is_active_session(payload, now)
        followup_candidate = self.peek_followup_candidate(payload.conversation_id, payload.user_id, now)
        name_call = self._contains_alias(payload.text)

        busy_messages = 0
        busy_users = 0
        overload_active = False
        listen_probe = False

        if not payload.is_private:
            self._record_group_activity(payload.conversation_id, payload.user_id, now)
            self._update_followup_state(payload.conversation_id, payload.user_id, now)
            busy_messages, busy_users = self._group_busy_stats(payload.conversation_id)
            overload_active = self._refresh_overload(payload.conversation_id, now, busy_messages, busy_users)
            listen_probe_reason = self._decide_ai_probe_reason(payload, now, busy_messages, busy_users)
            listen_probe = bool(listen_probe_reason)
        else:
            listen_probe_reason = ""

        if overload_active and self._can_send_overload_notice(payload.conversation_id, now):
            return TriggerResult(
                should_handle=True,
                reason="overload_notice",
                active_session=active_session,
                followup_candidate=followup_candidate,
                listen_probe=False,
                overload_active=True,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=100,
            )

        if overload_active:
            return TriggerResult(
                should_handle=False,
                reason="overload_pause",
                active_session=active_session,
                followup_candidate=followup_candidate,
                listen_probe=False,
                overload_active=True,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=0,
            )

        if payload.is_private or payload.mentioned:
            return TriggerResult(
                should_handle=True,
                reason="directed",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=90,
            )

        if name_call:
            return TriggerResult(
                should_handle=True,
                reason="name_call",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=85,
            )

        if self._looks_like_explicit_memory_declare(payload.text):
            return TriggerResult(
                should_handle=True,
                reason="explicit_memory_fact",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=84,
            )

        if followup_candidate:
            # 仅在用户消息真正命中 followup 窗口时才消费回合，
            # 避免“机器人刚发出就把 followup 回合耗尽”。
            self.consume_followup_turn(payload.conversation_id, payload.user_id, now=now)
            return TriggerResult(
                should_handle=True,
                reason="followup_window",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=70,
            )

        if listen_probe:
            return TriggerResult(
                should_handle=True,
                reason=listen_probe_reason or "ai_listen_probe",
                active_session=active_session,
                followup_candidate=False,
                listen_probe=True,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=20,
            )

        if self.delegate_undirected_to_ai:
            return TriggerResult(
                # 仅作为候选进入 AI 评估，不直接放行回复。
                should_handle=False,
                reason="ai_router_candidate",
                active_session=active_session,
                followup_candidate=False,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=10,
            )

        return TriggerResult(
            should_handle=False,
            reason="not_directed",
            active_session=active_session,
            followup_candidate=False,
            listen_probe=False,
            overload_active=False,
            busy_messages=busy_messages,
            busy_users=busy_users,
            ai_gate=True,
            priority=0,
        )

    def _contains_alias(self, text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False

        # 对单字符中文别名做严格匹配：
        # - 必须是独立出现（不能是 "下雪"、"雪花" 等词的一部分）
        # - 允许: "雪 你好"、"雪，帮我"、句首/句尾的 "雪"
        # 对多字符别名保持原有宽松匹配
        for alias in self.bot_aliases:
            if not alias:
                continue
            if len(alias) == 1 and '\u4e00' <= alias <= '\u9fff':
                # 单字符中文别名: 要求前后不是中文/字母/数字
                pattern = rf"(?<![a-z0-9\u4e00-\u9fff]){re.escape(alias)}(?![a-z0-9\u4e00-\u9fff])"
                if re.search(pattern, content):
                    return True
                continue
            if alias in content:
                return True

        compacted = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)
        if compacted:
            for alias in self.bot_aliases:
                if not alias:
                    continue
                # 单字符中文别名不走 compacted 匹配（去掉标点后 "下雪" 仍然包含 "雪"）
                if len(alias) == 1 and '\u4e00' <= alias <= '\u9fff':
                    continue
                if alias in compacted:
                    return True

        for alias in self.bot_aliases:
            if not alias:
                continue
            if len(alias) == 1 and '\u4e00' <= alias <= '\u9fff':
                continue
            if re.fullmatch(r"[a-z0-9_]+", alias):
                pattern = rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])"
            else:
                pattern = re.escape(alias)
            if re.search(pattern, content):
                return True
        return False

    def _is_active_session(self, payload: TriggerInput, now: datetime) -> bool:
        key = self._session_key(payload.conversation_id, payload.user_id, payload.is_private)
        ts = self._active_sessions.get(key)
        if not isinstance(ts, datetime):
            return False
        return now - ts <= self.session_timeout

    def _record_group_activity(self, conversation_id: str, user_id: str, now: datetime) -> None:
        queue = self._recent_group_messages[conversation_id]
        queue.append((now, user_id))
        while queue and now - queue[0][0] > self.busy_window:
            queue.popleft()

    def _group_busy_stats(self, conversation_id: str) -> tuple[int, int]:
        queue = self._recent_group_messages.get(conversation_id, deque())
        message_count = len(queue)
        unique_users = len({item[1] for item in queue})
        return message_count, unique_users

    def _refresh_overload(self, conversation_id: str, now: datetime, message_count: int, unique_users: int) -> bool:
        until = self._overload_until.get(conversation_id)
        if isinstance(until, datetime) and now < until:
            return True

        if isinstance(until, datetime) and now >= until:
            self._overload_until.pop(conversation_id, None)

        if not self.overload_enable:
            return False

        if message_count >= self.overload_min_messages and unique_users >= self.overload_min_unique_users:
            self._overload_until[conversation_id] = now + self.overload_pause
            return True
        return False

    def _can_send_overload_notice(self, conversation_id: str, now: datetime) -> bool:
        last = self._last_overload_notice_at.get(conversation_id)
        if isinstance(last, datetime) and now - last < self.overload_notice_cooldown:
            return False
        self._last_overload_notice_at[conversation_id] = now
        return True

    def _should_open_ai_probe(
        self,
        conversation_id: str,
        now: datetime,
        busy_messages: int,
        busy_users: int,
    ) -> bool:
        return bool(
            self._decide_ai_probe_reason_by_stats(
                conversation_id=conversation_id,
                now=now,
                busy_messages=busy_messages,
                busy_users=busy_users,
            )
        )

    def _decide_ai_probe_reason(
        self,
        payload: TriggerInput,
        now: datetime,
        busy_messages: int,
        busy_users: int,
    ) -> str:
        if not self.ai_listen_enable:
            return ""
        reason = self._decide_ai_probe_reason_by_stats(
            conversation_id=payload.conversation_id,
            now=now,
            busy_messages=busy_messages,
            busy_users=busy_users,
            text=payload.text,
        )
        return reason

    def _decide_ai_probe_reason_by_stats(
        self,
        conversation_id: str,
        now: datetime,
        busy_messages: int,
        busy_users: int,
        text: str = "",
    ) -> str:
        if not self.ai_listen_enable:
            return ""
        last = self._last_ai_probe_at.get(conversation_id)
        if isinstance(last, datetime) and now - last < self.ai_listen_interval:
            return ""

        clean_text = normalize_text(text).lower()
        # 群里几乎没人说话时，不走"监听探测"，直接交给正常路由链路处理。
        if busy_users <= 1 and busy_messages <= max(2, self.ai_listen_min_messages // 2):
            return ""

        # 明确向机器人提请求时，不走"监听探测"分支，避免被低置信拦截。
        if self._looks_like_explicit_bot_request(clean_text):
            return ""

        keyword_hits = self._count_listen_keyword_hits(clean_text)
        explicit_signal = self._explicit_request_signal(clean_text)

        heat_ok = busy_messages >= self.ai_listen_min_messages and busy_users >= self.ai_listen_min_unique_users
        keyword_ok = (
            self.ai_listen_keyword_enable
            and keyword_hits >= self.ai_listen_min_keyword_hits
        )
        score = self._build_listen_score(
            clean_text,
            busy_messages,
            busy_users,
            keyword_hits,
            explicit_signal=explicit_signal,
        )

        if not heat_ok and not keyword_ok and score < self.ai_listen_min_score:
            return ""

        self._last_ai_probe_at[conversation_id] = now
        if explicit_signal >= 1.35:
            return "ai_listen_probe_task"
        if keyword_ok:
            return "ai_listen_probe_keyword"
        if heat_ok:
            return "ai_listen_probe_heat"
        return "ai_listen_probe_score"

    def _looks_like_explicit_bot_request(self, text: str) -> bool:
        return self._explicit_request_signal(text) >= 1.0

    @staticmethod
    def _looks_like_explicit_memory_declare(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        # 过滤“我叫什么/你记得我叫什么吗”这类问句，避免误判成写入指令。
        if any(q in content for q in ("我叫什么", "我叫啥", "你记得我叫什么", "记得我叫什么")):
            return False
        cues = (
            "记住我叫",
            "永久记忆 我叫",
            "永久记忆，我叫",
            "永久记忆,我叫",
            "叫我",
            "喊我",
            "称呼我",
            "记住我的名字",
        )
        return any(cue in content for cue in cues)

    def _count_listen_keyword_hits(self, text: str) -> int:
        if not text or not self.ai_listen_keyword_enable:
            return 0
        matched: set[str] = set()
        for word in self.ai_listen_keywords:
            if not word:
                continue
            # 英文关键词按词边界匹配，避免 "research" 命中 "search" 这类误判。
            if re.fullmatch(r"[a-z0-9_]+", word):
                if re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text):
                    matched.add(word)
                continue
            if word in text:
                matched.add(word)
        return len(matched)

    @classmethod
    @classmethod
    def _explicit_request_signal_from_cues(cls, text: str, cues: tuple[str, ...]) -> float:
        _ = cues
        if not text:
            return 0.0
        score = 0.0

        if re.search(r"^[!/][a-z0-9_.:-]+", text, flags=re.IGNORECASE):
            score += 1.3
        if "?" in text or "?" in text:
            score += 0.6
        if re.search(r"https?://", text, flags=re.IGNORECASE):
            score += 0.7
        if re.search(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", text, flags=re.IGNORECASE):
            score += 0.7
        if re.search(r"\.(?:png|jpe?g|gif|webp|bmp|mp4|webm|mov|m4v|mp3|wav|flac|ogg|zip|7z|rar|exe|apk|ipa|msi|pdf|docx?|xlsx?|pptx?)\b", text, flags=re.IGNORECASE):
            score += 0.7
        if len(text) >= 20:
            score += 0.2

        return min(score, 3.0)

    def _explicit_request_signal(self, text: str) -> float:
        clean = normalize_text(text).lower()
        return self._explicit_request_signal_from_cues(clean, self.explicit_request_cues)

    def _build_listen_score(
        self,
        text: str,
        busy_messages: int,
        busy_users: int,
        keyword_hits: int,
        *,
        explicit_signal: float = 0.0,
    ) -> float:
        msg_ratio = busy_messages / max(1, self.ai_listen_min_messages)
        user_ratio = busy_users / max(1, self.ai_listen_min_unique_users)
        score = msg_ratio * 0.9 + user_ratio * 0.9 + float(keyword_hits) * 1.1

        if ("?" in text or "?" in text) or re.search(r"^[!/][a-z0-9_.:-]+", text, flags=re.IGNORECASE):
            score += 0.5
        score += min(1.6, explicit_signal * 0.9)
        return score

    def peek_followup_candidate(self, conversation_id: str, user_id: str, now: datetime) -> bool:
        targets = self._last_reply_targets.get(conversation_id)
        if not isinstance(targets, dict):
            return False
        uid = str(user_id)
        state = targets.get(uid)
        if not isinstance(state, dict):
            return False

        ts = state.get("ts")
        if not isinstance(ts, datetime) or now - ts > self.followup_reply_window:
            targets.pop(uid, None)
            if not targets:
                self._last_reply_targets.pop(conversation_id, None)
            return False

        remaining = int(state.get("remaining_turns", 0))
        if remaining <= 0:
            targets.pop(uid, None)
            if not targets:
                self._last_reply_targets.pop(conversation_id, None)
            return False
        return True

    def consume_followup_turn(self, conversation_id: str, user_id: str, now: datetime | None = None) -> None:
        """在消息成功发出后消费一次 followup 回合。"""
        ts = now or datetime.now(timezone.utc)
        targets = self._last_reply_targets.get(conversation_id)
        if not isinstance(targets, dict):
            return

        uid = str(user_id)
        state = targets.get(uid)
        if not isinstance(state, dict):
            return

        last_ts = state.get("ts")
        if not isinstance(last_ts, datetime) or ts - last_ts > self.followup_reply_window:
            targets.pop(uid, None)
            if not targets:
                self._last_reply_targets.pop(conversation_id, None)
            return

        remaining = int(state.get("remaining_turns", 0))
        if remaining <= 0:
            targets.pop(uid, None)
            if not targets:
                self._last_reply_targets.pop(conversation_id, None)
            return

        state["remaining_turns"] = remaining - 1
        state["ts"] = ts
        if int(state.get("remaining_turns", 0)) <= 0:
            targets.pop(uid, None)
        else:
            targets[uid] = state

        if not targets:
            self._last_reply_targets.pop(conversation_id, None)

    def _update_followup_state(self, conversation_id: str, user_id: str, now: datetime) -> None:
        _ = user_id
        targets = self._last_reply_targets.get(conversation_id)
        if not isinstance(targets, dict):
            return
        expired: list[str] = []
        for uid, state in targets.items():
            ts = state.get("ts") if isinstance(state, dict) else None
            if not isinstance(ts, datetime) or now - ts > self.followup_reply_window:
                expired.append(uid)
        for uid in expired:
            targets.pop(uid, None)
        if not targets:
            self._last_reply_targets.pop(conversation_id, None)

    def _cleanup(self, now: datetime) -> None:
        expired_sessions = [
            key for key, ts in self._active_sessions.items() if not isinstance(ts, datetime) or now - ts > self.session_timeout
        ]
        for key in expired_sessions:
            self._active_sessions.pop(key, None)

        for cid, targets in list(self._last_reply_targets.items()):
            if not isinstance(targets, dict):
                self._last_reply_targets.pop(cid, None)
                continue
            expired_users: list[str] = []
            for uid, state in targets.items():
                ts = state.get("ts") if isinstance(state, dict) else None
                if not isinstance(ts, datetime) or now - ts > self.followup_reply_window:
                    expired_users.append(uid)
            for uid in expired_users:
                targets.pop(uid, None)
            if not targets:
                self._last_reply_targets.pop(cid, None)

        expired_overload = [
            cid for cid, until in self._overload_until.items() if not isinstance(until, datetime) or now >= until
        ]
        for cid in expired_overload:
            self._overload_until.pop(cid, None)

        for cid, queue in list(self._recent_group_messages.items()):
            while queue and now - queue[0][0] > self.busy_window:
                queue.popleft()
            if not queue:
                self._recent_group_messages.pop(cid, None)
