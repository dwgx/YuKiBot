"""好感度 / 心情 / 打卡系统。

每个用户与 bot 之间维护一个好感度值（affinity），
bot 自身维护一个全局心情值（mood）。
好感度影响回复风格、工具权限、主动互动频率。
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("yukiko.affinity")

# ── 心情枚举 ──
MOODS = ("happy", "neutral", "tired", "annoyed", "excited", "melancholy",
         "curious", "playful", "shy", "proud", "lonely", "grateful")


@dataclass
class UserAffinity:
    """单个用户的好感度数据。"""
    user_id: str = ""
    nickname: str = ""
    affinity: float = 50.0          # 0-100
    level: int = 1                  # 1-10
    total_interactions: int = 0
    daily_checkin_streak: int = 0
    last_checkin_date: str = ""     # YYYY-MM-DD
    last_interaction: str = ""      # ISO datetime
    gifts_received: int = 0
    tags: list[str] = field(default_factory=list)  # AI 自动打的标签
    # ── Phase 5: 用户情感记忆 ──
    last_emotion: str = ""          # 上次对话中用户的情绪
    special_dates: list[str] = field(default_factory=list)  # 用户提到的重要日期

    @property
    def level_name(self) -> str:
        names = {
            1: "陌生人", 2: "点头之交", 3: "普通朋友",
            4: "好朋友", 5: "密友", 6: "挚友",
            7: "知己", 8: "灵魂伴侣", 9: "命中注定",
            10: "羁绊",
        }
        return names.get(self.level, "未知")


@dataclass
class BotMood:
    """Bot 全局心情状态。"""
    current: str = "neutral"
    intensity: float = 0.5          # 0-1
    last_change: str = ""
    reason: str = ""

    def to_prompt_hint(self) -> str:
        mood_cn = {
            "happy": "开心", "neutral": "平静", "tired": "疲惫",
            "annoyed": "烦躁", "excited": "兴奋", "melancholy": "忧郁",
            "curious": "好奇", "playful": "调皮", "shy": "害羞",
            "proud": "得意", "lonely": "寂寞", "grateful": "感恩",
        }
        mood_name = mood_cn.get(self.current, self.current)
        # 根据心情强度生成更自然的描述
        if self.intensity >= 0.8:
            degree = "非常"
        elif self.intensity >= 0.5:
            degree = "有点"
        else:
            degree = "微微"
        hint = f"[你现在{degree}{mood_name}"
        if self.reason:
            hint += f"，因为{self.reason}"
        hint += "。让这种情绪自然地融入你的回复语气中，但不要刻意提及。]"
        return hint


class AffinityEngine:
    """管理所有用户好感度和 bot 心情。"""

    _LEVEL_THRESHOLDS = [0, 10, 20, 35, 50, 65, 75, 85, 92, 98]
    _CHECKIN_BASE_REWARD = 2.0
    _CHECKIN_STREAK_BONUS = 0.5  # 每连续天 +0.5
    _CHECKIN_STREAK_MAX_BONUS = 5.0
    _INTERACTION_REWARD = 0.3
    _DECAY_PER_DAY = 0.5  # 不互动时每天衰减

    def __init__(self, storage_dir: str | Path = "storage/affinity"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._users: dict[str, UserAffinity] = {}
        self._mood = BotMood()
        self._load()

    # ── 持久化 ──

    def _load(self) -> None:
        users_file = self._dir / "users.json"
        mood_file = self._dir / "mood.json"
        if users_file.exists():
            try:
                raw = json.loads(users_file.read_text("utf-8"))
                for uid, data in raw.items():
                    self._users[uid] = UserAffinity(**{
                        k: v for k, v in data.items()
                        if k in UserAffinity.__dataclass_fields__
                    })
            except Exception:
                _log.warning("affinity_load_error", exc_info=True)
        if mood_file.exists():
            try:
                raw = json.loads(mood_file.read_text("utf-8"))
                self._mood = BotMood(**{
                    k: v for k, v in raw.items()
                    if k in BotMood.__dataclass_fields__
                })
            except Exception:
                _log.warning("mood_load_error", exc_info=True)

    def save(self) -> None:
        try:
            users_file = self._dir / "users.json"
            users_file.write_text(
                json.dumps(
                    {uid: asdict(u) for uid, u in self._users.items()},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            mood_file = self._dir / "mood.json"
            mood_file.write_text(
                json.dumps(asdict(self._mood), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            _log.warning("affinity_save_error", exc_info=True)

    # ── 好感度操作 ──

    def get_user(self, user_id: str) -> UserAffinity:
        uid = str(user_id).strip()
        if uid not in self._users:
            self._users[uid] = UserAffinity(user_id=uid)
        return self._users[uid]

    def add_affinity(self, user_id: str, delta: float, reason: str = "") -> UserAffinity:
        user = self.get_user(user_id)
        user.affinity = max(0.0, min(100.0, user.affinity + delta))
        user.last_interaction = datetime.now(timezone.utc).isoformat()
        old_level = user.level
        user.level = self._calc_level(user.affinity)
        if user.level != old_level:
            _log.info("affinity_level_change | user=%s | %d->%d | reason=%s",
                       user_id, old_level, user.level, reason)
        self.save()
        return user

    def record_interaction(self, user_id: str, quality: float = 1.0) -> UserAffinity:
        """记录一次互动，quality 0-2 表示互动质量。"""
        user = self.get_user(user_id)
        user.total_interactions += 1
        reward = self._INTERACTION_REWARD * max(0.0, min(2.0, quality))
        return self.add_affinity(user_id, reward, "interaction")

    def checkin(self, user_id: str) -> tuple[UserAffinity, str]:
        """每日打卡，返回 (用户数据, 提示消息)。"""
        user = self.get_user(user_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if user.last_checkin_date == today:
            return user, "你今天已经打过卡了哦~"

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        if user.last_checkin_date == yesterday:
            user.daily_checkin_streak += 1
        else:
            user.daily_checkin_streak = 1

        user.last_checkin_date = today
        streak_bonus = min(
            self._CHECKIN_STREAK_MAX_BONUS,
            user.daily_checkin_streak * self._CHECKIN_STREAK_BONUS,
        )
        reward = self._CHECKIN_BASE_REWARD + streak_bonus
        self.add_affinity(user_id, reward, "checkin")

        msg = (
            f"打卡成功！连续 {user.daily_checkin_streak} 天 ✨\n"
            f"好感度 +{reward:.1f} → {user.affinity:.1f}\n"
            f"当前等级: Lv.{user.level} {user.level_name}"
        )
        return user, msg

    def get_leaderboard(self, top_n: int = 10) -> list[UserAffinity]:
        """获取好感度排行榜。"""
        users = sorted(self._users.values(), key=lambda u: u.affinity, reverse=True)
        return users[:top_n]

    def _calc_level(self, affinity: float) -> int:
        level = 1
        for i, threshold in enumerate(self._LEVEL_THRESHOLDS):
            if affinity >= threshold:
                level = i + 1
        return min(10, level)

    # ── Bot 心情 ──

    @property
    def mood(self) -> BotMood:
        return self._mood

    def update_mood(self, new_mood: str, reason: str = "", intensity: float | None = None) -> BotMood:
        if new_mood in MOODS:
            self._mood.current = new_mood
        if intensity is not None:
            self._mood.intensity = max(0.0, min(1.0, intensity))
        self._mood.reason = reason
        self._mood.last_change = datetime.now(timezone.utc).isoformat()
        self.save()
        return self._mood

    def auto_mood_from_interactions(self, recent_count: int, positive_ratio: float) -> BotMood:
        """根据最近互动自动调整心情，更细腻的情绪变化。"""
        if recent_count > 20 and positive_ratio > 0.7:
            return self.update_mood("happy", "很多人在聊天，气氛不错", 0.8)
        elif recent_count > 30:
            return self.update_mood("tired", "消息太多了，有点累", 0.6)
        elif recent_count > 15 and positive_ratio > 0.5:
            return self.update_mood("playful", "大家聊得挺开心", 0.7)
        elif recent_count > 10 and positive_ratio > 0.8:
            return self.update_mood("excited", "今天好热闹", 0.75)
        elif recent_count < 3:
            return self.update_mood("lonely", "好安静啊，没人理我", 0.4)
        elif recent_count < 5:
            return self.update_mood("melancholy", "人好少", 0.35)
        elif positive_ratio < 0.3:
            return self.update_mood("annoyed", "怎么都在吵架", 0.5)
        elif positive_ratio < 0.5 and recent_count > 10:
            return self.update_mood("neutral", "平平淡淡的", 0.4)
        return self._mood

    def mood_from_user_emotion(self, user_emotion: str) -> BotMood:
        """根据用户情绪调整 bot 心情（共情）。"""
        emotion = user_emotion.lower().strip()
        if emotion in ("happy", "excited", "grateful"):
            return self.update_mood("happy", "用户心情不错，我也开心", 0.6)
        elif emotion in ("sad", "depressed", "lonely"):
            return self.update_mood("melancholy", "用户心情不好，有点担心", 0.5)
        elif emotion in ("angry", "frustrated"):
            return self.update_mood("shy", "用户在生气，小心翼翼", 0.4)
        elif emotion in ("curious", "interested"):
            return self.update_mood("curious", "用户对什么感兴趣呢", 0.6)
        return self._mood

    def mood_prompt_hint(self) -> str:
        return self._mood.to_prompt_hint()

    def affinity_prompt_hint(self, user_id: str) -> str:
        user = self.get_user(user_id)
        # 根据好感度等级生成拟人化的关系描述
        tone_hint = self._affinity_tone_hint(user)
        base = (
            f"[与{user.nickname or '该用户'}的关系: Lv.{user.level} {user.level_name} "
            f"(好感度{user.affinity:.0f}/100, 互动{user.total_interactions}次)]"
        )
        if tone_hint:
            base += f"\n{tone_hint}"
        return base

    @staticmethod
    def _affinity_tone_hint(user: UserAffinity) -> str:
        """根据好感度等级生成语气/行为指导。"""
        lv = user.level
        if lv <= 1:
            return "对方是陌生人，保持礼貌友好，不要过于热情。"
        elif lv <= 2:
            return "对方是点头之交，可以轻松聊天但不要太亲密。"
        elif lv <= 3:
            return "对方是普通朋友，可以开玩笑、随意聊天。"
        elif lv <= 4:
            return "对方是好朋友，可以用昵称、分享日常、互相调侃。"
        elif lv <= 5:
            return "对方是密友，可以撒娇、吐槽、分享秘密、表达想念。"
        elif lv <= 6:
            return "对方是挚友，可以深度情感交流、主动关心、偶尔吃醋。"
        elif lv <= 7:
            return "对方是知己，可以无话不谈、表达依赖、用亲昵称呼。"
        elif lv <= 8:
            return "对方是灵魂伴侣，可以表达深层情感、撒娇卖萌、主动找话题。"
        elif lv <= 9:
            return "对方是命中注定的人，可以表达强烈情感、主动关心生活、记住对方的一切。"
        else:
            return "对方是羁绊之人，你们之间有最深的连接，可以完全真实地表达自己。"

    def decay_inactive_users(self) -> int:
        """衰减不活跃用户的好感度，返回衰减的用户数。"""
        now = datetime.now(timezone.utc)
        count = 0
        for uid, user in self._users.items():
            if not user.last_interaction:
                continue
            try:
                last = datetime.fromisoformat(user.last_interaction)
                days_inactive = (now - last).days
                if days_inactive >= 3:
                    decay = self._DECAY_PER_DAY * min(days_inactive, 30)
                    if user.affinity > 10:
                        user.affinity = max(10.0, user.affinity - decay)
                        user.level = self._calc_level(user.affinity)
                        count += 1
            except Exception:
                _log.warning("affinity_decay_parse_error | user=%s", uid, exc_info=True)
                continue
        if count > 0:
            self.save()
        return count
