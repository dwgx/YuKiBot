from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.agent_tools import _handle_learn_knowledge, _handle_search_knowledge
from core.engine import EngineMessage, YukikoEngine
from core.knowledge import KnowledgeBase
from core.memory import MemoryEngine


class GroupMemoryAndKnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_learn_knowledge_auto_tags_user_and_conversation_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = KnowledgeBase(db_path=str(Path(tmp) / "knowledge.db"))
            try:
                result = await _handle_learn_knowledge(
                    {
                        "title": "喜欢的歌",
                        "content": "我最喜欢夜曲",
                        "category": "learned",
                    },
                    {
                        "knowledge_base": kb,
                        "user_id": "10001",
                        "conversation_id": "group:42",
                        "group_id": 42,
                    },
                )

                self.assertTrue(result.ok)
                tags = result.data.get("tags", [])
                self.assertIn("user:10001", tags)
                self.assertIn("conversation:group:42", tags)
                self.assertIn("group:42", tags)

                rows = kb.search("喜欢的歌", category="learned", limit=5)
                self.assertTrue(rows)
                self.assertIn("user:10001", list(getattr(rows[0], "tags", []) or []))
            finally:
                kb.close()

    async def test_search_knowledge_prioritizes_current_user_scoped_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = KnowledgeBase(db_path=str(Path(tmp) / "knowledge.db"))
            try:
                kb.add(
                    category="learned",
                    title="喜欢喝什么",
                    content="10001 喜欢乌龙茶",
                    source="chat",
                    tags=["user:10001", "conversation:group:42", "group:42"],
                    upsert=False,
                )
                kb.add(
                    category="learned",
                    title="喜欢喝什么（别人）",
                    content="20002 喜欢可乐",
                    source="chat",
                    tags=["user:20002", "conversation:group:42", "group:42"],
                    upsert=False,
                )

                result = await _handle_search_knowledge(
                    {"query": "喜欢喝什么", "category": "learned"},
                    {
                        "knowledge_base": kb,
                        "user_id": "10001",
                        "conversation_id": "group:42",
                        "group_id": 42,
                    },
                )

                self.assertTrue(result.ok)
                rows = result.data.get("results", [])
                self.assertGreaterEqual(len(rows), 2)
                self.assertEqual(rows[0]["title"], "喜欢喝什么")
                self.assertGreaterEqual(int(result.data.get("scoped_hits", 0)), 1)
            finally:
                kb.close()

    def test_memory_recent_speakers_keep_per_user_latest_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(config={}, memory_dir=Path(tmp) / "memory")
            try:
                conversation_id = "group:42"
                memory.add_message(
                    conversation_id=conversation_id,
                    user_id="10001",
                    user_name="小雨",
                    role="user",
                    content="我喜欢乌龙茶",
                )
                memory.add_message(
                    conversation_id=conversation_id,
                    user_id="20002",
                    user_name="阿风",
                    role="user",
                    content="我喜欢可乐",
                )
                memory.add_message(
                    conversation_id=conversation_id,
                    user_id="10001",
                    user_name="小雨",
                    role="user",
                    content="今天心情不错",
                )

                speakers = memory.get_recent_speakers(conversation_id, limit=12)
                self.assertEqual(len(speakers), 2)
                by_uid = {uid: (name, preview) for uid, name, preview in speakers}
                self.assertIn("10001", by_uid)
                self.assertIn("20002", by_uid)
                self.assertIn("今天心情不错", by_uid["10001"][1])
                self.assertIn("我喜欢可乐", by_uid["20002"][1])
            finally:
                memory.close()

    def test_short_group_reply_prunes_cross_user_noise(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        message = EngineMessage(
            conversation_id="group:42",
            user_id="10001",
            text="那他呢",
            reply_to_user_id="20002",
            bot_id="99999",
            is_private=False,
        )

        memory_context, related = engine._prune_memory_context_for_current_turn(
            message=message,
            current_text="那他呢",
            memory_context=[
                "[当前用户近期]小雨: 我喜欢乌龙茶",
                "[引用对象近期]阿风: 我喜欢可乐",
                "[群聊缓存]路人(QQ:30003): 我喜欢雪碧",
                "普通上下文",
            ],
            related_memories=[
                "小雨偏好乌龙茶",
                "阿风偏好可乐",
                "路人偏好雪碧",
            ],
        )

        self.assertTrue(memory_context)
        self.assertTrue(
            all(
                row.startswith("[当前用户近期]") or row.startswith("[引用对象近期]")
                for row in memory_context
            )
        )
        self.assertEqual(related, [])


if __name__ == "__main__":
    unittest.main()
