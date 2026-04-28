from __future__ import annotations

import asyncio
import json
import time
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
        state = nav.initial_state(
            ctx,
            [
                "think",
                "final_answer",
                "navigate_section",
                "fetch_webpage",
                "wayback_lookup",
                "wayback_extract",
            ],
        )
        self.assertEqual(state.active_section, "web_research")
        self.assertIn("url", state.evidence)
        scoped = nav.scoped_tools(state)
        self.assertIn("wayback_lookup", scoped)
        self.assertIn("wayback_extract", scoped)

    def test_research_request_preselects_web_research_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "我要看异环的新手教程"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "web_search"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("external_research_request", state.evidence)
        block = nav.render_system_block(state, nav.scoped_tools(state))
        self.assertIn("web_research", block)
        self.assertIn("media_search", block)

    def test_media_search_request_preselects_media_search_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "给我找一个异环新手教程视频，直接发最合适的"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "search_media"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("media_search_request", state.evidence)
        self.assertIn("media_search", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_short_image_request_preselects_media_search(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "来张猫图"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "search_media"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("media_search_request", state.evidence)

    def test_music_request_preselects_music_section_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "点歌 Never Gonna Give You Up - Rick Astley，直接发语音"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "music_play"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("music_request", state.evidence)

    def test_download_request_preselects_download_section_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "帮我找一下 OBS Windows 安装包 exe 下载"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "search_download_resources"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("download_request", state.evidence)

    def test_creative_generation_request_preselects_creative_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "帮我画一张赛博猫娘头像"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "generate_image_enhanced"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("creative_generation_request", state.evidence)

    def test_memory_request_preselects_memory_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "你记得我是谁吗"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "recall_about_user"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("memory_request", state.evidence)

    def test_sticker_request_preselects_sticker_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发一个点赞 QQ 表情"
        state = nav.initial_state(
            ctx, ["think", "final_answer", "navigate_section", "send_face"]
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("sticker_request", state.evidence)
        self.assertNotIn("send_face", nav.scoped_tools(state))
        self.assertIn("sticker_emoji", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_bot_strategy_request_preselects_admin_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "@YuKiKo 闭嘴一下"
        state = nav.initial_state(
            ctx, ["think", "final_answer", "navigate_section", "admin_command"]
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("bot_strategy_request", state.evidence)
        self.assertNotIn("admin_command", nav.scoped_tools(state))
        self.assertIn("qq_admin_social", nav.render_system_block(state, nav.scoped_tools(state)))

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

    def test_decorated_image_url_preselects_multimodal_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发这张图 https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "resolve_image", "parse_video"],
        )
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("image_url", state.evidence)
        self.assertIn("resolve_image", nav.scoped_tools(state))

    def test_current_image_url_overrides_recent_video_artifact(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发这张图 https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        ctx.recent_media_artifact = {
            "type": "video",
            "video_url": "/tmp/yukiko/demo.mp4",
            "source_url": "https://v.douyin.com/demo/",
        }
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "resolve_image", "parse_video"],
        )
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("image_url", state.evidence)
        self.assertNotIn("recent_media_artifact", state.evidence)

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


class _TimeoutThenSectionModelClient:
    enabled = True

    def __init__(self, section_id: str):
        self.section_id = section_id
        self.calls = 0

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        self.calls += 1
        if self.calls == 1:
            raise asyncio.TimeoutError()
        if self.calls > 2:
            raise asyncio.TimeoutError()
        return json.dumps(
            {"section_id": self.section_id, "reason": "tiny navigator retry"},
            ensure_ascii=False,
        )


class _ErrorModelClient:
    enabled = True

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        raise RuntimeError("HTTP 401: 无效的令牌")


class _SlowFirstThenFinalModelClient:
    enabled = True

    def __init__(self):
        self.calls = 0

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(2)
        return json.dumps(
            {"tool": "final_answer", "args": {"text": "解析好了。"}},
            ensure_ascii=False,
        )


class _Registry:
    tool_count = 4

    def __init__(self):
        self.names = [
            "web_search",
            "fetch_webpage",
            "parse_video",
            "search_media",
            "search_download_resources",
            "smart_download",
            "wayback_lookup",
            "wayback_extract",
            "wayback_timeline",
            "music_play",
            "send_face",
            "send_emoji",
            "send_sticker",
            "analyze_image",
            "resolve_image",
            "generate_image_enhanced",
            "remember_user_fact",
            "recall_about_user",
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
        if name.startswith("wayback_"):
            return ToolCallResult(
                ok=True,
                display="wayback ok",
                data={"url": args.get("url", ""), "text": "wayback ok"},
            )
        if name == "search_media":
            media_type = args.get("media_type", "")
            data = {"query": args.get("query", ""), "media_type": media_type, "text": "media ok"}
            if media_type == "video":
                data["video_url"] = "/tmp/yukiko/search.mp4"
            else:
                data["image_url"] = "https://example.test/image.jpg"
            return ToolCallResult(ok=True, display="media ok", data=data)
        if name == "search_download_resources":
            return ToolCallResult(
                ok=True,
                display="download candidates ok",
                data={
                    "query": args.get("query", ""),
                    "file_type": args.get("file_type", ""),
                    "items": [{"title": "demo", "url": "https://example.test/demo.exe"}],
                },
            )
        if name == "smart_download":
            return ToolCallResult(
                ok=True,
                display="download ok",
                data={"path": "/tmp/yukiko/demo.exe", "url": args.get("url", "")},
            )
        if name == "music_play":
            return ToolCallResult(
                ok=True,
                display="music ok",
                data={"audio_file": "/tmp/yukiko/song.mp3", "text": "music ok"},
            )
        if name in {"send_face", "send_emoji", "send_sticker"}:
            return ToolCallResult(
                ok=True,
                display=f"{name} ok",
                data={"sent": True, "query": args.get("query", "")},
            )
        if name == "analyze_image":
            return ToolCallResult(
                ok=True,
                display="image analysis ok",
                data={"analysis": "image analysis ok"},
            )
        if name == "resolve_image":
            url = args.get("url", "")
            return ToolCallResult(
                ok=True,
                display="image resolved",
                data={"image_url": url, "image_urls": [url]},
            )
        if name == "generate_image_enhanced":
            return ToolCallResult(
                ok=True,
                display="image generated",
                data={"image_url": "https://example.test/generated.png"},
            )
        if name == "remember_user_fact":
            return ToolCallResult(
                ok=True,
                display="remember ok",
                data={"fact": args.get("fact", "")},
            )
        if name == "recall_about_user":
            return ToolCallResult(
                ok=True,
                display="recall ok",
                data={"items": 1},
            )
        return ToolCallResult(ok=True, display=f"{name} ok", data={"name": name})


class _FailParseRegistry(_Registry):
    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        if name == "parse_video":
            self.calls.append((name, dict(args)))
            return ToolCallResult(
                ok=False,
                display="B站限流了（412），稍等一会儿再试就好。",
                error="bilibili_412_throttled",
            )
        return await super().call(name, args, context)


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

    def test_failed_tool_display_survives_llm_timeout_fallback(self):
        registry = _FailParseRegistry()
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
            message_text="解析 https://www.bilibili.com/video/BV1xx411c7mD/",
            trace_id="navigator-failed-tool-display-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertIn("B站限流了（412）", result.reply_text)
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_direct_image_url_llm_timeout_falls_back_to_resolve_image(self):
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
        image_url = "https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text=f"发这张图 {image_url}",
            trace_id="navigator-direct-image-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["resolve_image"])
        self.assertEqual(registry.calls[0][1]["url"], image_url)
        self.assertEqual(result.image_url, image_url)
        self.assertEqual(result.image_urls, [image_url])
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_obvious_navigator_tool_caps_initial_llm_wait(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SlowFirstThenFinalModelClient(),
            tool_registry=registry,
            config={
                "agent": {
                    "enable": True,
                    "max_steps": 5,
                    "fallback_on_parse_error": True,
                    "navigator_obvious_tool_timeout_seconds": 0.05,
                },
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
            trace_id="navigator-obvious-tool-cap-test",
        )

        started = time.perf_counter()
        result = asyncio.run(loop.run(ctx))
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.reply_text, "解析好了。")

    def test_video_url_llm_error_falls_back_to_parse_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_ErrorModelClient(),
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
            trace_id="navigator-llm-error-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertEqual(result.reason, "agent_fallback_llm_error")
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

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_web_research_url_with_extra_instruction_does_not_force_fetch_on_timeout(self):
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
            message_text="网络时光机 看 skiapi.dev",
            trace_id="navigator-web-wayback-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_general_chat_timeout_uses_tiny_navigator_retry_before_giving_up(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutThenSectionModelClient("media_search"),
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
            message_text="给我发一个异环的视频",
            trace_id="navigator-tiny-retry-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")
        self.assertEqual(registry.calls, [])

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

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_media_search_fallback_prefers_current_image_request_over_reply_video_text(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {
                                "section_id": "media_search",
                                "reason": "用户要发送图片，当前分区没有媒体检索工具",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "猫图", "media_type": "image"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "给你猫图。"}},
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
            message_text="那张猫图发一下，不要发视频",
            reply_to_text="解析好了，我直接把视频发出来。",
            trace_id="navigator-media-image-reply-video-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(registry.calls[0][1]["media_type"], "image")
        self.assertEqual(registry.calls[0][1]["query"], "猫图")
        self.assertEqual(result.image_url, "https://example.test/image.jpg")
        self.assertEqual(result.video_url, "")

    def test_media_search_free_text_uses_navigator_switch_before_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {
                                "section_id": "media_search",
                                "reason": "用户想看主题视频，需要进入媒体检索分区",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "异环宣传片", "media_type": "video"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "找到视频。"}},
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
            message_text="我想看异环宣传片，找最合适的直接发",
            trace_id="navigator-media-search-switch-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(registry.calls[0][1], {"query": "异环宣传片", "media_type": "video"})
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))

    def test_media_search_tool_image_survives_text_only_final_answer(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "猫咪", "media_type": "image"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "先给你一张猫咪图。"},
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
            message_text="找一张猫咪图片发出来",
            trace_id="navigator-media-image-final-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(result.image_url, "https://example.test/image.jpg")
        self.assertEqual(result.image_urls, ["https://example.test/image.jpg"])

    def test_download_llm_timeout_falls_back_to_download_search_tool(self):
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
            message_text="帮我找一下 OBS Windows 安装包 exe 下载",
            trace_id="navigator-download-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_creative_generation_llm_timeout_falls_back_to_image_tool(self):
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
            message_text="帮我画一张赛博猫娘头像",
            trace_id="navigator-image-gen-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_memory_llm_timeout_falls_back_to_recall_tool(self):
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
            message_text="你记得我是谁吗",
            trace_id="navigator-memory-recall-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_music_llm_timeout_falls_back_to_music_play_tool(self):
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
            message_text="点歌 Never Gonna Give You Up - Rick Astley，直接发语音",
            trace_id="navigator-music-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_sticker_llm_timeout_falls_back_to_send_face_tool(self):
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
            message_text="发一个点赞 QQ 表情",
            trace_id="navigator-sticker-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_multimodal_llm_timeout_falls_back_to_analyze_image_tool(self):
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
            message_text="分析一下引用的这张图",
            reply_media_summary=["image:https://example.test/cat.png"],
            trace_id="navigator-image-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["analyze_image"])
        self.assertIn("引用的这张图", registry.calls[0][1]["question"])
        self.assertEqual(result.reply_text, "image analysis ok")
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
            message_text="skiapi.dev",
            trace_id="navigator-fetch-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["fetch_webpage"])
        self.assertEqual(registry.calls[0][1]["url"], "https://skiapi.dev")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")


if __name__ == "__main__":
    unittest.main()
