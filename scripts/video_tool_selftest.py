from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _bootstrap_path() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_path()

from core.config_manager import ConfigManager  # noqa: E402
from core.image import ImageEngine  # noqa: E402
from core.search import SearchEngine  # noqa: E402
from core.tools import ToolExecutor  # noqa: E402


def _load_runtime_config(root: Path) -> dict[str, Any]:
    cm = ConfigManager(root / "config", root / "storage")
    cfg = cm.raw if isinstance(cm.raw, dict) else {}
    return dict(cfg)


async def _noop_plugin_runner(_name: str, _query: str, _payload: dict[str, Any]) -> str:
    return ""


def _build_tool_executor(config: dict[str, Any]) -> ToolExecutor:
    search = SearchEngine(config.get("search", {}) if isinstance(config, dict) else {})
    image = ImageEngine(config.get("image", {}) if isinstance(config, dict) else {}, model_client=None)
    return ToolExecutor(
        search_engine=search,
        image_engine=image,
        plugin_runner=_noop_plugin_runner,
        config=config,
    )


def _print_header(tools: ToolExecutor) -> None:
    print("== YuKiKo Video Tool Selftest ==")
    print(f"video_resolver_enable={bool(getattr(tools, '_video_resolver_enable', False))}")
    print(f"ffmpeg_available={bool(getattr(tools, '_ffmpeg_available', False))}")
    print(f"video_parse_api_enable={bool(getattr(tools, '_video_parse_enable', False))}")
    print(f"hybrid_resolver_enable={bool(getattr(tools, '_hybrid_resolver', None))}")


def _print_url_check(tools: ToolExecutor, label: str, url: str) -> bool:
    target = str(url or "").strip()
    if not target:
        print(f"{label}.url=skip(empty)")
        return True
    supported = bool(tools._is_supported_platform_video_url(target))
    detail = bool(tools._is_platform_video_detail_url(target)) if supported else False
    print(f"{label}.url={target}")
    print(f"{label}.supported={supported}")
    print(f"{label}.detail={detail}")
    return supported and detail


async def _run_parse_test(
    tools: ToolExecutor,
    *,
    label: str,
    url: str,
    print_payload: bool,
) -> bool:
    result = await tools._method_browser_resolve_video(
        "browser.resolve_video",
        {"url": url},
        url,
    )
    print(f"{label}.parse.ok={result.ok}")
    if result.error:
        print(f"{label}.parse.error={result.error}")
    if print_payload:
        print(f"{label}.parse.payload={json.dumps(result.payload, ensure_ascii=False)}")
    return bool(result.ok)


async def _run_analyze_test(
    tools: ToolExecutor,
    *,
    label: str,
    url: str,
    depth: str,
    print_payload: bool,
) -> bool:
    result = await tools._method_video_analyze(
        method_name="video.analyze",
        method_args={"url": url, "depth": depth},
        query=f"分析这个视频 {url}",
        message_text="",
    )
    print(f"{label}.analyze.ok={result.ok}")
    if result.error:
        print(f"{label}.analyze.error={result.error}")
    if print_payload:
        print(f"{label}.analyze.payload={json.dumps(result.payload, ensure_ascii=False)}")
    return bool(result.ok)


async def _run(ns: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    config = _load_runtime_config(root)
    tools = _build_tool_executor(config)
    _print_header(tools)

    targets = [
        ("bilibili", str(ns.bilibili_url or "").strip()),
        ("acfun", str(ns.acfun_url or "").strip()),
    ]

    all_ok = True
    for label, url in targets:
        if not url:
            continue
        detail_ok = _print_url_check(tools, label, url)
        if not detail_ok:
            all_ok = False
            continue

        if ns.run_parse:
            ok = await _run_parse_test(
                tools,
                label=label,
                url=url,
                print_payload=bool(ns.print_payload),
            )
            all_ok = all_ok and ok

        if ns.run_analyze:
            ok = await _run_analyze_test(
                tools,
                label=label,
                url=url,
                depth=ns.depth,
                print_payload=bool(ns.print_payload),
            )
            all_ok = all_ok and ok

    if not any(str(item[1]).strip() for item in targets):
        print("未提供 URL，仅输出本地能力状态。")
        return 0
    return 0 if all_ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo 视频工具链自检（B站/AcFun）")
    parser.add_argument("--bilibili-url", default="", help="可选：B站视频链接")
    parser.add_argument("--acfun-url", default="", help="可选：AcFun 视频链接")
    parser.add_argument("--run-parse", action="store_true", help="执行 parse_video 链路实测")
    parser.add_argument("--run-analyze", action="store_true", help="执行 analyze_video 链路实测")
    parser.add_argument("--depth", default="auto", choices=["auto", "rich_metadata", "multimodal"], help="analyze_video depth 参数")
    parser.add_argument("--print-payload", action="store_true", help="打印工具返回 payload")
    ns = parser.parse_args()
    return asyncio.run(_run(ns))


if __name__ == "__main__":
    raise SystemExit(main())
