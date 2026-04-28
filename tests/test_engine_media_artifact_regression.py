from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from core.engine import YukikoEngine
from core.engine_types import EngineMessage


class EngineMediaArtifactRegressionTests(unittest.TestCase):
    def test_recent_media_artifact_only_injected_for_reply_to_bot(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
        engine.search_followup_cache_enable = True
        engine.search_followup_cache_ttl_seconds = 1800
        now = datetime.now(UTC)
        engine._recent_search_cache = {
            "group:1:42": {
                "timestamp": now,
                "query": "解析 https://v.douyin.com/demo/",
                "summary": "解析成功",
                "evidence": [
                    {
                        "title": "媒体来源",
                        "source": "https://v.douyin.com/demo/",
                    }
                ],
                "choices": [
                    {
                        "title": "最近媒体结果",
                        "url": "/tmp/yukiko/demo.mp4",
                        "video_url": "/tmp/yukiko/demo.mp4",
                    }
                ],
            }
        }

        reply_to_bot = EngineMessage(
            conversation_id="group:1",
            user_id="42",
            text="那你发",
            bot_id="bot",
            reply_to_user_id="bot",
            reply_to_message_id="100",
            timestamp=now,
            trace_id="artifact-test",
        )
        normal_turn = EngineMessage(
            conversation_id="group:1",
            user_id="42",
            text="你好",
            bot_id="bot",
            timestamp=now,
            trace_id="artifact-test-2",
        )

        artifact = engine._recent_media_artifact_for_agent(reply_to_bot)
        self.assertEqual(artifact["type"], "video")
        self.assertEqual(artifact["video_url"], "/tmp/yukiko/demo.mp4")
        self.assertEqual(artifact["source_url"], "https://v.douyin.com/demo/")
        self.assertEqual(engine._recent_media_artifact_for_agent(normal_turn), {})

    def test_recent_web_artifact_injected_for_reply_to_bot(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
        engine.search_followup_cache_enable = True
        engine.search_followup_cache_ttl_seconds = 1800
        now = datetime.now(UTC)
        engine._recent_search_cache = {
            "group:1:42": {
                "timestamp": now,
                "query": "网络时光机 看 skiapi.dev",
                "summary": "https://skiapi.dev 当前 502",
                "evidence": [
                    {
                        "title": "来源",
                        "source": "https://skiapi.dev",
                    }
                ],
                "choices": [],
            }
        }
        reply_to_bot = EngineMessage(
            conversation_id="group:1",
            user_id="42",
            text="都说了网络时光机",
            bot_id="bot",
            reply_to_user_id="bot",
            reply_to_message_id="101",
            timestamp=now,
            trace_id="web-artifact-test",
        )

        artifact = engine._recent_media_artifact_for_agent(reply_to_bot)

        self.assertEqual(artifact["type"], "web")
        self.assertEqual(artifact["source_url"], "https://skiapi.dev")


if __name__ == "__main__":
    unittest.main()
