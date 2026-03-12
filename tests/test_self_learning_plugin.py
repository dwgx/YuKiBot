"""SelfLearning 插件测试

测试 Agent 自我学习系统的各项功能。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.self_learning import Plugin


class TestSelfLearningPlugin(unittest.TestCase):
    """测试 SelfLearning 插件"""

    def setUp(self):
        """测试前准备"""
        self.plugin = Plugin()
        self.config = {
            "enabled": True,
            "sandbox_mode": "isolated",
            "auto_test": True,
            "devlog_broadcast": True,
            "learning_source": "both",
            "save_skills": True,
            "max_learning_time_seconds": 300,
            "max_code_lines": 500,
            "test_timeout_seconds": 60,
            "devlog_cooldown_seconds": 30,
        }

    def test_plugin_initialization(self):
        """测试插件初始化"""
        self.assertEqual(self.plugin.name, "self_learning")
        self.assertTrue(self.plugin.agent_tool)
        self.assertFalse(self.plugin.internal_only)

    def test_needs_setup(self):
        """测试配置检查"""
        # 配置文件不存在时应该返回 True
        result = Plugin.needs_setup()
        self.assertIsInstance(result, bool)

    def test_learn_from_web_validation(self):
        """测试学习工具参数验证"""
        async def run_test():
            from core.agent_tools import ToolCallResult

            # 模拟 registry
            mock_registry = MagicMock()
            self.plugin._registry = mock_registry

            # 测试缺少参数
            result = await self.plugin._handle_learn_from_web(
                {"topic": "Python"},  # 缺少 goal
                {}
            )
            self.assertIsInstance(result, ToolCallResult)
            self.assertFalse(result.ok)

            # 测试完整参数
            result = await self.plugin._handle_learn_from_web(
                {
                    "topic": "Python JSON 处理",
                    "goal": "学会解析 JSON",
                    "context": "用于 API 数据处理"
                },
                {}
            )
            self.assertIsInstance(result, ToolCallResult)
            self.assertTrue(result.ok)

        asyncio.run(run_test())

    def test_create_skill_validation(self):
        """测试技能创建参数验证"""
        async def run_test():
            from core.agent_tools import ToolCallResult

            self.plugin._auto_test = False  # 禁用自动测试以加快测试速度
            self.plugin._save_skills = False  # 禁用保存以避免文件操作

            # 测试无效的技能名称
            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "Invalid-Name",  # 包含非法字符
                    "description": "测试",
                    "code": "print('test')"
                },
                {}
            )
            self.assertIsInstance(result, ToolCallResult)
            self.assertFalse(result.ok)

            # 测试有效的技能名称
            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "valid_skill_name",
                    "description": "测试技能",
                    "code": "def test():\n    return True"
                },
                {}
            )
            self.assertIsInstance(result, ToolCallResult)
            self.assertTrue(result.ok)

        asyncio.run(run_test())

    def test_sandbox_code_execution(self):
        """测试沙盒代码执行"""
        async def run_test():
            self.plugin._sandbox_mode = "isolated"
            self.plugin._test_timeout = 5

            # 测试简单代码
            result = await self.plugin._test_code_in_sandbox(
                "print('Hello, World!')\nprint(2 + 2)"
            )
            self.assertTrue(result["ok"])
            self.assertIn("Hello, World!", result["output"])

            # 测试错误代码
            result = await self.plugin._test_code_in_sandbox(
                "raise ValueError('Test error')"
            )
            self.assertFalse(result["ok"])
            self.assertIn("ValueError", result["error"])

        asyncio.run(run_test())

    def test_devlog_cooldown(self):
        """测试 DEVLOG 冷却时间"""
        async def run_test():
            from core.agent_tools import ToolCallResult

            self.plugin._devlog_cooldown = 5
            self.plugin._devlog_broadcast = True
            self.plugin._last_devlog_time = 0

            # 模拟 context
            mock_api_call = AsyncMock()
            context = {
                "api_call": mock_api_call,
                "group_id": 123456,
            }

            # 第一次发送应该成功
            result = await self.plugin._handle_send_devlog(
                {"message": "测试日志 1", "log_type": "learning"},
                context
            )
            self.assertTrue(result.ok)

            # 立即再次发送应该失败（冷却中）
            result = await self.plugin._handle_send_devlog(
                {"message": "测试日志 2", "log_type": "learning"},
                context
            )
            self.assertFalse(result.ok)

        asyncio.run(run_test())

    def test_list_skills(self):
        """测试技能列表"""
        async def run_test():
            from core.agent_tools import ToolCallResult

            result = await self.plugin._handle_list_skills({}, {})
            self.assertIsInstance(result, ToolCallResult)
            self.assertTrue(result.ok)

        asyncio.run(run_test())

    def test_code_line_limit(self):
        """测试代码行数限制"""
        async def run_test():
            from core.agent_tools import ToolCallResult

            self.plugin._max_code_lines = 10
            self.plugin._auto_test = False
            self.plugin._save_skills = False

            # 生成超过限制的代码
            long_code = "\n".join([f"print({i})" for i in range(20)])

            result = await self.plugin._handle_create_skill(
                {
                    "skill_name": "long_skill",
                    "description": "测试",
                    "code": long_code
                },
                {}
            )
            self.assertFalse(result.ok)
            self.assertIn("超过限制", result.display)

        asyncio.run(run_test())


class TestSandboxSecurity(unittest.TestCase):
    """测试沙盒安全性"""

    def setUp(self):
        self.plugin = Plugin()
        self.plugin._sandbox_mode = "isolated"
        self.plugin._test_timeout = 5

    def test_file_access_restriction(self):
        """测试文件访问限制"""
        async def run_test():
            # 尝试读取系统文件（应该失败或受限）
            code = """
import os
try:
    with open('/etc/passwd', 'r') as f:
        print(f.read())
except Exception as e:
    print(f'Access denied: {e}')
"""
            result = await self.plugin._test_code_in_sandbox(code)
            # 在隔离模式下，应该无法访问系统文件
            self.assertTrue(result["ok"])  # 代码运行成功
            self.assertIn("Access denied", result["output"])  # 但访问被拒绝

        asyncio.run(run_test())

    def test_timeout_protection(self):
        """测试超时保护"""
        async def run_test():
            self.plugin._test_timeout = 2

            # 无限循环代码
            code = """
import time
while True:
    time.sleep(0.1)
"""
            result = await self.plugin._test_code_in_sandbox(code)
            self.assertFalse(result["ok"])
            self.assertIn("超时", result["error"])

        asyncio.run(run_test())


def run_tests():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加测试
    suite.addTests(loader.loadTestsFromTestCase(TestSelfLearningPlugin))
    suite.addTests(loader.loadTestsFromTestCase(TestSandboxSecurity))

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
