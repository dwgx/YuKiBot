"""共享意图识别函数。

核心目标：
1) 避免 engine/router/tools/agent 多处维护重复硬编码。
2) 支持从 config.intent 覆盖关键词与正则，而不是写死在代码里。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from utils.text import normalize_text

_INTENT_DEFAULTS: dict[str, Any] = {
    "video_request_cues": [
        "视频",
        "影片",
        "发视频",
        "找视频",
        "video",
        "clip",
        "mv",
        ".mp4",
        ".webm",
        ".mov",
        ".m4v",
        "抖音",
        "快手",
        "b站",
        "哔哩",
        "bilibili",
        "acfun",
        "a站",
    ],
    "video_request_regexes": [
        r"\b(?:BV[a-zA-Z0-9]{10}|av\d{4,})\b",
    ],
    "github_request_cues": [
        "github",
        "git hub",
        "开源",
        "仓库",
        "repo",
        "repository",
        "源码",
        "source code",
    ],
    "github_request_regexes": [
        r"https?://(?:www\.)?github\.com/[^\s]+",
    ],
    "repo_readme_request_cues": [
        "readme",
        "文档",
        "学习",
        "分析",
        "怎么用",
        "怎么跑",
        "看下这个仓库",
        "看这个项目",
    ],
    "qq_avatar_intent_cues": [
        "头像",
        "头像图",
        "qq头像",
        "qlogo",
    ],
    "qq_profile_domain_cues": [
        "qq",
        "qzone",
        "空间",
        "头像",
        "资料卡",
        "控件",
        "动态",
        "相册",
        "企鹅号",
    ],
    "qq_profile_intent_cues": [
        "分析",
        "查",
        "查下",
        "查一下",
        "看看",
        "资料",
        "信息",
        "是谁",
        "头像",
        "空间",
        "qzone",
    ],
    "qq_profile_direct_hit_cues": [
        "空间",
        "头像",
        "资料卡",
        "控件",
        "动态",
        "相册",
        "qzone",
    ],
    "qq_profile_qq_with_intent_cues": [
        "分析",
        "查",
        "看",
        "资料",
        "信息",
    ],
}


def _heuristic_rules_enabled(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return False
    control = config.get("control", {})
    if not isinstance(control, dict):
        return False
    return bool(control.get("heuristic_rules_enable", False))


def _intent_section(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    section = config.get("intent", {})
    return section if isinstance(section, dict) else {}


def _normalize_list(values: Iterable[Any], *, lowercase: bool = True) -> list[str]:
    result: list[str] = []
    for item in values:
        text = normalize_text(str(item))
        if not text:
            continue
        result.append(text.lower() if lowercase else text)
    return result


def _get_cues(
    config: dict[str, Any] | None,
    key: str,
    *,
    lowercase: bool = True,
) -> list[str]:
    defaults = _INTENT_DEFAULTS.get(key, [])
    base = defaults if isinstance(defaults, list) else []
    section = _intent_section(config)
    raw = section.get(key, base)
    if not isinstance(raw, list):
        raw = base
    cues = _normalize_list(raw, lowercase=lowercase)
    return cues or _normalize_list(base, lowercase=lowercase)


def _match_any_pattern(content: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, content, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def looks_like_video_request(text: str, config: dict[str, Any] | None = None) -> bool:
    """检测是否为视频相关请求。"""
    if not _heuristic_rules_enabled(config):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    cues = _get_cues(config, "video_request_cues")
    if any(cue in content for cue in cues):
        return True
    patterns = _get_cues(config, "video_request_regexes", lowercase=False)
    return _match_any_pattern(content, patterns)


def looks_like_github_request(text: str, config: dict[str, Any] | None = None) -> bool:
    """检测是否为 GitHub 相关请求。"""
    if not _heuristic_rules_enabled(config):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    cues = _get_cues(config, "github_request_cues")
    if any(cue in content for cue in cues):
        return True
    patterns = _get_cues(config, "github_request_regexes", lowercase=False)
    return _match_any_pattern(content, patterns)


def looks_like_repo_readme_request(text: str, config: dict[str, Any] | None = None) -> bool:
    """检测是否为仓库 README/文档请求。"""
    if not _heuristic_rules_enabled(config):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    cues = _get_cues(config, "repo_readme_request_cues")
    return any(cue in content for cue in cues)


def looks_like_qq_avatar_intent(text: str, config: dict[str, Any] | None = None) -> bool:
    """检测是否为 QQ 头像请求。"""
    if not _heuristic_rules_enabled(config):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    cues = _get_cues(config, "qq_avatar_intent_cues")
    return any(cue in content for cue in cues)


def looks_like_qq_profile_analysis_request(text: str, config: dict[str, Any] | None = None) -> bool:
    """检测是否为 QQ 资料分析/查询请求。"""
    if not _heuristic_rules_enabled(config):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    direct_hit_cues = _get_cues(config, "qq_profile_direct_hit_cues")
    if any(cue in content for cue in direct_hit_cues):
        return True
    domain_cues = _get_cues(config, "qq_profile_domain_cues")
    intent_cues = _get_cues(config, "qq_profile_intent_cues")
    if any(cue in content for cue in domain_cues) and any(cue in content for cue in intent_cues):
        return True
    qq_with_intent_cues = _get_cues(config, "qq_profile_qq_with_intent_cues")
    return bool(re.search(r"\bqq\b", content) and any(cue in content for cue in qq_with_intent_cues))
