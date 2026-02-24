from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from utils.text import normalize_text


@dataclass(slots=True)
class EmotionDecision:
    state: str = "none"  # none | warn | strike
    reason: str = ""
    reply_text: str = ""
    score: float = 0.0
    strike_seconds: int = 0


@dataclass(slots=True)
class _EmotionState:
    score: float = 0.0
    last_update: datetime | None = None
    last_warn_at: datetime | None = None
    strike_until: datetime | None = None
    warned_since_last_strike: bool = False


class EmotionEngine:
    """
    Runtime emotion/load gate.
    - Warns first when pressure rises.
    - Strikes only after warning, then cools down for a short window.
    - Supports extension hooks for future plugins/strategies.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config if isinstance(config, dict) else {}

        self.enable = bool(cfg.get("enable", True))
        self.max_score = float(cfg.get("max_score", 100.0))
        self.warn_threshold = float(cfg.get("warn_threshold", 18.0))
        self.strike_threshold = max(self.warn_threshold + 0.1, float(cfg.get("strike_threshold", 28.0)))
        self.warn_reset_ratio = max(0.1, min(0.95, float(cfg.get("warn_reset_ratio", 0.6))))
        self.decay_per_second = max(0.0, float(cfg.get("decay_per_second", 0.06)))

        self.warn_cooldown_seconds = max(3, int(cfg.get("warn_cooldown_seconds", 18)))
        self.strike_cooldown_seconds = max(6, int(cfg.get("strike_cooldown_seconds", 45)))

        self.burst_window_seconds = max(6, int(cfg.get("burst_window_seconds", 20)))
        self.burst_trigger_count = max(2, int(cfg.get("burst_trigger_count", 4)))
        self.burst_weight = max(0.0, float(cfg.get("burst_weight", 0.9)))
        self.queue_depth_weight = max(0.0, float(cfg.get("queue_depth_weight", 0.5)))
        self.busy_users_weight = max(0.0, float(cfg.get("busy_users_weight", 0.8)))
        self.explicit_request_weight = max(0.0, float(cfg.get("explicit_request_weight", 0.6)))
        self.directed_weight = max(0.0, float(cfg.get("directed_weight", 0.3)))
        self.default_action_weight = max(0.0, float(cfg.get("default_action_weight", 1.0)))

        raw_action_weights = cfg.get("action_weights", {})
        self.action_weights: dict[str, float] = {
            "reply": 1.0,
            "search": 1.8,
            "generate_image": 2.2,
            "music_search": 1.1,
            "music_play": 1.4,
            "plugin_call": 1.6,
            "get_group_member_count": 1.0,
            "get_group_member_names": 1.0,
        }
        if isinstance(raw_action_weights, dict):
            for key, value in raw_action_weights.items():
                name = normalize_text(str(key)).lower()
                if not name:
                    continue
                try:
                    self.action_weights[name] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue

        self.warn_messages = self._normalize_messages(
            cfg.get("warn_messages"),
            default=[
                "消息有点多，我正在处理中，稍等一下哦。",
                "收到了，我在排队处理，马上就到你。",
            ],
        )
        self.strike_messages = self._normalize_messages(
            cfg.get("strike_messages"),
            default=[
                "当前消息太密集了，我需要 {remain} 秒消化一下，之后继续回复你。",
                "请求堆积了，我暂停 {remain} 秒整理一下，马上回来。",
            ],
        )

        self._warn_cursor = 0
        self._strike_cursor = 0

        self._states: dict[str, _EmotionState] = {}
        self._user_events: dict[str, deque[datetime]] = defaultdict(lambda: deque(maxlen=64))
        self._load_hooks: dict[str, Callable[[dict[str, Any]], float]] = {}

    def register_load_hook(self, name: str, hook: Callable[[dict[str, Any]], float]) -> None:
        key = normalize_text(name)
        if not key:
            return
        self._load_hooks[key] = hook

    def unregister_load_hook(self, name: str) -> None:
        key = normalize_text(name)
        if not key:
            return
        self._load_hooks.pop(key, None)

    def evaluate(
        self,
        *,
        conversation_id: str,
        user_id: str,
        now: datetime,
        action: str,
        queue_depth: int = 0,
        busy_users: int = 0,
        is_private: bool = False,
        mentioned: bool = False,
        explicit_request: bool = False,
    ) -> EmotionDecision:
        if not self.enable:
            return EmotionDecision()

        ts = self._normalize_time(now)
        conv_key = normalize_text(conversation_id) or "_default"
        user_key = normalize_text(user_id) or "_unknown"
        state = self._states.get(conv_key)
        if state is None:
            state = _EmotionState(last_update=ts)
            self._states[conv_key] = state

        self._decay_state(state, ts)
        self._cleanup(ts)

        if isinstance(state.strike_until, datetime) and ts < state.strike_until:
            remain = max(1, int((state.strike_until - ts).total_seconds()))
            text = self._format_text(self._pick_strike_message(), remain)
            return EmotionDecision(
                state="strike",
                reason="strike_cooldown",
                reply_text=text,
                score=state.score,
                strike_seconds=remain,
            )

        delta = self._calc_delta(
            conversation_id=conv_key,
            user_id=user_key,
            now=ts,
            action=action,
            queue_depth=queue_depth,
            busy_users=busy_users,
            is_private=is_private,
            mentioned=mentioned,
            explicit_request=explicit_request,
        )
        state.score = max(0.0, min(self.max_score, state.score + delta))

        if state.score < self.warn_threshold * self.warn_reset_ratio:
            state.warned_since_last_strike = False

        if state.score >= self.strike_threshold:
            if not state.warned_since_last_strike and self._can_warn(state, ts):
                state.warned_since_last_strike = True
                state.last_warn_at = ts
                return EmotionDecision(
                    state="warn",
                    reason="pre_strike_warning",
                    reply_text=self._pick_warn_message(),
                    score=state.score,
                )

            hold = self.strike_cooldown_seconds
            state.strike_until = ts + timedelta(seconds=hold)
            state.warned_since_last_strike = False
            state.last_warn_at = ts
            state.score = max(self.warn_threshold * 0.5, state.score * 0.45)
            return EmotionDecision(
                state="strike",
                reason="strike_threshold",
                reply_text=self._format_text(self._pick_strike_message(), hold),
                score=state.score,
                strike_seconds=hold,
            )

        if state.score >= self.warn_threshold and self._can_warn(state, ts):
            state.warned_since_last_strike = True
            state.last_warn_at = ts
            return EmotionDecision(
                state="warn",
                reason="warn_threshold",
                reply_text=self._pick_warn_message(),
                score=state.score,
            )

        return EmotionDecision(state="none", score=state.score)

    def _calc_delta(
        self,
        *,
        conversation_id: str,
        user_id: str,
        now: datetime,
        action: str,
        queue_depth: int,
        busy_users: int,
        is_private: bool,
        mentioned: bool,
        explicit_request: bool,
    ) -> float:
        action_key = normalize_text(action).lower()
        delta = float(self.action_weights.get(action_key, self.default_action_weight))
        delta += max(0, int(queue_depth)) * self.queue_depth_weight
        delta += max(0, int(busy_users) - 1) * self.busy_users_weight
        if explicit_request:
            delta += self.explicit_request_weight
        if mentioned or is_private:
            delta += self.directed_weight

        burst = self._user_burst_hits(conversation_id=conversation_id, user_id=user_id, now=now)
        if burst > 0:
            delta += burst * self.burst_weight

        hook_payload = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "action": action_key,
            "queue_depth": max(0, int(queue_depth)),
            "busy_users": max(0, int(busy_users)),
            "is_private": bool(is_private),
            "mentioned": bool(mentioned),
            "explicit_request": bool(explicit_request),
        }
        for hook in self._load_hooks.values():
            try:
                extra = float(hook(hook_payload))
            except Exception:
                extra = 0.0
            if extra > 0:
                delta += extra
        return max(0.0, delta)

    def _user_burst_hits(self, *, conversation_id: str, user_id: str, now: datetime) -> int:
        key = f"{conversation_id}:{user_id}"
        rows = self._user_events[key]
        rows.append(now)
        expire_at = now - timedelta(seconds=self.burst_window_seconds)
        while rows and rows[0] < expire_at:
            rows.popleft()
        count = len(rows)
        if count < self.burst_trigger_count:
            return 0
        return count - self.burst_trigger_count + 1

    def _decay_state(self, state: _EmotionState, now: datetime) -> None:
        if state.last_update is None:
            state.last_update = now
            return
        elapsed = max(0.0, (now - state.last_update).total_seconds())
        if elapsed > 0 and self.decay_per_second > 0:
            state.score = max(0.0, state.score - elapsed * self.decay_per_second)
        state.last_update = now
        if isinstance(state.strike_until, datetime) and now >= state.strike_until:
            state.strike_until = None

    def _can_warn(self, state: _EmotionState, now: datetime) -> bool:
        if not isinstance(state.last_warn_at, datetime):
            return True
        return (now - state.last_warn_at).total_seconds() >= self.warn_cooldown_seconds

    def _cleanup(self, now: datetime) -> None:
        if self._states:
            stale_states: list[str] = []
            max_idle = max(self.strike_cooldown_seconds * 3, self.burst_window_seconds * 3, 180)
            for key, state in self._states.items():
                if isinstance(state.strike_until, datetime) and now < state.strike_until:
                    continue
                if not isinstance(state.last_update, datetime):
                    stale_states.append(key)
                    continue
                if (now - state.last_update).total_seconds() > max_idle and state.score <= 0.1:
                    stale_states.append(key)
            for key in stale_states:
                self._states.pop(key, None)

        if self._user_events:
            stale_users: list[str] = []
            expire_at = now - timedelta(seconds=max(self.burst_window_seconds * 2, 30))
            for key, rows in self._user_events.items():
                while rows and rows[0] < expire_at:
                    rows.popleft()
                if not rows:
                    stale_users.append(key)
            for key in stale_users:
                self._user_events.pop(key, None)

    @staticmethod
    def _normalize_messages(value: Any, default: list[str]) -> list[str]:
        if isinstance(value, str):
            text = normalize_text(value)
            return [text] if text else default
        if isinstance(value, list):
            rows = [normalize_text(str(item)) for item in value if normalize_text(str(item))]
            return rows or default
        return default

    @staticmethod
    def _normalize_time(value: datetime | None) -> datetime:
        now = value if isinstance(value, datetime) else datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _pick_warn_message(self) -> str:
        if not self.warn_messages:
            return "消息有点多，我正在处理中，稍等一下。"
        idx = self._warn_cursor % len(self.warn_messages)
        self._warn_cursor += 1
        return self.warn_messages[idx]

    def _pick_strike_message(self) -> str:
        if not self.strike_messages:
            return "请求堆积了，我暂停 {remain} 秒整理一下，马上回来。"
        idx = self._strike_cursor % len(self.strike_messages)
        self._strike_cursor += 1
        return self.strike_messages[idx]

    @staticmethod
    def _format_text(template: str, remain: int) -> str:
        try:
            return str(template).format(remain=max(1, int(remain)))
        except Exception:
            return str(template)
