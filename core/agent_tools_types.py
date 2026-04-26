"""Agent 工具公共类型定义。

从 agent_tools.py 拆分，所有模块共享的数据结构和类型别名。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolSchema:
    """描述一个可被 Agent 调用的工具。"""
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    category: str = "general"  # general / napcat / search / media / admin
    group: str = ""  # backward-compat metadata only; not used for local intent routing


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    display: str = ""  # 给 LLM 看的摘要


@dataclass(slots=True)
class PromptHint:
    """插件注入到 Agent 系统提示的静态文本块。

    section:
        - "rules": 出现在 ## 规则 区域
        - "tools_guidance": 出现在 ## 工具使用指南 区域
        - "context": 出现在 ## 上下文 区域
    priority: 数字越小越靠前，默认 50
    """
    source: str
    section: str
    content: str
    priority: int = 50
    tool_names: tuple[str, ...] = ()


ToolHandler = Callable[..., Awaitable[ToolCallResult]]
ContextProvider = Callable[[dict[str, Any]], str | Awaitable[str]]
