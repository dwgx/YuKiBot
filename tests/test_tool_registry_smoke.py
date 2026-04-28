"""工具注册表冒烟测试 — 验证工具元数据完整性和参数校验逻辑。

覆盖场景:
- 内置工具注册后的元数据完整性
- 参数校验器的类型转换和拒绝逻辑
- 权限分层控制
- QQ ID / Message ID 严格校验
"""
from __future__ import annotations

import unittest
from typing import Any

from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_types import ToolCallResult, ToolSchema

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_registry_with_tools() -> AgentToolRegistry:
    """构建一个包含几个测试工具的注册表。"""
    registry = AgentToolRegistry()

    async def _dummy_handler(args: dict, context: dict) -> ToolCallResult:
        return ToolCallResult(ok=True, display="ok")

    # 普通工具
    registry.register(
        ToolSchema(
            name="web_search",
            description="搜索互联网",
            parameters={
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "mode": {"type": "string", "description": "搜索模式"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _dummy_handler,
    )
    registry.register(
        ToolSchema(
            name="final_answer",
            description="给出最终回复",
            parameters={
                "properties": {
                    "text": {"type": "string", "description": "回复文本"},
                },
                "required": ["text"],
            },
            category="general",
        ),
        _dummy_handler,
    )
    registry.register(
        ToolSchema(
            name="think",
            description="内部思考",
            parameters={
                "properties": {
                    "thought": {"type": "string", "description": "思考内容"},
                },
                "required": ["thought"],
            },
            category="general",
        ),
        _dummy_handler,
    )
    # 管理员工具
    registry.register(
        ToolSchema(
            name="set_group_ban",
            description="禁言群成员",
            parameters={
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "user_id": {"type": "integer", "description": "用户QQ号"},
                    "duration": {"type": "integer", "description": "时长(秒)"},
                },
                "required": ["group_id", "user_id"],
            },
            category="admin",
        ),
        _dummy_handler,
    )
    # 超级管理员工具
    registry.register(
        ToolSchema(
            name="config_update",
            description="修改配置（不可逆）",
            parameters={
                "properties": {
                    "key": {"type": "string", "description": "配置键"},
                    "value": {"type": "string", "description": "配置值"},
                },
                "required": ["key", "value"],
            },
            category="admin",
        ),
        _dummy_handler,
    )
    return registry


# ===========================================================================
# A2.1: 工具元数据完整性
# ===========================================================================


class ToolRegistryMetadataTests(unittest.TestCase):
    """工具注册表元数据完整性测试。"""

    def test_all_tools_have_description(self):
        """每个注册的工具都应有描述。"""
        reg = _make_registry_with_tools()
        for name in ("web_search", "final_answer", "think", "set_group_ban", "config_update"):
            schema = reg.get_schema(name)
            self.assertIsNotNone(schema, f"{name} should be registered")
            self.assertTrue(bool(schema.description), f"{name} should have description")

    def test_all_tools_have_category(self):
        """每个注册的工具都应有分类。"""
        reg = _make_registry_with_tools()
        for name in ("web_search", "final_answer", "set_group_ban"):
            schema = reg.get_schema(name)
            self.assertIsNotNone(schema)
            self.assertTrue(bool(schema.category), f"{name} should have category")

    def test_tool_count(self):
        """工具数量应符合预期。"""
        reg = _make_registry_with_tools()
        self.assertEqual(reg.tool_count, 5)

    def test_no_duplicate_tool_names(self):
        """不应有重名工具。"""
        reg = _make_registry_with_tools()
        # 注册同名工具应覆盖
        old_count = reg.tool_count
        async def noop(args, context):
            return ToolCallResult(ok=True)
        reg.register(ToolSchema(name="web_search", description="v2", category="search"), noop)
        self.assertEqual(reg.tool_count, old_count)  # 不增加

    def test_has_tool(self):
        """has_tool 应正确判断。"""
        reg = _make_registry_with_tools()
        self.assertTrue(reg.has_tool("web_search"))
        self.assertTrue(reg.has_tool("final_answer"))
        self.assertFalse(reg.has_tool("nonexistent"))

    def test_schema_for_prompt_renders_required_params(self):
        """schema 渲染应标记必填参数。"""
        reg = _make_registry_with_tools()
        prompt = reg.get_schemas_for_prompt_filtered(["web_search"])
        self.assertIn("query*", prompt)  # 必填标记


# ===========================================================================
# A2.2: 参数校验器
# ===========================================================================


class ToolArgValidationTests(unittest.TestCase):
    """参数校验逻辑测试。"""

    def test_valid_args_pass(self):
        """合法参数应通过校验。"""
        reg = _make_registry_with_tools()
        sanitized, err = reg._sanitize_and_validate_args(
            "web_search", {"query": "python"}
        )
        self.assertEqual(err, "")
        self.assertEqual(sanitized["query"], "python")

    def test_missing_required_arg_rejected(self):
        """缺少必填参数应被拒绝。"""
        reg = _make_registry_with_tools()
        _, err = reg._sanitize_and_validate_args("web_search", {})
        self.assertIn("missing_required_args", err)

    def test_empty_required_arg_rejected(self):
        """空字符串的必填参数应被拒绝。"""
        reg = _make_registry_with_tools()
        _, err = reg._sanitize_and_validate_args("web_search", {"query": ""})
        self.assertIn("missing_required_args", err)

    def test_unknown_args_dropped(self):
        """未知参数应被丢弃（不报错）。"""
        reg = _make_registry_with_tools()
        sanitized, err = reg._sanitize_and_validate_args(
            "web_search", {"query": "python", "unknown_param": "value"}
        )
        self.assertEqual(err, "")
        self.assertNotIn("unknown_param", sanitized)

    def test_string_to_integer_coercion(self):
        """字符串 "12345" 应被转为 int。"""
        reg = _make_registry_with_tools()
        sanitized, err = reg._sanitize_and_validate_args(
            "set_group_ban", {"group_id": "123456", "user_id": "789012"}
        )
        self.assertEqual(err, "")
        self.assertIsInstance(sanitized["group_id"], int)

    def test_invalid_integer_rejected(self):
        """非数字字符串转 int 应被拒绝。"""
        reg = _make_registry_with_tools()
        _, err = reg._sanitize_and_validate_args(
            "set_group_ban", {"group_id": "not_a_number", "user_id": "789012"}
        )
        self.assertTrue(bool(err))

    def test_qq_id_validation(self):
        """QQ ID 应通过严格校验（5-12位数字，不以0开头）。"""
        reg = _make_registry_with_tools()
        # 合法 QQ
        _, err = reg._sanitize_and_validate_args(
            "set_group_ban", {"group_id": "123456", "user_id": "789012"}
        )
        self.assertEqual(err, "")

        # 太短
        _, err = reg._sanitize_and_validate_args(
            "set_group_ban", {"group_id": "123", "user_id": "789012"}
        )
        self.assertIn("invalid", err)

        # 以0开头
        _, err = reg._sanitize_and_validate_args(
            "set_group_ban", {"group_id": "0123456", "user_id": "789012"}
        )
        self.assertIn("invalid", err)

    def test_arg_alias_resolution(self):
        """参数别名应被正确解析。"""
        reg = _make_registry_with_tools()
        # web_search 的 q -> query 别名
        sanitized, err = reg._sanitize_and_validate_args(
            "web_search", {"q": "python"}
        )
        self.assertEqual(err, "")
        self.assertEqual(sanitized.get("query"), "python")

    def test_analyze_image_url_alias_resolution(self):
        """analyze_image 应接受模型常见的 image_url 参数别名。"""
        reg = AgentToolRegistry()

        async def _dummy_handler(args: dict, context: dict) -> ToolCallResult:
            return ToolCallResult(ok=True, display="ok")

        reg.register(
            ToolSchema(
                name="analyze_image",
                description="分析图片",
                parameters={
                    "properties": {
                        "url": {"type": "string", "description": "图片 URL"},
                        "question": {"type": "string", "description": "问题"},
                    },
                    "required": [],
                },
                category="media",
            ),
            _dummy_handler,
        )
        sanitized, err = reg._sanitize_and_validate_args(
            "analyze_image",
            {"image_url": "https://example.test/cat.png", "question": "看图"},
        )
        self.assertEqual(err, "")
        self.assertEqual(sanitized.get("url"), "https://example.test/cat.png")
        self.assertNotIn("image_url", sanitized)

    def test_boolean_coercion(self):
        """字符串 "true"/"false" 应被转为 bool。"""
        coerced, ok = AgentToolRegistry._coerce_basic_type("true", "boolean")
        self.assertTrue(ok)
        self.assertTrue(coerced)

        coerced, ok = AgentToolRegistry._coerce_basic_type("false", "boolean")
        self.assertTrue(ok)
        self.assertFalse(coerced)

        coerced, ok = AgentToolRegistry._coerce_basic_type("maybe", "boolean")
        self.assertFalse(ok)


# ===========================================================================
# A2.3: 权限控制
# ===========================================================================


class ToolPermissionTests(unittest.TestCase):
    """工具权限分层测试。"""

    def test_user_sees_basic_tools(self):
        """普通用户应能看到基础工具。"""
        reg = _make_registry_with_tools()
        tools = reg.select_tools_for_intent(
            message_text="你好", permission_level="user"
        )
        self.assertIn("web_search", tools)
        self.assertIn("final_answer", tools)

    def test_user_cannot_see_super_admin_tools(self):
        """普通用户不应看到超级管理员工具。"""
        reg = _make_registry_with_tools()
        tools = reg.select_tools_for_intent(
            message_text="修改配置", permission_level="user"
        )
        self.assertNotIn("config_update", tools)

    def test_group_admin_sees_group_tools(self):
        """群管理员应能看到群管理工具。"""
        reg = _make_registry_with_tools()
        tools = reg.select_tools_for_intent(
            message_text="禁言", permission_level="group_admin"
        )
        self.assertIn("set_group_ban", tools)

    def test_group_admin_cannot_see_super_admin_tools(self):
        """群管理员不应看到超级管理员工具。"""
        reg = _make_registry_with_tools()
        tools = reg.select_tools_for_intent(
            message_text="test", permission_level="group_admin"
        )
        self.assertNotIn("config_update", tools)

    def test_super_admin_sees_all(self):
        """超级管理员应能看到所有工具。"""
        reg = _make_registry_with_tools()
        tools = reg.select_tools_for_intent(
            message_text="test", permission_level="super_admin"
        )
        self.assertIn("config_update", tools)
        self.assertIn("set_group_ban", tools)
        self.assertIn("web_search", tools)

    def test_always_include_tools(self):
        """final_answer 和 think 在任何权限下都应可见。"""
        reg = _make_registry_with_tools()
        for level in ("user", "group_admin", "super_admin"):
            tools = reg.select_tools_for_intent(
                message_text="test", permission_level=level
            )
            self.assertIn("final_answer", tools, f"final_answer missing for {level}")
            self.assertIn("think", tools, f"think missing for {level}")


if __name__ == "__main__":
    unittest.main()
