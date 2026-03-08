from __future__ import annotations

import re
from typing import Iterable

from utils.text import normalize_text

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_WRAP_TOKEN_PATTERN = re.compile(r"https?://[^\s]+|[^\s]+", re.IGNORECASE)


def split_semantic_text(
    text: str,
    max_lines: int = 3,
    max_chars: int = 220,
    max_chunks: int = 6,
) -> list[str]:
    """Split reply text by semantic sections first, then by sentence length."""
    if max_lines <= 0 or max_chars <= 0 or max_chunks <= 0:
        return []

    normalized = _normalize_reply_text(text)
    if not normalized:
        return []

    sections = _split_sections(normalized)
    chunks: list[str] = []
    for section in sections:
        for chunk in _split_section(section, max_lines=max_lines, max_chars=max_chars):
            if chunk.strip():
                chunks.append(chunk.strip())
    if len(chunks) <= max_chunks:
        return chunks
    return _limit_chunks_preserve_tail(
        chunks=chunks,
        max_chunks=max_chunks,
    )


def coalesce_for_rate_limit(
    chunks: list[str],
    max_chars: int = 260,
    short_chunk_chars: int = 80,
) -> list[str]:
    """Merge short neighbor chunks to reduce message count when rate-limited."""
    if not chunks:
        return []
    merged: list[str] = []
    pending = ""
    for chunk in chunks:
        c = normalize_text(chunk)
        if not c:
            continue
        if not pending:
            pending = c
            continue
        should_merge = len(pending) <= short_chunk_chars or len(c) <= short_chunk_chars
        candidate = f"{pending}\n{c}"
        if should_merge and len(candidate) <= max_chars:
            pending = candidate
            continue
        merged.append(pending)
        pending = c
    if pending:
        merged.append(pending)
    return merged


def _split_sections(text: str) -> list[str]:
    parts = [seg.strip() for seg in re.split(r"\n\s*\n+", text) if seg.strip()]
    if not parts:
        return [text]
    out: list[str] = []
    for part in parts:
        # 把步骤型内容拆成独立 section，提升“解释/步骤/总结”的可读性。
        lines = [line.strip() for line in part.splitlines() if line.strip()]
        if len(lines) <= 1:
            out.append(part)
            continue
        step_like = 0
        for line in lines:
            if re.match(r"^(?:\d+[.)、]|[-*•])\s*", line):
                step_like += 1
        if step_like >= 2:
            out.extend(lines)
        else:
            out.append(part)
    return out


def _split_section(section: str, max_lines: int, max_chars: int) -> list[str]:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    if not lines:
        return []
    tokens: list[str] = []
    for line in lines:
        tokens.extend(_split_line_by_sentence(line, max_chars=max_chars))

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for token in tokens:
        projected_lines = len(current) + 1
        projected_len = current_len + (1 if current else 0) + len(token)
        if current and (projected_lines > max_lines or projected_len > max_chars):
            chunks.append("\n".join(current))
            current = [token]
            current_len = len(token)
            continue
        current.append(token)
        current_len = projected_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _split_line_by_sentence(line: str, max_chars: int) -> list[str]:
    text = normalize_text(line)
    if not text:
        return []
    url_tokens: dict[str, str] = {}

    def _mask_url(match: re.Match[str]) -> str:
        key = f"URLTOKEN{len(url_tokens)}PLACEHOLDER"
        url_tokens[key] = match.group(0)
        return key

    masked_text = _URL_PATTERN.sub(_mask_url, text)
    segments = [seg.strip() for seg in re.split(r"(?<=[。！？!?；;])", masked_text) if seg.strip()]
    if url_tokens:
        restored_segments: list[str] = []
        for seg in segments:
            restored = seg
            for key, value in url_tokens.items():
                restored = restored.replace(key, value)
            restored_segments.append(restored)
        segments = restored_segments
    if len(segments) <= 1:
        return _hard_wrap(text, max_chars)

    out: list[str] = []
    current = ""
    for seg in segments:
        if not current:
            current = seg
            if len(current) > max_chars:
                out.extend(_hard_wrap(current, max_chars))
                current = ""
            continue
        candidate = f"{current}{seg}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        out.append(current)
        current = seg
        if len(current) > max_chars:
            out.extend(_hard_wrap(current, max_chars))
            current = ""
    if current:
        out.append(current)
    return out


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    content = normalize_text(text)
    if not content:
        return []
    tokens = _WRAP_TOKEN_PATTERN.findall(content)
    if not tokens:
        return [content]

    out: list[str] = []
    current = ""
    for token in tokens:
        if _URL_PATTERN.fullmatch(token):
            if current:
                out.append(current)
                current = ""
            out.append(token)
            continue
        if not current:
            current = token
            continue
        candidate = f"{current} {token}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        out.append(current)
        current = token
    if current:
        out.append(current)
    return [row for row in out if normalize_text(row)]


def _limit_chunks_preserve_tail(chunks: list[str], max_chunks: int) -> list[str]:
    if max_chunks <= 0:
        return []
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks == 1:
        merged = normalize_text("\n".join(chunks))
        return [merged] if merged else []
    head = chunks[: max_chunks - 1]
    tail = normalize_text("\n".join(chunks[max_chunks - 1 :]))
    if tail:
        head.append(tail)
    return [row for row in head if normalize_text(row)]


def _normalize_reply_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""
    lines: list[str] = []
    blank = False
    for line in raw.split("\n"):
        clean = line.strip()
        if clean:
            lines.append(clean)
            blank = False
            continue
        if lines and not blank:
            lines.append("")
            blank = True
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
