from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n]+")
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}")
_MARKDOWN_SYMBOLS = re.compile(r"[`*_~]")
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)
_PREFERRED_KAOMOJI = ("QWQ", "AWA", "OwO", "UwU", "QAQ", ">_<")
_PREFERRED_KAOMOJI_PATTERN = re.compile(
    r"\b(?:QWQ|AWA|OWO|UWU|QAQ|TAT|XD)\b|>_<",
    flags=re.IGNORECASE,
)
_BRACKET_KAOMOJI_PATTERN = re.compile(r"\((?=[^)]*[^\w\s])[^\n()]{1,20}\)")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


@lru_cache(maxsize=1)
def _get_opencc():
    try:
        from opencc import OpenCC

        return OpenCC("t2s")
    except Exception:
        return None


def _to_simplified(text: str) -> str:
    converter = _get_opencc()
    if converter is None:
        return text
    try:
        return str(converter.convert(text))
    except Exception:
        return text


def normalize_matching_text(text: str) -> str:
    """规范化用于匹配的文本（更激进的清理）。"""
    normalized = _to_simplified(normalize_text(text))
    # 移除常见的标点和特殊字符，保留字母数字和中文
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _compact_for_match(text: str) -> str:
    return re.sub(r"[\s\-\_·•./|\\,，;；:&()（）\[\]{}]+", "", normalize_matching_text(text).lower())


def has_unrequested_title_qualifier(actual_title: str, requested_title: str) -> bool:
    """判断实际标题是否包含了用户未请求的后缀/版本信息。"""
    actual_compact = _compact_for_match(actual_title)
    requested_compact = _compact_for_match(requested_title)

    # 单字请求极易误判（例如「回」命中「回到」），直接放行。
    if not actual_compact or len(requested_compact) < 2:
        return False
    if requested_compact not in actual_compact:
        return False
    if requested_compact == actual_compact:
        return False

    remainder = actual_compact.replace(requested_compact, "", 1)
    if not remainder:
        return False

    requested_tokens = set(tokenize(normalize_matching_text(requested_title)))
    remainder_tokens = set(tokenize(remainder))
    if requested_tokens and remainder_tokens and remainder_tokens.issubset(requested_tokens):
        return False
    return True


def tokenize(text: str) -> list[str]:
    return [item.lower() for item in _TOKEN_PATTERN.findall(text or "")]


def extract_sentence_starts(text: str, max_sentences: int = 3, max_chars: int = 18) -> list[str]:
    starts: list[str] = []
    for part in _SPLIT_PATTERN.split(text or ""):
        clean = normalize_text(part)
        if clean:
            starts.append(clean[:max_chars])
        if len(starts) >= max_sentences:
            break
    return starts


def clip_text(text: str, max_len: int) -> str:
    clean = text or ""
    if max_len <= 0:
        return ""
    if len(clean) <= max_len:
        return clean
    if max_len <= 3:
        return clean[:max_len]
    return clean[: max_len - 3] + "..."


def remove_markdown(text: str) -> str:
    no_symbols = _MARKDOWN_SYMBOLS.sub("", text or "")
    no_links = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", no_symbols)
    no_headers = re.sub(r"^\s*#+\s*", "", no_links, flags=re.MULTILINE)
    return no_headers


def replace_emoji_with_kaomoji(text: str, kaomoji: str = "QWQ") -> str:
    content = str(text or "")
    has_emoji = bool(_EMOJI_PATTERN.search(content))
    cleaned = _EMOJI_PATTERN.sub("", content)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if not has_emoji:
        return cleaned
    if not cleaned:
        return kaomoji
    return f"{cleaned} {kaomoji}"


def normalize_kaomoji_style(text: str, default: str = "QWQ") -> str:
    content = str(text or "")
    preferred_hits = _PREFERRED_KAOMOJI_PATTERN.findall(content)
    has_bracket_kaomoji = bool(_BRACKET_KAOMOJI_PATTERN.search(content))
    keep = ""

    if preferred_hits:
        keep = preferred_hits[0]
    elif has_bracket_kaomoji:
        keep = default

    # 删除旧式括号颜文字和过量颜文字 token，最后统一只保留一个
    content = _BRACKET_KAOMOJI_PATTERN.sub("", content)
    content = _PREFERRED_KAOMOJI_PATTERN.sub("", content)
    content = re.sub(r"[ \t]{2,}", " ", content).strip()

    if keep:
        return f"{content} {keep}".strip()
    return content


def join_nonempty(lines: Iterable[str], sep: str = "\n") -> str:
    return sep.join(item for item in lines if item)
