from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from utils.text import normalize_text, strip_invisible_format_chars

_PREFERRED_NAME_SENTENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:请|麻烦)?(?:记住|记一下|记好了|给我记住|帮我记住|记得)(?:[，,:：\s]*)"
        r"(?:(?:以后|之后|从现在开始)(?:[都就统一]*)?)?"
        r"(?:叫我|喊我|称呼我|管我叫)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:(?:以后|之后|从现在开始)(?:[都就统一]*)?)"
        r"(?:叫我|喊我|称呼我|管我叫)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:我的名字是|我名字是|我叫|我是)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
)
_PREFERRED_NAME_TITLE_CUES = (
    "用户称呼偏好",
    "偏好称呼",
    "称呼偏好",
    "preferred_name",
    "nickname_preference",
    "用户昵称",
)
_NON_SERIOUS_TEXT_CUES = (
    "哈哈",
    "笑死",
    "整活",
    "玩梗",
    "起哄",
    "扣1",
    "刷起来",
    "绷不住",
    "乐子",
    "doge",
    "233",
    "666",
)
_COLLECTIVE_NAME_CUES = (
    "以后都叫我",
    "以后大家叫我",
    "以后你们叫我",
    "以后全都叫我",
    "全体叫我",
    "所有人叫我",
    "群里叫我",
    "都喊我",
    "都称呼我",
)
_GROUP_ROLEPLAY_NAMES = frozenset(
    {
        "妈妈",
        "爸爸",
        "爹",
        "爷爷",
        "奶奶",
        "老公",
        "老婆",
        "主人",
        "宝贝",
        "宝宝",
        "儿子",
        "孙子",
        "皇上",
        "女王",
    }
)
_CANDIDATE_STOP_CUES = (
    "以后",
    "记住",
    "帮我",
    "谢谢",
    "求你",
    "行吗",
    "可以吗",
    "懂吗",
    "知道吗",
)
_QUESTION_NAME_CUES = frozenset(
    {
        "什么",
        "啥",
        "谁",
        "叫什么",
        "叫啥",
        "啥名",
        "什么名",
        "什么名字",
        "啥名字",
        "哪位",
    }
)


@dataclass(frozen=True, slots=True)
class PreferredNameDecision:
    allow: bool
    candidate: str = ""
    reason: str = ""


def _contains_alias(text: str, bot_aliases: Iterable[str]) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    aliases = [normalize_text(str(item)).lower() for item in bot_aliases if normalize_text(str(item))]
    if not aliases:
        return False
    for alias in aliases:
        if len(alias) == 1 and "\u4e00" <= alias <= "\u9fff":
            pattern = rf"(?<![a-z0-9\u4e00-\u9fff]){re.escape(alias)}(?![a-z0-9\u4e00-\u9fff])"
            if re.search(pattern, content):
                return True
            continue
        if alias in content:
            return True
    compacted = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)
    if not compacted:
        return False
    return any(alias and len(alias) > 1 and alias in compacted for alias in aliases)


def _clean_candidate(raw: str) -> str:
    candidate = strip_invisible_format_chars(normalize_text(raw))
    if not candidate:
        return ""
    candidate = re.sub(r"^[\s\"'“”‘’《》〈〉【】\[\]\(\)（）,，。:：;；!！?？~～]+", "", candidate)
    candidate = re.sub(r"[\s\"'“”‘’《》〈〉【】\[\]\(\)（）,，。:：;；!！?？~～]+$", "", candidate)
    candidate = re.sub(r"^(?:叫做|叫作|叫成|称作)", "", candidate)
    candidate = re.sub(r"(?:吧|哈|啊|呀|哦|奥|噢|啦|嘛|呗|捏)+$", "", candidate)
    candidate = strip_invisible_format_chars(normalize_text(candidate))
    if not candidate or len(candidate) > 24:
        return ""
    lowered = candidate.lower()
    if lowered in _QUESTION_NAME_CUES:
        return ""
    if any(cue in candidate for cue in _CANDIDATE_STOP_CUES):
        return ""
    if any(cue in lowered for cue in ("什么", "啥", "谁")):
        return ""
    if candidate.endswith(("吗", "嘛", "呢")):
        return ""
    if re.search(r"[，,。.!！?？:：;；/\\]", candidate):
        return ""
    return candidate


def extract_explicit_preferred_name(text: str) -> str:
    content = strip_invisible_format_chars(normalize_text(text))
    if not content:
        return ""
    if "?" in content or "？" in content:
        return ""
    for pattern in _PREFERRED_NAME_SENTENCE_PATTERNS:
        match = pattern.match(content)
        if not match:
            continue
        candidate = _clean_candidate(match.group("name"))
        if candidate:
            return candidate
    return ""


def looks_like_non_serious_name_context(text: str) -> bool:
    content = strip_invisible_format_chars(normalize_text(text)).lower()
    if not content:
        return False
    if any(cue in content for cue in _COLLECTIVE_NAME_CUES):
        return True
    if any(cue in content for cue in _NON_SERIOUS_TEXT_CUES):
        return True
    if re.search(r"(?:全体|大家|你们|群友|兄弟们|都给我).{0,12}(?:叫我|喊我|称呼我)", content):
        return True
    if re.search(r"(?:哈哈|233|666|doge).{0,10}$", content):
        return True
    return False


def is_safe_user_profile_learning_context(
    text: str,
    *,
    is_private: bool,
    mentioned: bool = False,
    explicit_bot_addressed: bool = False,
    bot_aliases: Iterable[str] = (),
    at_other_user_ids: Iterable[str] = (),
    reply_to_user_id: str = "",
    bot_id: str = "",
) -> bool:
    if looks_like_non_serious_name_context(text):
        return False
    directed = bool(
        is_private
        or mentioned
        or explicit_bot_addressed
        or _contains_alias(text, bot_aliases)
    )
    if not is_private and not directed:
        return False
    if not is_private and any(normalize_text(str(item)) for item in at_other_user_ids):
        return False
    reply_uid = normalize_text(str(reply_to_user_id))
    bot_uid = normalize_text(str(bot_id))
    if not is_private and reply_uid and bot_uid and reply_uid != bot_uid:
        return False
    return True


def assess_preferred_name_learning(
    text: str,
    *,
    is_private: bool,
    mentioned: bool = False,
    explicit_bot_addressed: bool = False,
    bot_aliases: Iterable[str] = (),
    at_other_user_ids: Iterable[str] = (),
    reply_to_user_id: str = "",
    bot_id: str = "",
) -> PreferredNameDecision:
    candidate = extract_explicit_preferred_name(text)
    if not candidate:
        return PreferredNameDecision(False, reason="missing_explicit_name_statement")
    safe_context = is_safe_user_profile_learning_context(
        text,
        is_private=is_private,
        mentioned=mentioned,
        explicit_bot_addressed=explicit_bot_addressed,
        bot_aliases=bot_aliases,
        at_other_user_ids=at_other_user_ids,
        reply_to_user_id=reply_to_user_id,
        bot_id=bot_id,
    )
    if not safe_context:
        if looks_like_non_serious_name_context(text):
            return PreferredNameDecision(False, candidate=candidate, reason="non_serious_context")
        reply_uid = normalize_text(str(reply_to_user_id))
        bot_uid = normalize_text(str(bot_id))
        if not is_private and any(normalize_text(str(item)) for item in at_other_user_ids):
            return PreferredNameDecision(False, candidate=candidate, reason="group_at_other_users")
        if not is_private and reply_uid and bot_uid and reply_uid != bot_uid:
            return PreferredNameDecision(False, candidate=candidate, reason="group_reply_to_other")
        return PreferredNameDecision(False, candidate=candidate, reason="group_not_directed")
    if not is_private and candidate in _GROUP_ROLEPLAY_NAMES:
        return PreferredNameDecision(False, candidate=candidate, reason="group_roleplay_name")
    return PreferredNameDecision(True, candidate=candidate, reason="ok")


def looks_like_preferred_name_knowledge(title: str, content: str, tags: Iterable[str] = ()) -> bool:
    merged = " ".join(
        [
            strip_invisible_format_chars(normalize_text(title)).lower(),
            strip_invisible_format_chars(normalize_text(content)).lower(),
            " ".join(strip_invisible_format_chars(normalize_text(str(item))).lower() for item in tags),
        ]
    )
    if any(cue in merged for cue in _PREFERRED_NAME_TITLE_CUES):
        return True
    return bool(extract_explicit_preferred_name(content))
