from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from plugins.wayback_plugin import Plugin, _Snapshot


class WaybackPluginRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wayback_lookup_uses_first_successful_variant_without_waiting_for_slow_one(self) -> None:
        plugin = Plugin()

        async def fake_query_cdx(
            *,
            url: str,
            from_ts: str = "",
            to_ts: str = "",
            limit: int = 8,
            include_non_200: bool = False,
        ) -> list[_Snapshot]:
            if url == "slow-variant":
                await asyncio.sleep(1)
                return [
                    _Snapshot(timestamp="20120101000000", original="http://slow.example/"),
                ]
            return [
                _Snapshot(timestamp="20120919222356", original="http://fast.example/", statuscode="200"),
            ]

        plugin._query_cdx = fake_query_cdx  # type: ignore[method-assign]

        with patch("plugins.wayback_plugin._url_variants", return_value=["slow-variant", "fast-variant"]):
            result = await asyncio.wait_for(
                plugin._handle_wayback_lookup({"url": "http://example.com", "year": 2012, "limit": 5}, {}),
                timeout=0.2,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("source_variant"), "fast-variant")
        self.assertIn("2012-09-19 22:23:56", result.display)

    async def test_resolve_snapshot_does_not_block_on_slow_variant_lookup(self) -> None:
        plugin = Plugin()

        async def fake_lookup_closest(*, url: str, timestamp: str = "") -> _Snapshot | None:
            if url == "slow-variant":
                await asyncio.sleep(1)
                return None
            return _Snapshot(timestamp="20120616160753", original="http://fast.example/")

        plugin._lookup_closest = fake_lookup_closest  # type: ignore[method-assign]

        with patch("plugins.wayback_plugin._url_variants", return_value=["slow-variant", "fast-variant"]):
            snapshot, mode = await asyncio.wait_for(
                plugin._resolve_snapshot(url="http://example.com", year=2012, timestamp=""),
                timeout=0.2,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.timestamp, "20120616160753")
        self.assertEqual(mode, "available_closest:fast-variant")

    async def test_wayback_timeline_falls_back_to_available_years_when_cdx_fails(self) -> None:
        plugin = Plugin()

        async def fake_query_cdx(
            *,
            url: str,
            from_ts: str = "",
            to_ts: str = "",
            limit: int = 8,
            include_non_200: bool = False,
        ) -> list[_Snapshot]:
            raise TimeoutError()

        async def fake_query_available_years(*, url: str, from_year: int = 0, to_year: int = 0) -> list[int]:
            return [2012, 2023]

        plugin._query_cdx = fake_query_cdx  # type: ignore[method-assign]
        plugin._query_available_years = fake_query_available_years  # type: ignore[method-assign]

        result = await plugin._handle_wayback_timeline({"url": "http://example.com"}, {})

        self.assertTrue(result.ok)
        self.assertTrue(result.data.get("approximate"))
        self.assertEqual(result.data.get("years"), [2012, 2023])
        self.assertIn("available-years fallback", result.display)

    async def test_wayback_lookup_falls_back_to_closest_snapshot_when_cdx_fails(self) -> None:
        plugin = Plugin()

        async def fake_query_cdx(
            *,
            url: str,
            from_ts: str = "",
            to_ts: str = "",
            limit: int = 8,
            include_non_200: bool = False,
        ) -> list[_Snapshot]:
            raise TimeoutError(f"rate limited for {url}")

        async def fake_resolve_snapshot(*, url: str, year: int | None, timestamp: str) -> tuple[_Snapshot | None, str]:
            return _Snapshot(timestamp="20050701000000", original=url), "available_closest:http://example.com"

        plugin._query_cdx = fake_query_cdx  # type: ignore[method-assign]
        plugin._resolve_snapshot = fake_resolve_snapshot  # type: ignore[method-assign]

        result = await plugin._handle_wayback_lookup({"url": "http://example.com", "year": 2005}, {})

        self.assertTrue(result.ok)
        self.assertTrue(result.data.get("approximate"))
        self.assertEqual(result.data.get("count"), 1)
        self.assertIn("closest snapshot", result.display)
        self.assertEqual(result.data.get("recommended", {}).get("timestamp"), "20050701000000")


if __name__ == "__main__":
    unittest.main()
