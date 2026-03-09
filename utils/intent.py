"""Legacy intent helpers.

项目已切到“纯 AI 路由”模式，这里仅保留兼容函数签名，
不再执行任何本地关键词 / 正则启发式判断。
"""
from __future__ import annotations

from typing import Any


def looks_like_video_request(text: str, config: dict[str, Any] | None = None) -> bool:
    _ = (text, config)
    return False


def looks_like_github_request(text: str, config: dict[str, Any] | None = None) -> bool:
    _ = (text, config)
    return False


def looks_like_repo_readme_request(text: str, config: dict[str, Any] | None = None) -> bool:
    _ = (text, config)
    return False


def looks_like_qq_avatar_intent(text: str, config: dict[str, Any] | None = None) -> bool:
    _ = (text, config)
    return False


def looks_like_qq_profile_analysis_request(text: str, config: dict[str, Any] | None = None) -> bool:
    _ = (text, config)
    return False
