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
class _QueueItem:
    seq: int
    created_at: datetime
    trace_id: str
    on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None = None
    state: str = "pending"  # pending / running / finished / cancelled
    cancel_reason: str = ""
    runner_task: asyncio.Task[Any] | None = None
    process_task: asyncio.Task[Any] | None = None
    final_dispatched: bool = False
    interruptible: bool = True


@dataclass(slots=True)
class _GroupState:
    semaphore: asyncio.Semaphore
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seq_counter: int = 0
    pending_count: int = 0
    items: dict[int, _QueueItem] = field(default_factory=dict)
    last_overload_notice_at: datetime | None = None
    last_active_at: datetime | None = None


class GroupQueueDispatcher:
    def __init__(self, config: dict[str, Any]):
        requested_group_concurrency = max(1, int(config.get("group_concurrency", 2)))
        self.single_inflight_per_conversation = bool(config.get("single_inflight_per_conversation", True))
        if self.single_inflight_per_conversation and requested_group_concurrency != 1:
            _logger.warning(
                "queue_force_single_inflight | requested_group_concurrency=%d -> forced=1",
                requested_group_concurrency,
            )
        self.group_concurrency = 1 if self.single_inflight_per_conversation else requested_group_concurrency
        self.cancel_previous_on_new = bool(config.get("cancel_previous_on_new", True))

        self.max_pending_per_group = max(1, int(config.get("max_pending_per_group", 80)))
        ttl_s = max(1, int(config.get("message_ttl_seconds", 90)))
        timeout_s = max(1, int(config.get("process_timeout_seconds", 120)))
        # TTL 必须 >= timeout + 裕度，否则消息在处理中过期被丢弃
        min_ttl = timeout_s + 15
        if ttl_s < min_ttl:
            _logger.warning(
                "queue_ttl_auto_adjust | ttl=%ds < timeout=%ds+15 | adjusted_to=%ds",
                ttl_s, timeout_s, min_ttl,
            )
            ttl_s = min_ttl
        self.message_ttl = timedelta(seconds=ttl_s)
        self.process_timeout_seconds = timeout_s
        self.overload_notice_cooldown = timedelta(
            seconds=max(5, int(config.get("overload_notice_cooldown_seconds", 30)))
        )
        self.overload_notice_text = str(
            config.get(
                "overload_notice_text",
                "你们等等呀，我回复不过来了。请 @我 或叫我的名字（雪 / yukiko），我会优先回你。",
            )
        )

        self.max_concurrent_total = max(0, int(config.get("max_concurrent_total", 0)))
        self._global_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self.max_concurrent_total) if self.max_concurrent_total > 0 else None
        )
        self._groups: dict[str, _GroupState] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

        _logger.info(
            "queue_init | group_concurrency=%d | max_concurrent_total=%d | ttl=%ds | timeout=%ds | single_inflight=%s | cancel_previous=%s",
            self.group_concurrency,
            self.max_concurrent_total,
            int(self.message_ttl.total_seconds()),
            self.process_timeout_seconds,
            self.single_inflight_per_conversation,
            self.cancel_previous_on_new,
        )

    def next_seq(self, conversation_id: str) -> int:
        state = self._state(conversation_id)
        state.seq_counter += 1
        return state.seq_counter

    def pending_count(self, conversation_id: str) -> int:
        return self._state(conversation_id).pending_count

    def get_conversation_state(self, conversation_id: str) -> dict[str, Any]:
        """返回某会话的队列状态快照（用于 WebUI 观测）。"""
        cid = str(conversation_id or "")
        state = self._groups.get(cid)
        if state is None:
            return {
                "conversation_id": cid,
                "exists": False,
                "pending_count": 0,
                "running_count": 0,
                "queued_count": 0,
                "interruptible_count": 0,
                "latest_trace_id": "",
            }
        pending = 0
        running = 0
        interruptible = 0
        latest_trace_id = ""
        latest_seq = -1
        for item in state.items.values():
            if item.final_dispatched:
                continue
            if item.interruptible:
                interruptible += 1
            if item.state == "running":
                running += 1
            else:
                pending += 1
            if item.seq > latest_seq:
                latest_seq = int(item.seq)
                latest_trace_id = str(item.trace_id or "")
        return {
            "conversation_id": cid,
            "exists": True,
            "pending_count": int(state.pending_count),
            "running_count": running,
            "queued_count": pending,
            "interruptible_count": interruptible,
            "latest_trace_id": latest_trace_id,
        }

    def list_conversation_states(self, limit: int = 200) -> list[dict[str, Any]]:
        """返回所有有活跃任务的会话快照。"""
        rows: list[dict[str, Any]] = []
        for cid in list(self._groups.keys()):
            snap = self.get_conversation_state(cid)
            if not bool(snap.get("exists")):
                continue
            if int(snap.get("pending_count", 0) or 0) <= 0:
                continue
            rows.append(snap)
        rows.sort(key=lambda item: int(item.get("pending_count", 0) or 0), reverse=True)
        return rows[: max(1, int(limit))]

    async def submit(
        self,
        conversation_id: str,
        seq: int,
        created_at: datetime,
        process: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
        high_priority: bool = False,
        allow_cancel_previous: bool = True,
        interruptible: bool = True,
        process_timeout_seconds: int | None = None,
        allow_late_emit_on_timeout: bool = False,  # noqa: ARG002 - kept for backward compatibility
        trace_id: str = "",
        send_overload_notice: Callable[[str], Awaitable[None]] | None = None,
        on_complete: Callable[[QueueDispatchResult], Awaitable[None]] | None = None,
        force_cancel_previous: bool = False,
        cancel_previous_reason: str = "cancelled_by_new_trace",
    ) -> QueueDispatchResult:
        state = self._state(conversation_id)
        cancel_reason = str(cancel_previous_reason or "").strip()
        if not cancel_reason:
            cancel_reason = "cancelled_by_new_trace"
        if allow_cancel_previous and (self.cancel_previous_on_new or force_cancel_previous):
            await self._cancel_previous_items(
                conversation_id=conversation_id,
                keep_seq=seq,
                reason=cancel_reason,
            )

        if not high_priority and state.pending_count >= self.max_pending_per_group:
            notice_sent = await self._try_send_overload_notice(state, send_overload_notice)
            _logger.warning(
                "queue_overload_drop | conversation=%s | seq=%d | pending=%d | max=%d | notice_sent=%s | trace=%s",
                conversation_id, seq, state.pending_count, self.max_pending_per_group, notice_sent, trace_id,
            )
            dropped = QueueDispatchResult(
                status="cancelled",
                reason="queue_overload_notice" if notice_sent else "queue_overload",
                conversation_id=conversation_id,
                seq=seq,
                pending_count=state.pending_count,
                trace_id=trace_id,
            )
            await self._notify_complete(dropped, on_complete)
            return dropped

        item = _QueueItem(
            seq=seq,
            created_at=created_at,
            trace_id=trace_id,
            on_complete=on_complete,
            interruptible=bool(interruptible),
        )

        async with state.state_lock:
            state.pending_count += 1
            state.items[seq] = item
            pending = state.pending_count

        task = asyncio.create_task(
            self._run_item(
                conversation_id=conversation_id,
                item=item,
                process=process,
                send=send,
                process_timeout_seconds=process_timeout_seconds,
            )
        )
        item.runner_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return QueueDispatchResult(
            status="queued",
            reason="accepted",
            conversation_id=conversation_id,
            seq=seq,
            pending_count=pending,
            trace_id=trace_id,
        )

    async def _cancel_previous_items(
        self,
        conversation_id: str,
        keep_seq: int,
        reason: str,
    ) -> None:
        state = self._state(conversation_id)
        victims: list[_QueueItem] = []
        skipped_non_interruptible = 0
        skipped_finished = 0
        async with state.state_lock:
            for seq, item in state.items.items():
                if seq >= keep_seq or item.final_dispatched:
                    continue
                if item.state in {"finished", "cancelled"}:
                    skipped_finished += 1
                    continue
                if not item.interruptible:
                    skipped_non_interruptible += 1
                    continue
                item.state = "cancelled"
                item.cancel_reason = reason
                victims.append(item)

        if not victims and (skipped_non_interruptible or skipped_finished):
            _logger.info(
                "queue_cancel_previous_skip | conversation=%s | keep_seq=%d | reason=%s | skipped_non_interruptible=%d | skipped_finished=%d",
                conversation_id,
                keep_seq,
                reason,
                skipped_non_interruptible,
                skipped_finished,
            )

        for item in victims:
            if item.process_task is not None and not item.process_task.done():
                item.process_task.cancel()
            if item.runner_task is not None and not item.runner_task.done():
                item.runner_task.cancel()
            await self._finalize_item(
                state=state,
                item=item,
                dispatch=QueueDispatchResult(
                    status="cancelled",
                    reason=reason,
                    conversation_id=conversation_id,
                    seq=item.seq,
                    trace_id=item.trace_id,
                ),
            )
            _logger.info(
                "queue_cancel_previous | conversation=%s | seq=%d | trace=%s | reason=%s | interruptible=%s",
                conversation_id,
                item.seq,
                item.trace_id,
                reason,
                item.interruptible,
            )

    async def cancel_conversation(
        self,
        conversation_id: str,
        *,
        reason: str = "cancelled_by_webui",
        include_running: bool = True,
        interruptible_only: bool = True,
    ) -> dict[str, int]:
        """取消某会话的全部待处理任务，返回取消统计。"""
        cid = str(conversation_id or "")
        state = self._groups.get(cid)
        if state is None:
            return {"cancelled": 0, "skipped_non_interruptible": 0, "skipped_running": 0, "skipped_finished": 0}

        victims: list[_QueueItem] = []
        skipped_non_interruptible = 0
        skipped_finished = 0
        skipped_running = 0
        async with state.state_lock:
            for item in state.items.values():
                if item.final_dispatched or item.state in {"finished", "cancelled"}:
                    skipped_finished += 1
                    continue
                if interruptible_only and not item.interruptible:
                    skipped_non_interruptible += 1
                    continue
                if not include_running and item.state == "running":
                    skipped_running += 1
                    continue
                item.state = "cancelled"
                item.cancel_reason = reason
                victims.append(item)

        for item in victims:
            if item.process_task is not None and not item.process_task.done():
                item.process_task.cancel()
            if item.runner_task is not None and not item.runner_task.done():
                item.runner_task.cancel()
            await self._finalize_item(
                state=state,
                item=item,
                dispatch=QueueDispatchResult(
                    status="cancelled",
                    reason=reason,
                    conversation_id=cid,
                    seq=item.seq,
                    trace_id=item.trace_id,
                ),
            )

        return {
            "cancelled": len(victims),
            "skipped_non_interruptible": skipped_non_interruptible,
            "skipped_running": skipped_running,
            "skipped_finished": skipped_finished,
        }

    async def _run_item(
        self,
        conversation_id: str,
        item: _QueueItem,
        process: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
        process_timeout_seconds: int | None = None,
    ) -> None:
        state = self._state(conversation_id)
        timeout_seconds = self.process_timeout_seconds
        if isinstance(process_timeout_seconds, (int, float)) and process_timeout_seconds > 0:
            timeout_seconds = max(1, int(process_timeout_seconds))

        status = "cancelled"
        reason = item.cancel_reason or "cancelled"

        try:
            if item.final_dispatched:
                return
            if item.cancel_reason:
                raise asyncio.CancelledError
            if self._is_expired(item.created_at):
                reason = "message_ttl_expired"
                _logger.warning(
                    "message_ttl_expired | conversation=%s | seq=%d | age=%.1fs | ttl=%.0fs | trace=%s",
                    conversation_id, item.seq,
                    (datetime.now(timezone.utc) - item.created_at).total_seconds(),
                    self.message_ttl.total_seconds(),
                    item.trace_id,
                )
                raise asyncio.CancelledError

            async def _execute_process() -> Any:
                item.state = "running"
                item.process_task = asyncio.create_task(process())
                return await asyncio.wait_for(asyncio.shield(item.process_task), timeout=timeout_seconds)

            if self._global_semaphore is not None:
                async with self._global_semaphore:
                    async with state.semaphore:
                        response = await _execute_process()
            else:
                async with state.semaphore:
                    response = await _execute_process()

            if item.cancel_reason:
                raise asyncio.CancelledError

            if response is None:
                reason = "empty_response"
                status = "cancelled"
            else:
                try:
                    await send(response)
                    status = "finished"
                    reason = "ok"
                except Exception:
                    _logger.exception(
                        "queue_send_error | conversation=%s | seq=%d | trace=%s",
                        conversation_id,
                        item.seq,
                        item.trace_id,
                    )
                    status = "cancelled"
                    reason = "send_error"
        except asyncio.TimeoutError:
            if item.process_task is not None and not item.process_task.done():
                item.process_task.cancel()
            status = "cancelled"
            reason = "process_timeout"
        except asyncio.CancelledError:
            if item.process_task is not None and not item.process_task.done():
                item.process_task.cancel()
            status = "cancelled"
            reason = item.cancel_reason or reason or "cancelled"
        except Exception:
            if item.process_task is not None and not item.process_task.done():
                item.process_task.cancel()
            _logger.exception(
                "queue_process_error | conversation=%s | seq=%d | trace=%s",
                conversation_id,
                item.seq,
                item.trace_id,
            )
            status = "cancelled"
            reason = "process_error"

        dispatch = QueueDispatchResult(
            status=status,
            reason=reason,
            conversation_id=conversation_id,
            seq=item.seq,
            trace_id=item.trace_id,
        )
        await self._finalize_item(state=state, item=item, dispatch=dispatch)

    async def _finalize_item(
        self,
        state: _GroupState,
        item: _QueueItem,
        dispatch: QueueDispatchResult,
    ) -> None:
        callback = item.on_complete
        async with state.state_lock:
            if item.final_dispatched:
                return
            item.final_dispatched = True
            item.state = dispatch.status
            state.items.pop(item.seq, None)
            state.pending_count = max(0, state.pending_count - 1)
            dispatch.pending_count = state.pending_count

        await self._notify_complete(dispatch, callback)

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
            _logger.warning("overload_notice_send_failed", exc_info=True)
            return False

    def _state(self, conversation_id: str) -> _GroupState:
        state = self._groups.get(conversation_id)
        if state is None:
            self._cleanup_idle_groups()
            state = _GroupState(semaphore=asyncio.Semaphore(self.group_concurrency))
            self._groups[conversation_id] = state
        state.last_active_at = datetime.now(timezone.utc)
        return state

    def _cleanup_idle_groups(self) -> None:
        if len(self._groups) < 50:
            return
        now = datetime.now(timezone.utc)
        idle_threshold = timedelta(hours=1)
        to_remove: list[str] = []
        for cid, state in self._groups.items():
            if state.pending_count > 0 or state.items:
                continue
            if state.last_active_at is None or now - state.last_active_at > idle_threshold:
                to_remove.append(cid)
        for cid in to_remove:
            self._groups.pop(cid, None)
        if to_remove:
            _logger.info("queue_cleanup_idle_groups | removed=%d | remaining=%d", len(to_remove), len(self._groups))

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
                "queue_complete_callback_error | conversation=%s | seq=%d | status=%s | trace=%s",
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
