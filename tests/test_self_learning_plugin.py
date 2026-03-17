from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.agent import AgentContext, AgentLoop
from core.agent_tools import AgentToolRegistry
from plugins.self_learning import Plugin


class _DummyModelClient:
    enabled = False


class TestSelfLearningPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = Plugin()

    def test_plugin_initialization_is_safe_by_default(self) -> None:
        self.assertEqual(self.plugin.name, "self_learning")
        self.assertTrue(self.plugin.agent_tool)
        self.assertTrue(self.plugin.internal_only)
        self.assertFalse(self.plugin._allow_code_execution)
        self.assertTrue(self.plugin._super_admin_only)

    def test_learn_from_web_validation(self) -> None:
        async def run_test() -> None:
            from core.agent_tools import ToolCallResult

            self.plugin._registry = MagicMock()

            result = await self.plugin._handle_learn_from_web({"topic": "Python"}, {})
            self.assertIsInstance(result, ToolCallResult)
            self.assertFalse(result.ok)

            result = await self.plugin._handle_learn_from_web(
                {
                    "topic": "Python JSON 处理",
                    "goal": "学会解析 JSON",
                    "context": "用于 API 数据处理",
                },
                {},
            )
            self.assertIsInstance(result, ToolCallResult)
            self.assertTrue(result.ok)
            stats = self.plugin.get_stats()
            self.assertEqual(stats["total_sessions"], 1)
            self.assertEqual(stats["active_sessions"], 0)

        asyncio.run(run_test())

    def test_create_skill_requires_super_admin(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._super_admin_only = True
            self.plugin._auto_test = False
            self.plugin._save_skills = False

            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "valid_skill_name",
                    "description": "测试技能",
                    "code": "def test():\n    return True",
                },
                {"permission_level": "user"},
            )
            self.assertFalse(result.ok)
            self.assertIn("super_admin", str(result.display))

        asyncio.run(run_test())

    def test_create_skill_rejects_unsafe_code(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._super_admin_only = True
            self.plugin._auto_test = False
            self.plugin._save_skills = False

            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "unsafe_skill",
                    "description": "测试技能",
                    "code": "import os\nprint(os.getcwd())",
                },
                {"permission_level": "super_admin"},
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "unsafe_code")

        asyncio.run(run_test())

    def test_create_skill_validation_kept_for_safe_code(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._super_admin_only = True
            self.plugin._auto_test = False
            self.plugin._save_skills = False

            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "valid_skill_name",
                    "description": "测试技能",
                    "code": "def test():\n    return 42",
                },
                {"permission_level": "super_admin"},
            )
            self.assertTrue(result.ok)
            self.assertEqual(self.plugin._stats["successful_skills"], 1)

        asyncio.run(run_test())

    def test_create_skill_rejects_duplicate_code_hash(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._super_admin_only = True
            self.plugin._auto_test = False
            self.plugin._save_skills = False
            code = "def test():\n    return 42"
            self.plugin._cache_loaded = True
            self.plugin._skill_cache = {
                "existing_skill": {
                    "name": "existing_skill",
                    "code_hash": self.plugin._get_skill_hash(code),
                }
            }

            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "valid_skill_name",
                    "description": "测试技能",
                    "code": code,
                },
                {"permission_level": "super_admin"},
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "duplicate_skill")

        asyncio.run(run_test())

    def test_sandbox_disabled_until_explicit_opt_in(self) -> None:
        async def run_test() -> None:
            result = await self.plugin._test_code_in_sandbox("print('hello')")
            self.assertFalse(result["ok"])
            self.assertIn("未开启受信任代码执行", result["error"])

        asyncio.run(run_test())

    def test_setup_backfills_local_backend_for_legacy_opt_in(self) -> None:
        async def run_test() -> None:
            registry = MagicMock()
            context = MagicMock(agent_tool_registry=registry)

            await self.plugin.setup(
                {
                    "enabled": True,
                    "allow_code_execution": True,
                    "acknowledge_unsafe_execution": True,
                },
                context,
            )

            self.assertEqual(self.plugin._execution_backend_name, "local_subprocess")
            self.assertTrue(self.plugin._code_runner.is_available)

        asyncio.run(run_test())

    def test_unknown_execution_backend_is_blocked(self) -> None:
        async def run_test() -> None:
            registry = MagicMock()
            context = MagicMock(agent_tool_registry=registry)

            await self.plugin.setup(
                {
                    "enabled": True,
                    "allow_code_execution": True,
                    "acknowledge_unsafe_execution": True,
                    "execution_backend": "mystery-box",
                },
                context,
            )

            result = await self.plugin._test_code_in_sandbox("print('hello')")
            self.assertFalse(result["ok"])
            self.assertIn("不支持的 execution_backend", result["error"])

        asyncio.run(run_test())

    def test_sandbox_runs_safe_code_after_opt_in(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._sandbox_mode = "isolated"
            self.plugin._test_timeout = 5
            self.plugin._execution_backend_name = "local_subprocess"
            from plugins.self_learning_runtime import create_code_execution_backend
            self.plugin._code_runner = create_code_execution_backend(
                "local_subprocess",
                sandbox_root=Path(__file__).parent.parent / "storage" / "sandbox",
            )

            result = await self.plugin._test_code_in_sandbox("print('Hello, World!')\nprint(2 + 2)")
            self.assertTrue(result["ok"])
            self.assertIn("Hello, World!", result["output"])
            self.assertIn("4", result["output"])

        asyncio.run(run_test())

    def test_devlog_cooldown(self) -> None:
        async def run_test() -> None:
            self.plugin._devlog_cooldown = 5
            self.plugin._devlog_broadcast = True
            self.plugin._last_devlog_time = 0

            mock_api_call = AsyncMock()
            context = {"api_call": mock_api_call, "group_id": 123456}

            result = await self.plugin._handle_send_devlog(
                {"message": "测试日志 1", "log_type": "learning"},
                context,
            )
            self.assertTrue(result.ok)
            self.assertEqual(self.plugin._stats["devlogs_sent"], 1)

            result = await self.plugin._handle_send_devlog(
                {"message": "测试日志 2", "log_type": "learning"},
                context,
            )
            self.assertFalse(result.ok)

        asyncio.run(run_test())

    def test_setup_registers_dynamic_context_provider(self) -> None:
        async def run_test() -> None:
            registry = AgentToolRegistry()
            context = MagicMock(agent_tool_registry=registry)

            await self.plugin.setup({"enabled": True}, context)
            self.plugin._stats["total_sessions"] = 3
            self.plugin._stats["successful_skills"] = 1
            self.plugin._cache_loaded = True
            self.plugin._skill_cache = {"json_helper": {"name": "json_helper"}}

            dynamic_context = registry.get_dynamic_context(
                {"ctx": None, "config": {}, "selected_tools": ["learn_from_web"]},
                tool_names=["learn_from_web"],
            )

            self.assertIn("自学习状态", dynamic_context)
            self.assertIn("执行后端: disabled", dynamic_context)
            self.assertIn("累计学习会话: 3", dynamic_context)
            self.assertIn("缓存技能数: 1", dynamic_context)

        asyncio.run(run_test())

    def test_agent_prompt_includes_self_learning_dynamic_context(self) -> None:
        async def run_test() -> None:
            registry = AgentToolRegistry()
            context = MagicMock(agent_tool_registry=registry)

            await self.plugin.setup({"enabled": True}, context)
            self.plugin._stats["total_sessions"] = 1
            registry.select_tools_for_intent = lambda message_text, perm_level: [  # type: ignore[method-assign]
                "learn_from_web",
                "send_devlog",
                "list_my_skills",
            ]

            loop = AgentLoop(
                _DummyModelClient(),
                registry,
                {"admin": {"super_users": []}, "agent": {"enable": True}},
            )
            ctx = AgentContext(
                conversation_id="group:1",
                user_id="10001",
                user_name="tester",
                group_id=1,
                bot_id="99999",
                is_private=False,
                mentioned=True,
                message_text="继续学习 LangGraph 怎么做状态编排",
            )

            prompt = loop._build_system_prompt(ctx)
            self.assertIn("## 动态上下文", prompt)
            self.assertIn("自学习状态", prompt)
            self.assertIn("默认策略: 先学习、整理方案和补丁建议", prompt)

        asyncio.run(run_test())

    def test_list_skills(self) -> None:
        async def run_test() -> None:
            result = await self.plugin._handle_list_skills({}, {})
            self.assertTrue(result.ok)

        asyncio.run(run_test())

    def test_code_line_limit(self) -> None:
        async def run_test() -> None:
            self.plugin._allow_code_execution = True
            self.plugin._acknowledge_unsafe_execution = True
            self.plugin._super_admin_only = False
            self.plugin._max_code_lines = 10
            self.plugin._auto_test = False
            self.plugin._save_skills = False

            long_code = "\n".join([f"print({i})" for i in range(20)])
            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "long_skill",
                    "description": "测试",
                    "code": long_code,
                },
                {"permission_level": "super_admin"},
            )
            self.assertFalse(result.ok)
            self.assertIn("超过限制", str(result.display))

        asyncio.run(run_test())


class TestSandboxSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = Plugin()
        self.plugin._allow_code_execution = True
        self.plugin._acknowledge_unsafe_execution = True
        self.plugin._sandbox_mode = "isolated"
        self.plugin._test_timeout = 5
        self.plugin._execution_backend_name = "local_subprocess"
        from plugins.self_learning_runtime import create_code_execution_backend
        self.plugin._code_runner = create_code_execution_backend(
            "local_subprocess",
            sandbox_root=Path(__file__).parent.parent / "storage" / "sandbox",
        )

    def test_file_access_attempt_is_rejected_before_execution(self) -> None:
        async def run_test() -> None:
            code = """
import os
with open('../outside.txt', 'w', encoding='utf-8') as f:
    f.write('escape')
"""
            result = await self.plugin._test_code_in_sandbox(code)
            self.assertFalse(result["ok"])
            self.assertIn("拒绝执行", result["error"])

        asyncio.run(run_test())

    def test_timeout_protection(self) -> None:
        async def run_test() -> None:
            self.plugin._test_timeout = 1
            code = """
while True:
    pass
"""
            result = await self.plugin._test_code_in_sandbox(code)
            self.assertFalse(result["ok"])
            self.assertIn("超时", result["error"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
