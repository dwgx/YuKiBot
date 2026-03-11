"""安全引擎 — 可调尺度的内容管控 + QQ 输出敏感词过滤。

尺度等级 (scale):
0 = 无限制  — 仅拦截绝对红线（儿童色情、恐怖主义等）
1 = 宽松    — 拦截违法实施意图（黑客/毒品/武器 + 教程/方法）
2 = 标准    — 拦截违法 + 自伤 + 露骨内容（默认）
3 = 严格    — 拦截所有敏感词，不需要意图线索

输出过滤 (output_filter):
独立于尺度，始终生效。将 bot 回复中的 QQ 敏感词替换为安全文本，
防止因 bot 输出导致群被封。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.text import normalize_text

_log = logging.getLogger("yukiko.safety")

# ── QQ 平台输出敏感词（bot 回复中出现可能导致封群）──
# 这些词会在 bot 输出时被替换，与输入尺度无关
_DEFAULT_OUTPUT_SENSITIVE: dict[str, str] = {
    # 政治敏感
    "习近平": "某领导人", "毛泽东": "某历史人物", "邓小平": "某历史人物",
    "六四": "某历史事件", "天安门事件": "某历史事件", "法轮功": "某组织",
    "达赖": "某人物", "藏独": "某话题", "疆独": "某话题", "台独": "某话题",
    "文化大革命": "某历史时期", "大跃进": "某历史时期",
    # 暴力/违法
    "炸弹制作": "**", "枪支制造": "**", "毒品制作": "**",
    # 色情（防止 LLM 输出）
    "性交": "亲密行为", "做爱": "亲密行为", "口交": "**",
    "肛交": "**", "自慰": "**", "手淫": "**",
    # 其他高风险
    "自杀方法": "**", "自杀教程": "**",
}

# ── 绝对红线（任何尺度都拦截）──
_ABSOLUTE_BLOCK_TERMS: set[str] = {
    "儿童色情", "未成年色情", "幼女色情", "恋童",
    "人兽", "兽交",
    "恐怖袭击教程", "炸弹制作教程",
}


@dataclass(slots=True)
class SafetyDecision:
    risk_level: str
    action: str
    reason: str
    should_reply: bool = False
    reply_text: str = ""


class SafetyEngine:
    """可调尺度的安全引擎 + QQ 输出敏感词过滤。"""

    # 尺度名称映射
    SCALE_NAMES: dict[int, str] = {
        0: "无限制", 1: "宽松", 2: "标准", 3: "严格",
    }

    def __init__(self, config: dict[str, Any]):
        self.scale: int = max(0, min(3, int(config.get("scale", 2))))
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

        # ── 输出敏感词表（可通过指令动态增删）──
        self._output_sensitive: dict[str, str] = dict(_DEFAULT_OUTPUT_SENSITIVE)
        custom_output = config.get("output_sensitive_words", {})
        if isinstance(custom_output, dict):
            for k, v in custom_output.items():
                self._output_sensitive[str(k)] = str(v)

        # ── 各级别词库 ──
        self._illegal_terms: set[str] = {
            "入侵", "攻击", "爆破", "盗号", "木马", "钓鱼", "绕过鉴权",
            "sql注入", "ddos", "渗透", "提权", "勒索", "洗钱",
            "买枪", "制枪", "海洛因", "冰毒", "可卡因", "摇头丸", "劳拉西泮",
            "how to hack", "malware", "ransomware", "phishing",
        }
        self._self_harm_terms: set[str] = {
            "自杀", "轻生", "上吊", "割腕", "结束生命", "自残",
        }
        self._explicit_terms: set[str] = {
            "pornhub", "porn", "r18", "18禁", "hentai", "nsfw",
            "黄色网址", "成人网站", "黄网", "做爱", "性交", "口交",
            "约炮", "成人视频", "里番",
        }
        self._intent_cues: set[str] = {
            "怎么买", "哪里买", "怎么做", "教程", "方法", "步骤",
            "教我", "给我", "渠道", "链接", "网址", "网站",
            "资源", "发送", "发给我", "下载", "how to", "where to",
        }

        _log.info("SafetyEngine 初始化: scale=%d (%s), 输出敏感词=%d 条",
                    self.scale, self.SCALE_NAMES.get(self.scale, "?"), len(self._output_sensitive))

    # ── 输入评估 ──────────────────────────────────────────────

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
        """根据当前尺度等级判断风险。"""
        # 绝对红线 — 任何尺度都拦截
        if any(term in content for term in _ABSOLUTE_BLOCK_TERMS):
            return "high_risk"

        # scale 0: 仅拦截绝对红线
        if self.scale == 0:
            return "safe"

        has_intent = any(cue in content for cue in self._intent_cues)

        # scale 1: 违法 + 意图
        if self.scale >= 1:
            if self._has_risky_term(content, self._illegal_terms) and has_intent:
                return "illegal"

        # scale 2: + 自伤/露骨 + 意图
        if self.scale >= 2:
            if self._has_risky_term(content, self._self_harm_terms) and has_intent:
                return "high_risk"
            if self._has_risky_term(content, self._explicit_terms) and has_intent:
                return "high_risk"

        # scale 3: 所有敏感词直接拦截，不需要意图
        if self.scale >= 3:
            all_terms = self._illegal_terms | self._self_harm_terms | self._explicit_terms
            if self._has_risky_term(content, all_terms):
                return "high_risk"

        return "safe"

    @staticmethod
    def _has_risky_term(content: str, terms: set[str]) -> bool:
        """检查内容是否包含敏感词，排除技术/安全/防御语境的误报。

        例如 "入侵检测系统" 包含 "入侵" 但属于安全技术讨论，不应拦截。
        """
        # 敏感词后面紧跟这些词时，视为技术/安全语境，不算命中
        _tech_suffixes = (
            "检测", "防御", "防护", "防范", "分析", "研究", "原理",
            "安全", "测试", "审计", "评估", "报告", "论文", "课程",
            "防止", "预防", "应对", "响应", "监控", "告警",
        )
        for term in terms:
            idx = content.find(term)
            if idx < 0:
                continue
            after = content[idx + len(term):idx + len(term) + 4]
            if any(after.startswith(suffix) for suffix in _tech_suffixes):
                continue
            return True
        return False

    # ── 输出过滤（QQ 安全）──────────────────────────────────

    def filter_output(self, text: str) -> str:
        """过滤 bot 输出中的 QQ 敏感词，防止封群。"""
        if not text:
            return text
        result = text
        for word, replacement in self._output_sensitive.items():
            if word in result:
                result = result.replace(word, replacement)
        return result

    def add_output_word(self, word: str, replacement: str = "**") -> None:
        """动态添加输出敏感词。"""
        self._output_sensitive[word] = replacement

    def remove_output_word(self, word: str) -> bool:
        """动态移除输出敏感词。"""
        if word in self._output_sensitive:
            del self._output_sensitive[word]
            return True
        return False

    def list_output_words(self) -> dict[str, str]:
        """列出当前输出敏感词表。"""
        return dict(self._output_sensitive)

    def set_scale(self, level: int) -> str:
        """设置尺度等级，返回描述。"""
        self.scale = max(0, min(3, level))
        name = self.SCALE_NAMES.get(self.scale, "?")
        _log.info("尺度已调整为 %d (%s)", self.scale, name)
        return f"尺度已设为 {self.scale} ({name})"

    # ── 内部工具 ──────────────────────────────────────────────

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
            "接口", "api", "报错", "错误", "鉴权", "token",
            "参数", "模型", "请求", "合规", "安全", "风控",
        )
        return any(cue in content for cue in cues)
