"""engine.py 公共类型定义。

从 core/engine.py 拆分。包含:
- EngineMessage: 引擎输入消息
- EngineResponse: 引擎输出响应
- PluginSetupContext: 插件初始化上下文
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EngineMessage:

    conversation_id: str

    user_id: str

    text: str

    user_name: str = ""

    message_id: str = ""

    seq: int = 0

    raw_segments: list[dict[str, Any]] = field(default_factory=list)

    queue_depth: int = 0

    mentioned: bool = False

    is_private: bool = False

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    group_id: int = 0

    bot_id: str = ""

    at_other_user_only: bool = False

    at_other_user_ids: list[str] = field(default_factory=list)

    reply_to_message_id: str = ""

    reply_to_user_id: str = ""

    reply_to_user_name: str = ""

    reply_to_text: str = ""

    reply_media_segments: list[dict[str, Any]] = field(default_factory=list)

    api_call: Callable[..., Awaitable[Any]] | None = None

    trace_id: str = ""

    sender_role: str = ""  # "owner" / "admin" / "member" — 来自 OneBot sender.role

    # 原始 OneBot/NapCat 事件快照（用于 Agent 上下文和工具侧高级判断）
    event_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EngineResponse:

    action: str

    reason: str

    reply_text: str = ""

    image_url: str = ""

    image_urls: list[str] = field(default_factory=list)

    video_url: str = ""

    cover_url: str = ""

    record_b64: str = ""

    audio_file: str = ""

    pre_ack: str = ""

    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginSetupContext:
    """Dependencies injected into plugins during setup()."""

    model_client: Any = None

    config: dict[str, Any] = field(default_factory=dict)

    logger: Any = None

    storage_dir: Path | None = None

    agent_tool_registry: Any = None
