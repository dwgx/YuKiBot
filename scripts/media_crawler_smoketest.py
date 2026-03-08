from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.crawlers import CrawlerHub
from core.image import ImageEngine
from core.search import SearchEngine
from core.tools import ToolExecutor
from utils.text import clip_text, normalize_text


class _DummyModelClient:
    enabled = False


async def _dummy_plugin_runner(_name: str, _tool_name: str, _args: dict[str, Any]) -> str:
    return ""


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = False


def _safe_text(text: str) -> str:
    value = str(text)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(enc, "ignore").decode(enc, "ignore")


async def _pick_douyin_candidate(search: SearchEngine) -> str:
    queries = [
        "site:douyin.com/video 抖音",
        "site:v.douyin.com 抖音 分享",
        "site:douyin.com/note 抖音 图文",
    ]
    for query in queries:
        try:
            rows = await search.search(query)
        except Exception:
            continue
        for row in rows:
            url = normalize_text(str(getattr(row, "url", "")))
            if "douyin.com" in url:
                return url
    return ""


async def run(args: argparse.Namespace) -> int:
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        print("配置文件格式错误")
        return 2

    search = SearchEngine(cfg.get("search", {}))
    image = ImageEngine(cfg.get("image", {}), _DummyModelClient())
    tools = ToolExecutor(
        search_engine=search,
        image_engine=image,
        plugin_runner=_dummy_plugin_runner,
        config=cfg,
    )
    crawler = CrawlerHub(cfg)

    checks: list[Check] = []
    try:
        bili_url = normalize_text(str(args.bili_url))
        parse_bili = await tools._method_browser_resolve_video("parse_video", {"url": bili_url}, bili_url)
        checks.append(
            Check(
                name="video.parse.bilibili",
                ok=bool(parse_bili.ok and normalize_text(str((parse_bili.payload or {}).get("video_url", "")))),
                detail=clip_text(str((parse_bili.payload or {}).get("text", "") or parse_bili.error), 160),
                critical=True,
            )
        )

        if not args.skip_analyze:
            analyze_bili = await tools._method_video_analyze(
                "analyze_video",
                {"url": bili_url},
                bili_url,
                bili_url,
            )
            checks.append(
                Check(
                    name="video.analyze.bilibili",
                    ok=bool(analyze_bili.ok and normalize_text(str((analyze_bili.payload or {}).get("text", "")))),
                    detail=clip_text(str((analyze_bili.payload or {}).get("text", "") or analyze_bili.error), 160),
                    critical=True,
                )
            )

        douyin_url = normalize_text(str(args.douyin_url))
        if not douyin_url:
            douyin_url = await _pick_douyin_candidate(search)
        if douyin_url:
            parse_dy = await tools._method_browser_resolve_video("parse_video", {"url": douyin_url}, douyin_url)
            checks.append(
                Check(
                    name="video.parse.douyin",
                    ok=bool(parse_dy.ok and normalize_text(str((parse_dy.payload or {}).get("video_url", "")))),
                    detail=clip_text(str((parse_dy.payload or {}).get("text", "") or parse_dy.error), 180),
                    critical=True,
                )
            )
        else:
            checks.append(Check(name="video.parse.douyin", ok=False, detail="未找到可测试的抖音链接", critical=True))

        trends = await crawler.get_trends_cached(max_age=0)
        trend_counts = {k: len(v or []) for k, v in trends.items()}
        non_empty = sum(1 for v in trend_counts.values() if v > 0)
        checks.append(
            Check(
                name="crawler.trends.aggregate",
                ok=non_empty >= 2,
                detail=", ".join(f"{k}:{v}" for k, v in trend_counts.items()),
                critical=True,
            )
        )

        zhihu_rows = await crawler.zhihu.hot_list(limit=8)
        checks.append(
            Check(
                name="crawler.zhihu.hot",
                ok=len(zhihu_rows) > 0,
                detail=f"count={len(zhihu_rows)}",
                critical=False,
            )
        )

        wiki_rows = await crawler.wiki.lookup("周杰伦")
        checks.append(
            Check(
                name="crawler.wiki.lookup",
                ok=len(wiki_rows) > 0,
                detail=f"count={len(wiki_rows)}",
                critical=False,
            )
        )
    finally:
        await crawler.close()

    passed = [c for c in checks if c.ok]
    failed = [c for c in checks if not c.ok]
    critical_failed = [c for c in checks if (not c.ok and c.critical)]

    print("== Media + Crawler Smoke Test ==")
    print(f"total={len(checks)} pass={len(passed)} fail={len(failed)} critical_fail={len(critical_failed)}")
    for item in checks:
        mark = "PASS" if item.ok else ("FAIL" if item.critical else "WARN")
        detail = f" | {_safe_text(item.detail)}" if item.detail else ""
        print(f"[{mark}] {item.name}{detail}")

    return 0 if not critical_failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo media/crawler smoke test")
    parser.add_argument("--config", default="config/config.yml", help="配置文件路径")
    parser.add_argument("--bili-url", default="https://www.bilibili.com/video/BV1gy4y1B7aK/", help="B站测试链接")
    parser.add_argument("--douyin-url", default="", help="抖音测试链接（可选，不传则自动搜索）")
    parser.add_argument("--skip-analyze", action="store_true", help="跳过 analyze_video 深度分析")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
