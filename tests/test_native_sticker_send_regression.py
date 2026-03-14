from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.agent_tools import _handle_send_emoji, _make_learn_sticker_handler
from core.sticker import FaceInfo, StickerManager


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class NativeStickerManagerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self._tmpdir.name) / "storage" / "state"
        self.manager = StickerManager(self.storage_dir)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_preferred_segment_uses_native_mface_and_persists(self) -> None:
        key = self.manager._save_chat_emoji(
            user_id="10001",
            img_data=_PNG_BYTES,
            description="贴贴",
            emotions=["开心"],
            category="反应",
            tags=["贴贴"],
            native_segment_type="mface",
            native_segment_data={
                "emoji_package_id": "321",
                "emoji_id": "654",
                "key": "mface-key",
                "summary": "[贴贴]",
            },
        )

        seg, mode, meta = self.manager.get_preferred_emoji_segment(key)
        self.assertEqual(mode, "mface")
        self.assertEqual(seg["type"], "mface")
        self.assertEqual(meta["fallback"], "native")
        self.assertEqual(seg["data"]["key"], "mface-key")

        reloaded = StickerManager(self.storage_dir)
        seg2, mode2, meta2 = reloaded.get_preferred_emoji_segment(key)
        self.assertEqual(mode2, "mface")
        self.assertEqual(seg2["type"], "mface")
        self.assertEqual(seg2["data"]["emoji_package_id"], "321")
        self.assertEqual(meta2["fallback"], "native")

    def test_preferred_segment_falls_back_to_face_before_image(self) -> None:
        self.manager._faces[76] = FaceInfo(face_id=76, desc="/赞")
        key = self.manager._save_chat_emoji(
            user_id="10002",
            img_data=_PNG_BYTES,
            description="给你点个赞",
            emotions=["赞"],
            category="反应",
            tags=["点赞"],
        )

        seg, mode, meta = self.manager.get_preferred_emoji_segment(key)
        self.assertEqual(mode, "face")
        self.assertEqual(seg["type"], "face")
        self.assertEqual(seg["data"]["id"], "76")
        self.assertEqual(meta["fallback"], "semantic_face")

    def test_preferred_segment_falls_back_to_image_last(self) -> None:
        key = self.manager._save_chat_emoji(
            user_id="10003",
            img_data=_PNG_BYTES,
            description="宇宙谜语图",
            emotions=["玄学"],
            category="其他",
            tags=["未知"],
        )

        seg, mode, meta = self.manager.get_preferred_emoji_segment(key)
        self.assertEqual(mode, "image")
        self.assertEqual(seg["type"], "image")
        self.assertIn("file:///", seg["data"]["file"])
        self.assertEqual(meta["fallback"], "image")


class NativeStickerToolRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_emoji_prefers_native_mface_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = StickerManager(Path(tmpdir) / "storage" / "state")
            manager._save_chat_emoji(
                user_id="10001",
                img_data=_PNG_BYTES,
                description="贴贴",
                emotions=["开心"],
                category="反应",
                tags=["贴贴"],
                native_segment_type="mface",
                native_segment_data={
                    "emoji_package_id": "1",
                    "emoji_id": "2",
                    "key": "native-key",
                    "summary": "[贴贴]",
                },
            )

            calls: list[tuple[str, dict]] = []

            async def api_call(api: str, **kwargs):
                calls.append((api, kwargs))
                return {"status": "ok"}

            result = await _handle_send_emoji(
                {"query": "贴贴"},
                {
                    "sticker_manager": manager,
                    "api_call": api_call,
                    "group_id": 123456,
                    "user_id": "10001",
                    "config": {},
                },
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.data["send_mode"], "mface")
            self.assertEqual(calls[0][0], "send_group_msg")
            self.assertEqual(calls[0][1]["message"][0]["type"], "mface")

    async def test_learn_sticker_extracts_native_segment_from_replied_message(self) -> None:
        sticker_mgr = SimpleNamespace(
            learn_from_chat=AsyncMock(return_value=(True, "")),
        )
        model_client = SimpleNamespace(chat_text=AsyncMock(return_value="{}"))
        handler = _make_learn_sticker_handler(model_client)

        async def api_call(api: str, **kwargs):
            self.assertEqual(api, "get_msg")
            self.assertEqual(str(kwargs.get("message_id")), "778899")
            return {
                "data": {
                    "message_id": 778899,
                    "message": [
                        {
                            "type": "mface",
                            "data": {
                                "emoji_package_id": "11",
                                "emoji_id": "22",
                                "key": "reply-native-key",
                                "summary": "[拍拍]",
                                "url": "https://example.com/sticker.png",
                            },
                        },
                        {
                            "type": "image",
                            "data": {
                                "url": "https://example.com/sticker.png",
                                "file": "reply-image.png",
                            },
                        },
                    ],
                }
            }

        result = await handler(
            {},
            {
                "sticker_manager": sticker_mgr,
                "api_call": api_call,
                "reply_to_message_id": "778899",
                "reply_media_segments": [],
                "raw_segments": [],
                "user_id": "10086",
            },
        )

        self.assertTrue(result.ok)
        sticker_mgr.learn_from_chat.assert_awaited_once()
        kwargs = sticker_mgr.learn_from_chat.await_args.kwargs
        self.assertEqual(kwargs["image_url"], "https://example.com/sticker.png")
        self.assertEqual(kwargs["image_file"], "reply-image.png")
        self.assertEqual(kwargs["native_segment_type"], "mface")
        self.assertEqual(kwargs["native_segment_data"]["key"], "reply-native-key")


if __name__ == "__main__":
    unittest.main()
