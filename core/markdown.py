from __future__ import annotations

from typing import Any

from utils.text import clip_text, remove_markdown


class MarkdownRenderer:
    def __init__(self, config: dict[str, Any], enabled: bool = True):
        self.enabled = bool(enabled)
        self.enable_code_highlight = bool(config.get("enable_code_highlight", True))
        self.enable_quote_style = bool(config.get("enable_quote_style", True))
        self.enable_table = bool(config.get("enable_table", True))
        self.max_output_chars = max(80, int(config.get("max_output_chars", 260)))
        self.max_output_lines = max(1, int(config.get("max_output_lines", 4)))
        self.collapse_blank_lines = bool(config.get("collapse_blank_lines", True))

    def render(
        self,
        text: str,
        max_len: int | None = None,
        max_lines: int | None = None,
    ) -> str:
        content = text or ""
        if not self.enabled:
            content = remove_markdown(content)

        if self.collapse_blank_lines and "```" not in content:
            content = self._normalize_blank_lines(content)

        limit = int(max_len) if max_len is not None else self.max_output_chars
        content = clip_text(content, max_len=limit)

        line_limit = int(max_lines) if max_lines is not None else self.max_output_lines
        if "```" not in content:
            content = self._limit_lines(content, line_limit)
        return content

    @staticmethod
    def _normalize_blank_lines(text: str) -> str:
        lines = [line.strip() for line in text.splitlines()]
        normalized = [line for line in lines if line]
        return "\n".join(normalized)

    @staticmethod
    def _limit_lines(text: str, max_lines: int) -> str:
        if max_lines <= 0:
            return text
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        kept = lines[:max_lines]
        last = kept[-1].rstrip()
        if last and not last.endswith("..."):
            kept[-1] = last + "..."
        elif not last:
            kept[-1] = "..."
        return "\n".join(kept)
