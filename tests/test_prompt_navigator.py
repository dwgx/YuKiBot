from __future__ import annotations

import asyncio
import json
import unittest

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult
from core.prompt_navigator import (
    PromptNavigator,
    default_prompt_navigator_payload,
    validate_prompt_navigator_payload,
)


class _Ctx:
    message_text = ""
    original_message_text = ""
    reply_to_text = ""
    media_summary: list[str] = []
    reply_media_summary: list[str] = []
    raw_segments: list[dict] = []
    reply_media_segments: list[dict] = []
    at_other_user_ids: list[str] = []


class PromptNavigatorConfigTests(unittest.TestCase):
    def test_video_url_preselects_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "https://www.acfun.cn/v/ac12345 帮我解析"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("video_url", state.candidate_sections)
        self.assertIn("video_url", state.evidence)

    def test_common_video_platform_urls_with_suffix_text_preselect_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        samples = [
            "https://www.bilibili.com/video/BV123解析",
            "https://v.douyin.com/abc123/看看",
            "https://www.acfun.cn/v/ac12345总结",
            "https://v.qq.com/x/cover/demo.html解析一下",
        ]
        for sample in samples:
            ctx = _Ctx()
            ctx.message_text = sample
            state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
            self.assertEqual(state.active_section, "video_url", sample)

    def test_media_segment_preselects_multimodal_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.raw_segments = [{"type": "image", "data": {"url": "file://demo.png"}}]
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "analyze_image"])
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_image", nav.scoped_tools(state))

    def test_section_tools_are_filtered_by_permission_visible_tools(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])
        ok, status = nav.switch_section(state, "qq_admin_social")
        self.assertTrue(ok, status)
        scoped = nav.scoped_tools(state)
        self.assertIn("final_answer", scoped)
        self.assertIn("navigate_section", scoped)
        self.assertNotIn("set_group_ban", scoped)

    def test_max_switches_stops_section_loop(self):
        payload = default_prompt_navigator_payload()
        payload["max_switches"] = 1
        nav = PromptNavigator.from_payload(payload)
        ctx = _Ctx()
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])
        self.assertTrue(nav.switch_section(state, "web_research")[0])
        ok, status = nav.switch_section(state, "video_url")
        self.assertFalse(ok)
        self.assertIn("max_switches", status)

    def test_validation_reports_missing_fallback_and_unknown_tool_warning(self):
        payload = default_prompt_navigator_payload()
        payload["sections"]["general_chat"]["fallback_sections"] = ["missing_section"]
        payload["sections"]["general_chat"]["tools"] = ["think", "unknown_tool"]
        errors, warnings = validate_prompt_navigator_payload(payload, known_tools={"think"})
        self.assertTrue(any("fallback" in item for item in errors))
        self.assertTrue(any("unknown_tool" in item for item in warnings))


class _SequencedModelClient:
    enabled = True

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        if not self.responses:
            raise AssertionError("No more responses")
        return self.responses.pop(0)


class _Registry:
    tool_count = 4

    def __init__(self):
        self.names = ["web_search", "think", "final_answer", "navigate_section"]
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self.names

    def get_schema(self, name: str):
        _ = name
        return None

    def select_tools_for_intent(self, message_text: str, permission_level: str) -> list[str]:
        _ = (message_text, permission_level)
        return list(self.names)

    def get_schemas_for_prompt_filtered(self, tool_names: list[str]) -> str:
        return "\n".join(f"### {name}" for name in tool_names if name in self.names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        _ = tool_names
        return []

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        return ToolCallResult(ok=True, display=f"{name} ok", data={"name": name})


class AgentPromptNavigatorTests(unittest.TestCase):
    def test_agent_can_switch_section_then_call_new_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {"section_id": "web_research", "reason": "need search"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "web_search", "args": {"query": "YuKiKo"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "查好了。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="查一下 YuKiKo",
            trace_id="navigator-test",
        )
        result = asyncio.run(loop.run(ctx))
        self.assertEqual(result.reply_text, "查好了。")
        self.assertEqual([name for name, _ in registry.calls], ["web_search"])
        self.assertIsNotNone(ctx.navigator_state)
        self.assertEqual(ctx.navigator_state.active_section, "web_research")


if __name__ == "__main__":
    unittest.main()
