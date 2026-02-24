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
    """仅负责会话状态与节流，不做关键词语义判定。"""

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
            seconds=max(5, int(trigger_config.get("followup_reply_window_seconds", 30)))
        )
        self.followup_max_turns = max(1, int(trigger_config.get("followup_max_turns", 2)))

        self.busy_window = timedelta(seconds=max(15, int(trigger_config.get("busy_window_seconds", 60))))

        self.ai_listen_enable = bool(trigger_config.get("ai_listen_enable", True))
        self.ai_listen_interval = timedelta(
            seconds=max(15, int(trigger_config.get("ai_listen_interval_seconds", 45)))
        )
        self.ai_listen_min_messages = max(1, int(trigger_config.get("ai_listen_min_messages", 8)))
        self.ai_listen_min_unique_users = max(1, int(trigger_config.get("ai_listen_min_unique_users", 3)))
        self.ai_listen_keyword_enable = bool(trigger_config.get("ai_listen_keyword_enable", True))
        keywords_raw = trigger_config.get(
            "ai_listen_keywords",
            [
                "怎么看",
                "你觉得",
                "谁懂",
                "有没有人知道",
                "帮我",
                "求助",
                "怎么弄",
                "为什么",
                "什么情况",
                "真的假的",
            ],
        )
        if not isinstance(keywords_raw, list):
            keywords_raw = []
        self.ai_listen_keywords = [
            normalize_text(str(item)).lower()
            for item in keywords_raw
            if normalize_text(str(item))
        ]
        self.ai_listen_min_keyword_hits = max(1, int(trigger_config.get("ai_listen_min_keyword_hits", 1)))
        self.ai_listen_min_score = max(0.5, float(trigger_config.get("ai_listen_min_score", 2.2)))
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

        if followup_candidate:
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
                should_handle=True,
                reason="ai_router_gate",
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
        if any(alias in content for alias in self.bot_aliases):
            return True

        compacted = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)
        if compacted and any(alias and alias in compacted for alias in self.bot_aliases):
            return True

        for alias in self.bot_aliases:
            if not alias:
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

        heat_ok = busy_messages >= self.ai_listen_min_messages and busy_users >= self.ai_listen_min_unique_users
        keyword_ok = (
            self.ai_listen_keyword_enable
            and keyword_hits >= self.ai_listen_min_keyword_hits
        )
        score = self._build_listen_score(clean_text, busy_messages, busy_users, keyword_hits)

        if not heat_ok and not keyword_ok and score < self.ai_listen_min_score:
            return ""

        self._last_ai_probe_at[conversation_id] = now
        if keyword_ok:
            return "ai_listen_probe_keyword"
        if heat_ok:
            return "ai_listen_probe_heat"
        return "ai_listen_probe_score"

    @staticmethod
    def _looks_like_explicit_bot_request(text: str) -> bool:
        if not text:
            return False
        cues = (
            "你帮我",
            "帮我",
            "给我找",
            "给我发",
            "请你",
            "你能",
            "你可以",
            "你去",
            "你来",
            "你给我",
        )
        return any(cue in text for cue in cues)

    def _count_listen_keyword_hits(self, text: str) -> int:
        if not text or not self.ai_listen_keyword_enable:
            return 0
        hits = 0
        for word in self.ai_listen_keywords:
            if word and word in text:
                hits += 1
        return hits

    def _build_listen_score(self, text: str, busy_messages: int, busy_users: int, keyword_hits: int) -> float:
        msg_ratio = busy_messages / max(1, self.ai_listen_min_messages)
        user_ratio = busy_users / max(1, self.ai_listen_min_unique_users)
        score = msg_ratio * 0.9 + user_ratio * 0.9 + float(keyword_hits) * 1.1

        # 问句/求助句更可能需要机器人插话。
        if any(cue in text for cue in ("?", "？", "吗", "咋办", "怎么", "为什么", "求助")):
            score += 0.5
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
