from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

_logger = logging.getLogger("yukiko.queue")


@dataclass(slots=True)
class QueueDispatchResult:
    status: str
    reason: str
    conversation_id: str
    seq: int
    pending_count: int = 0
    trace_id: str = ""


@dataclass(slots=True)
class _CompletedItem:
    response: Any | None
    send: Callable[[Any], Awaitable[None]]
    dispatch: QueueDispatchResult
    on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None = None


@dataclass(slots=True)
class _GroupState:
    semaphore: asyncio.Semaphore
    buffer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seq_counter: int = 0
    next_emit_seq: int = 1
    pending_count: int = 0
    completed: dict[int, _CompletedItem] = field(default_factory=dict)
    last_overload_notice_at: datetime | None = None


class GroupQueueDispatcher:
    def __init__(self, config: dict[str, Any]):
        self.group_concurrency = max(1, int(config.get("group_concurrency", 2)))
        self.max_pending_per_group = max(1, int(config.get("max_pending_per_group", 80)))
        self.message_ttl = timedelta(seconds=max(1, int(config.get("message_ttl_seconds", 35))))
        raw_policy = str(config.get("send_policy", "")).strip().lower()
        if raw_policy in {"strict_order", "latest_ready"}:
            self.send_policy = raw_policy
        else:
            # 兼容旧配置
            self.send_policy = "strict_order" if bool(config.get("send_in_order", True)) else "latest_ready"
        self.send_in_order = self.send_policy == "strict_order"
        self.process_timeout_seconds = max(1, int(config.get("process_timeout_seconds", 120)))
        self.late_emit_timeout_seconds = max(20, int(config.get("late_emit_timeout_seconds", 210)))
        self.overload_notice_cooldown = timedelta(
            seconds=max(5, int(config.get("overload_notice_cooldown_seconds", 30)))
        )
        self.overload_notice_text = str(
            config.get(
                "overload_notice_text",
                "你们等等呀，我回复不过来了。请 @我 或叫我的名字（雪 / yukiko），我会优先回你。",
            )
        )
        self._groups: dict[str, _GroupState] = {}

    def next_seq(self, conversation_id: str) -> int:
        state = self._state(conversation_id)
        state.seq_counter += 1
        return state.seq_counter

    def pending_count(self, conversation_id: str) -> int:
        return self._state(conversation_id).pending_count

    async def submit(
        self,
        conversation_id: str,
        seq: int,
        created_at: datetime,
        process: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
        high_priority: bool = False,
        process_timeout_seconds: int | None = None,
        allow_late_emit_on_timeout: bool = False,
        trace_id: str = "",
        send_overload_notice: Callable[[str], Awaitable[None]] | None = None,
        on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None = None,
    ) -> QueueDispatchResult:
        state = self._state(conversation_id)
        if not high_priority and state.pending_count >= self.max_pending_per_group:
            notice_sent = await self._try_send_overload_notice(state, send_overload_notice)
            dropped = QueueDispatchResult(
                status="dropped",
                reason="queue_overload_notice" if notice_sent else "queue_overload",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dropped, on_complete)
            return dropped

        state.pending_count += 1
        asyncio.create_task(
            self._run_item(
                conversation_id=conversation_id,
                seq=seq,
                created_at=created_at,
                process=process,
                send=send,
                process_timeout_seconds=process_timeout_seconds,
                allow_late_emit_on_timeout=allow_late_emit_on_timeout,
                trace_id=trace_id,
                on_complete=on_complete,
            )
        )
        return QueueDispatchResult(
            status="queued",
            reason="accepted",
            conversation_id=conversation_id,
            seq=seq,
            pending_count=state.pending_count,
            trace_id=trace_id,
        )

    async def _run_item(
        self,
        conversation_id: str,
        seq: int,
        created_at: datetime,
        process: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
        process_timeout_seconds: int | None = None,
        allow_late_emit_on_timeout: bool = False,
        trace_id: str = "",
        on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None = None,
    ) -> None:
        state = self._state(conversation_id)
        response: Any | None = None
        status = "sent"
        reason = "ok"
        timeout_seconds = self.process_timeout_seconds
        if isinstance(process_timeout_seconds, (int, float)) and process_timeout_seconds > 0:
            timeout_seconds = max(1, int(process_timeout_seconds))
        process_task: asyncio.Task[Any] | None = None

        try:
            async with state.semaphore:
                if self._is_expired(created_at):
                    status = "expired"
                    reason = "message_ttl_expired"
                else:
                    process_task = asyncio.create_task(process())
                    response = await asyncio.wait_for(asyncio.shield(process_task), timeout=timeout_seconds)
        except TimeoutError:
            if allow_late_emit_on_timeout and process_task is not None and not process_task.done():
                status = "process_timeout_deferred"
                reason = "process_timeout_deferred"
                self._schedule_late_emit(
                    process_task=process_task,
                    send=send,
                    conversation_id=conversation_id,
                    seq=seq,
                    trace_id=trace_id,
                    on_complete=on_complete,
                )
            else:
                if process_task is not None and not process_task.done():
                    process_task.cancel()
                status = "process_timeout"
                reason = "process_timeout"
        except Exception:
            if process_task is not None and not process_task.done():
                process_task.cancel()
            status = "process_error"
            reason = "process_error"

        dispatch = QueueDispatchResult(
            status=status,
            reason=reason,
            conversation_id=conversation_id,
            seq=seq,
            trace_id=trace_id,
        )
        item = _CompletedItem(response=response, send=send, dispatch=dispatch, on_complete=on_complete)
        async with state.buffer_lock:
            state.pending_count = max(0, state.pending_count - 1)
            dispatch.pending_count = state.pending_count
            if self.send_in_order:
                state.completed[seq] = item

        if self.send_in_order:
            await self._emit_ready_in_order(conversation_id)
        else:
            final_dispatch = await self._emit_item(item)
            await self._notify_complete(final_dispatch, on_complete)

    async def _emit_ready_in_order(self, conversation_id: str) -> None:
        state = self._state(conversation_id)
        async with state.emit_lock:
            while True:
                async with state.buffer_lock:
                    item = state.completed.pop(state.next_emit_seq, None)
                    if item is None:
                        break
                    state.next_emit_seq += 1
                final_dispatch = await self._emit_item(item)
                await self._notify_complete(final_dispatch, item.on_complete)

    async def _emit_item(self, item: _CompletedItem) -> QueueDispatchResult:
        if item.dispatch.status != "sent":
            return item.dispatch
        if item.response is None:
            return QueueDispatchResult(
                status="dropped",
                reason="empty_response",
                conversation_id=item.dispatch.conversation_id,
                seq=item.dispatch.seq,
                pending_count=item.dispatch.pending_count,
                trace_id=item.dispatch.trace_id,
            )
        try:
            await item.send(item.response)
            return item.dispatch
        except Exception:
            _logger.exception(
                "队列发送失败 | 会话=%s | 序号=%d | 原因=%s | trace=%s",
                item.dispatch.conversation_id,
                item.dispatch.seq,
                item.dispatch.reason,
                item.dispatch.trace_id,
            )
            return QueueDispatchResult(
                status="dropped",
                reason="send_error",
                conversation_id=item.dispatch.conversation_id,
                seq=item.dispatch.seq,
                pending_count=item.dispatch.pending_count,
                trace_id=item.dispatch.trace_id,
            )

    def _schedule_late_emit(
        self,
        process_task: asyncio.Task[Any],
        send: Callable[[Any], Awaitable[None]],
        conversation_id: str,
        seq: int,
        trace_id: str,
        on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None,
    ) -> None:
        asyncio.create_task(
            self._emit_late_result(
                process_task=process_task,
                send=send,
                conversation_id=conversation_id,
                seq=seq,
                trace_id=trace_id,
                on_complete=on_complete,
            )
        )

    async def _emit_late_result(
        self,
        process_task: asyncio.Task[Any],
        send: Callable[[Any], Awaitable[None]],
        conversation_id: str,
        seq: int,
        trace_id: str,
        on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None,
    ) -> None:
        state = self._state(conversation_id)
        try:
            response = await asyncio.wait_for(asyncio.shield(process_task), timeout=self.late_emit_timeout_seconds)
        except TimeoutError:
            dispatch = QueueDispatchResult(
                status="late_timeout",
                reason="late_timeout",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dispatch, on_complete)
            return
        except asyncio.CancelledError:
            dispatch = QueueDispatchResult(
                status="late_cancelled",
                reason="late_cancelled",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dispatch, on_complete)
            return
        except Exception:
            dispatch = QueueDispatchResult(
                status="late_process_error",
                reason="late_process_error",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dispatch, on_complete)
            return

        if response is None:
            dispatch = QueueDispatchResult(
                status="late_dropped",
                reason="late_empty_response",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dispatch, on_complete)
            return

        try:
            await send(response)
            dispatch = QueueDispatchResult(
                status="sent_late",
                reason="late_ok",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
        except Exception:
            _logger.exception(
                "队列延迟发送失败 | 会话=%s | 序号=%d | trace=%s",
                conversation_id,
                seq,
                trace_id,
            )
            dispatch = QueueDispatchResult(
                status="late_dropped",
                reason="late_send_error",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
        await self._notify_complete(dispatch, on_complete)

    async def _try_send_overload_notice(
        self,
        state: _GroupState,
        sender: Callable[[str], Awaitable[None]] | None,
    ) -> bool:
        if sender is None:
            return False

        now = datetime.now(timezone.utc)
        if isinstance(state.last_overload_notice_at, datetime):
            if now - state.last_overload_notice_at < self.overload_notice_cooldown:
                return False

        state.last_overload_notice_at = now
        try:
            await sender(self.overload_notice_text)
            return True
        except Exception:
            return False

    def _state(self, conversation_id: str) -> _GroupState:
        state = self._groups.get(conversation_id)
        if state is None:
            state = _GroupState(semaphore=asyncio.Semaphore(self.group_concurrency))
            self._groups[conversation_id] = state
        return state

    async def _notify_complete(
        self,
        dispatch: QueueDispatchResult,
        callback: Callable[[QueueDispatchResult], Awaitable[None]] | None,
    ) -> None:
        if callback is None:
            return
        try:
            await callback(dispatch)
        except Exception:
            _logger.exception(
                "队列完成回调失败 | 会话=%s | 序号=%d | 状态=%s | trace=%s",
                dispatch.conversation_id,
                dispatch.seq,
                dispatch.status,
                dispatch.trace_id,
            )

    def _is_expired(self, created_at: datetime) -> bool:
        now = datetime.now(timezone.utc)
        baseline = created_at if isinstance(created_at, datetime) else now
        if baseline.tzinfo is None:
            baseline = baseline.replace(tzinfo=timezone.utc)
        return now - baseline > self.message_ttl
