from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.admin import AdminEngine
from core.agent import AgentLoop
from core.agent_tools import _handle_set_group_ban
from core.memory import MemoryEngine
from utils.learning_guard import extract_explicit_preferred_name


class AdminRuntimePolicyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_admin_can_disable_group_high_risk_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            admin = AdminEngine(
                config={
                    "admin": {
                        "enable": True,
                        "super_users": ["10001"],
                        "whitelist_groups": [1075046273],
                    }
                },
                storage_dir=Path(tmp),
            )

            reply = await admin.handle_command(
                text="/yuki 高风险确认 off group",
                user_id="20002",
                group_id=1075046273,
                sender_role="admin",
            )

            self.assertIsInstance(reply, str)
            self.assertIn("已关闭本群", reply or "")
            policy = admin.get_high_risk_confirmation_policy(group_id=1075046273)
            self.assertFalse(policy["high_risk_confirmation_required"])
            self.assertEqual(policy["source"], "group")

    async def test_group_admin_cannot_change_global_high_risk_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            admin = AdminEngine(
                config={
                    "admin": {
                        "enable": True,
                        "super_users": ["10001"],
                        "whitelist_groups": [1075046273],
                    }
                },
                storage_dir=Path(tmp),
            )

            reply = await admin.handle_command(
                text="/yuki 高风险确认 off global",
                user_id="20002",
                group_id=1075046273,
                sender_role="admin",
            )

            self.assertIsInstance(reply, str)
            self.assertIn("只有超级管理员", reply or "")
            policy = admin.get_high_risk_confirmation_policy(group_id=1075046273)
            self.assertTrue(policy["high_risk_confirmation_required"])
            self.assertEqual(policy["source"], "default")


class RuntimeHighRiskGuardRegressionTests(unittest.TestCase):
    def test_agent_uses_runtime_admin_policy_for_high_risk_confirmation(self) -> None:
        agent = AgentLoop.__new__(AgentLoop)
        agent.high_risk_default_require_confirmation = True

        ctx = SimpleNamespace(
            runtime_admin_policy={"high_risk_confirmation_required": False},
        )

        self.assertFalse(AgentLoop._require_high_risk_confirmation_for_user(agent, ctx))

    def test_memory_profile_filters_out_high_risk_confirmation_fields(self) -> None:
        memory = MemoryEngine.__new__(MemoryEngine)
        memory._user_profiles = {
            "10001": {
                "agent_policies": {
                    "high_risk_confirmation_required": False,
                    "high_risk_confirmation_updated_at": "2026-03-15T00:00:00+00:00",
                    "other_flag": True,
                }
            }
        }

        policies = MemoryEngine.get_agent_policies(memory, "10001")

        self.assertEqual(policies, {"other_flag": True})


class PreferredNameLearningRegressionTests(unittest.TestCase):
    def test_question_like_name_query_is_not_learned(self) -> None:
        self.assertEqual(extract_explicit_preferred_name("我叫什么"), "")
        self.assertEqual(extract_explicit_preferred_name("我叫啥"), "")
        self.assertEqual(extract_explicit_preferred_name("我是什么名字？"), "")


class GroupBanTargetGuardRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ban_uses_unique_shared_context_candidate(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"status": "ok", "retcode": 0, "data": {}}

        with patch(
            "core.agent_tools_napcat._verify_group_ban_applied",
            return_value=(True, {"shut_up_timestamp": 9999999999}),
        ):
            result = await _handle_set_group_ban(
                {"group_id": 1075046273, "duration": 2592000},
                {
                    "api_call": fake_api_call,
                    "permission_level": "group_admin",
                    "user_id": "136666451",
                    "bot_id": "1145141919",
                    "reply_to_user_id": "",
                    "at_other_user_ids": [],
                    "recent_speakers": [
                        ("3862205188", "SM", "禁言我"),
                        ("2529638913", "Alice", "别搞我"),
                    ],
                    "runtime_group_context": [
                        "SM(QQ:3862205188): 禁言我",
                        "Alice(QQ:2529638913): 别搞我",
                    ],
                    "original_message_text": "禁言那个M 30天",
                    "message_text": "禁言那个M 30天",
                },
            )

        self.assertTrue(result.ok)
        self.assertTrue(calls)
        self.assertEqual(calls[0][0], "set_group_ban")
        self.assertEqual(calls[0][1]["user_id"], "3862205188")
        self.assertIn("已校验", result.display)

    async def test_ban_rejects_ambiguous_shared_context_targets(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"status": "ok", "retcode": 0, "data": {}}

        result = await _handle_set_group_ban(
            {"group_id": 1075046273, "duration": 2592000},
            {
                "api_call": fake_api_call,
                "permission_level": "group_admin",
                "user_id": "136666451",
                "bot_id": "1145141919",
                "reply_to_user_id": "",
                "at_other_user_ids": [],
                "recent_speakers": [
                    ("3862205188", "SM", "禁言我"),
                    ("2529638913", "Momo", "禁言我啊"),
                ],
                "runtime_group_context": [
                    "SM(QQ:3862205188): 禁言我",
                    "Momo(QQ:2529638913): 禁言我啊",
                ],
                "original_message_text": "禁言那个M 30天",
                "message_text": "禁言那个M 30天",
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("target_resolve_failed", result.error)
        self.assertEqual(calls, [])

    async def test_regular_user_can_self_ban(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"status": "ok", "retcode": 0, "data": {}}

        with patch(
            "core.agent_tools_napcat._verify_group_ban_applied",
            return_value=(True, {"shut_up_timestamp": 9999999999}),
        ):
            result = await _handle_set_group_ban(
                {"group_id": 1075046273, "user_id": 3862205188, "duration": 60},
                {
                    "api_call": fake_api_call,
                    "permission_level": "user",
                    "user_id": "3862205188",
                    "bot_id": "1145141919",
                    "reply_to_user_id": "",
                    "at_other_user_ids": [],
                    "recent_speakers": [],
                    "runtime_group_context": [],
                    "original_message_text": "禁言我 60秒",
                    "message_text": "禁言我 60秒",
                },
            )

        self.assertTrue(result.ok)
        self.assertTrue(calls)
        self.assertEqual(calls[0][1]["user_id"], "3862205188")

    async def test_regular_user_cannot_ban_others(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"status": "ok", "retcode": 0, "data": {}}

        result = await _handle_set_group_ban(
            {"group_id": 1075046273, "user_id": 2529638913, "duration": 60},
            {
                "api_call": fake_api_call,
                "permission_level": "user",
                "user_id": "3862205188",
                "bot_id": "1145141919",
                "reply_to_user_id": "",
                "at_other_user_ids": [],
                "recent_speakers": [],
                "runtime_group_context": [],
                "original_message_text": "禁言 2529638913 60秒",
                "message_text": "禁言 2529638913 60秒",
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:self_ban_only")
        self.assertEqual(calls, [])

    async def test_ban_requires_verification_before_success(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"status": "ok", "retcode": 0, "data": {}}

        with patch("core.agent_tools_napcat._verify_group_ban_applied", return_value=(False, {})):
            result = await _handle_set_group_ban(
                {"group_id": 1075046273, "user_id": 3862205188, "duration": 60},
                {
                    "api_call": fake_api_call,
                    "permission_level": "group_admin",
                    "user_id": "136666451",
                    "bot_id": "1145141919",
                    "reply_to_user_id": "",
                    "at_other_user_ids": [],
                    "recent_speakers": [],
                    "runtime_group_context": [],
                    "original_message_text": "禁言 3862205188 60秒",
                    "message_text": "禁言 3862205188 60秒",
                },
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "ban_unverified")
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
