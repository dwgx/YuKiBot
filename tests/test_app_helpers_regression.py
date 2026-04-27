from __future__ import annotations

import unittest

from app_helpers import (
    _looks_like_download_heavy_request,
    _looks_like_sticker_learning_request,
    _looks_like_video_heavy_request,
    _looks_like_web_heavy_request,
)


class AppHelpersRegressionTests(unittest.TestCase):
    def test_video_heavy_detects_platform_parse_requests(self) -> None:
        self.assertTrue(
            _looks_like_video_heavy_request(
                "https://www.bilibili.com/video/BV16aw4zAEqD/?x=1解析",
                [],
            )
        )
        self.assertTrue(
            _looks_like_video_heavy_request(
                "帮我看看这个腾讯视频 https://v.qq.com/x/page/a1234567890.html",
                [],
            )
        )

    def test_web_heavy_detects_bare_domain_without_video(self) -> None:
        self.assertTrue(_looks_like_web_heavy_request("skiapi.dev这个网站帮我看看", []))
        self.assertFalse(
            _looks_like_web_heavy_request(
                "https://www.bilibili.com/video/BV16aw4zAEqD/解析",
                [],
            )
        )

    def test_download_and_sticker_learning_detectors_are_not_dead_stubs(self) -> None:
        self.assertTrue(_looks_like_download_heavy_request("帮我下载这个 app.apk", []))
        self.assertTrue(
            _looks_like_sticker_learning_request(
                "学习这个表情包",
                [{"type": "image", "data": {"url": "https://example.com/a.png"}}],
            )
        )


if __name__ == "__main__":
    unittest.main()
