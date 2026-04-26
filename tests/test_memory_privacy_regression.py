from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent_tools_knowledge import _register_crawler_tools
from core.agent_tools_memory import _register_memory_tools
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_social import _register_daily_report_tools


def _build_registry() -> AgentToolRegistry:
    registry = AgentToolRegistry()
    _register_memory_tools(registry)
    _register_crawler_tools(registry)
    _register_daily_report_tools(registry)
    return registry


def _build_memory_engine() -> MagicMock:
    memory = MagicMock()
    memory.list_memory_records.return_value = ([], 0)
    memory.add_memory_record.return_value = (
        True,
        "memory_added",
        {"id": 1, "conversation_id": "group:1:user:10001", "user_id": "10001"},
    )
    memory.get_memory_record.return_value = None
    memory.update_memory_record.return_value = (True, "memory_updated", {"id": 1})
    memory.delete_memory_record.return_value = (True, "memory_deleted", {"id": 1})
    memory.list_memory_audit_logs.return_value = ([], 0)
    memory.compact_memory_records.return_value = (
        True,
        "memory_compact_preview",
        {"scanned": 0, "duplicates": 0},
    )
    memory.add_user_fact.return_value = True
    memory.get_user_profile_summary.return_value = "profile"
    memory.get_explicit_facts.return_value = ["喜欢编程"]
    memory.get_agent_policies.return_value = []
    memory.get_user_portrait.return_value = "portrait"
    memory.knowledge_get_user_summary.return_value = ""
    return memory


def _build_context(
    *,
    permission_level: str = "user",
    user_id: str = "100001",
    conversation_id: str = "group:1:user:100001",
) -> dict[str, object]:
    return {
        "permission_level": permission_level,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "memory_engine": _build_memory_engine(),
        "knowledge_base": None,
    }


class MemoryPrivacyRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _build_registry()

    def _call(
        self,
        tool_name: str,
        args: dict[str, object],
        context: dict[str, object],
    ):
        return asyncio.run(self.registry.call(tool_name, args, context))

    def test_user_cannot_list_other_users_memory(self) -> None:
        context = _build_context()
        result = self._call("memory_list", {"user_id": "200002"}, context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:memory_scope")
        memory = context["memory_engine"]
        memory.list_memory_records.assert_not_called()

    def test_user_cannot_add_memory_for_other_user(self) -> None:
        context = _build_context()
        result = self._call(
            "memory_add",
            {"user_id": "200002", "content": "poisoned"},
            context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:memory_scope")
        memory = context["memory_engine"]
        memory.add_memory_record.assert_not_called()

    def test_user_cannot_update_other_users_record_by_id(self) -> None:
        context = _build_context()
        memory = context["memory_engine"]
        memory.get_memory_record.return_value = {
            "id": 7,
            "conversation_id": "group:1:user:200002",
            "user_id": "200002",
            "role": "user",
            "content": "secret",
            "created_at": "2026-04-24T00:00:00+00:00",
        }

        result = self._call(
            "memory_update",
            {"record_id": 7, "content": "tampered", "note": "edit"},
            context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:memory_scope")
        memory.update_memory_record.assert_not_called()

    def test_user_cannot_dump_global_memory_audit_logs(self) -> None:
        context = _build_context()
        result = self._call("memory_audit", {}, context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:memory_scope")
        memory = context["memory_engine"]
        memory.list_memory_audit_logs.assert_not_called()

    def test_user_cannot_recall_or_write_other_users_profile_memory(self) -> None:
        context = _build_context()

        recall_result = self._call("recall_about_user", {"user_id": "200002"}, context)
        remember_result = self._call(
            "remember_user_fact",
            {"user_id": "200002", "fact": "他喜欢摄影"},
            context,
        )
        portrait_result = self._call("user_portrait", {"user_id": "200002"}, context)

        self.assertFalse(recall_result.ok)
        self.assertEqual(recall_result.error, "permission_denied:user_scope")
        self.assertFalse(remember_result.ok)
        self.assertEqual(remember_result.error, "permission_denied:user_scope")
        self.assertFalse(portrait_result.ok)
        self.assertEqual(portrait_result.error, "permission_denied:user_scope")

        memory = context["memory_engine"]
        memory.add_user_fact.assert_not_called()
        memory.get_user_profile_summary.assert_not_called()
        memory.get_user_portrait.assert_not_called()

    def test_super_admin_can_access_other_users_memory_and_profile(self) -> None:
        context = _build_context(permission_level="super_admin")
        memory = context["memory_engine"]
        memory.list_memory_records.return_value = (
            [{"id": 9, "content": "other user memory", "role": "user", "user_id": "200002"}],
            1,
        )
        memory.get_user_profile_summary.return_value = "目标用户画像"
        memory.get_explicit_facts.return_value = ["喜欢摄影"]

        memory_result = self._call("memory_list", {"user_id": "200002"}, context)
        recall_result = self._call("recall_about_user", {"user_id": "200002"}, context)

        self.assertTrue(memory_result.ok)
        self.assertTrue(recall_result.ok)
        memory.list_memory_records.assert_called_once()
        memory.get_user_profile_summary.assert_called_once_with("200002")


if __name__ == "__main__":
    unittest.main()
