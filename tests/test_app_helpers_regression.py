from __future__ import annotations

import base64
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import app_helpers
from app_helpers import (
    _looks_like_download_heavy_request,
    _looks_like_sticker_learning_request,
    _looks_like_video_heavy_request,
    _looks_like_web_heavy_request,
)
from nonebot.adapters.onebot.v11 import Message, MessageSegment


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


class AppHelpersNapCatMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_send_does_not_count_media_plain_text_fallback_as_success(self) -> None:
        class FakeBot:
            self_id = "3223915831"

            def __init__(self) -> None:
                self.sent: list[Message] = []

            async def send(self, *, event, message):
                self.sent.append(message)
                raise RuntimeError("bad request: invalid segment")

        class FakeEvent:
            group_id = 901738883

        async def noop(*args, **kwargs) -> None:
            return None

        bot = FakeBot()
        message = Message("解析好了，我直接把视频发出来。")
        message += MessageSegment.image("https://example.com/cat.jpg")

        with (
            patch.object(app_helpers, "_check_bot_send_suspended", lambda bot_id: (False, ""), create=True),
            patch.object(app_helpers, "_check_group_send_block", lambda group_id: (False, ""), create=True),
            patch.object(app_helpers, "_maybe_block_group_send_on_error", noop, create=True),
            patch.object(app_helpers, "_is_hard_send_channel_error", lambda exc: False, create=True),
            patch.object(app_helpers, "_is_transient_send_error", lambda exc: False, create=True),
            patch.object(app_helpers, "_is_payload_send_error", lambda exc: True, create=True),
        ):
            ok = await app_helpers._safe_send(bot=bot, event=FakeEvent(), message=message)

        self.assertFalse(ok)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("image", str(bot.sent[0]))

    async def test_remote_image_segment_downloads_to_base64_before_direct_url(self) -> None:
        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, url: str):
                return SimpleNamespace(
                    status_code=200,
                    content=b"image-bytes",
                    headers={"content-type": "image/jpeg"},
                    url=url,
                )

        with patch.object(app_helpers.httpx, "AsyncClient", FakeAsyncClient):
            segment = await app_helpers._build_image_segment_from_remote_url("https://example.com/cat.jpg")

        self.assertIsNotNone(segment)
        self.assertEqual(segment.type, "image")
        self.assertEqual(segment.data["file"], "base64://" + base64.b64encode(b"image-bytes").decode("ascii"))

    async def test_video_segment_uses_napcat_file_uris_for_local_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "demo.mp4"
            thumb = Path(tmpdir) / "demo.jpg"
            video.write_bytes(b"video")
            thumb.write_bytes(b"thumb")

            async def passthrough(path: Path) -> Path:
                return path

            async def healthy(path: Path) -> tuple[bool, str]:
                return True, ""

            async def thumbnail(path: Path) -> Path:
                return thumb

            with (
                patch.object(app_helpers, "_compress_video_if_needed", passthrough),
                patch.object(app_helpers, "_ensure_qq_preview_video", passthrough),
                patch.object(app_helpers, "_probe_local_video_health", healthy),
                patch.object(app_helpers, "_generate_video_thumbnail", thumbnail),
            ):
                segment = await app_helpers._video_seg_with_thumb(video)

            self.assertIsNotNone(segment)
            self.assertEqual(segment.type, "video")
            self.assertTrue(segment.data["file"].startswith("file://"))
            self.assertTrue(segment.data["thumb"].startswith("file://"))

    def test_stage_media_for_napcat_copies_to_configured_directory(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as stage_dir:
            video = Path(src_dir) / "demo.mp4"
            video.write_bytes(b"video")

            staged = app_helpers._stage_media_for_napcat(video, stage_dir)

            self.assertIsNotNone(staged)
            self.assertEqual(staged.read_bytes(), b"video")
            self.assertEqual(staged.parent.resolve(), Path(stage_dir).resolve())
            self.assertNotEqual(staged.resolve(), video.resolve())

    def test_video_probe_helpers_tolerate_missing_ffmpeg_binaries(self) -> None:
        with patch.object(app_helpers, "_FFPROBE_BIN", None), patch.object(app_helpers, "_FFMPEG_BIN", None):
            info = app_helpers._read_media_stream_info_sync(Path("missing.mp4"))

        self.assertEqual(info, {"video_codec": "", "audio_codec": "", "pix_fmt": ""})

    async def test_generate_video_thumbnail_tolerates_missing_ffmpeg(self) -> None:
        with patch.object(app_helpers, "_FFMPEG_BIN", None):
            thumb = await app_helpers._generate_video_thumbnail(Path("missing.mp4"))

        self.assertIsNone(thumb)

    async def test_private_video_upload_fallback_uses_private_file_api(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def call_api(self, api: str, **kwargs):
                self.calls.append((api, dict(kwargs)))
                return {"status": "ok", "retcode": 0, "data": {}}

        class FakePrivateEvent:
            user_id = 123456

            def get_user_id(self) -> str:
                return "123456"

        async def healthy(path: Path) -> tuple[bool, str]:
            return True, ""

        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "demo.mp4"
            video.write_bytes(b"video")
            bot = FakeBot()
            stage_dir = Path(tmpdir) / "stage"
            with patch.object(app_helpers, "_probe_local_video_health", healthy):
                uploaded = await app_helpers._try_upload_video_file(
                    bot,
                    FakePrivateEvent(),
                    str(video),
                    stage_dir=str(stage_dir),
                )

        self.assertTrue(uploaded)
        self.assertEqual(bot.calls[0][0], "upload_private_file")
        self.assertEqual(bot.calls[0][1]["user_id"], "123456")
        self.assertEqual(bot.calls[0][1]["name"], "demo.mp4")
        sent_path = Path(str(bot.calls[0][1]["file"])).resolve()
        self.assertTrue(sent_path.is_relative_to(stage_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
