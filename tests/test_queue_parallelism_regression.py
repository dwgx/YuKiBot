from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from core.queue import GroupQueueDispatcher


class QueueParallelismRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_different_conversations_can_run_in_parallel(self) -> None:
        dispatcher = GroupQueueDispatcher(
            {
                "group_concurrency": 1,
                "single_inflight_per_conversation": False,
                "cancel_previous_on_new": False,
            }
        )

        started_names: list[str] = []
        started_both = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        sent: list[str] = []

        async def make_process(name: str) -> str:
            started_names.append(name)
            if len(started_names) >= 2:
                started_both.set()
            await release.wait()
            return name

        async def send(value: str) -> None:
            sent.append(value)
            if len(sent) >= 2:
                finished.set()

        now = datetime.now(timezone.utc)
        await dispatcher.submit(
            "conversation:a",
            dispatcher.next_seq("conversation:a"),
            now,
            process=lambda: make_process("a"),
            send=send,
        )
        await dispatcher.submit(
            "conversation:b",
            dispatcher.next_seq("conversation:b"),
            now,
            process=lambda: make_process("b"),
            send=send,
        )

        await asyncio.wait_for(started_both.wait(), timeout=0.3)
        self.assertCountEqual(started_names, ["a", "b"])

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        self.assertCountEqual(sent, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
