import asyncio
import argparse
import sys
import time
import logging
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.search import SearchEngine
from core.image import ImageEngine
from core.tools import ToolExecutor
from core.music import MusicEngine
from plugins.wayback_plugin import Plugin as WaybackPlugin
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_log = logging.getLogger("stress_test")

class _DummyModelClient:
    enabled = False

async def _dummy_plugin_runner(*args, **kwargs) -> str:
    return ""

class StressTester:
    def __init__(self, config: dict):
        self.config = config
        self.search = SearchEngine(config.get("search", {}))
        self.image = ImageEngine(config.get("image", {}), _DummyModelClient())
        self.tools = ToolExecutor(
            search_engine=self.search,
            image_engine=self.image,
            plugin_runner=_dummy_plugin_runner,
            config=config,
        )
        self.music = MusicEngine(config.get("music", {}))
        self.wayback = WaybackPlugin()

    async def setup(self):
        await self.wayback.setup(self.config.get("wayback", {}), type("DummyCtx", (), {"agent_tool_registry": None})())

    async def teardown(self):
        await self.wayback.teardown()

    async def test_wayback_lookup(self, url: str):
        t0 = time.time()
        try:
            snaps = await self.wayback._query_cdx_with_budget(url=url, limit=5, deadline=time.time()+15.0)
            return True, f"Found {len(snaps)} snaps", time.time() - t0
        except Exception as e:
            return False, str(e), time.time() - t0

    async def test_bilibili_parse(self, url: str):
        t0 = time.time()
        try:
            res = await self.tools._method_browser_resolve_video("parse_video", {"url": url}, url)
            if res.ok and res.payload and "video_url" in res.payload:
                return True, "Parsed Bilibili Video", time.time() - t0
            return False, str(res.error or "Failed parsing"), time.time() - t0
        except Exception as e:
            return False, str(e), time.time() - t0

    async def test_bilibili_analyze(self, url: str):
        t0 = time.time()
        try:
            res = await self.tools._method_video_analyze("analyze_video", {"url": url}, url, url)
            if res.ok and res.payload and "text" in res.payload:
                # Check if it includes comments
                text = str(res.payload["text"])
                if "hot_comments" in res.payload or "评论" in text:
                    return True, "Analyzed Bilibili + Comments", time.time() - t0
                return True, "Analyzed Bilibili (No comments found)", time.time() - t0
            return False, str(res.error or "Failed analyze"), time.time() - t0
        except Exception as e:
            return False, str(e), time.time() - t0

    async def test_douyin_parse(self, url: str):
        t0 = time.time()
        try:
            res = await self.tools._method_browser_resolve_video("parse_video", {"url": url}, url)
            if res.ok and res.payload and "video_url" in res.payload:
                return True, "Parsed Douyin Video", time.time() - t0
            return False, str(res.error or "Failed parsing"), time.time() - t0
        except Exception as e:
            return False, str(e), time.time() - t0

    async def test_music_search(self, keyword: str):
        t0 = time.time()
        try:
            res = await self.music.search(keyword)
            if res:
                return True, f"Found {len(res)} songs for '{keyword}'", time.time() - t0
            return False, "No songs found", time.time() - t0
        except Exception as e:
            return False, str(e), time.time() - t0

    async def _pick_douyin_candidate(self) -> str:
        queries = ["site:douyin.com/video 抖音", "site:v.douyin.com 抖音 分享"]
        for q in queries:
            try:
                rows = await self.search.search(q)
                for r in rows:
                    url = str(getattr(r, "url", ""))
                    if "douyin.com" in url:
                        return url
            except Exception:
                continue
        return "https://v.douyin.com/idr8D37T/"

    async def run_all(self):
        await self.setup()
        tasks = []
        
        wayback_urls = ["apple.com", "microsoft.com", "qq.com", "baidu.com", "bilibili.com"]
        bili_urls = ["https://www.bilibili.com/video/BV1gy4y1B7aK/"] * 3
        
        live_dy = await self._pick_douyin_candidate()
        dy_urls = [live_dy] * 3  # Use a live link
        music_keywords = ["周杰伦 晴天", "Taylor Swift", "yoasobi", "Aimer", "Eminem"]

        # 1. Wayback
        for u in wayback_urls:
            tasks.append(("Wayback", u, self.test_wayback_lookup(u)))
            
        # 2. Bilibili Parse
        for u in bili_urls:
            tasks.append(("Bilibili_Parse", u, self.test_bilibili_parse(u)))
            
        # 3. Bilibili Analyze (Comments)
        for u in bili_urls:
            tasks.append(("Bilibili_Analyze", u, self.test_bilibili_analyze(u)))
            
        # 4. Douyin Parse
        for u in dy_urls:
            tasks.append(("Douyin_Parse", u, self.test_douyin_parse(u)))
            
        # 5. Music Search
        for k in music_keywords:
            tasks.append(("Music_Search", k, self.test_music_search(k)))

        _log.info(f"Starting {len(tasks)} concurrent tasks for stress test...")
        results = await asyncio.gather(*(t[2] for t in tasks), return_exceptions=True)
        
        passed = 0
        for i, (name, target, _) in enumerate(tasks):
            res = results[i]
            if isinstance(res, Exception):
                _log.error(f"[{name}] {target} -> EXCEPTION: {res}")
            else:
                ok, msg, dt = res
                if ok:
                    passed += 1
                    _log.info(f"[{name}] {target} -> OK ({dt:.2f}s): {msg}")
                else:
                    _log.error(f"[{name}] {target} -> FAIL ({dt:.2f}s): {msg}")
                    
        _log.info(f"Stress test complete: {passed}/{len(tasks)} passed.")
        await self.teardown()

async def main():
    cfg = yaml.safe_load(Path("config/config.yml").read_text(encoding="utf-8"))
    tester = StressTester(cfg)
    await tester.run_all()

if __name__ == "__main__":
    asyncio.run(main())
