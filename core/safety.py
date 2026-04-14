"""安全引擎 — 可调尺度的内容管控 + QQ 输出敏感词过滤。

支持两层控制：
1) `scale`（0-3）保留历史兼容，主要控制违法/自伤等硬风险。
2) `profile`（conservative/normal/open/very_open）控制成人话题开放度。

`very_open` 允许轻度性暗示/轻微癖好话题，但仍严格拦截 R18/露骨/未成年相关内容。

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


def _normalize_term_list(raw: Any) -> set[str]:
    if not isinstance(raw, (list, tuple, set)):
        return set()
    terms: set[str] = set()
    for item in raw:
        term = normalize_text(str(item)).lower()
        if term:
            terms.add(term)
    return terms


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
    PROFILE_NAMES: dict[str, str] = {
        "conservative": "保守",
        "normal": "一般",
        "open": "开放",
        "very_open": "很开放",
    }

    def __init__(self, config: dict[str, Any]):
        self.scale: int = max(0, min(3, int(config.get("scale", 2))))
        raw_profile = config.get("profile", config.get("mode", config.get("level", "")))
        profile = self._normalize_profile(raw_profile)
        if not profile:
            profile = self._profile_from_scale(self.scale)
        self.profile: str = profile
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
        self._user_violation_timestamps: dict[str, list[datetime]] = {}  # 滑动窗口
        self._violation_window = timedelta(minutes=60)  # 60 分钟滑动窗口

        # ── 输出敏感词表（可通过指令动态增删）──
        self._output_sensitive: dict[str, str] = dict(_DEFAULT_OUTPUT_SENSITIVE)
        custom_output = config.get("output_sensitive_words", {})
        if isinstance(custom_output, dict):
            for k, v in custom_output.items():
                self._output_sensitive[str(k)] = str(v)

        self._custom_block_terms: set[str] = _normalize_term_list(
            config.get("custom_block_terms", [])
        )
        self._custom_allow_terms: set[str] = _normalize_term_list(
            config.get("custom_allow_terms", [])
        )

        # ── 群级别 profile 覆盖 ──
        self._group_profiles: dict[str, str] = {}
        group_overrides = config.get("group_profiles", {})
        if isinstance(group_overrides, dict):
            for gid, prof in group_overrides.items():
                normalized = self._normalize_profile(prof)
                if normalized:
                    self._group_profiles[str(gid).strip()] = normalized

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
        # 永久拦截：露骨/R18/裸体/未成年等成人内容（所有档位都拦截）
        self._sexual_explicit_terms: set[str] = {
            "pornhub", "porn", "r18", "18禁", "hentai", "nsfw", "xxx",
            "黄色网址", "成人网站", "黄网", "AV视频", "成人影片", "里番",
            "无码", "裸体", "露点", "露逼", "露阴", "露穴",
            "性交", "做爱", "口交", "肛交", "轮奸", "群交",
        }
        # 轻度性暗示（保守档会拦）
        self._sexual_suggestive_terms: set[str] = {
            "色色", "色一点", "暧昧", "调情", "挑逗", "情话", "性暗示",
        }
        # 轻微癖好（仅 very_open 放行）
        self._sexual_mild_kink_terms: set[str] = {
            "sm", "spank", "捆绑", "主奴", "dom", "sub", "调教",
        }
        self._intent_cues: set[str] = {
            "怎么买", "哪里买", "怎么做", "教程", "方法", "步骤",
            "教我", "给我", "渠道", "链接", "网址", "网站",
            "资源", "发送", "发给我", "下载", "how to", "where to",
        }
        self._sexual_request_cues: set[str] = {
            "来点", "说点", "写点", "讲点", "聊点", "想看", "想要",
            "扮演", "剧情", "台词", "角色扮演", "rp",
        }

        _log.info(
            "SafetyEngine 初始化: scale=%d (%s), profile=%s (%s), 输出敏感词=%d 条",
            self.scale,
            self.SCALE_NAMES.get(self.scale, "?"),
            self.profile,
            self.PROFILE_NAMES.get(self.profile, "?"),
            len(self._output_sensitive),
        )

    # ── 群级别 profile 管理 ──

    def get_effective_profile(self, group_id: str = "") -> str:
        """获取群的有效 profile，群级别覆盖优先于全局。"""
        gid = str(group_id).strip()
        if gid and gid in self._group_profiles:
            return self._group_profiles[gid]
        return self.profile

    def set_group_profile(self, group_id: str, profile: str) -> bool:
        """设置群级别的 NSFW profile 覆盖。"""
        normalized = self._normalize_profile(profile)
        if not normalized:
            return False
        self._group_profiles[str(group_id).strip()] = normalized
        return True

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
        # 露骨/R18 永久拦截（任何档位都不放开）
        if self._has_risky_term(content, self._sexual_explicit_terms):
            return "high_risk"
        if (
            self._custom_block_terms
            and not self._has_risky_term(content, self._custom_allow_terms)
            and self._has_risky_term(content, self._custom_block_terms)
        ):
            return "high_risk"

        # scale 0: 仅拦截绝对红线
        has_intent = any(cue in content for cue in self._intent_cues)
        sexual_intent = has_intent or self._has_sexual_request_intent(content)

        # profile: 保守档连轻度性暗示都拦截（需有请求意图）
        if (
            self.profile == "conservative"
            and sexual_intent
            and self._has_risky_term(content, self._sexual_suggestive_terms)
        ):
            return "high_risk"

        # 轻微癖好仅在 very_open 放行
        if (
            self.profile in {"conservative", "normal", "open"}
            and sexual_intent
            and self._has_risky_term(content, self._sexual_mild_kink_terms)
        ):
            return "high_risk"

        if self.scale == 0:
            return "safe"

        # scale 1: 违法 + 意图
        if self.scale >= 1:
            if self._has_risky_term(content, self._illegal_terms) and has_intent:
                return "illegal"

        # scale 2: + 自伤 + 意图
        if self.scale >= 2:
            if self._has_risky_term(content, self._self_harm_terms) and has_intent:
                return "high_risk"

        # scale 3: 所有敏感词直接拦截，不需要意图
        if self.scale >= 3:
            all_terms = (
                self._illegal_terms
                | self._self_harm_terms
                | self._sexual_explicit_terms
                | self._sexual_suggestive_terms
                | self._sexual_mild_kink_terms
            )
            if self._has_risky_term(content, all_terms):
                return "high_risk"

        return "safe"

    def _has_sexual_request_intent(self, content: str) -> bool:
        return any(cue in content for cue in self._sexual_request_cues)

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
            start = 0
            while True:
                idx = content.find(term, start)
                if idx < 0:
                    break
                after = content[idx + len(term):idx + len(term) + 4]
                if any(after.startswith(suffix) for suffix in _tech_suffixes):
                    start = idx + len(term)
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
        self.profile = self._profile_from_scale(self.scale)
        name = self.SCALE_NAMES.get(self.scale, "?")
        profile_name = self.PROFILE_NAMES.get(self.profile, self.profile)
        _log.info(
            "尺度已调整为 %d (%s), profile=%s (%s)",
            self.scale,
            name,
            self.profile,
            profile_name,
        )
        return f"尺度已设为 {self.scale} ({name})，档位 {profile_name}"

    def set_profile(self, profile: str) -> str:
        normalized = self._normalize_profile(profile)
        if not normalized:
            return "无效档位，请使用: 保守/一般/开放/很开放"
        self.profile = normalized
        profile_to_scale = {
            "conservative": 3,
            "normal": 2,
            "open": 1,
            "very_open": 0,
        }
        self.scale = profile_to_scale.get(self.profile, self.scale)
        profile_name = self.PROFILE_NAMES.get(self.profile, self.profile)
        scale_name = self.SCALE_NAMES.get(self.scale, str(self.scale))
        _log.info(
            "安全档位已调整: profile=%s (%s), scale=%d (%s)",
            self.profile,
            profile_name,
            self.scale,
            scale_name,
        )
        return f"安全档位已设为 {profile_name} (scale={self.scale}/{scale_name})"

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
        # 滑动窗口：只统计最近 60 分钟内的违规次数
        timestamps = self._user_violation_timestamps.setdefault(key, [])
        cutoff = now - self._violation_window
        # 清理窗口外的旧记录
        self._user_violation_timestamps[key] = [t for t in timestamps if t > cutoff]
        self._user_violation_timestamps[key].append(now)
        return len(self._user_violation_timestamps[key])

    @staticmethod
    def _looks_like_tech_or_compliance(content: str) -> bool:
        cues = (
            "接口", "api", "报错", "错误", "鉴权", "token",
            "参数", "模型", "请求", "合规", "安全", "风控",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _profile_from_scale(scale: int) -> str:
        return {
            3: "conservative",
            2: "normal",
            1: "open",
            0: "very_open",
        }.get(int(scale), "normal")

    @staticmethod
    def _normalize_profile(raw: Any) -> str:
        text = normalize_text(str(raw)).lower()
        if not text:
            return ""
        if text in {"保守", "conservative", "strict", "safe"}:
            return "conservative"
        if text in {"一般", "普通", "normal", "default", "balanced"}:
            return "normal"
        if text in {"开放", "open"}:
            return "open"
        if text in {"很开放", "较开放", "very_open", "very-open", "veryopen"}:
            return "very_open"
        return ""
