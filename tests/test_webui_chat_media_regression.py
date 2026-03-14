from __future__ import annotations

import unittest

from core.webui import (
    _format_chat_message_item,
    _guess_media_type_from_hint,
    _render_message_text,
)


class WebuiChatMediaRegressionTests(unittest.TestCase):
    def test_render_message_text_maps_cq_image_to_placeholder(self) -> None:
        text = _render_message_text("[CQ:image,file=abc.jpg]", [])
        self.assertEqual(text, "[image]")

    def test_format_chat_message_item_parses_cq_string_segments(self) -> None:
        mapped = _format_chat_message_item(
            {
                "message_id": "1",
                "message_seq": "2",
                "time": 1710000000,
                "sender": {
                    "user_id": "123456",
                    "nickname": "Tester",
                },
                "message": "[CQ:image,file=abc.jpg]",
            },
            bot_self_id="999999",
        )

        segments = mapped.get("segments", [])
        self.assertTrue(isinstance(segments, list) and segments)
        self.assertEqual(segments[0].get("type"), "image")
        self.assertEqual(
            str((segments[0].get("data", {}) or {}).get("file", "")),
            "abc.jpg",
        )

    def test_guess_media_type_uses_file_extension(self) -> None:
        self.assertEqual(_guess_media_type_from_hint("foo.jpg"), "image/jpeg")


if __name__ == "__main__":
    unittest.main()
