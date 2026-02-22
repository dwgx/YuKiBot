from __future__ import annotations

import re
from typing import Iterable


_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n]+")
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}")
_MARKDOWN_SYMBOLS = re.compile(r"[`*_~]")


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
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3] + "..."


def remove_markdown(text: str) -> str:
    no_symbols = _MARKDOWN_SYMBOLS.sub("", text or "")
    no_blocks = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", no_symbols)
    no_headers = re.sub(r"^\s*#+\s*", "", no_blocks, flags=re.MULTILINE)
    return no_headers


def join_nonempty(lines: Iterable[str], sep: str = "\n") -> str:
    return sep.join(item for item in lines if item)

