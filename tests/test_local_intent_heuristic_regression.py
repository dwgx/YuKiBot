from __future__ import annotations

import unittest

from core.agent import AgentLoop
from core.engine import YukikoEngine
from core.router import RouterEngine
from core.tools import ToolExecutor
from core.trigger import TriggerEngine


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class LocalIntentHeuristicRegressionTests(unittest.TestCase):
    def test_engine_followup_keywords_do_not_trigger_local_guesses(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False
        engine._get_bot_aliases = lambda: {"yukiko"}

        self.assertFalse(YukikoEngine._looks_like_summary_followup("\u603b\u7ed3\u4e00\u4e0b"))
        self.assertFalse(YukikoEngine._looks_like_resend_followup("\u518d\u53d1\u4e00\u904d"))
        self.assertFalse(YukikoEngine._looks_like_source_trace_followup("\u4f60\u7528\u4e86\u4ec0\u4e48\u94fe\u63a5"))
        self.assertFalse(YukikoEngine._looks_like_sticker_request("\u53d1\u4e2a\u8868\u60c5\u5305"))
        self.assertFalse(YukikoEngine._looks_like_video_text_only_intent("\u53ea\u8981\u603b\u7ed3"))
        self.assertFalse(YukikoEngine._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1"))
        self.assertFalse(engine._looks_like_qq_avatar_intent("\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"))
        self.assertFalse(YukikoEngine._looks_like_local_file_request("\u628a\u684c\u9762\u7684\u6587\u4ef6\u53d1\u6211"))
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
        self.assertTrue(YukikoEngine._looks_like_music_request("/music \u70ed\u6c34\u6fa1"))
        self.assertTrue(engine._looks_like_qq_avatar_intent("/avatar target=self"))
        self.assertTrue(YukikoEngine._looks_like_local_file_request(r"/upload C:\temp\demo.zip"))
        self.assertTrue(engine._looks_like_bot_call("yukiko?"))

    def test_agent_context_and_inference_helpers_require_structure(self) -> None:
        self.assertFalse(AgentLoop._looks_like_reference_to_previous_link("\u90a3\u4e2a\u94fe\u63a5"))
        self.assertTrue(AgentLoop._looks_like_reference_to_previous_link("/source"))
        self.assertFalse(AgentLoop._is_context_continuation_phrase("\u7ee7\u7eed"))
        self.assertTrue(AgentLoop._is_context_continuation_phrase("/next"))
        self.assertEqual(AgentLoop._strip_continuation_prefix("\u7ee7\u7eed \u5e2e\u6211\u770b"), "\u7ee7\u7eed \u5e2e\u6211\u770b")
        self.assertEqual(AgentLoop._strip_continuation_prefix("/next foo"), "foo")

        self.assertEqual(AgentLoop._infer_search_mode("\u641c\u56fe \u732b"), "text")
        self.assertEqual(AgentLoop._infer_search_mode("/image cat"), "image")
        self.assertEqual(AgentLoop._infer_search_mode("https://example.com/demo.mp4"), "video")

        self.assertEqual(AgentLoop._infer_media_type("\u52a8\u56fe\u8868\u60c5"), "")
        self.assertEqual(AgentLoop._infer_media_type("type=gif"), "gif")

        self.assertEqual(AgentLoop._infer_resource_file_type("\u5b89\u5353\u5b89\u88c5\u5305"), "apk")
        self.assertEqual(AgentLoop._infer_resource_file_type("prefer_ext=apk"), "apk")
        self.assertEqual(AgentLoop._infer_resource_file_type("demo.exe"), "exe")

        self.assertEqual(AgentLoop._infer_split_video_mode("\u63d0\u53d6\u97f3\u9891"), "")
        self.assertEqual(AgentLoop._infer_split_video_mode("mode=audio"), "audio")
        self.assertEqual(AgentLoop._infer_split_video_mode("12s-20s"), "clip")

        self.assertEqual(AgentLoop._infer_frame_count_hint("\u4e5d\u5bab\u683c"), 0)
        self.assertEqual(AgentLoop._infer_frame_count_hint("max_frames=9"), 9)
        self.assertEqual(AgentLoop._infer_frame_count_hint("9 screenshots"), 9)

        self.assertEqual(AgentLoop._infer_video_time_hints("\u4ece 10 \u5230 20 \u79d2"), {"point": 10.0})
        self.assertEqual(AgentLoop._infer_video_time_hints("10s-20s"), {"start": 10.0, "end": 20.0})

        fallback = AgentLoop._fallback_tool_on_failure(
            "smart_download",
            {"query": "demo app"},
            "download_untrusted_source",
        )
        self.assertEqual(fallback, ("web_search", {"query": "demo app", "mode": "text"}))

        mismatch_fallback = AgentLoop._fallback_tool_on_failure(
            "smart_download",
            {"query": "demo app 安卓安装包", "prefer_ext": "apk"},
            "download_signature_mismatch",
        )
        self.assertEqual(
            mismatch_fallback,
            ("search_download_resources", {"query": "demo app 安卓安装包", "limit": 8, "file_type": "apk"}),
        )

    def test_trigger_and_router_drop_local_keyword_defaults(self) -> None:
        trigger = TriggerEngine({}, {"name": "YuKiKo", "nicknames": []})
        self.assertEqual(trigger.ai_listen_keywords, [])
        self.assertEqual(trigger.explicit_request_cues, ())
        self.assertEqual(trigger._explicit_request_signal("\u5e2e\u6211\u67e5\u4e00\u4e0b"), 0.0)
        self.assertGreater(trigger._explicit_request_signal("/lookup test"), 0.0)
        self.assertFalse(RouterEngine._contains_explicit_adult_intent("\u6da9\u56fe"))
        self.assertTrue(RouterEngine._contains_explicit_adult_intent("/nsfw"))

    def test_tools_do_not_route_on_natural_language_cues(self) -> None:
        executor = _DummyExecutor()

        self.assertFalse(executor._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1"))
        self.assertFalse(executor._looks_like_video_request("\u53d1\u4e2a\u6296\u97f3\u89c6\u9891"))
        self.assertFalse(executor._looks_like_image_analysis_request("\u770b\u770b\u8fd9\u5f20\u56fe"))
        self.assertFalse(executor._looks_like_video_analysis_request("\u603b\u7ed3\u4e00\u4e0b\u8fd9\u4e2a\u89c6\u9891"))
        self.assertFalse(executor._looks_like_qq_avatar_request("\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"))
        self.assertFalse(executor._looks_like_analysis_text_only_request("\u53ea\u8981\u603b\u7ed3"))
        self.assertFalse(executor._looks_like_weak_vision_answer("\u7ed3\u679c\u4e0d\u591f\u7a33\u5b9a"))

    def test_tools_accept_only_explicit_tokens_or_media_locators(self) -> None:
        executor = _DummyExecutor()

        self.assertTrue(executor._looks_like_music_request("/music \u70ed\u6c34\u6fa1"))
        self.assertTrue(executor._looks_like_video_request("https://example.com/demo.mp4"))
        self.assertTrue(executor._looks_like_video_analysis_request("/analyze https://example.com/demo.mp4"))
        self.assertTrue(executor._looks_like_image_analysis_request("/analyze https://example.com/demo.png"))
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
            ],
        )
        self.assertEqual(
            executor._build_targeted_video_queries("platform=douyin cat"),
            ["platform=douyin cat site:douyin.com/video"],
        )

    def test_memory_followups_require_structure_not_local_link_words(self) -> None:
        self.assertFalse(YukikoEngine._looks_like_ambiguous_link_memory_query("\u8fd8\u8bb0\u5f97\u90a3\u4e2a\u94fe\u63a5\u5417"))
        self.assertTrue(YukikoEngine._looks_like_ambiguous_link_memory_query("/link"))
        self.assertFalse(YukikoEngine._looks_like_ambiguous_link_memory_query("/link `migu`"))
        self.assertEqual(YukikoEngine._extract_topic_terms_for_memory("\u8fd9\u4e2a \u90a3\u4e2a"), [])
        self.assertEqual(YukikoEngine._extract_topic_terms_for_memory("`migu` \u90a3\u4e2a", max_terms=2), ["migu"])

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
        self.assertEqual(untouched, "\u6211\u53ef\u80fd\u8bb0\u5f97\u4f60\u4e4b\u524d\u8bf4\u8fc7\u8fd9\u4e2a")

    def test_choice_followups_accept_only_structural_number_forms(self) -> None:
        self.assertEqual(YukikoEngine._extract_choice_index("1"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c1\u4e2a"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c\u4e00\u4e2a"), 1)
        self.assertIsNone(YukikoEngine._extract_choice_index("\u90091"))
        self.assertIsNone(YukikoEngine._extract_choice_index("\u53d1\u7ed9\u6211\u7b2c\u4e00\u4e2a"))

    def test_tools_require_explicit_avatar_and_download_controls(self) -> None:
        executor = _DummyExecutor()

        self.assertEqual(executor._extract_avatar_name_candidates("/avatar alice"), ["alice"])
        self.assertEqual(executor._extract_avatar_name_candidates("alice avatar"), [])
        self.assertFalse(executor._looks_like_github_request("github foo"))
        self.assertTrue(executor._looks_like_github_request("https://github.com/foo/bar"))
        self.assertFalse(executor._looks_like_repo_readme_request("docs please"))
        self.assertTrue(executor._looks_like_repo_readme_request("/readme foo/bar"))
        self.assertFalse(executor._looks_like_download_request_text("download demo"))
        self.assertTrue(executor._looks_like_download_request_text("/download demo"))


if __name__ == "__main__":
    unittest.main()
