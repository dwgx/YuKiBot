"""平台工具全量冒烟测试 — 逐一验证每个已注册工具的调用链路。

设计思路:
    1. 构建一个完全隔离的 AgentToolRegistry，注册所有内置工具
    2. 为每个工具注入 Mock 的 api_call / 外部依赖
    3. 逐工具调用 registry.call()，验证：
       - handler 不会抛出未捕获异常
       - 返回值是 ToolCallResult
       - ok=True 或有明确的 error 字符串（不是 unknown_tool）
    4. 分组断言确保覆盖率

运行方式:
    python -m pytest tests/test_platform_tool_smoke.py -v
    或:
    python tests/test_platform_tool_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_types import ToolCallResult, ToolSchema


# ---------------------------------------------------------------------------
# Mock factories — 模拟所有外部依赖
# ---------------------------------------------------------------------------

def _mock_api_call() -> AsyncMock:
    """模拟 NapCat OneBot V11 API 调用。"""
    api = AsyncMock()
    api.return_value = {"status": "ok", "data": {}, "message_id": 12345}
    return api


def _mock_search_engine() -> MagicMock:
    """模拟搜索引擎。"""
    se = MagicMock()
    se.search = AsyncMock(return_value=[])
    se.search_images = AsyncMock(return_value=[])
    se.search_videos = AsyncMock(return_value=[])
    se.search_download = AsyncMock(return_value=[])
    se.hot_trends = AsyncMock(return_value=[])
    se.search_zhihu = AsyncMock(return_value=[])
    se.lookup_wiki = AsyncMock(return_value="")
    return se


def _mock_image_engine() -> MagicMock:
    """模拟图片引擎。"""
    ie = MagicMock()
    ie.generate = AsyncMock(return_value="https://example.com/generated.png")
    return ie


def _mock_model_client() -> MagicMock:
    """模拟 LLM 客户端。"""
    mc = MagicMock()
    mc.enabled = True
    mc.model = "mock-model"
    mc.chat_text = AsyncMock(return_value="mock LLM response")
    mc.chat_text_with_retry = AsyncMock(return_value="mock LLM response")
    mc.chat_completion = AsyncMock(return_value={
        "choices": [{"message": {"content": "mock response"}}]
    })
    mc.chat_completion_with_retry = AsyncMock(return_value={
        "choices": [{"message": {"content": "mock response"}}]
    })
    mc.supports_vision_input = MagicMock(return_value=False)
    return mc


def _mock_sticker_manager() -> MagicMock:
    """模拟表情管理器。"""
    sm = MagicMock()
    sm.list_faces = MagicMock(return_value=[{"id": 1, "name": "微笑"}])
    sm.list_emojis = MagicMock(return_value=[{"key": "test", "url": "https://example.com/e.gif"}])
    sm.list_categories = MagicMock(return_value=["默认"])
    sm.search = MagicMock(return_value=None)
    sm.learn = AsyncMock(return_value="学习成功")
    sm.correct = MagicMock(return_value="修正完成")
    sm.find_best_match = MagicMock(return_value=None)
    sm.scan_stickers = AsyncMock(return_value="扫描完毕")
    return sm


def _mock_tool_executor() -> MagicMock:
    """模拟工具执行器。"""
    te = MagicMock()
    te.execute = AsyncMock(return_value=MagicMock(ok=True, tool_name="mock", payload={}))
    return te


def _mock_crawler_hub() -> MagicMock:
    """模拟爬虫中心。"""
    ch = MagicMock()
    ch.extract = AsyncMock(return_value="extracted content")
    ch.summarize = AsyncMock(return_value="summary")
    ch.structured = AsyncMock(return_value={})
    ch.follow_links = AsyncMock(return_value=[])
    return ch


def _mock_knowledge_base() -> MagicMock:
    """模拟知识库。"""
    kb = MagicMock()
    kb.search = AsyncMock(return_value=[])
    kb.learn = AsyncMock(return_value="已学习")
    return kb


def _mock_memory_engine() -> MagicMock:
    """模拟记忆引擎。"""
    me = MagicMock()
    me.list_records = AsyncMock(return_value=[])
    me.add_record = AsyncMock(return_value="added")
    me.update_record = AsyncMock(return_value="updated")
    me.delete_record = AsyncMock(return_value="deleted")
    me.audit_record = AsyncMock(return_value="audited")
    me.compact = AsyncMock(return_value="compacted")
    me.remember_user_fact = AsyncMock(return_value="remembered")
    me.recall_about_user = AsyncMock(return_value=[])
    me.summarize_conversation = AsyncMock(return_value="summary")
    return me


def _build_standard_context(**overrides: Any) -> dict[str, Any]:
    """构建标准工具调用上下文。"""
    base = {
        "api_call": _mock_api_call(),
        "admin_handler": AsyncMock(return_value=None),
        "config_patch_handler": AsyncMock(return_value=(True, "ok", {})),
        "sticker_manager": _mock_sticker_manager(),
        "tool_executor": _mock_tool_executor(),
        "crawler_hub": _mock_crawler_hub(),
        "knowledge_base": _mock_knowledge_base(),
        "memory_engine": _mock_memory_engine(),
        "conversation_id": "group:999:user:10001",
        "user_id": "10001",
        "user_name": "TestUser",
        "group_id": 999,
        "bot_id": "bot_1",
        "is_private": False,
        "mentioned": True,
        "explicit_bot_addressed": True,
        "trace_id": "smoke-test-001",
        "message_text": "测试消息",
        "original_message_text": "测试消息",
        "message_id": "99999",
        "raw_segments": [],
        "reply_media_segments": [],
        "reply_to_message_id": "",
        "reply_to_user_id": "",
        "reply_to_user_name": "",
        "reply_to_text": "",
        "at_other_user_ids": [],
        "at_other_user_names": {},
        "memory_context": [],
        "related_memories": [],
        "user_profile_summary": "",
        "preferred_name": "",
        "recent_speakers": [],
        "thread_state": {},
        "runtime_group_context": [],
        "runtime_admin_policy": {},
        "media_summary": [],
        "reply_media_summary": [],
        "event_payload": {},
        "user_policies": {},
        "user_directives": [],
        "sender_role": "owner",
        "is_whitelisted_group": True,
        "is_admin_user": True,
        "permission_level": "super_admin",
        "config": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Registry factory — 构建完整的工具注册表
# ---------------------------------------------------------------------------

def _build_full_registry() -> AgentToolRegistry:
    """注册所有内置工具到一个干净的注册表中，使用 mock 依赖。"""
    from core.agent_tools_registry import register_builtin_tools

    registry = AgentToolRegistry()
    search_engine = _mock_search_engine()
    image_engine = _mock_image_engine()
    model_client = _mock_model_client()
    config = {
        "admin": {"super_users": ["10001"], "whitelist_groups": [999]},
        "agent": {"enable": True, "max_steps": 6},
        "search": {},
        "vision": {"enable": False},
        "qzone": {"enable": False},
    }
    register_builtin_tools(registry, search_engine, image_engine, model_client, config)
    return registry


# ---------------------------------------------------------------------------
# 针对每个工具的参数模板
# ---------------------------------------------------------------------------

# 每个工具的最小合法参数，确保通过校验层
TOOL_ARG_TEMPLATES: dict[str, dict[str, Any]] = {
    # === Utility ===
    "final_answer": {"text": "冒烟测试回复"},
    "think": {"thought": "正在思考..."},
    "send_face": {"query": "微笑"},
    "send_emoji": {"query": "开心"},
    "send_sticker": {"query": "可爱"},
    "list_faces": {},
    "list_emojis": {},
    "browse_sticker_categories": {},
    "learn_sticker": {"key": "test_sticker", "url": "https://example.com/s.gif"},
    "correct_sticker": {"key": "test_sticker"},
    "scan_stickers": {},

    # === NapCat (core) ===
    "send_group_message": {"group_id": "123456", "message": "hello"},
    "send_private_message": {"user_id": "123456", "message": "hello"},
    "get_group_member_list": {"group_id": "123456"},
    "get_group_info": {"group_id": "123456"},
    "get_user_info": {"user_id": "123456"},
    "get_message": {"message_id": "12345"},
    "delete_message": {"message_id": "12345"},
    "recall_recent_messages": {"count": 3},
    "set_group_ban": {"group_id": "123456", "user_id": "789012", "duration": 60},
    "set_group_card": {"group_id": "123456", "user_id": "789012", "card": "test"},
    "set_group_kick": {"group_id": "123456", "user_id": "789012"},
    "set_group_special_title": {"group_id": "123456", "user_id": "789012", "special_title": "test"},
    "get_group_honor_info": {"group_id": "123456"},
    "upload_group_file": {"group_id": "123456", "file": "/tmp/test.txt", "name": "test.txt"},
    "get_group_notice": {"group_id": "123456"},
    "send_group_notice": {"group_id": "123456", "content": "test notice"},
    "get_friend_list": {},
    "get_group_list": {},
    "send_like": {"user_id": "123456", "times": 1},
    "set_group_whole_ban": {"group_id": "123456", "enable": True},
    "set_group_admin": {"group_id": "123456", "user_id": "789012", "enable": True},
    "set_group_sign": {"group_id": "123456"},
    "set_group_name": {"group_id": "123456", "group_name": "test_name"},
    "get_login_info": {},
    "forward_message": {"user_id": "123456", "message_id": "12345"},
    "get_group_history": {"group_id": "123456"},
    "get_chat_history": {"user_id": "123456"},
    "get_group_files": {"group_id": "123456"},
    "get_group_file_url": {"group_id": "123456", "file_id": "abc123"},
    "get_muted_list": {"group_id": "123456"},
    "check_user_status": {"user_id": "123456"},
    "send_poke": {"user_id": "123456"},

    # === NapCat (extended batch 1) ===
    "group_poke": {"group_id": "123456", "user_id": "789012"},
    "friend_poke": {"user_id": "123456"},
    "set_msg_emoji_like": {"message_id": "12345", "emoji_id": 76},
    "get_group_msg_history": {"group_id": "123456"},
    "get_friend_msg_history": {"user_id": "123456"},
    "forward_group_single_msg": {"message_id": "12345", "group_id": "123456"},
    "forward_friend_single_msg": {"message_id": "12345", "user_id": "123456"},
    "get_essence_msg_list": {"group_id": "123456"},
    "set_essence_msg": {"message_id": "12345"},
    "ocr_image": {"image": "file:///tmp/test.png"},
    "get_ai_characters": {"group_id": "123456"},
    "send_group_ai_record": {"group_id": "123456", "character": "char_1", "text": "你好"},
    "mark_msg_as_read": {"message_id": "12345"},
    "get_group_shut_list": {"group_id": "123456"},
    "get_group_member_info": {"group_id": "123456", "user_id": "789012"},
    "set_input_status": {"user_id": "123456", "event_type": 1},
    "download_file": {"url": "https://example.com/file.txt"},
    "smart_download": {"url": "https://example.com/app.apk"},
    "nc_get_user_status": {"user_id": "123456"},
    "translate_en2zh": {"words": ["hello", "world"]},

    # === NapCat (extended batch 2 — forwarding/group ops) ===
    "send_group_forward_msg": {"group_id": "123456", "messages": [{"type": "node", "data": {"id": "12345"}}]},
    "send_private_forward_msg": {"user_id": "123456", "messages": [{"type": "node", "data": {"id": "12345"}}]},
    "get_forward_msg": {"message_id": "12345"},
    "set_group_leave": {"group_id": "123456"},
    "delete_essence_msg": {"message_id": "12345"},
    "get_group_at_all_remain": {"group_id": "123456"},
    "set_friend_add_request": {"flag": "test_flag"},
    "set_group_add_request": {"flag": "test_flag", "sub_type": "add"},
    "delete_friend": {"user_id": "123456"},
    "get_group_file_system_info": {"group_id": "123456"},
    "get_group_root_files": {"group_id": "123456"},
    "upload_private_file": {"user_id": "123456", "file": "/tmp/test.txt", "name": "test.txt"},
    "set_qq_avatar": {"file": "https://example.com/avatar.png"},
    "set_group_portrait": {"group_id": "123456", "file": "https://example.com/portrait.png"},
    "set_online_status": {"status": 11},
    "send_msg": {"message": "test message"},
    "check_url_safely": {"url": "https://example.com"},
    "get_status": {},
    "get_version_info": {},

    # === NapCat (extended batch 3 — profile/collection/misc) ===
    "set_self_longnick": {"longnick": "测试签名"},
    "get_recent_contact": {},
    "get_profile_like": {},
    "fetch_custom_face": {},
    "fetch_emoji_like": {"message_id": "12345"},
    "get_group_info_ex": {"group_id": "123456"},
    "get_group_files_by_folder": {"group_id": "123456", "folder_id": "test_folder"},
    "delete_group_file": {"group_id": "123456", "file_id": "abc123"},
    "create_group_file_folder": {"group_id": "123456", "name": "test_folder"},
    "get_group_system_msg": {},
    "send_forward_msg": {"messages": [{"type": "node", "data": {"id": "12345"}}]},
    "mark_all_as_read": {},
    "get_friends_with_category": {},
    "get_image": {"file": "file:///tmp/test.png"},
    "get_record": {"file": "file:///tmp/test.amr"},
    "get_ai_record": {"group_id": "123456", "character": "char_1", "text": "你好"},
    "ark_share_peer": {},
    "get_mini_app_ark": {"content": "{}"},
    "create_collection": {"raw_data": "测试收藏"},
    "get_collection_list": {},
    "del_group_notice": {"group_id": "123456", "notice_id": "notice_1"},
    "nc_get_packet_status": {},
    "clean_cache": {},
    "set_qq_profile": {"nickname": "TestBot"},
    "send_group_sign": {"group_id": "123456"},
    "delete_group_folder": {"group_id": "123456", "folder_id": "test_folder"},
    "get_file": {"file_id": "abc123"},
    "get_cookies": {"domain": "qzone.qq.com"},
    "get_csrf_token": {},
    "get_credentials": {},
    "can_send_image": {},
    "can_send_record": {},
    "mark_private_msg_as_read": {"user_id": "123456"},
    "mark_group_msg_as_read": {"group_id": "123456"},
    "nc_get_rkey": {},
    "get_robot_uin_range": {},
    "get_group_ignore_add_request": {"group_id": "123456"},
    "ArkShareGroup": {"group_id": "123456"},

    # === Search ===
    "web_search": {"query": "python tutorial"},
    "search_web_media": {"query": "cute cats", "type": "image"},
    "search_download_resources": {"query": "python 3.11", "type": "software"},

    # === Media ===
    "generate_image": {"prompt": "a cute cat"},
    "parse_video": {"url": "https://example.com/video.mp4"},
    "analyze_video": {"url": "https://example.com/video.mp4"},
    "analyze_image": {"url": "https://example.com/image.png"},
    "analyze_voice": {"url": "https://example.com/voice.mp3"},
    "analyze_local_video": {"file_path": "/tmp/test.mp4"},
    "split_video": {"url": "https://example.com/video.mp4", "start": 0, "end": 10},

    # === Admin ===
    "admin_command": {"command": "status"},
    "config_update": {"key": "test_key", "value": "test_value"},
    "music_search": {"keyword": "周杰伦"},
    "music_play": {"keyword": "稻香"},
    "music_play_by_id": {"platform": "163", "song_id": "123456"},
    "bilibili_audio_extract": {"url": "https://www.bilibili.com/video/BV1xx"},

    # === Knowledge ===
    "get_hot_trends": {},
    "search_zhihu": {"query": "python学习"},
    "lookup_wiki": {"query": "Python"},
    "search_knowledge": {"query": "机器人"},
    "learn_knowledge": {"content": "这是测试知识"},
    "remember_user_fact": {"user_id": "123456", "fact": "喜欢编程"},
    "recall_about_user": {"user_id": "123456"},
    "summarize_conversation": {},

    # === Memory ===
    "memory_list": {},
    "memory_add": {"content": "test memory"},
    "memory_update": {"record_id": "1", "content": "updated memory"},
    "memory_delete": {"record_id": "1"},
    "memory_audit": {"record_id": "1"},
    "memory_compact": {},

    # === Social ===
    "daily_report": {},
    "user_portrait": {"user_id": "123456"},

    # === Web / Crawler ===
    "fetch_webpage": {"url": "https://example.com"},
    "github_search": {"query": "python bot"},
    "github_readme": {"repo": "owner/repo"},
    "douyin_search": {"query": "搞笑"},
    "get_qq_avatar": {"user_id": "123456"},
    "scrape_extract": {"url": "https://example.com", "query": "test"},
    "scrape_summarize": {"url": "https://example.com"},
    "scrape_structured": {"url": "https://example.com", "fields": ["title"]},
    "scrape_follow_links": {"url": "https://example.com", "query": "test"},

    # === QZone (可能被跳过) ===
    "get_qzone_profile": {"user_id": "123456"},
    "get_qzone_moods": {"user_id": "123456"},
    "get_qzone_albums": {"user_id": "123456"},
    "analyze_qzone": {"user_id": "123456"},
    "get_qzone_photos": {"user_id": "123456", "album_id": "1"},
}


# ===========================================================================
# Test Cases
# ===========================================================================


class PlatformToolRegistrationTests(unittest.TestCase):
    """验证工具注册表的完整性。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()

    def test_minimum_tool_count(self):
        """注册表应包含 30+ 工具。"""
        count = self.registry.tool_count
        self.assertGreaterEqual(count, 30, f"Expected 30+ tools, got {count}")
        print(f"\n✅ 已注册 {count} 个工具")

    def test_all_tools_have_handler(self):
        """每个 schema 都应有对应的 handler。"""
        for name in self.registry._schemas:
            self.assertIn(name, self.registry._handlers, f"Tool '{name}' has schema but no handler")

    def test_all_tools_have_description(self):
        """每个工具都应有描述。"""
        for name, schema in self.registry._schemas.items():
            self.assertTrue(bool(schema.description), f"Tool '{name}' has empty description")

    def test_all_tools_have_category(self):
        """每个工具都应有分类。"""
        for name, schema in self.registry._schemas.items():
            self.assertTrue(bool(schema.category), f"Tool '{name}' has empty category")

    def test_no_orphan_handler(self):
        """不应有孤立的 handler（无 schema）。"""
        for name in self.registry._handlers:
            self.assertIn(name, self.registry._schemas, f"Handler '{name}' has no schema")


class PlatformToolInvocationSmokeTests(unittest.TestCase):
    """逐工具冒烟测试 — 验证每个工具的调用链路不会崩溃。

    此测试类不验证业务正确性，只验证：
    1. handler 不会抛出未捕获异常
    2. 返回值是 ToolCallResult
    3. 不是 unknown_tool 错误
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()
        cls.context = _build_standard_context()
        cls._results: dict[str, tuple[bool, str]] = {}

    def _invoke_tool(self, tool_name: str, args: dict[str, Any]) -> ToolCallResult:
        """同步包装异步调用。"""
        return asyncio.run(self.registry.call(tool_name, args, dict(self.context)))

    def _test_tool(self, tool_name: str):
        """通用工具测试逻辑。"""
        if not self.registry.has_tool(tool_name):
            self.skipTest(f"Tool '{tool_name}' not registered")
            return

        args = TOOL_ARG_TEMPLATES.get(tool_name, {})
        try:
            result = self._invoke_tool(tool_name, args)
        except Exception as exc:
            self.__class__._results[tool_name] = (False, f"EXCEPTION: {type(exc).__name__}: {exc}")
            self.fail(f"Tool '{tool_name}' raised uncaught exception: {exc}")
            return

        self.assertIsInstance(result, ToolCallResult, f"Tool '{tool_name}' did not return ToolCallResult")
        self.assertNotEqual(result.error, f"unknown_tool: {tool_name}",
                           f"Tool '{tool_name}' returned unknown_tool error")

        status = "OK" if result.ok else f"HANDLED_ERROR: {result.error}"
        self.__class__._results[tool_name] = (True, status)


def _generate_tool_tests():
    """动态生成每个工具的独立测试方法。"""
    for tool_name in TOOL_ARG_TEMPLATES:
        def make_test(name):
            def test_method(self):
                self._test_tool(name)
            test_method.__doc__ = f"冒烟测试: {name}"
            return test_method
        setattr(PlatformToolInvocationSmokeTests, f"test_tool_{tool_name}", make_test(tool_name))


_generate_tool_tests()


class PlatformToolPermissionTests(unittest.TestCase):
    """权限分层验证 — 普通用户不应看到管理员工具。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()

    def test_user_cannot_call_super_admin_tools(self):
        """普通用户调用超级管理员工具应被拒绝。"""
        super_admin_tools = ["config_update", "admin_command", "cli_invoke"]
        context = _build_standard_context(permission_level="user", is_admin_user=False)

        for tool_name in super_admin_tools:
            if not self.registry.has_tool(tool_name):
                continue
            result = asyncio.run(self.registry.call(tool_name, {}, context))
            self.assertFalse(result.ok, f"Tool '{tool_name}' should be denied for user")
            self.assertIn("permission_denied", result.error,
                         f"Tool '{tool_name}' should return permission_denied")

    def test_group_admin_cannot_call_super_admin_tools(self):
        """群管理员调用超级管理员工具应被拒绝。"""
        context = _build_standard_context(permission_level="group_admin", is_admin_user=True)
        super_only = ["config_update", "admin_command"]
        for tool_name in super_only:
            if not self.registry.has_tool(tool_name):
                continue
            result = asyncio.run(self.registry.call(tool_name, {}, context))
            self.assertFalse(result.ok, f"Tool '{tool_name}' should be denied for group_admin")

    def test_super_admin_can_call_all_tools(self):
        """超级管理员应能调用所有工具。"""
        context = _build_standard_context(permission_level="super_admin", is_admin_user=True)
        tools_to_test = ["config_update", "admin_command", "web_search", "final_answer"]
        for tool_name in tools_to_test:
            if not self.registry.has_tool(tool_name):
                continue
            args = TOOL_ARG_TEMPLATES.get(tool_name, {})
            result = asyncio.run(self.registry.call(tool_name, args, dict(context)))
            # 不检查 ok（可能业务层失败），只确保不是 permission_denied
            if not result.ok:
                self.assertNotIn("permission_denied", result.error,
                               f"Super admin should not be permission_denied for '{tool_name}'")


class PlatformToolArgValidationTests(unittest.TestCase):
    """参数校验层冒烟测试 — 验证类型转换和必填参数。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()

    def test_missing_required_arg_rejected(self):
        """缺少必填参数应返回 invalid_args 错误。"""
        context = _build_standard_context()
        # web_search 需要 query
        if self.registry.has_tool("web_search"):
            result = asyncio.run(self.registry.call("web_search", {}, context))
            self.assertFalse(result.ok)
            self.assertIn("invalid_args", result.error)

    def test_qq_id_validation_rejects_garbage(self):
        """QQ ID 字段应拒绝垃圾输入。"""
        context = _build_standard_context()
        if self.registry.has_tool("set_group_ban"):
            result = asyncio.run(self.registry.call(
                "set_group_ban",
                {"group_id": "not_a_number", "user_id": "789012"},
                context,
            ))
            self.assertFalse(result.ok)
            self.assertIn("invalid", result.error)

    def test_integer_coercion(self):
        """字符串数字应被正确转换为整数。"""
        sanitized, err = self.registry._sanitize_and_validate_args(
            "set_group_ban",
            {"group_id": "123456", "user_id": "789012", "duration": "60"},
        )
        self.assertEqual(err, "")
        self.assertIsInstance(sanitized["group_id"], int)


class PlatformToolCoverageTests(unittest.TestCase):
    """覆盖率验证 — 确保测试模板覆盖了所有已注册工具。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()

    def test_all_registered_tools_have_templates(self):
        """每个已注册工具都应有参数模板。"""
        registered = set(self.registry._schemas.keys())
        templated = set(TOOL_ARG_TEMPLATES.keys())
        missing = registered - templated
        # 不严格断言，只报告（有些工具可能是动态注册的）
        if missing:
            print(f"\n⚠️ 以下 {len(missing)} 个工具没有冒烟测试模板（可能是动态注册的）:")
            for name in sorted(missing):
                print(f"   - {name}")
        # 至少 90% 覆盖率
        coverage = len(templated & registered) / max(len(registered), 1)
        self.assertGreaterEqual(coverage, 0.85,
                               f"Tool coverage {coverage:.0%} is below 85% threshold")


# ===========================================================================
# Summary report
# ===========================================================================


class _FinalSummaryTests(unittest.TestCase):
    """最终测试 — 输出汇总报告（放在最后执行）。"""

    def test_zzz_print_summary(self):
        """输出冒烟测试汇总报告。"""
        results = PlatformToolInvocationSmokeTests._results
        if not results:
            self.skipTest("No invocation results to report")
            return

        ok_count = sum(1 for passed, _ in results.values() if passed)
        fail_count = len(results) - ok_count

        print("\n" + "=" * 60)
        print("📊 平台工具冒烟测试汇总报告")
        print("=" * 60)
        print(f"  总计测试: {len(results)} 个工具")
        print(f"  ✅ 通过:  {ok_count}")
        print(f"  ❌ 失败:  {fail_count}")
        print("-" * 60)

        for name, (passed, detail) in sorted(results.items()):
            icon = "✅" if passed else "❌"
            print(f"  {icon} {name:40s} {detail}")

        print("=" * 60)
        self.assertEqual(fail_count, 0, f"{fail_count} tools failed smoke test")


if __name__ == "__main__":
    # Windows 上推荐使用 SelectorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main(verbosity=2)
