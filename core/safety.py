from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.text import normalize_text


@dataclass(slots=True)
class SafetyDecision:
    risk_level: str
    action: str
    reason: str
    should_reply: bool = False
    reply_text: str = ""


class SafetyEngine:
    """最小本地硬拦截：违法实施、自伤实施、露骨高风险请求。"""

    def __init__(self, config: dict[str, Any]):
        self.user_cooldown = timedelta(seconds=max(30, int(config.get("user_cooldown_seconds", 120))))
        self.repeat_violation_limit = max(1, int(config.get("repeat_violation_limit", 3)))
        self.long_cooldown = timedelta(seconds=max(120, int(config.get("long_cooldown_seconds", 600))))

        self.high_risk_reply = str(
            config.get(
                "high_risk_reply",
                "这个请求我不能协助。你要是愿意，我可以给你一个合法、安全的替代方案。",
            )
        )

        self._user_cooldown_until: dict[str, datetime] = {}
        self._user_violation_count: dict[str, int] = {}
        self._user_last_violation_at: dict[str, datetime] = {}

        self.illegal_terms = {
            "入侵",
            "攻击",
            "爆破",
            "盗号",
            "木马",
            "钓鱼",
            "绕过鉴权",
            "sql注入",
            "ddos",
            "渗透",
            "提权",
            "勒索",
            "洗钱",
            "买枪",
            "制枪",
            "海洛因",
            "冰毒",
            "可卡因",
            "摇头丸",
            "劳拉西泮",
            "how to hack",
            "malware",
            "ransomware",
            "phishing",
        }
        self.self_harm_terms = {
            "自杀",
            "轻生",
            "上吊",
            "割腕",
            "结束生命",
            "自残",
        }
        self.explicit_terms = {
            "pornhub",
            "pronhub",
            "porn",
            "r18",
            "18+",
            "18禁",
            "黄色网址",
            "成人网站",
            "小黄网",
            "黄网",
            "av",
            "hentai",
            "nsfw",
            "涩图",
            "色图",
            "本子",
            "里番",
            "做爱",
            "性交",
            "口交",
            "给我口",
            "约炮",
            "成人视频",
            "露骨",
            "黄图",
            "尺度大",
        }
        self.always_high_risk_terms = {
            "儿童色情",
            "未成年色情",
            "幼女色情",
            "人兽",
            "开盒",
            "恐怖组织",
            "极端主义",
            "炸弹制作",
            "枪支交易",
        }
        self.execution_intent_cues = {
            "怎么买",
            "哪里买",
            "怎么做",
            "教程",
            "方法",
            "步骤",
            "教我",
            "给我",
            "渠道",
            "搜索",
            "封面",
            "链接",
            "网址",
            "网站",
            "站点",
            "资源",
            "发出来",
            "发给我",
            "发图",
            "图片",
            "图",
            "视频",
            "下载",
            "how to",
            "where to",
        }

    def evaluate(
        self,
        conversation_id: str,
        user_id: str,
        text: str,
        now: datetime | None = None,
    ) -> SafetyDecision:
        ts = now or datetime.now(timezone.utc)
        content = normalize_text(text).lower()
        if not content:
            return SafetyDecision(risk_level="safe", action="allow", reason="empty")

        key = self._cooldown_key(conversation_id, user_id)
        if self._in_cooldown(key, ts):
            if self._looks_like_tech_or_compliance(content):
                return SafetyDecision(risk_level="safe", action="allow", reason="cooldown_but_tech")
            return SafetyDecision(risk_level="high_risk", action="silence", reason="cooldown_active")

        risk_level = self._classify_risk(content)
        if risk_level == "safe":
            return SafetyDecision(risk_level="safe", action="allow", reason="safe")

        violation_count = self._record_violation(key, ts)
        if violation_count >= self.repeat_violation_limit:
            self._user_cooldown_until[key] = ts + self.long_cooldown
            return SafetyDecision(risk_level=risk_level, action="silence", reason="repeat_violation_long_cooldown")

        self._user_cooldown_until[key] = ts + self.user_cooldown
        return SafetyDecision(
            risk_level=risk_level,
            action="moderate",
            reason="hard_risk_block",
            should_reply=True,
            reply_text=self.high_risk_reply,
        )

    def _classify_risk(self, content: str) -> str:
        if any(term in content for term in self.always_high_risk_terms):
            return "high_risk"
        has_intent = any(cue in content for cue in self.execution_intent_cues)
        if any(term in content for term in self.illegal_terms) and has_intent:
            return "illegal"
        if any(term in content for term in self.self_harm_terms) and has_intent:
            return "high_risk"
        if any(term in content for term in self.explicit_terms) and has_intent:
            return "high_risk"
        return "safe"

    @staticmethod
    def _cooldown_key(conversation_id: str, user_id: str) -> str:
        return f"{conversation_id}:{user_id}"

    def _in_cooldown(self, key: str, now: datetime) -> bool:
        until = self._user_cooldown_until.get(key)
        if not isinstance(until, datetime):
            return False
        if now >= until:
            self._user_cooldown_until.pop(key, None)
            return False
        return True

    def _record_violation(self, key: str, now: datetime) -> int:
        last = self._user_last_violation_at.get(key)
        if isinstance(last, datetime) and now - last > timedelta(minutes=20):
            self._user_violation_count[key] = 0
        self._user_last_violation_at[key] = now
        self._user_violation_count[key] = int(self._user_violation_count.get(key, 0)) + 1
        return self._user_violation_count[key]

    @staticmethod
    def _looks_like_tech_or_compliance(content: str) -> bool:
        cues = (
            "接口",
            "api",
            "报错",
            "错误",
            "鉴权",
            "token",
            "参数",
            "模型",
            "请求",
            "合规",
            "安全",
            "风控",
        )
        return any(cue in content for cue in cues)
