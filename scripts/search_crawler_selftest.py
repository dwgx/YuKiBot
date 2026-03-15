from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.crawlers import CrawlerHub
from core.search import SearchEngine
from core.tools import ToolExecutor
from utils.text import normalize_text


class _DummyImageEngine:
    model_client = None

    async def generate(self, prompt: str, size: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(ok=False, url="", message="disabled")


async def _dummy_plugin_runner(_name: str, _tool_name: str, _args: dict[str, Any]) -> str:
    return ""


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


def _load_config(path: str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


async def run(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    raw_search_cfg = cfg.get("search", {}) if isinstance(cfg.get("search", {}), dict) else {}
    search_cfg = {
        "enable": True,
        "max_results": 8,
        "max_image_results": 4,
        "timeout_seconds": 18,
    }
    search_cfg.update(raw_search_cfg)

    engine = SearchEngine(search_cfg)
    tool = ToolExecutor(
        search_engine=engine,
        image_engine=_DummyImageEngine(),  # type: ignore[arg-type]
        plugin_runner=_dummy_plugin_runner,
        config=cfg,
    )

    checks: list[CheckResult] = []

    rows = await engine.search(args.query)
    checks.append(
        CheckResult(
            name="search_engine.text",
            ok=len(rows) > 0,
            detail=f"query={args.query} count={len(rows)}",
        )
    )

    tool_result = await tool.execute(
        action="search",
        tool_name="search",
        tool_args={"query": args.query},
        message_text=args.query,
        conversation_id="selftest:search",
        user_id="0",
        user_name="selftest",
        group_id=0,
        api_call=None,
        raw_segments=[],
    )
    payload_rows = (tool_result.payload or {}).get("results") if isinstance(tool_result.payload, dict) else []
    payload_rows = payload_rows if isinstance(payload_rows, list) else []
    checks.append(
        CheckResult(
            name="tool_executor.search",
            ok=bool(tool_result.ok and payload_rows),
            detail=f"ok={tool_result.ok} count={len(payload_rows)} error={tool_result.error}",
        )
    )

    fetch_url = normalize_text(args.fetch_url)
    if not fetch_url and payload_rows:
        fetch_url = normalize_text(str(payload_rows[0].get("url", "")))
    if not fetch_url and rows:
        fetch_url = normalize_text(rows[0].url)
    if fetch_url:
        page = await tool._fetch_webpage_summary(fetch_url)
        checks.append(
            CheckResult(
                name="tool_executor.fetch_webpage",
                ok=bool(page and int(page.get("status_code", 0) or 0) > 0),
                detail=f"url={fetch_url} status={int(page.get('status_code', 0) or 0) if page else 0}",
                critical=False,
            )
        )

    if args.run_crawler:
        hub = CrawlerHub(cfg)
        try:
            trends = await hub.get_trends_cached(max_age=0)
            counts = {key: len(value or []) for key, value in trends.items()}
            non_empty = sum(1 for value in counts.values() if value > 0)
            checks.append(
                CheckResult(
                    name="crawler.trends.aggregate",
                    ok=non_empty >= 2,
                    detail=",".join(f"{key}:{value}" for key, value in counts.items()),
                    critical=False,
                )
            )
        finally:
            await hub.close()

    passed = [item for item in checks if item.ok]
    failed_critical = [item for item in checks if (not item.ok and item.critical)]

    print("== Search + Crawler Selftest ==")
    print(f"total={len(checks)} pass={len(passed)} critical_fail={len(failed_critical)}")
    for item in checks:
        mark = "PASS" if item.ok else ("FAIL" if item.critical else "WARN")
        detail = f" | {item.detail}" if item.detail else ""
        print(f"[{mark}] {item.name}{detail}")

    if rows:
        print("\nTop search results:")
        for idx, row in enumerate(rows[:5], start=1):
            print(f"{idx}. {row.title[:80]}")
            print(f"   {row.url}")

    if payload_rows:
        print("\nTop tool results:")
        for idx, row in enumerate(payload_rows[:5], start=1):
            title = normalize_text(str(row.get("title", ""))) or f"result-{idx}"
            url = normalize_text(str(row.get("url", "")))
            print(f"{idx}. {title[:80]}")
            print(f"   {url}")

    return 0 if not failed_critical else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo search/crawler selftest")
    parser.add_argument("--config", default="config/config.yml", help="配置文件路径")
    parser.add_argument("--query", default="OpenAI Responses API docs", help="搜索关键词")
    parser.add_argument("--fetch-url", default="", help="指定网页抓取链接（可选）")
    parser.add_argument("--run-crawler", action="store_true", help="额外执行热榜聚合抓取检查")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
