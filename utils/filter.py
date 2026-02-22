from __future__ import annotations

from collections import Counter

from .text import tokenize


STOP_WORDS = {
    "这个",
    "那个",
    "一下",
    "一个",
    "我们",
    "你们",
    "他们",
    "然后",
    "就是",
    "但是",
    "because",
    "that",
    "with",
    "from",
}


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lower = (text or "").lower()
    hits: list[str] = []
    for keyword in keywords:
        key = (keyword or "").strip()
        if not key:
            continue
        if key.lower() in lower:
            hits.append(key)
    return hits


def contains_any_keyword(text: str, keywords: list[str]) -> bool:
    return bool(matched_keywords(text, keywords))


def top_keywords_from_texts(texts: list[str], top_n: int = 10) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        for token in tokenize(text):
            if token in STOP_WORDS:
                continue
            counter[token] += 1
    return counter.most_common(top_n)

