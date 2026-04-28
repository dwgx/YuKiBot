from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.agent import AgentLoop
from core.agent_tools_search import _infer_media_search_type
from core.config_templates import _built_in_config_defaults
from core.engine import EngineMessage, YukikoEngine
from core.router import RouterDecision, RouterEngine
from core.tools import ToolExecutor
from core.trigger import TriggerEngine


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class LocalIntentHeuristicRegressionTests(unittest.TestCase):
    def test_passive_group_image_followup_does_not_enter_agent(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._recent_directed_hints = {}
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="MULTIMODAL_EVENT user sent multimodal message: image:[image]",
            mentioned=False,
            is_private=False,
        )
        trigger = SimpleNamespace(reason="followup_window")

        self.assertTrue(
            engine._should_ignore_passive_multimodal_turn(
                message=message,
                text=message.text,
                trigger=trigger,
                explicit_bot_addressed=False,
            )
        )

    def test_directed_image_still_enters_agent(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._recent_directed_hints = {}
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:[image]",
            mentioned=True,
            is_private=False,
        )
        trigger = SimpleNamespace(reason="directed")

        self.assertFalse(
            engine._should_ignore_passive_multimodal_turn(
                message=message,
                text=message.text,
                trigger=trigger,
                explicit_bot_addressed=True,
            )
        )

    def test_engine_followup_keywords_do_not_trigger_local_guesses(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False
        engine._get_bot_aliases = lambda: {"yukiko"}

        self.assertFalse(
            YukikoEngine._looks_like_summary_followup("\u603b\u7ed3\u4e00\u4e0b")
        )
        self.assertFalse(
            YukikoEngine._looks_like_resend_followup("\u518d\u53d1\u4e00\u904d")
        )
        self.assertFalse(
            YukikoEngine._looks_like_source_trace_followup(
                "\u4f60\u7528\u4e86\u4ec0\u4e48\u94fe\u63a5"
            )
        )
        self.assertFalse(
            YukikoEngine._looks_like_sticker_request("\u53d1\u4e2a\u8868\u60c5\u5305")
        )
        self.assertFalse(
            YukikoEngine._looks_like_video_text_only_intent("\u53ea\u8981\u603b\u7ed3")
        )
        self.assertFalse(
            YukikoEngine._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1")
        )
        self.assertFalse(
            engine._looks_like_qq_avatar_intent(
                "\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"
            )
        )
        self.assertFalse(
            YukikoEngine._looks_like_local_file_request(
                "\u628a\u684c\u9762\u7684\u6587\u4ef6\u53d1\u6211"
            )
        )
        self.assertFalse(engine._looks_like_bot_call("\u4f60\u770b\u770b\u8fd9\u4e2a"))

    def test_engine_only_accepts_explicit_control_tokens_or_structure(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False
        engine._get_bot_aliases = lambda: {"yukiko"}

        self.assertTrue(YukikoEngine._looks_like_summary_followup("/summary"))
        self.assertTrue(YukikoEngine._looks_like_resend_followup("/resend"))
        self.assertTrue(YukikoEngine._looks_like_source_trace_followup("/sources"))
        self.assertTrue(YukikoEngine._looks_like_sticker_request("/sticker"))
        self.assertTrue(YukikoEngine._looks_like_video_text_only_intent("output=text"))
        self.assertTrue(
            YukikoEngine._looks_like_music_request("/music \u70ed\u6c34\u6fa1")
        )
        self.assertTrue(engine._looks_like_qq_avatar_intent("/avatar target=self"))
        self.assertTrue(
            YukikoEngine._looks_like_local_file_request(r"/upload C:\temp\demo.zip")
        )
        self.assertTrue(engine._looks_like_bot_call("yukiko?"))

    def test_recent_user_image_followup_can_wake_from_not_directed(self) -> None:
        class _RecentMediaTools:
            def __init__(self) -> None:
                self._recent_media_by_conversation = {
                    "group:901:user:347": {
                        "image": ["https://example.com/a.png"],
                    },
                    "group:901:user:999": {
                        "image": ["https://example.com/other.png"],
                    },
                }

        engine = YukikoEngine.__new__(YukikoEngine)
        engine.tools = _RecentMediaTools()
        message = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="347",
            text="\u76f4\u63a5cyber\u6389",
            mentioned=False,
            is_private=False,
        )

        self.assertTrue(
            engine._looks_like_recent_media_followup_instruction(message.text)
        )
        self.assertTrue(engine._looks_like_recent_media_followup(message, message.text))
        self.assertEqual(
            engine._build_recent_media_summary_for_followup(message),
            ["image:https://example.com/a.png"],
        )

        quiet = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="347",
            text="\u968f\u4fbf\u804a\u804a",
            mentioned=False,
            is_private=False,
        )
        self.assertFalse(engine._looks_like_recent_media_followup(quiet, quiet.text))

        cross_user = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="555",
            text="\u76f4\u63a5cyber\u6389",
            mentioned=False,
            is_private=False,
        )
        self.assertFalse(
            engine._looks_like_recent_media_followup(cross_user, cross_user.text)
        )

    def test_agent_context_and_inference_helpers_require_structure(self) -> None:
        self.assertFalse(
            AgentLoop._looks_like_reference_to_previous_link("\u90a3\u4e2a\u94fe\u63a5")
        )
        self.assertTrue(AgentLoop._looks_like_reference_to_previous_link("/source"))
        self.assertFalse(AgentLoop._is_context_continuation_phrase("\u7ee7\u7eed"))
        self.assertFalse(
            AgentLoop._is_context_continuation_phrase("\u6240\u4ee5\u5462")
        )
        self.assertTrue(AgentLoop._is_context_continuation_phrase("/next"))
        self.assertEqual(
            AgentLoop._strip_continuation_prefix("\u7ee7\u7eed \u5e2e\u6211\u770b"),
            "\u7ee7\u7eed \u5e2e\u6211\u770b",
        )
        self.assertEqual(AgentLoop._strip_continuation_prefix("/next foo"), "foo")
        self.assertEqual(
            AgentLoop._strip_continuation_prefix(
                "\u6240\u4ee5\u5462 \u5e2e\u6211\u770b"
            ),
            "\u6240\u4ee5\u5462 \u5e2e\u6211\u770b",
        )

        self.assertEqual(AgentLoop._infer_search_mode("\u641c\u56fe \u732b"), "image")
        self.assertEqual(AgentLoop._infer_search_mode("/image cat"), "image")
        self.assertEqual(AgentLoop._infer_search_mode("mode=image cat"), "image")
        self.assertEqual(
            AgentLoop._infer_search_mode("https://example.com/demo.mp4"), "video"
        )

        self.assertEqual(AgentLoop._infer_media_type("\u52a8\u56fe\u8868\u60c5"), "gif")
        self.assertEqual(AgentLoop._infer_media_type("type=gif"), "gif")
        self.assertEqual(AgentLoop._infer_media_type("来张抖音截图"), "image")
        self.assertEqual(_infer_media_search_type("来张抖音截图", "video"), "image")
        self.assertEqual(_infer_media_search_type("找一个抖音视频", ""), "video")

        self.assertEqual(
            AgentLoop._infer_resource_file_type("\u5b89\u5353\u5b89\u88c5\u5305"), ""
        )
        self.assertEqual(AgentLoop._infer_resource_file_type("prefer_ext=apk"), "apk")
        self.assertEqual(AgentLoop._infer_resource_file_type("demo.exe"), "exe")
        self.assertFalse(
            AgentLoop._looks_like_download_file_request(
                "\u5e2e\u6211\u4e0b\u8f7d\u5b89\u5353\u5b89\u88c5\u5305"
            )
        )
        self.assertTrue(
            AgentLoop._looks_like_download_file_request("/download demo.apk")
        )
        self.assertFalse(
            AgentLoop._looks_like_file_send_request("\u76f4\u63a5\u53d1\u6211")
        )
        self.assertTrue(AgentLoop._looks_like_file_send_request("/upload demo.apk"))

        self.assertEqual(
            AgentLoop._infer_split_video_mode("\u63d0\u53d6\u97f3\u9891"), ""
        )
        self.assertEqual(AgentLoop._infer_split_video_mode("mode=audio"), "audio")
        self.assertEqual(AgentLoop._infer_split_video_mode("12s-20s"), "clip")

        self.assertEqual(AgentLoop._infer_frame_count_hint("\u4e5d\u5bab\u683c"), 0)
        self.assertEqual(AgentLoop._infer_frame_count_hint("max_frames=9"), 9)
        self.assertEqual(AgentLoop._infer_frame_count_hint("9 screenshots"), 9)

        self.assertEqual(
            AgentLoop._infer_video_time_hints("\u4ece 10 \u5230 20 \u79d2"),
            {"point": 10.0},
        )
        self.assertEqual(
            AgentLoop._infer_video_time_hints("10s-20s"), {"start": 10.0, "end": 20.0}
        )

        fallback = AgentLoop._fallback_tool_on_failure(
            "smart_download",
            {"query": "demo app"},
            "download_untrusted_source",
        )
        self.assertEqual(
            fallback, ("web_search", {"query": "demo app", "mode": "text"})
        )

        mismatch_fallback = AgentLoop._fallback_tool_on_failure(
            "smart_download",
            {"query": "demo app 安卓安装包", "prefer_ext": "apk"},
            "download_signature_mismatch",
        )
        self.assertEqual(
            mismatch_fallback,
            (
                "search_download_resources",
                {"query": "demo app 安卓安装包", "limit": 8, "file_type": "apk"},
            ),
        )

    def test_trigger_and_router_drop_local_keyword_defaults(self) -> None:
        trigger = TriggerEngine({}, {"name": "YuKiKo", "nicknames": []})
        self.assertEqual(trigger.ai_listen_keywords, [])
        self.assertEqual(trigger.explicit_request_cues, ())
        self.assertEqual(
            trigger._explicit_request_signal("\u5e2e\u6211\u67e5\u4e00\u4e0b"), 0.0
        )
        self.assertGreater(trigger._explicit_request_signal("/lookup test"), 0.0)
        self.assertFalse(RouterEngine._contains_explicit_adult_intent("\u6da9\u56fe"))
        self.assertTrue(RouterEngine._contains_explicit_adult_intent("/nsfw"))

    def test_tools_do_not_route_on_natural_language_cues(self) -> None:
        executor = _DummyExecutor()

        self.assertFalse(
            executor._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1")
        )
        self.assertFalse(
            executor._looks_like_video_request("\u53d1\u4e2a\u6296\u97f3\u89c6\u9891")
        )
        self.assertFalse(
            executor._looks_like_image_analysis_request(
                "\u770b\u770b\u8fd9\u5f20\u56fe"
            )
        )
        self.assertFalse(
            executor._looks_like_video_analysis_request(
                "\u603b\u7ed3\u4e00\u4e0b\u8fd9\u4e2a\u89c6\u9891"
            )
        )
        self.assertFalse(
            executor._looks_like_qq_avatar_request(
                "\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"
            )
        )
        self.assertFalse(
            executor._looks_like_analysis_text_only_request("\u53ea\u8981\u603b\u7ed3")
        )
        self.assertFalse(
            executor._looks_like_weak_vision_answer(
                "\u7ed3\u679c\u4e0d\u591f\u7a33\u5b9a"
            )
        )

    def test_tools_accept_only_explicit_tokens_or_media_locators(self) -> None:
        executor = _DummyExecutor()

        self.assertTrue(executor._looks_like_music_request("/music \u70ed\u6c34\u6fa1"))
        self.assertTrue(
            executor._looks_like_video_request("https://example.com/demo.mp4")
        )
        self.assertTrue(
            executor._looks_like_video_analysis_request(
                "/analyze https://example.com/demo.mp4"
            )
        )
        self.assertTrue(
            executor._looks_like_image_analysis_request(
                "/analyze https://example.com/demo.png"
            )
        )
        self.assertTrue(executor._looks_like_qq_avatar_request("/avatar target=self"))
        self.assertTrue(executor._looks_like_analysis_text_only_request("output=text"))
        self.assertTrue(executor._looks_like_weak_vision_answer("???"))
        self.assertEqual(
            executor._build_targeted_video_queries("\u6296\u97f3 \u732b\u732b"),
            [
                "\u6296\u97f3 \u732b\u732b site:bilibili.com/video",
                "\u6296\u97f3 \u732b\u732b site:douyin.com/video",
                "\u6296\u97f3 \u732b\u732b site:kuaishou.com/short-video",
                "\u6296\u97f3 \u732b\u732b site:acfun.cn/v/ac",
                "\u6296\u97f3 \u732b\u732b site:youtube.com/watch",
                "\u6296\u97f3 \u732b\u732b site:v.qq.com/x",
                "\u6296\u97f3 \u732b\u732b site:iqiyi.com/v_",
                "\u6296\u97f3 \u732b\u732b site:iqiyi.com/a_",
                "\u6296\u97f3 \u732b\u732b site:iq.com/play",
            ],
        )
        self.assertEqual(
            executor._build_targeted_video_queries("platform=douyin cat"),
            ["platform=douyin cat site:douyin.com/video"],
        )
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://v.qq.com/x/page/m3534f3t3hb.html",
            "https://www.iqiyi.com/v_19rr7p0r18.html",
            "https://www.iq.com/play/demo-12345",
        ):
            self.assertTrue(executor._is_supported_platform_video_url(url), url)
            self.assertTrue(executor._is_platform_video_detail_url(url), url)
        self.assertEqual(executor._pick_gif_keyframe_indexes(1), [0])
        self.assertEqual(executor._pick_gif_keyframe_indexes(3), [0, 1, 2])
        self.assertEqual(executor._pick_gif_keyframe_indexes(8), [0, 1, 3, 4, 6, 7])
        animated_prompt = executor._build_vision_prompt(
            query="这个表情什么意思",
            message_text="[动画表情]",
            animated_hint=True,
        )
        self.assertIn("动画表情", animated_prompt)
        self.assertIn("多帧拼图", animated_prompt)
        self.assertFalse(executor._looks_like_weak_vision_answer("看不清"))

    def test_tools_no_longer_local_match_vision_refusal_templates(self) -> None:
        executor = _DummyExecutor()
        executor._vision_retry_translate_enable = False

        payload = "I'm an AI assistant and cannot analyze this image."

        self.assertEqual(
            asyncio.run(executor._normalize_vision_answer(payload, prompt="")),
            payload,
        )

    def test_memory_followups_require_structure_not_local_link_words(self) -> None:
        self.assertFalse(
            YukikoEngine._looks_like_ambiguous_link_memory_query(
                "\u8fd8\u8bb0\u5f97\u90a3\u4e2a\u94fe\u63a5\u5417"
            )
        )
        self.assertTrue(YukikoEngine._looks_like_ambiguous_link_memory_query("/link"))
        self.assertFalse(
            YukikoEngine._looks_like_ambiguous_link_memory_query("/link `migu`")
        )
        self.assertEqual(
            YukikoEngine._extract_topic_terms_for_memory("\u8fd9\u4e2a \u90a3\u4e2a"),
            [],
        )
        self.assertEqual(
            YukikoEngine._extract_topic_terms_for_memory(
                "`migu` \u90a3\u4e2a", max_terms=2
            ),
            ["migu"],
        )

    def test_memory_guard_only_checks_explicit_structured_references(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        fallback = "\u6211\u521a\u624d\u90a3\u53e5\u5386\u53f2\u5f15\u7528\u4e0d\u51c6\u786e\uff0c\u5ffd\u7565\u5b83\u3002\u4f60\u73b0\u5728\u76f4\u63a5\u544a\u8bc9\u6211\u9700\u6c42\uff0c\u6211\u6309\u4f60\u8fd9\u6761\u6765\u3002"

        guarded = engine._guard_unverified_memory_claims(
            reply_text="\u4f60\u4e4b\u524d\u63d0\u5230\u8fc7\u300aOcean\u300b",
            user_text="",
            current_user_recent=["[\u5f53\u524d\u7528\u6237\u8fd1\u671f] Daylight"],
            related_memories=[],
        )
        self.assertEqual(guarded, fallback)

        untouched = engine._guard_unverified_memory_claims(
            reply_text="\u6211\u53ef\u80fd\u8bb0\u5f97\u4f60\u4e4b\u524d\u8bf4\u8fc7\u8fd9\u4e2a",
            user_text="",
            current_user_recent=["[\u5f53\u524d\u7528\u6237\u8fd1\u671f] Daylight"],
            related_memories=[],
        )
        self.assertEqual(
            untouched,
            "\u6211\u53ef\u80fd\u8bb0\u5f97\u4f60\u4e4b\u524d\u8bf4\u8fc7\u8fd9\u4e2a",
        )

    def test_self_check_allows_ai_router_candidate_to_use_high_confidence_gate(
        self,
    ) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.self_check_enable = True
        engine.self_check_block_at_other = False
        engine.routing_zero_disables_undirected = True
        engine.non_directed_high_confidence_only = True
        engine.self_check_listen_probe_min_confidence = 0.6
        engine.self_check_non_direct_reply_min_confidence = 0.82
        engine._extract_first_image_url_from_text = lambda text: ""
        engine._extract_first_video_url_from_text = lambda text: ""
        engine._looks_like_image_analyze_intent = lambda text: False
        engine._looks_like_video_resolve_intent = lambda text: False
        engine._looks_like_local_file_request = lambda text: False
        engine._pick_local_path_candidate = lambda text: ""
        engine._looks_like_github_request = lambda text: False
        engine._looks_like_repo_readme_request = lambda text: False
        engine._looks_like_explicit_request = lambda text: False
        engine._is_passive_multimodal_text = lambda text: False
        engine._has_recent_directed_hint = lambda message: False
        engine._looks_like_bot_call = lambda text: False
        engine._looks_like_media_instruction = lambda text: False
        engine._extract_multimodal_user_text = lambda text: text
        engine._is_cross_user_context_collision = lambda message, trigger, text: False
        engine._looks_like_low_info_group_chitchat = lambda text: False
        engine._allow_at_other_target_dialog = lambda message, text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="这波怎么说",
            mentioned=False,
            is_private=False,
        )
        trigger = SimpleNamespace(
            followup_candidate=False,
            active_session=False,
            busy_users=3,
            listen_probe=False,
            reason="ai_router_candidate",
        )
        decision = RouterDecision(
            should_handle=True,
            action="reply",
            reason="ai",
            confidence=0.93,
        )

        self.assertEqual(
            engine._self_check_decision(message, trigger, decision),
            "self_check:undirected_requires_listen_probe",
        )

    def test_self_check_allows_quiet_group_non_directed_reply_without_listen_probe(
        self,
    ) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.self_check_enable = True
        engine.self_check_block_at_other = False
        engine.routing_zero_disables_undirected = True
        engine.non_directed_high_confidence_only = True
        engine.self_check_listen_probe_min_confidence = 0.6
        engine.self_check_non_direct_reply_min_confidence = 0.82
        engine._extract_first_image_url_from_text = lambda text: ""
        engine._extract_first_video_url_from_text = lambda text: ""
        engine._looks_like_image_analyze_intent = lambda text: False
        engine._looks_like_video_resolve_intent = lambda text: False
        engine._looks_like_local_file_request = lambda text: False
        engine._pick_local_path_candidate = lambda text: ""
        engine._looks_like_github_request = lambda text: False
        engine._looks_like_repo_readme_request = lambda text: False
        engine._looks_like_explicit_request = lambda text: False
        engine._is_passive_multimodal_text = lambda text: False
        engine._has_recent_directed_hint = lambda message: False
        engine._looks_like_bot_call = lambda text: False
        engine._looks_like_media_instruction = lambda text: False
        engine._extract_multimodal_user_text = lambda text: text
        engine._is_cross_user_context_collision = (
            lambda message, trigger, text: False
        )
        engine._looks_like_low_info_group_chitchat = lambda text: False
        engine._allow_at_other_target_dialog = lambda message, text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="这题怎么写",
            mentioned=False,
            is_private=False,
        )
        trigger = SimpleNamespace(
            followup_candidate=False,
            active_session=False,
            busy_users=1,
            listen_probe=False,
            reason="not_directed",
        )
        decision = RouterDecision(
            should_handle=True,
            action="reply",
            reason="ai",
            confidence=0.93,
        )

        self.assertEqual(engine._self_check_decision(message, trigger, decision), "")

    def test_choice_followups_accept_only_structural_number_forms(self) -> None:
        self.assertEqual(YukikoEngine._extract_choice_index("1"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c1\u4e2a"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c\u4e00\u4e2a"), 1)
        self.assertIsNone(YukikoEngine._extract_choice_index("\u90091"))
        self.assertIsNone(
            YukikoEngine._extract_choice_index("\u53d1\u7ed9\u6211\u7b2c\u4e00\u4e2a")
        )

    def test_engine_defaults_to_agent_for_directed_plain_text_chat(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {}
        engine._extract_first_image_url_from_text = lambda text: ""
        engine._extract_first_video_url_from_text = lambda text: ""
        engine._extract_first_url = lambda text: ""
        engine._looks_like_download_task_intent = lambda text: False
        engine._looks_like_local_file_request = lambda text: False
        engine._pick_local_path_candidate = lambda text: ""
        engine._looks_like_github_request = lambda text: False
        engine._looks_like_repo_readme_request = lambda text: False
        engine._looks_like_explicit_request = lambda text: False
        engine._looks_like_qq_avatar_intent = lambda text: False
        engine._looks_like_image_analyze_intent = lambda text: False
        engine._looks_like_video_request = lambda text: False
        engine._looks_like_video_analysis_intent = lambda text: False
        engine._looks_like_video_resolve_intent = lambda text: False
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            user_name="tester",
            text="你好呀",
            mentioned=True,
            is_private=False,
        )
        trigger = SimpleNamespace(scene_hint="chat", active_session=False)

        self.assertFalse(
            engine._should_prefer_router_for_plain_text(message, "你好呀", trigger)
        )

    def test_engine_can_opt_in_router_for_directed_plain_text_chat(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {"agent": {"prefer_router_for_directed_plain_text": True}}
        engine._extract_first_image_url_from_text = lambda text: ""
        engine._extract_first_video_url_from_text = lambda text: ""
        engine._extract_first_url = lambda text: ""
        engine._looks_like_download_task_intent = lambda text: False
        engine._looks_like_local_file_request = lambda text: False
        engine._pick_local_path_candidate = lambda text: ""
        engine._looks_like_github_request = lambda text: False
        engine._looks_like_repo_readme_request = lambda text: False
        engine._looks_like_explicit_request = lambda text: False
        engine._looks_like_qq_avatar_intent = lambda text: False
        engine._looks_like_image_analyze_intent = lambda text: False
        engine._looks_like_video_request = lambda text: False
        engine._looks_like_video_analysis_intent = lambda text: False
        engine._looks_like_video_resolve_intent = lambda text: False
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            user_name="tester",
            text="你好呀",
            mentioned=True,
            is_private=False,
        )
        trigger = SimpleNamespace(scene_hint="chat", active_session=False)

        self.assertTrue(
            engine._should_prefer_router_for_plain_text(message, "你好呀", trigger)
        )

    def test_engine_keeps_agent_for_media_or_tool_tasks(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._extract_first_image_url_from_text = lambda text: ""
        engine._extract_first_video_url_from_text = lambda text: ""
        engine._extract_first_url = lambda text: ""
        engine._looks_like_download_task_intent = lambda text: False
        engine._looks_like_local_file_request = lambda text: False
        engine._pick_local_path_candidate = lambda text: ""
        engine._looks_like_github_request = lambda text: False
        engine._looks_like_repo_readme_request = lambda text: False
        engine._looks_like_explicit_request = lambda text: False
        engine._looks_like_qq_avatar_intent = lambda text: False
        engine._looks_like_image_analyze_intent = lambda text: False
        engine._looks_like_video_request = lambda text: False
        engine._looks_like_video_analysis_intent = lambda text: False
        engine._looks_like_video_resolve_intent = lambda text: False
        engine._looks_like_bot_call = lambda text: False

        media_message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            user_name="tester",
            text="这是什么",
            mentioned=True,
            raw_segments=[
                {"type": "image", "data": {"url": "https://example.com/a.png"}}
            ],
        )
        trigger = SimpleNamespace(scene_hint="chat", active_session=False)
        self.assertFalse(
            engine._should_prefer_router_for_plain_text(
                media_message, "这是什么", trigger
            )
        )

        tool_message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            user_name="tester",
            text="帮我看看这个仓库 README",
            mentioned=True,
        )
        engine._looks_like_github_request = lambda text: True
        engine._looks_like_repo_readme_request = lambda text: True
        self.assertFalse(
            engine._should_prefer_router_for_plain_text(
                tool_message, "帮我看看这个仓库 README", trigger
            )
        )

    def test_engine_detects_structural_echo_of_recent_bot_reply(self) -> None:
        reply = (
            "This appears to be a QQ bot event log showing a recursive or self-referential loop. "
            "The user is sending messages that describe the bot's previous responses, creating a pattern."
        )
        incoming = f"武庸，{reply}"
        self.assertTrue(
            YukikoEngine._looks_like_recent_bot_reply_echo(incoming, [reply])
        )

    def test_engine_does_not_flag_fresh_chat_as_recent_bot_reply_echo(self) -> None:
        incoming = "你刚才那句话什么意思，直接讲白一点。"
        recent_bot_replies = [
            "这是上一轮回复，主要是在解释一个群聊里的消息循环问题。",
        ]
        self.assertFalse(
            YukikoEngine._looks_like_recent_bot_reply_echo(incoming, recent_bot_replies)
        )

    def test_tools_require_explicit_avatar_and_download_controls(self) -> None:
        executor = _DummyExecutor()

        self.assertEqual(
            executor._extract_avatar_name_candidates("/avatar alice"), ["alice"]
        )
        self.assertEqual(executor._extract_avatar_name_candidates("alice avatar"), [])
        self.assertFalse(executor._looks_like_github_request("github foo"))
        self.assertTrue(
            executor._looks_like_github_request("https://github.com/foo/bar")
        )
        self.assertFalse(executor._looks_like_repo_readme_request("docs please"))
        self.assertTrue(executor._looks_like_repo_readme_request("/readme foo/bar"))
        self.assertFalse(executor._looks_like_download_request_text("download demo"))
        self.assertTrue(executor._looks_like_download_request_text("/download demo"))

    def test_data_uri_media_value_never_treated_as_local_path(self) -> None:
        executor = _DummyExecutor()
        huge_data_uri = "data:image/png;base64," + ("a" * 12000)
        normalized = executor._normalize_message_media_value(huge_data_uri)
        self.assertTrue(normalized.startswith("data:image/png;base64,"))

    def test_built_in_defaults_disable_short_ping_heuristics_and_enable_risk_confirm(
        self,
    ) -> None:
        defaults = _built_in_config_defaults()
        bot_cfg = defaults.get("bot", {})
        agent_cfg = defaults.get("agent", {})
        hr_cfg = agent_cfg.get("high_risk_control", {})

        self.assertEqual(bot_cfg.get("short_ping_phrases"), [])
        self.assertTrue(bot_cfg.get("short_ping_require_directed", False))
        self.assertFalse(agent_cfg.get("prefer_router_for_directed_plain_text", True))
        self.assertTrue(hr_cfg.get("default_require_confirmation", False))
        self.assertTrue(bool(hr_cfg.get("tool_name_patterns")))
        self.assertTrue(bool(hr_cfg.get("description_patterns")))


if __name__ == "__main__":
    unittest.main()
