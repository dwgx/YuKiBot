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
    recent_media_artifact: dict = {}


class PromptNavigatorConfigTests(unittest.TestCase):
    def test_video_url_preselects_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "https://www.acfun.cn/v/ac12345 帮我解析"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("video_url", state.candidate_sections)
        self.assertIn("video_url", state.evidence)

    def test_bare_domain_preselects_web_research(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "网络是时光机 看skiapi.dev"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "fetch_webpage"])
        self.assertEqual(state.active_section, "web_research")
        self.assertIn("url", state.evidence)

    def test_research_request_preselects_web_research_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "我要看异环的新手教程"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "web_search"])
        self.assertEqual(state.active_section, "web_research")
        self.assertIn("external_research_request", state.evidence)

    def test_media_search_request_preselects_media_search_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "给我找一个异环新手教程视频，直接发最合适的"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "search_media"])
        self.assertEqual(state.active_section, "media_search")
        self.assertIn("media_search_request", state.evidence)

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

    def test_recent_video_artifact_preselects_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.recent_media_artifact = {
            "type": "video",
            "video_url": "/tmp/yukiko/demo.mp4",
            "source_url": "https://v.douyin.com/demo/",
        }
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("recent_media_artifact", state.evidence)

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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _TimeoutModelClient:
    enabled = True

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        raise asyncio.TimeoutError()


class _Registry:
    tool_count = 4

    def __init__(self):
        self.names = [
            "web_search",
            "fetch_webpage",
            "parse_video",
            "search_media",
            "think",
            "final_answer",
            "navigate_section",
        ]
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self.names

    def get_schema(self, name: str):
        _ = name
        return None

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self.names)

    def select_tools_for_intent(self, message_text: str, permission_level: str) -> list[str]:
        _ = (message_text, permission_level)
        raise AssertionError("strict navigator should not use legacy intent selector")

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
        if name == "parse_video":
            return ToolCallResult(
                ok=True,
                display="parse_video ok",
                data={
                    "video_url": "/tmp/yukiko/demo.mp4",
                    "source_url": args.get("url", ""),
                    "text": "demo",
                },
            )
        if name == "fetch_webpage":
            return ToolCallResult(ok=True, display="fetch ok", data={"url": args.get("url", "")})
        if name == "web_search":
            return ToolCallResult(ok=True, display="search ok", data={"query": args.get("query", "")})
        if name == "search_media":
            media_type = args.get("media_type", "")
            data = {"query": args.get("query", ""), "media_type": media_type, "text": "media ok"}
            if media_type == "video":
                data["video_url"] = "/tmp/yukiko/search.mp4"
            else:
                data["image_url"] = "https://example.test/image.jpg"
            return ToolCallResult(ok=True, display="media ok", data=data)
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

    def test_strict_routing_blocks_toolless_final_for_video_url(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "我看不了。"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "parse_video", "args": {"url": "https://v.douyin.com/demo/"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "解析好了。"}},
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
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-strict-test",
        )
        result = asyncio.run(loop.run(ctx))
        self.assertEqual(result.reply_text, "解析好了。")
        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertTrue(any(step.get("tool") == "policy_guard" for step in result.steps))

    def test_video_url_llm_timeout_falls_back_to_parse_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
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
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")
        self.assertEqual(result.tool_calls_made, 1)

    def test_timeout_after_navigator_policy_block_still_falls_back_to_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "没有工具结果，先硬答。"},
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "解析好了，我直接把视频发出来。"},
                        },
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
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-policy-block-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertTrue(any(step.get("error") == "navigator_tool_required_before_final_answer" for step in result.steps))
        self.assertEqual(result.reason, "agent_final_answer")

    def test_web_research_llm_timeout_falls_back_to_search_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
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
            message_text="我要看异环的新手教程",
            trace_id="navigator-web-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["web_search"])
        self.assertEqual(registry.calls[0][1]["query"], "异环的新手教程")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_media_search_llm_timeout_falls_back_to_search_media_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
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
            message_text="给我找一个异环新手教程视频，直接发最合适的",
            trace_id="navigator-media-search-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(registry.calls[0][1]["media_type"], "video")
        self.assertIn("异环新手教程视频", registry.calls[0][1]["query"])
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_web_url_llm_timeout_falls_back_to_fetch_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
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
            message_text="网络时光机看 skiapi.dev",
            trace_id="navigator-fetch-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["fetch_webpage"])
        self.assertEqual(registry.calls[0][1]["url"], "https://skiapi.dev")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")


if __name__ == "__main__":
    unittest.main()
