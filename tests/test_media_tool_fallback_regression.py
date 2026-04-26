from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.agent_tools import _handle_analyze_voice
from core.sticker import StickerManager


class AnalyzeVoiceFallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_voice_falls_back_to_segment_url_when_explicit_url_fails(
        self,
    ) -> None:
        explicit_url = (
            "https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=truncated"
        )
        segment_url = (
            "https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=full"
            "&format=amr&rkey=ok"
        )
        attempted_urls: list[str] = []

        async def fake_download(url: str, output_path: Path, **kwargs: object) -> bool:
            _ = (output_path, kwargs)
            attempted_urls.append(url)
            return url == segment_url

        with (
            patch("utils.media.download_file", new=AsyncMock(side_effect=fake_download)),
            patch("utils.media.extract_audio", new=AsyncMock(return_value="voice.wav")),
            patch("utils.media.transcribe_audio_enhanced", new=AsyncMock(return_value={"text": "voice text", "formatted_text": "voice text", "score": -0.5, "pass": "Pass-1-BeamSearch"})),
        ):
            result = await _handle_analyze_voice(
                {"url": explicit_url},
                {
                    "raw_segments": [
                        {"type": "record", "data": {"url": segment_url, "file": "voice.amr"}}
                    ],
                    "reply_media_segments": [],
                    "api_call": None,
                },
            )

        self.assertTrue(result.ok, result.display)
        self.assertEqual(result.data.get("text"), "voice text")
        self.assertEqual(attempted_urls[:2], [explicit_url, segment_url])

    async def test_analyze_voice_uses_file_id_lookup_even_with_explicit_url(self) -> None:
        explicit_url = (
            "https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=truncated"
        )
        recovered_url = (
            "https://multimedia.nt.qq.com.cn/download?appid=1403&fileid=recovered"
            "&format=amr&rkey=ok"
        )
        attempted_urls: list[str] = []

        async def fake_download(url: str, output_path: Path, **kwargs: object) -> bool:
            _ = (output_path, kwargs)
            attempted_urls.append(url)
            return url == recovered_url

        with (
            patch(
                "core.agent_tools_media.call_napcat_api",
                new=AsyncMock(return_value={"url": recovered_url}),
            ) as mock_call_napcat_api,
            patch("utils.media.download_file", new=AsyncMock(side_effect=fake_download)),
            patch("utils.media.extract_audio", new=AsyncMock(return_value="voice.wav")),
            patch("utils.media.transcribe_audio_enhanced", new=AsyncMock(return_value={"text": "voice text", "formatted_text": "voice text", "score": -0.5, "pass": "Pass-1-BeamSearch"})),
        ):
            result = await _handle_analyze_voice(
                {"url": explicit_url},
                {
                    "raw_segments": [{"type": "record", "data": {"file": "voice-file-id"}}],
                    "reply_media_segments": [],
                    "api_call": lambda *args, **kwargs: None,
                },
            )

        self.assertTrue(result.ok, result.display)
        self.assertEqual(result.data.get("text"), "voice text")
        self.assertEqual(attempted_urls[:2], [explicit_url, recovered_url])
        mock_call_napcat_api.assert_awaited()


class StickerLearnFallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_learn_from_chat_recovers_when_inline_image_is_too_small(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = StickerManager(Path(tmpdir) / "storage" / "state")
            real_image_path = Path(tmpdir) / "real.png"
            real_image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 256))

            tiny_inline = "data:image/png;base64," + base64.b64encode(b"tiny").decode()

            async def llm_call(messages: list[dict[str, object]]) -> str:
                _ = messages
                return (
                    '{"legal": true, "reason": "", "description": "test sticker", '
                    '"emotions": ["happy"], "category": "reaction", "tags": ["test"]}'
                )

            async def api_call(api: str, **kwargs: object) -> dict[str, object]:
                self.assertEqual(api, "get_image")
                self.assertIn("file", kwargs)
                return {"data": {"file_path": str(real_image_path)}}

            ok, message = await manager.learn_from_chat(
                image_url=tiny_inline,
                image_file="reply-image.png",
                image_sub_type="",
                user_id="10086",
                llm_call=llm_call,
                api_call=api_call,
            )

        self.assertTrue(ok, message)
        self.assertEqual(message, "")
        self.assertEqual(manager.learned_count, 1)


if __name__ == "__main__":
    unittest.main()
