from __future__ import annotations

import unittest

from core.engine import YukikoEngine
from core.tools import ToolExecutor


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class LocalIntentHeuristicRegressionTests(unittest.TestCase):
    def test_engine_followup_keywords_do_not_trigger_local_guesses(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False

        self.assertFalse(YukikoEngine._looks_like_summary_followup("总结一下"))
        self.assertFalse(YukikoEngine._looks_like_resend_followup("再发一遍"))
        self.assertFalse(YukikoEngine._looks_like_source_trace_followup("你用了什么链接"))
        self.assertFalse(YukikoEngine._looks_like_sticker_request("发个表情包"))
        self.assertFalse(YukikoEngine._looks_like_video_text_only_intent("只要总结"))
        self.assertFalse(YukikoEngine._looks_like_music_request("点歌 热水澡"))
        self.assertFalse(engine._looks_like_qq_avatar_intent("查一下我的头像"))

    def test_engine_only_accepts_explicit_control_tokens_or_structure(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False

        self.assertTrue(YukikoEngine._looks_like_summary_followup("/summary"))
        self.assertTrue(YukikoEngine._looks_like_resend_followup("/resend"))
        self.assertTrue(YukikoEngine._looks_like_source_trace_followup("/sources"))
        self.assertTrue(YukikoEngine._looks_like_sticker_request("/sticker"))
        self.assertTrue(YukikoEngine._looks_like_video_text_only_intent("output=text"))
        self.assertTrue(YukikoEngine._looks_like_music_request("/music 热水澡"))
        self.assertTrue(engine._looks_like_qq_avatar_intent("/avatar target=self"))
        self.assertTrue(engine._looks_like_local_media_request(r"C:\temp\demo.mp4"))

    def test_tools_do_not_route_on_natural_language_cues(self) -> None:
        executor = _DummyExecutor()

        self.assertFalse(executor._looks_like_music_request("点歌 热水澡"))
        self.assertFalse(executor._looks_like_video_request("发个抖音视频"))
        self.assertFalse(executor._looks_like_image_analysis_request("看看这张图"))
        self.assertFalse(executor._looks_like_video_analysis_request("总结一下这个视频"))
        self.assertFalse(executor._looks_like_qq_avatar_request("查一下我的头像"))
        self.assertFalse(executor._looks_like_analysis_text_only_request("只要总结"))

    def test_tools_accept_only_explicit_tokens_or_media_locators(self) -> None:
        executor = _DummyExecutor()

        self.assertTrue(executor._looks_like_music_request("/music 热水澡"))
        self.assertTrue(executor._looks_like_video_request("https://example.com/demo.mp4"))
        self.assertTrue(executor._looks_like_video_analysis_request("/analyze https://example.com/demo.mp4"))
        self.assertTrue(executor._looks_like_image_analysis_request("/analyze https://example.com/demo.png"))
        self.assertTrue(executor._looks_like_qq_avatar_request("/avatar target=self"))
        self.assertTrue(executor._looks_like_analysis_text_only_request("output=text"))


if __name__ == "__main__":
    unittest.main()
