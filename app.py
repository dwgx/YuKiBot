from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from core.engine import EngineMessage, YukikoEngine


def create_engine() -> YukikoEngine:
    root = Path(__file__).resolve().parent
    return YukikoEngine.from_default_paths(project_root=root)


def register_handlers(engine: YukikoEngine) -> None:
    router = on_message(priority=90, block=False)

    @router.handle()
    async def handle_message(bot: Bot, event: MessageEvent) -> None:
        if str(event.get_user_id()) == str(bot.self_id):
            return

        text = event.get_plaintext().strip()
        if not text:
            return

        payload = EngineMessage(
            conversation_id=_build_conversation_id(event),
            user_id=str(event.get_user_id()),
            text=text,
            mentioned=_is_mentioned(bot, event),
            is_private=getattr(event, "message_type", "") == "private",
            timestamp=_event_timestamp(event),
        )
        result = await engine.handle_message(payload)
        if result.action == "ignore":
            return

        output = Message()
        if result.reply_text:
            output += Message(result.reply_text)
        if result.image_url:
            output += MessageSegment.image(result.image_url)
        if output:
            await router.finish(output)


def _event_timestamp(event: MessageEvent) -> datetime:
    ts = getattr(event, "time", None)
    if isinstance(ts, int):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _build_conversation_id(event: MessageEvent) -> str:
    msg_type = getattr(event, "message_type", "")
    user_id = str(event.get_user_id())
    if msg_type == "group":
        group_id = getattr(event, "group_id", 0)
        return f"group:{group_id}"
    if msg_type == "private":
        return f"private:{user_id}"
    return f"{msg_type}:{user_id}"


def _is_mentioned(bot: Bot, event: MessageEvent) -> bool:
    if bool(getattr(event, "to_me", False)):
        return True

    for segment in event.get_message():
        if segment.type != "at":
            continue
        target = str(segment.data.get("qq", ""))
        if target in {"all", str(bot.self_id)}:
            return True
    return False

