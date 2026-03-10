from __future__ import annotations

import asyncio
import unittest
from types import ModuleType

from core.agent_tools import (
    _handle_music_play,
    _handle_music_play_by_id,
    _handle_music_search,
)
from core.music import MusicEngine, MusicPlayResult, MusicSearchResult, build_music_keyword
from core.search import SearchEngine
from core.tools import ToolExecutor, ToolResult
from utils.text import has_unrequested_title_qualifier, normalize_matching_text


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class MusicRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_matching_text_converts_traditional_variants(self) -> None:
        self.assertEqual(normalize_matching_text("點歌 蛋堡的熱水澡 原聲"), "点歌 蛋堡的热水澡 原声")

    def test_has_unrequested_title_qualifier_prefers_structure_over_local_word_list(self) -> None:
        self.assertTrue(has_unrequested_title_qualifier("热水澡 (伴奏)", "热水澡"))
        self.assertTrue(has_unrequested_title_qualifier("金赌兰 (VIII 250106 Remix)", "金赌兰"))
        self.assertTrue(has_unrequested_title_qualifier("热水澡伴奏", "热水澡"))
        self.assertFalse(has_unrequested_title_qualifier("金赌兰 (VIII 250106 Remix)", "金赌兰 remix"))
        self.assertFalse(has_unrequested_title_qualifier("回到", "回"))

    def test_build_music_keyword_keeps_structured_song_info_with_modifier(self) -> None:
        self.assertEqual(build_music_keyword(keyword="原聲", title="熱水澡", artist="蛋堡"), "热水澡 蛋堡 原声")
        self.assertEqual(build_music_keyword(keyword="熱水澡 原聲", title="熱水澡", artist="蛋堡"), "蛋堡 热水澡 原声")

    def test_title_match_level_accepts_traditional_and_simplified(self) -> None:
        self.assertGreaterEqual(MusicEngine._title_match_level("熱水澡", "热水澡"), 2)

    async def test_music_search_rejects_near_match_for_structured_request(self) -> None:
        executor = _DummyExecutor()

        async def fake_search(keyword: str, limit: int = 5, title: str = "", artist: str = ""):
            return [
                MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=76881, name="关于小熊", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=76899, name="收敛水", artist="蛋堡", duration_ms=180000),
            ]

        executor._music_engine.search = fake_search

        result = await executor._music_search({"title": "洗澡水", "artist": "蛋堡"}, "")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_exact_match")
        self.assertEqual(result.payload.get("results"), [])
        self.assertIn("不能直接当成同一首播", result.payload.get("text", ""))

    async def test_music_search_returns_only_exact_structured_matches(self) -> None:
        executor = _DummyExecutor()

        async def fake_search(keyword: str, limit: int = 5, title: str = "", artist: str = ""):
            return [
                MusicSearchResult(song_id=1, name="洗澡水", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=2, name="洗澡水 (伴奏)", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=3, name="洗澡水", artist="别人", duration_ms=180000),
            ]

        executor._music_engine.search = fake_search

        result = await executor._music_search({"title": "洗澡水", "artist": "蛋堡"}, "")

        self.assertTrue(result.ok)
        self.assertEqual(
            result.payload.get("results"),
            [{"id": 1, "name": "洗澡水", "artist": "蛋堡"}],
        )
        self.assertNotIn("伴奏", result.payload.get("text", ""))
        self.assertNotIn("别人", result.payload.get("text", ""))

    async def test_music_search_accepts_traditional_title_input(self) -> None:
        executor = _DummyExecutor()

        async def fake_search(keyword: str, limit: int = 5, title: str = "", artist: str = ""):
            return [
                MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=88, name="热水澡 (翻自 蛋堡)", artist="别人", duration_ms=180000),
            ]

        executor._music_engine.search = fake_search

        result = await executor._music_search({"title": "熱水澡", "artist": "蛋堡"}, "")

        self.assertTrue(result.ok)
        self.assertEqual(result.payload.get("results"), [{"id": 76897, "name": "热水澡", "artist": "蛋堡"}])

    async def test_music_search_with_modifier_keeps_title_and_artist_in_query(self) -> None:
        executor = _DummyExecutor()
        seen_queries: list[tuple[str, str, str]] = []

        async def fake_search(keyword: str, limit: int = 5, title: str = "", artist: str = ""):
            seen_queries.append((keyword, title, artist))
            return [MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000)]

        executor._music_engine.search = fake_search

        result = await executor._music_search({"title": "热水澡", "artist": "蛋堡", "keyword": "原聲"}, "")

        self.assertTrue(result.ok)
        self.assertEqual(seen_queries[0], ("原声", "热水澡", "蛋堡"))

    async def test_music_search_reports_artist_mismatch_without_playable_results(self) -> None:
        executor = _DummyExecutor()

        async def fake_search(keyword: str, limit: int = 5, title: str = "", artist: str = ""):
            return [
                MusicSearchResult(song_id=3, name="洗澡水", artist="别人", duration_ms=180000),
                MusicSearchResult(song_id=4, name="洗澡水 (伴奏)", artist="蛋堡", duration_ms=180000),
            ]

        executor._music_engine.search = fake_search

        result = await executor._music_search({"title": "洗澡水", "artist": "蛋堡"}, "")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "artist_mismatch")
        self.assertEqual(result.payload.get("results"), [])
        self.assertIn("不能直接替你播", result.payload.get("text", ""))

    async def test_music_play_does_not_retry_after_no_exact_match(self) -> None:
        executor = _DummyExecutor()
        calls = {"search": 0, "play_by_id": 0}

        class FakeMusicEngine:
            async def play(self, *args, **kwargs):
                return MusicPlayResult(
                    ok=False,
                    message="没找到和《洗澡水》明确匹配的可播歌曲。",
                    error="no_exact_match",
                )

        async def fake_music_search(tool_args: dict, message_text: str):
            calls["search"] += 1
            return ToolResult(
                ok=True,
                tool_name="music_search",
                payload={"results": [{"id": 76897, "name": "热水澡", "artist": "蛋堡"}]},
            )

        async def fake_music_play_by_id(tool_args: dict, api_call, group_id: int):
            calls["play_by_id"] += 1
            return ToolResult(ok=True, tool_name="music_play_by_id", payload={"text": "不该走到这里"})

        executor._music_engine = FakeMusicEngine()
        executor._music_search = fake_music_search
        executor._music_play_by_id = fake_music_play_by_id

        result = await executor._music_play(
            {"title": "洗澡水", "artist": "蛋堡"},
            "",
            None,
            0,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_exact_match")
        self.assertEqual(calls, {"search": 0, "play_by_id": 0})

    async def test_music_play_normalizes_traditional_title_and_modifier_before_engine_call(self) -> None:
        executor = _DummyExecutor()
        seen_calls: list[tuple[str, str, str, bool]] = []

        class FakeMusicEngine:
            async def play(self, keyword: str = "", as_voice: bool = True, title: str = "", artist: str = ""):
                seen_calls.append((keyword, title, artist, as_voice))
                return MusicPlayResult(
                    ok=False,
                    message="没找到和《热水澡》明确匹配的可播歌曲。",
                    error="no_exact_match",
                )

        executor._music_engine = FakeMusicEngine()

        result = await executor._music_play(
            {"title": "熱水澡", "artist": "蛋堡", "keyword": "原聲"},
            "",
            None,
            0,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_exact_match")
        self.assertEqual(seen_calls, [("原声", "热水澡", "蛋堡", True)])

    async def test_search_bilibili_videos_filters_multi_token_mismatch(self) -> None:
        import sys

        fake_api = ModuleType("bilibili_api")
        fake_search = ModuleType("bilibili_api.search")
        fake_search.SearchObjectType = ModuleType("SearchObjectType")
        fake_search.SearchObjectType.VIDEO = "video"

        async def fake_search_by_type(keyword: str, search_type, page: int):
            return {
                "result": [
                    {
                        "bvid": "BV1AXAZeeENK",
                        "title": "『下架歌曲』《××你好》 in3 阴三儿（附下载链接）",
                        "description": "相关搬运",
                        "duration": "05:45",
                    },
                    {
                        "bvid": "BV1RightHit",
                        "title": "IN3《黑》完整版",
                        "description": "in3 官方歌词版",
                        "duration": "03:45",
                    },
                ]
            }

        fake_search.search_by_type = fake_search_by_type
        fake_api.search = fake_search
        original_module = sys.modules.get("bilibili_api")
        sys.modules["bilibili_api"] = fake_api
        try:
            engine = SearchEngine({})
            results = await engine.search_bilibili_videos("IN3 黑", limit=5)
        finally:
            if original_module is None:
                del sys.modules["bilibili_api"]
            else:
                sys.modules["bilibili_api"] = original_module

        self.assertEqual(len(results), 1)
        self.assertIn("《黑》", results[0].title)

    async def test_search_bilibili_videos_returns_empty_when_no_row_matches_multi_token_query(self) -> None:
        import sys

        fake_api = ModuleType("bilibili_api")
        fake_search = ModuleType("bilibili_api.search")
        fake_search.SearchObjectType = ModuleType("SearchObjectType")
        fake_search.SearchObjectType.VIDEO = "video"

        async def fake_search_by_type(keyword: str, search_type, page: int):
            return {
                "result": [
                    {
                        "bvid": "BV1OnlyIN3",
                        "title": "『下架歌曲』《××你好》 in3 阴三儿（附下载链接）",
                        "description": "相关搬运",
                        "duration": "05:45",
                    },
                    {
                        "bvid": "BV1OnlyHei",
                        "title": "黑色幽默 现场版",
                        "description": "无关描述",
                        "duration": "04:00",
                    },
                ]
            }

        fake_search.search_by_type = fake_search_by_type
        fake_api.search = fake_search
        original_module = sys.modules.get("bilibili_api")
        sys.modules["bilibili_api"] = fake_api
        try:
            engine = SearchEngine({})
            results = await engine.search_bilibili_videos("IN3 黑", limit=5)
        finally:
            if original_module is None:
                del sys.modules["bilibili_api"]
            else:
                sys.modules["bilibili_api"] = original_module

        self.assertEqual(results, [])

    async def test_music_engine_search_prefers_canonical_query_before_modifier_only_keyword(self) -> None:
        engine = MusicEngine({"music": {"enable": True, "local_source_enable": False}})
        alger_queries: list[str] = []
        netease_queries: list[str] = []

        async def fake_search_alger(keyword: str, limit: int):
            alger_queries.append(keyword)
            if keyword == "热水澡 蛋堡":
                return [MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000)]
            return []

        async def fake_search_netease(keyword: str, limit: int):
            netease_queries.append(keyword)
            return []

        engine._search_alger = fake_search_alger
        engine._search_netease = fake_search_netease

        results = await engine.search("原聲", title="熱水澡", artist="蛋堡")

        self.assertEqual([(song.song_id, song.name) for song in results], [(76897, "热水澡")])
        self.assertEqual(alger_queries[0], "热水澡 蛋堡")
        self.assertEqual(netease_queries, [])

    async def test_get_play_url_with_alternative_skips_local_fallback_for_original_request(self) -> None:
        engine = MusicEngine({"music": {"enable": True, "local_source_enable": True}})
        song = MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000)
        source_calls = {"count": 0}

        async def fake_get_play_url(song_id: int):
            from core.music import MusicPlayUrl

            return MusicPlayUrl(url="", duration_ms=0, is_trial=False, source="", level="")

        class FakeMatcher:
            async def find_alternative(self, *args, **kwargs):
                source_calls["count"] += 1
                raise AssertionError("original request should not search alternative sources")

        engine._get_play_url = fake_get_play_url
        engine._source_matcher = FakeMatcher()

        play_url = await engine._get_play_url_with_alternative(song, require_verified_original=True)

        self.assertEqual(play_url.url, "")
        self.assertEqual(source_calls["count"], 0)

    async def test_get_play_url_with_alternative_respects_configured_source_order(self) -> None:
        engine = MusicEngine({"music": {"enable": True, "local_source_enable": True, "unblock_sources": "soundcloud"}})
        song = MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000)
        seen_sources: list[list[str] | None] = []

        async def fake_get_play_url(song_id: int):
            from core.music import MusicPlayUrl

            return MusicPlayUrl(url="", duration_ms=0, is_trial=False, source="", level="")

        class FakeMatcher:
            async def find_alternative(self, song_name: str, artist: str, duration_ms: int, sources: list[str] | None = None):
                seen_sources.append(list(sources) if sources is not None else None)
                return None

        engine._get_play_url = fake_get_play_url
        engine._source_matcher = FakeMatcher()

        await engine._get_play_url_with_alternative(song)

        self.assertEqual(seen_sources, [["soundcloud"]])

    async def test_music_play_with_exact_title_does_not_drift_to_other_song_after_preview_only(self) -> None:
        engine = MusicEngine({"music": {"enable": True, "local_source_enable": False}})
        seen_song_ids: list[int] = []

        async def fake_search(keyword: str, limit: int = 12, title: str = "", artist: str = ""):
            return [
                MusicSearchResult(song_id=76897, name="热水澡", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=76881, name="关于小熊", artist="蛋堡", duration_ms=180000),
                MusicSearchResult(song_id=29343276, name="回到", artist="蛋堡", duration_ms=180000),
            ]

        async def fake_play_song(song, as_voice: bool = True, require_verified_original: bool = False):
            seen_song_ids.append(song.song_id)
            return MusicPlayResult(
                ok=False,
                song=song,
                message="当前只能拿到试听片段。",
                error="preview_only",
            )

        engine.search = fake_search
        engine._play_song = fake_play_song

        result = await engine.play("热水澡", title="热水澡", artist="蛋堡")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "preview_only")
        self.assertEqual(seen_song_ids, [76897])

    def test_should_avoid_version_blocks_remix_when_not_requested(self) -> None:
        self.assertTrue(MusicEngine._should_avoid_version("金赌兰 (VIII 250106 Remix)", "金赌兰"))
        self.assertFalse(MusicEngine._should_avoid_version("金赌兰 (VIII 250106 Remix)", "金赌兰 remix"))

    async def test_handle_music_search_propagates_exact_match_failure(self) -> None:
        class FakeExecutor:
            async def execute(self, **kwargs):
                return ToolResult(
                    ok=False,
                    tool_name="music_search",
                    error="no_exact_match",
                    payload={"text": "没找到和《洗澡水》明确匹配的歌曲。"},
                )

        result = await _handle_music_search(
            {"title": "洗澡水", "artist": "蛋堡"},
            {
                "tool_executor": FakeExecutor(),
                "conversation_id": "",
                "user_id": "",
                "user_name": "",
                "group_id": 0,
                "api_call": None,
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_exact_match")
        self.assertIn("明确匹配", result.display)

    async def test_handle_music_play_returns_media_when_play_succeeds(self) -> None:
        class FakeExecutor:
            async def execute(self, **kwargs):
                return ToolResult(
                    ok=True,
                    tool_name="music_play",
                    payload={
                        "text": "给你点了蛋堡的《热水澡》~",
                        "audio_file": "storage/cache/music/demo.mp3",
                    },
                )

        result = await _handle_music_play(
            {"title": "热水澡", "artist": "蛋堡"},
            {
                "tool_executor": FakeExecutor(),
                "conversation_id": "",
                "user_id": "",
                "user_name": "",
                "group_id": 0,
                "api_call": object(),
                "trace_id": "test-trace",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("audio_file"), "storage/cache/music/demo.mp3")
        self.assertIn("热水澡", result.display)

    async def test_music_play_by_id_returns_audio_payload(self) -> None:
        executor = _DummyExecutor()

        class FakeMusicEngine:
            async def _play_song(self, song, as_voice: bool = True, require_verified_original: bool = False):
                return MusicPlayResult(
                    ok=True,
                    song=song,
                    message="热水澡",
                    audio_path="storage/cache/music/demo.mp3",
                    silk_path="storage/cache/music/demo.silk",
                )

        executor._music_engine = FakeMusicEngine()

        result = await executor._music_play_by_id(
            {"song_id": 76897, "song_name": "热水澡", "artist": "蛋堡"},
            api_call=object(),
            group_id=0,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload.get("audio_file"), "storage/cache/music/demo.mp3")
        self.assertEqual(result.payload.get("audio_file_silk"), "storage/cache/music/demo.silk")

    async def test_music_play_by_id_preserves_original_requirement_from_keyword(self) -> None:
        executor = _DummyExecutor()
        seen_calls: list[tuple[bool, str, str]] = []

        class FakeMusicEngine:
            async def _play_song(self, song, as_voice: bool = True, require_verified_original: bool = False):
                seen_calls.append((require_verified_original, song.name, song.artist))
                return MusicPlayResult(
                    ok=True,
                    song=song,
                    message="热水澡",
                    audio_path="storage/cache/music/demo.mp3",
                    silk_path="storage/cache/music/demo.silk",
                )

        executor._music_engine = FakeMusicEngine()

        result = await executor._music_play_by_id(
            {"song_id": 76897, "song_name": "热水澡", "artist": "蛋堡", "keyword": "原聲"},
            api_call=object(),
            group_id=0,
        )

        self.assertTrue(result.ok)
        self.assertEqual(seen_calls, [(True, "热水澡", "蛋堡")])

    async def test_handle_music_play_by_id_requires_media_payload(self) -> None:
        class FakeExecutor:
            async def execute(self, **kwargs):
                return ToolResult(
                    ok=True,
                    tool_name="music_play_by_id",
                    payload={"text": "热水澡"},
                )

        result = await _handle_music_play_by_id(
            {"song_id": 76897, "song_name": "热水澡", "artist": "蛋堡"},
            {
                "tool_executor": FakeExecutor(),
                "conversation_id": "",
                "user_id": "",
                "user_name": "",
                "group_id": 0,
                "api_call": object(),
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "voice_prepare_failed")

    async def test_handle_music_play_by_id_returns_media_when_available(self) -> None:
        class FakeExecutor:
            async def execute(self, **kwargs):
                return ToolResult(
                    ok=True,
                    tool_name="music_play_by_id",
                    payload={
                        "text": "热水澡",
                        "audio_file": "storage/cache/music/demo.mp3",
                        "audio_file_silk": "storage/cache/music/demo.silk",
                    },
                )

        result = await _handle_music_play_by_id(
            {"song_id": 76897, "song_name": "热水澡", "artist": "蛋堡"},
            {
                "tool_executor": FakeExecutor(),
                "conversation_id": "",
                "user_id": "",
                "user_name": "",
                "group_id": 0,
                "api_call": object(),
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("audio_file"), "storage/cache/music/demo.mp3")
        self.assertEqual(result.data.get("audio_file_silk"), "storage/cache/music/demo.silk")


if __name__ == "__main__":
    unittest.main()
