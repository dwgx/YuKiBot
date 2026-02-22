from __future__ import annotations

from typing import Any

from utils.text import clip_text, remove_markdown


class MarkdownRenderer:
    def __init__(self, config: dict[str, Any], enabled: bool = True):
        self.enabled = bool(enabled)
        self.enable_code_highlight = bool(config.get("enable_code_highlight", True))
        self.enable_quote_style = bool(config.get("enable_quote_style", True))
        self.enable_table = bool(config.get("enable_table", True))

    def render(self, text: str, max_len: int = 2000) -> str:
        content = text or ""
        if not self.enabled:
            content = remove_markdown(content)
        return clip_text(content, max_len=max_len)

