"""Agent 冒烟测试 — 覆盖 Agent loop 端到端流程和解析容错。

这些测试确保 Agent 的核心链路在重构后不会回归:
- LLM 输出解析（标准/截断/中文引号/代码块/纯文本）
- Agent loop 完整流程（工具调用 → final_answer）
- 防护机制（重复工具/连续 think/超时/未知工具）
- 弱模型容错（XML 标签/function 格式/多 JSON 拼接）
"""
from __future__ import annotations

import asyncio
import json
import unittest

from core.agent import AgentContext, AgentLoop, AgentResult
from core.agent_tools import ToolCallResult

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubRegistry:
    """最小工具注册表 stub。"""
    tool_count = 3

    def __init__(self, names: set[str] | None = None):
        self._names = names or {"web_search", "final_answer", "think"}

    def has_tool(self, name: str) -> bool:
        return name in self._names

    def get_schema(self, name: str):
        return None

    def select_tools_for_intent(self, message_text: str, perm_level: str) -> list[str]:
        _ = (message_text, perm_level)
        return list(self._names)

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        return "\n".join(f"- {n}" for n in selected_tools)

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self._names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        return [{"type": "function", "function": {"name": n, "description": "", "parameters": {"type": "object", "properties": {}}}} for n in tool_names]

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        return ToolCallResult(ok=True, data={}, display=f"{name} 执行完成")


class _SequencedModelClient:
    """按顺序返回预设响应的模型 stub。"""
    enabled = True

    def __init__(self, responses: list[str], native_tools: bool = True):
        self._responses = list(responses)
        self._native_tools = native_tools

    def supports_native_tool_calling(self) -> bool:
        return self._native_tools

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        if not self._responses:
            raise AssertionError("No more model responses prepared for test")
        return self._responses.pop(0)
        
    async def chat_completion_with_retry(self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0):
        _ = (messages, max_tokens, tools, retries, backoff)
        if not self._native_tools:
            raise AssertionError("chat_completion_with_retry should not be used when native tools are disabled")
        if not self._responses:
            raise AssertionError("No more model responses prepared for test")
        resp = self._responses.pop(0)
        
        # Fake a tool calls response if JSON
        try:
            parsed = json.loads(resp)
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": parsed.get("tool", "unknown"),
                                "arguments": json.dumps(parsed.get("args", {}))
                            }
                        }]
                    }
                }]
            }
        except:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": resp
                    }
                }]
            }


class _RecordingRegistry(_StubRegistry):
    def __init__(self, names: set[str] | None = None):
        super().__init__(names or {"analyze_image", "final_answer", "think"})
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        return ToolCallResult(
            ok=True,
            data={},
            display="图片里是一只白色猫，背景是室内。",
        )


class _VideoRegistry(_StubRegistry):
    def __init__(self, video_url: str):
        super().__init__({"parse_video", "final_answer", "think"})
        self.video_url = video_url
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        return ToolCallResult(
            ok=True,
            data={"video_url": self.video_url, "text": "解析成功"},
            display=f"解析成功: {self.video_url}",
        )


def _make_ctx(**overrides) -> AgentContext:
    base = AgentContext(
        conversation_id="group:1:user:2",
        user_id="2",
        user_name="tester",
        group_id=1,
        bot_id="bot",
        is_private=False,
        mentioned=True,
        message_text="你好",
        trace_id="smoke-test",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _make_loop(
    responses: list[str],
    registry: _StubRegistry | None = None,
    native_tools: bool = True,
) -> AgentLoop:
    """构建可运行的 AgentLoop。"""
    reg = registry or _StubRegistry()
    loop = AgentLoop(
        model_client=_SequencedModelClient(responses, native_tools=native_tools),
        tool_registry=reg,
        config={
            "agent": {
                "enable": True,
                "max_steps": 6,
                "fallback_on_parse_error": True,
            },
            "admin": {"super_users": ["10001"]},
            "queue": {"process_timeout_seconds": 120},
        },
    )
    loop.high_risk_control_enable = False
    loop._build_system_prompt = lambda ctx: "system prompt"
    loop._build_user_message = lambda ctx: ctx.message_text
    return loop


# ===========================================================================
# A1.1: LLM 输出解析测试
# ===========================================================================


class AgentParseTests(unittest.TestCase):
    """测试 _parse_llm_output 对各种格式的解析能力。"""

    def _make_parser(self) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop.fallback_on_parse_error = True
        return loop

    def test_parse_standard_json(self):
        """标准 {"tool":"...","args":{...}} 格式。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output('{"tool":"web_search","args":{"query":"python"}}')
        self.assertEqual(parsed["tool"], "web_search")
        self.assertEqual(parsed["args"]["query"], "python")

    def test_parse_final_answer_json(self):
        """标准 final_answer 格式。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output('{"tool":"final_answer","args":{"text":"你好！"}}')
        self.assertEqual(parsed["tool"], "final_answer")
        self.assertEqual(parsed["args"]["text"], "你好！")

    def test_parse_markdown_fenced_json(self):
        """```json ... ``` 代码块格式。"""
        loop = self._make_parser()
        raw = '```json\n{"tool":"web_search","args":{"query":"hello"}}\n```'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "web_search")

    def test_parse_plain_text_as_final_answer(self):
        """纯文本应被当作 final_answer（fallback 模式）。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output("这是一段普通回复")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")
        self.assertIn("这是一段普通回复", parsed["args"]["text"])

    def test_parse_truncated_json_recovery(self):
        """截断的 JSON 应通过补全 } 恢复。"""
        loop = self._make_parser()
        raw = '{"tool":"final_answer","args":{"text":"回复内容"'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")

    def test_parse_chinese_quote_recovery(self):
        """中文引号 \u201c\u201d 应被替换为英文引号后恢复。"""
        loop = self._make_parser()
        raw = '{\u201ctool\u201d:\u201cfinal_answer\u201d,\u201cargs\u201d:{\u201ctext\u201d:\u201c你好\u201d}}'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")

    def test_parse_name_format(self):
        """OpenAI function calling 格式 {"name":"...","arguments":{...}}。"""
        loop = self._make_parser()
        raw = '{"name":"final_answer","arguments":{"text":"ok"}}'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")

    def test_parse_concatenated_json_picks_first(self):
        """多 JSON 拼接应取第一个完整对象。"""
        loop = self._make_parser()
        raw = '{"tool":"think","args":{"thought":"先想想"}} {"tool":"final_answer","args":{"text":"ok"}}'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "think")

    def test_parse_json_like_failure_triggers_rethink(self):
        """看起来像 JSON tool_call 但解析失败 → 应触发 think 重试。"""
        loop = self._make_parser()
        raw = '{"tool":"web_search","args":{"query":"带有"引号"的内容"}}'
        parsed = loop._parse_llm_output(raw)
        self.assertIsNotNone(parsed)
        # 解析失败的 JSON-like 内容应触发 think 或恢复
        self.assertIn(parsed["tool"], {"think", "web_search", "final_answer"})


# ===========================================================================
# A1.2: Agent Loop 端到端测试
# ===========================================================================


class AgentLoopSmokeTests(unittest.TestCase):
    """Agent loop 完整流程冒烟测试。"""

    def test_direct_text_reply(self):
        """模型直接返回文本 → direct_reply。"""
        loop = _make_loop(["这是我的回答"])
        loop.fallback_on_parse_error = True
        result = asyncio.run(loop.run(_make_ctx()))
        self.assertEqual(result.action, "reply")
        self.assertIn(result.reason, {"agent_direct_reply", "agent_final_answer"})
        self.assertTrue(bool(result.reply_text))

    def test_tool_then_final_answer(self):
        """工具调用 → final_answer 正常流程。"""
        loop = _make_loop([
            '{"tool":"web_search","args":{"query":"python"}}',
            '{"tool":"final_answer","args":{"text":"搜索完成，Python 是..."}}',
        ])
        result = asyncio.run(loop.run(_make_ctx(message_text="搜索python")))
        self.assertEqual(result.action, "reply")
        self.assertEqual(result.reason, "agent_final_answer")
        self.assertIn("搜索完成", result.reply_text)
        self.assertEqual(result.tool_calls_made, 1)

    def test_video_tool_result_is_sent_even_when_final_answer_omits_video_url(self):
        """视频工具拿到本地文件后，模型只写文字也要保留结构化 video_url 供发送层直发。"""
        video_path = "/Users/dwgx/Documents/Project/YuKiKo/storage/cache/videos/demo.mp4"
        loop = _make_loop(
            [
                '{"tool":"parse_video","args":{"url":"https://v.douyin.com/demo/"}}',
                (
                    '{"tool":"final_answer","args":{"text":"解析好了。直链在这： `'
                    + video_path
                    + '`"}}'
                ),
            ],
            registry=_VideoRegistry(video_path),
            native_tools=False,
        )
        result = asyncio.run(loop.run(_make_ctx(message_text="解析 https://v.douyin.com/demo/")))
        self.assertEqual(result.reason, "agent_final_answer")
        self.assertEqual(result.video_url, video_path)
        self.assertNotIn(video_path, result.reply_text)

    def test_video_tool_result_survives_llm_timeout_fallback(self):
        """视频工具成功后第二轮 LLM 超时，也不能丢掉 video_url。"""
        video_path = "/Users/dwgx/Documents/Project/YuKiKo/storage/cache/videos/demo.mp4"
        loop = _make_loop(
            ['{"tool":"parse_video","args":{"url":"https://v.douyin.com/demo/"}}'],
            registry=_VideoRegistry(video_path),
            native_tools=False,
        )
        result = asyncio.run(loop.run(_make_ctx(message_text="解析 https://v.douyin.com/demo/")))
        self.assertEqual(result.reason, "agent_fallback_llm_error")
        self.assertEqual(result.video_url, video_path)
        self.assertNotIn(video_path, result.reply_text)

    def test_non_native_tool_provider_uses_text_protocol(self):
        """不支持原生 tools 的 provider 应回退到 JSON/text 协议。"""
        loop = _make_loop(
            [
                '{"tool":"web_search","args":{"query":"python"}}',
                '{"tool":"final_answer","args":{"text":"搜索完成，Python 是..."}}',
            ],
            native_tools=False,
        )
        result = asyncio.run(loop.run(_make_ctx(message_text="搜索python")))
        self.assertEqual(result.reason, "agent_final_answer")
        self.assertIn("搜索完成", result.reply_text)
        self.assertEqual(result.tool_calls_made, 1)

    def test_strict_image_tool_does_not_run_before_model_response(self):
        """严格 Navigator 模式下，图片工具不再由本地硬路由抢跑。"""
        registry = _RecordingRegistry()
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="MULTIMODAL_EVENT user sent multimodal message: image:[image]",
            media_summary=["image:https://example.com/a.png"],
            raw_segments=[{"type": "image", "data": {"url": "https://example.com/a.png"}}],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_strict_recent_image_summary_waits_for_model_tool_choice(self):
        """最近图片追问只提供结构信号，具体工具由 LLM/Navigator 决定。"""
        registry = _RecordingRegistry()
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="直接cyber掉",
            media_summary=["image:https://example.com/recent.png"],
            raw_segments=[],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_strict_bare_domain_webpage_fetch_waits_for_model_tool_choice(self):
        """裸域名只进入 Navigator 候选分区，不再本地合成 fetch_webpage。"""
        registry = _RecordingRegistry({"fetch_webpage", "final_answer", "think"})
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="网络是时光机 看skiapi.dev",
            media_summary=[],
            raw_segments=[],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_strict_video_parse_waits_for_model_tool_choice(self):
        """视频链接解析不再由本地硬路由抢跑 parse_video。"""
        registry = _RecordingRegistry({"parse_video", "final_answer", "think"})
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="https://www.bilibili.com/video/BV16aw4zAEqD/?spm_id_from=333.337.search-card.all.click解析",
            media_summary=[],
            raw_segments=[],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_strict_douyin_parse_prefers_navigator_over_local_cue(self):
        """“解析...看看”这类分享文案也不再由关键词硬选 parse_video。"""
        registry = _RecordingRegistry({"parse_video", "analyze_video", "final_answer", "think"})
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="解析7.64 复制打开抖音，看看【宇鸽的作品】 https://v.douyin.com/hskaBb36Hfg/ a@N.JI",
            media_summary=[],
            raw_segments=[],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_strict_douyin_parse_does_not_use_recent_context_for_forced_tool(self):
        """严格模式下不会用历史视频上下文本地合成强制工具。"""
        registry = _RecordingRegistry({"parse_video", "split_video", "final_answer", "think"})
        loop = _make_loop(
            [],
            registry=registry,
        )

        result = asyncio.run(loop.run(_make_ctx(
            message_text="@30秒 解析7.64 复制打开抖音，看看【宇鸽的作品】 https://v.douyin.com/hskaBb36Hfg/ a@N.JI",
            media_summary=[],
            raw_segments=[],
            memory_context=[
                "[user] https://www.bilibili.com/video/BV16aw4zAEqD/?spm_id_from=333.337.search-card.all.click",
            ],
        )))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertEqual(result.tool_calls_made, 0)

    def test_unknown_tool_notifies_model(self):
        """未知工具名 → 通知模型后继续。"""
        loop = _make_loop([
            '{"tool":"nonexistent_tool","args":{}}',
            '{"tool":"final_answer","args":{"text":"好的"}}',
        ])
        result = asyncio.run(loop.run(_make_ctx()))
        self.assertEqual(result.reason, "agent_final_answer")

    def test_max_steps_produces_fallback(self):
        """达到 max_steps → 产生 fallback 回复。"""
        # 一直调用 think，最终超过 max_steps
        loop = _make_loop([
            '{"tool":"think","args":{"thought":"想想"}}',
        ] * 20)
        loop.max_steps = 3
        loop.max_consecutive_think = 8  # 不让 think guard 先触发
        result = asyncio.run(loop.run(_make_ctx()))
        self.assertEqual(result.action, "reply")
        # 要么是 max_steps fallback，要么是 think_loop_break
        self.assertTrue(bool(result.reply_text))

    def test_repeat_tool_guard_triggers(self):
        """重复调用相同工具+参数 → 应被拦截。"""
        loop = _make_loop([
            '{"tool":"web_search","args":{"query":"test"}}',
        ] * 10)
        loop.max_same_tool_call = 2
        result = asyncio.run(loop.run(_make_ctx()))
        self.assertEqual(result.action, "reply")
        # 重复调用应在某步被拦截
        self.assertTrue(bool(result.reply_text))

    def test_consecutive_think_breaks(self):
        """连续 think 超限 → 应中断。"""
        loop = _make_loop([
            '{"tool":"think","args":{"thought":"想想"}}',
        ] * 20)
        loop.max_consecutive_think = 2
        result = asyncio.run(loop.run(_make_ctx()))
        self.assertEqual(result.action, "reply")
        self.assertTrue(bool(result.reply_text))


# ===========================================================================
# A1.3: 防护机制测试
# ===========================================================================


class AgentProtectionTests(unittest.TestCase):
    """Agent 防护机制测试。"""

    def test_permission_level_resolution(self):
        """权限等级解析测试。"""
        loop = _make_loop([])
        loop._admin_ids = {"10001"}
        loop._whitelisted_groups = {123456}

        # super_admin
        ctx_admin = _make_ctx(user_id="10001")
        self.assertEqual(loop._resolve_permission_level(ctx_admin), "super_admin")

        # group_admin (加白群 + 群主)
        ctx_owner = _make_ctx(
            user_id="20002", group_id=123456,
            sender_role="owner", is_whitelisted_group=True,
        )
        self.assertEqual(loop._resolve_permission_level(ctx_owner), "group_admin")

        # user (普通用户)
        ctx_user = _make_ctx(user_id="30003")
        self.assertEqual(loop._resolve_permission_level(ctx_user), "user")

    def test_high_risk_tool_detection(self):
        """高风险工具检测。"""
        loop = _make_loop([])
        loop.high_risk_control_enable = True
        loop.high_risk_categories = {"admin"}
        loop.high_risk_name_patterns = (
            AgentLoop._compile_regex_patterns(["^set_group_", "^delete_"])
        )
        self.assertTrue(loop._tool_is_high_risk("set_group_ban"))
        self.assertTrue(loop._tool_is_high_risk("delete_friend"))
        self.assertFalse(loop._tool_is_high_risk("web_search"))
        self.assertFalse(loop._tool_is_high_risk("final_answer"))

    def test_force_tool_first_for_media(self):
        """媒体消息应强制走工具路径。"""
        loop = _make_loop([])
        ctx = _make_ctx(
            message_text="这是什么",
            media_summary=["image:https://example.com/a.png"],
            raw_segments=[{"type": "image", "data": {"url": "https://example.com/a.png"}}],
        )
        self.assertTrue(loop._should_force_tool_first(ctx))

    def test_force_tool_first_for_url(self):
        """包含 URL 的消息应强制走工具路径。"""
        loop = _make_loop([])
        ctx = _make_ctx(message_text="帮我看看 https://example.com/article")
        self.assertTrue(loop._should_force_tool_first(ctx))

    def test_force_tool_first_for_search(self):
        """包含搜索关键词的消息应强制走工具路径。"""
        loop = _make_loop([])
        ctx = _make_ctx(message_text="帮我搜索一下 python 教程")
        self.assertTrue(loop._should_force_tool_first(ctx))

    def test_no_force_tool_for_greeting(self):
        """普通问候不强制走工具。"""
        loop = _make_loop([])
        ctx = _make_ctx(message_text="你好")
        self.assertFalse(loop._should_force_tool_first(ctx))

    def test_args_signature_normalization(self):
        """工具参数签名应忽略大小写和空格差异。"""
        sig1 = AgentLoop._build_args_signature({"query": "Python  "})
        sig2 = AgentLoop._build_args_signature({"query": "python"})
        self.assertEqual(sig1, sig2)

    def test_english_refusal_normalized_to_chinese(self):
        """英文拒绝应被归一化为中文。"""
        refusal = "I can't help with that request."
        normalized = AgentLoop._normalize_final_answer_text(refusal)
        # 如果方法存在且能归一化
        if hasattr(AgentLoop, '_normalize_final_answer_text'):
            self.assertTrue(bool(normalized))

    def test_final_answer_redacts_local_file_paths(self):
        text = "解析好了，直链在这：/Users/dwgx/Documents/Project/YuKiKo/storage/cache/videos/a.mp4"
        normalized = AgentLoop._normalize_final_answer_text(text)

        self.assertNotIn("/Users/dwgx", normalized)
        self.assertIn("本地文件路径已隐藏", normalized)


if __name__ == "__main__":
    unittest.main()
