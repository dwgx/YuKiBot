from __future__ import annotations

import re
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
