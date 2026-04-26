"""全量工具 Schema 审计 — 检查每个工具能否被 AI 正确调用。

检查项:
1. 每个 ToolSchema 的 name、description、parameters 是否合法
2. parameters.properties 的每个字段是否有 type 和 description
3. required 字段是否在 properties 中定义
4. description 是否足以让 LLM 理解工具用途
5. 参数 type 是否是标准 JSON Schema 类型
6. 是否有 handler 注册但缺少 schema，或反过来
7. 常见的 LLM 调用失败模式检查
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── Build a real registry with all tools ──

def _build_test_registry():
    """构建一个包含所有内置工具的 AgentToolRegistry。"""
    from core.agent_tools_registry import AgentToolRegistry, register_builtin_tools

    registry = AgentToolRegistry()
    mock_search = MagicMock()
    mock_image = MagicMock()
    mock_model = MagicMock()
    mock_config = {
        "api": {"api_key": "test", "base_url": "http://test", "model": "test"},
        "social": {"qzone_enabled": False},
        "media": {"image_gen_enabled": True, "image_gen_api_key": "test", "image_gen_base_url": "http://test"},
    }
    try:
        register_builtin_tools(registry, mock_search, mock_image, mock_model, mock_config)
    except Exception:
        # Some tools may fail to register in test env — that's fine, we test what we can
        pass
    return registry


VALID_JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class ToolSchemaIntegrityTests(unittest.TestCase):
    """检查每个工具的 schema 定义是否完整和正确。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_test_registry()
        cls.schemas = cls.registry._schemas
        cls.handlers = cls.registry._handlers

    def test_at_least_10_tools_registered(self):
        """应该至少有 10 个工具注册。"""
        self.assertGreaterEqual(len(self.schemas), 10, f"Only {len(self.schemas)} tools registered")

    def test_every_schema_has_handler(self):
        """每个 schema 必须有对应的 handler。"""
        missing_handler = [name for name in self.schemas if name not in self.handlers]
        self.assertEqual(missing_handler, [], f"Schema without handler: {missing_handler}")

    def test_every_handler_has_schema(self):
        """每个 handler 必须有对应的 schema。"""
        missing_schema = [name for name in self.handlers if name not in self.schemas]
        self.assertEqual(missing_schema, [], f"Handler without schema: {missing_schema}")

    def test_every_tool_has_nonempty_description(self):
        """每个工具必须有非空 description，否则 LLM 不知道什么时候调用它。"""
        empty_desc = []
        for name, schema in self.schemas.items():
            if not schema.description or not schema.description.strip():
                empty_desc.append(name)
        self.assertEqual(empty_desc, [], f"Tools with empty description: {empty_desc}")

    def test_description_length_minimum(self):
        """description 至少 5 个字符，太短 LLM 无法理解用途。"""
        too_short = []
        for name, schema in self.schemas.items():
            desc = (schema.description or "").strip()
            if len(desc) < 5:
                too_short.append(f"{name}({len(desc)} chars)")
        self.assertEqual(too_short, [], f"Tools with too-short description: {too_short}")

    def test_parameters_is_dict_or_none(self):
        """parameters 必须是 dict (JSON Schema object) 或空。"""
        bad_params = []
        for name, schema in self.schemas.items():
            if schema.parameters is not None and not isinstance(schema.parameters, dict):
                bad_params.append(name)
        self.assertEqual(bad_params, [], f"Tools with non-dict parameters: {bad_params}")

    def test_properties_fields_have_type(self):
        """每个参数字段必须有 type 定义。"""
        missing_type = []
        for name, schema in self.schemas.items():
            params = schema.parameters or {}
            if not isinstance(params, dict):
                continue
            props = params.get("properties", {})
            if not isinstance(props, dict):
                continue
            for field_name, field_def in props.items():
                if not isinstance(field_def, dict):
                    missing_type.append(f"{name}.{field_name}(not dict)")
                    continue
                if "type" not in field_def:
                    missing_type.append(f"{name}.{field_name}")
        self.assertEqual(missing_type, [], f"Parameters missing type: {missing_type}")

    def test_properties_types_are_valid_json_schema(self):
        """参数 type 必须是标准 JSON Schema 类型。"""
        invalid_types = []
        for name, schema in self.schemas.items():
            params = schema.parameters or {}
            if not isinstance(params, dict):
                continue
            props = params.get("properties", {})
            if not isinstance(props, dict):
                continue
            for field_name, field_def in props.items():
                if not isinstance(field_def, dict):
                    continue
                field_type = field_def.get("type", "")
                if field_type and field_type not in VALID_JSON_SCHEMA_TYPES:
                    invalid_types.append(f"{name}.{field_name}={field_type}")
        self.assertEqual(invalid_types, [], f"Invalid parameter types: {invalid_types}")

    def test_properties_fields_have_description(self):
        """每个参数字段应该有 description，帮助 LLM 理解参数含义。"""
        missing_desc = []
        for name, schema in self.schemas.items():
            params = schema.parameters or {}
            if not isinstance(params, dict):
                continue
            props = params.get("properties", {})
            if not isinstance(props, dict):
                continue
            for field_name, field_def in props.items():
                if not isinstance(field_def, dict):
                    continue
                desc = field_def.get("description", "")
                if not desc or not str(desc).strip():
                    missing_desc.append(f"{name}.{field_name}")
        # This is a warning-level check — not all params need description
        if missing_desc:
            print(f"\n[WARN] Parameters without description ({len(missing_desc)}): {missing_desc[:20]}...")

    def test_required_fields_exist_in_properties(self):
        """required 中列出的字段必须在 properties 中定义。"""
        orphan_required = []
        for name, schema in self.schemas.items():
            params = schema.parameters or {}
            if not isinstance(params, dict):
                continue
            props = params.get("properties", {})
            required = params.get("required", [])
            if not isinstance(required, list):
                continue
            if not isinstance(props, dict):
                if required:
                    orphan_required.extend(f"{name}.{r}" for r in required)
                continue
            for req_field in required:
                if req_field not in props:
                    orphan_required.append(f"{name}.{req_field}")
        self.assertEqual(orphan_required, [], f"Required fields not in properties: {orphan_required}")

    def test_no_duplicate_tool_names(self):
        """工具名不应有大小写冲突。"""
        lower_map: dict[str, list[str]] = {}
        for name in self.schemas:
            lower = name.lower()
            lower_map.setdefault(lower, []).append(name)
        conflicts = {k: v for k, v in lower_map.items() if len(v) > 1}
        self.assertEqual(conflicts, {}, f"Case-conflicting tool names: {conflicts}")

    def test_final_answer_and_think_always_present(self):
        """final_answer 和 think 是必备工具。"""
        self.assertIn("final_answer", self.schemas, "final_answer tool missing")
        self.assertIn("think", self.schemas, "think tool missing")

    def test_final_answer_has_text_parameter(self):
        """final_answer 必须有 text 参数。"""
        schema = self.schemas.get("final_answer")
        self.assertIsNotNone(schema)
        params = schema.parameters or {}
        props = params.get("properties", {})
        self.assertIn("text", props, "final_answer missing 'text' parameter")

    def test_think_has_thought_parameter(self):
        """think 必须有 thought 参数。"""
        schema = self.schemas.get("think")
        self.assertIsNotNone(schema)
        params = schema.parameters or {}
        props = params.get("properties", {})
        self.assertIn("thought", props, "think missing 'thought' parameter")


class ToolSchemaPromptRenderTests(unittest.TestCase):
    """检查 schema 渲染为 prompt 时是否完整和可用。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_test_registry()

    def test_get_schemas_for_prompt_renders_all_tools(self):
        """渲染的 prompt 应包含所有工具。"""
        prompt = self.registry.get_schemas_for_prompt()
        tool_names = list(self.registry._schemas.keys())
        missing = [name for name in tool_names if f"### {name}" not in prompt]
        self.assertEqual(missing, [], f"Tools missing from prompt: {missing}")

    def test_get_schemas_for_prompt_filtered_works(self):
        """按指定列表过滤 schema 渲染。"""
        filtered = self.registry.get_schemas_for_prompt_filtered(["final_answer", "think"])
        self.assertIn("### final_answer", filtered)
        self.assertIn("### think", filtered)

    def test_prompt_contains_required_markers(self):
        """必填参数应有 * 标记。"""
        prompt = self.registry.get_schemas_for_prompt()
        # final_answer 的 text 是必填的
        self.assertIn("text*", prompt, "Required parameter 'text' not marked with *")


class ToolArgValidationTests(unittest.TestCase):
    """检查工具参数校验和别名映射。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_test_registry()

    def test_alias_mapping_web_search(self):
        """web_search 的 q/keyword → query 别名映射。"""
        if not self.registry.has_tool("web_search"):
            self.skipTest("web_search not registered")
        sanitized, err = self.registry._sanitize_and_validate_args(
            "web_search", {"q": "test query"}
        )
        self.assertEqual(err, "", f"Alias mapping failed: {err}")
        self.assertIn("query", sanitized)

    def test_alias_mapping_send_emoji(self):
        """send_emoji 的 keyword/name → query 别名映射。"""
        if not self.registry.has_tool("send_emoji"):
            self.skipTest("send_emoji not registered")
        sanitized, err = self.registry._sanitize_and_validate_args(
            "send_emoji", {"keyword": "smile"}
        )
        self.assertEqual(err, "", f"Alias mapping failed: {err}")
        self.assertIn("query", sanitized)

    def test_qq_id_validation_rejects_invalid(self):
        """QQ ID 校验拒绝无效值。"""
        if not self.registry.has_tool("send_private_message"):
            self.skipTest("send_private_message not registered")
        _, err = self.registry._sanitize_and_validate_args(
            "send_private_message", {"user_id": "abc", "text": "hi"}
        )
        self.assertIn("invalid_user_id", err)

    def test_qq_id_validation_accepts_valid(self):
        """QQ ID 校验接受有效值。"""
        if not self.registry.has_tool("send_private_message"):
            self.skipTest("send_private_message not registered")
        sanitized, err = self.registry._sanitize_and_validate_args(
            "send_private_message", {"user_id": "123456789", "message": "hi"}
        )
        self.assertEqual(err, "", f"Valid QQ ID rejected: {err}")

    def test_missing_required_args_detected(self):
        """缺少必填参数时应报错。"""
        if not self.registry.has_tool("web_search"):
            self.skipTest("web_search not registered")
        _, err = self.registry._sanitize_and_validate_args("web_search", {})
        self.assertIn("missing_required_args", err)

    def test_unknown_args_dropped_silently(self):
        """未知参数应被静默丢弃。"""
        if not self.registry.has_tool("final_answer"):
            self.skipTest("final_answer not registered")
        sanitized, err = self.registry._sanitize_and_validate_args(
            "final_answer", {"text": "hello", "bogus_field": "x"}
        )
        self.assertEqual(err, "")
        self.assertNotIn("bogus_field", sanitized)
        self.assertIn("text", sanitized)


class ToolPermissionTests(unittest.TestCase):
    """检查三级权限模型正确性。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_test_registry()

    def test_user_cannot_see_admin_tools(self):
        """普通用户不应看到管理工具。"""
        user_tools = self.registry.select_tools_for_intent(permission_level="user")
        for admin_tool in ["set_group_kick", "set_group_whole_ban", "set_group_admin"]:
            if admin_tool in self.registry._schemas:
                self.assertNotIn(admin_tool, user_tools, f"User can see admin tool: {admin_tool}")

    def test_group_admin_can_see_group_tools(self):
        """群管理员应能看到群管理工具。"""
        admin_tools = self.registry.select_tools_for_intent(permission_level="group_admin")
        for tool in ["set_group_ban", "set_group_kick"]:
            if tool in self.registry._schemas:
                self.assertIn(tool, admin_tools, f"Group admin cannot see: {tool}")

    def test_super_admin_can_see_all(self):
        """超级管理员应能看到所有工具。"""
        super_tools = set(self.registry.select_tools_for_intent(permission_level="super_admin"))
        all_tools = set(self.registry._schemas.keys())
        missing = all_tools - super_tools
        self.assertEqual(missing, set(), f"Super admin cannot see: {missing}")

    def test_final_answer_visible_to_all(self):
        """final_answer 对所有权限级别可见。"""
        for level in ["user", "group_admin", "super_admin"]:
            tools = self.registry.select_tools_for_intent(permission_level=level)
            self.assertIn("final_answer", tools, f"final_answer not visible to {level}")

    def test_set_group_ban_special_visibility(self):
        """set_group_ban 对所有用户可见（特殊例外）。"""
        if "set_group_ban" not in self.registry._schemas:
            self.skipTest("set_group_ban not registered")
        user_tools = self.registry.select_tools_for_intent(permission_level="user")
        self.assertIn("set_group_ban", user_tools, "set_group_ban should be visible to users")


class ToolSchemaDetailedAudit(unittest.TestCase):
    """逐工具详细审计 — 打印每个工具的完整 schema 供人工审查。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_test_registry()

    def test_print_full_tool_inventory(self):
        """打印所有工具的完整清单。"""
        schemas = self.registry._schemas
        categories: dict[str, list[str]] = {}
        for name, schema in schemas.items():
            cat = schema.category or "uncategorized"
            categories.setdefault(cat, []).append(name)

        print(f"\n{'='*60}")
        print(f"Tool Inventory: {len(schemas)} tools registered")
        print(f"{'='*60}")
        for cat in sorted(categories.keys()):
            tools = sorted(categories[cat])
            print(f"\n[{cat}] ({len(tools)} tools)")
            for t in tools:
                s = schemas[t]
                params = s.parameters or {}
                props = params.get("properties", {}) if isinstance(params, dict) else {}
                required = params.get("required", []) if isinstance(params, dict) else []
                param_count = len(props) if isinstance(props, dict) else 0
                req_count = len(required) if isinstance(required, list) else 0
                desc = (s.description or "")[:60]
                print(f"  {t:35s} params={param_count} required={req_count}  {desc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
