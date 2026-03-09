from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _bootstrap_path() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_path()

from core.cookie_auth import (  # noqa: E402
    extract_bilibili_cookies,
    extract_douyin_cookie,
    extract_kuaishou_cookie,
    is_browser_running,
)
from core.image import ImageEngine  # noqa: E402
from core.search import SearchEngine  # noqa: E402
from core.tools import ToolExecutor  # noqa: E402


def _print_header(browser: str, force_close: bool, url: str) -> None:
    has_rookiepy = True
    has_browser_cookie3 = True
    try:
        import rookiepy  # noqa: F401
    except Exception:
        has_rookiepy = False
    try:
        import browser_cookie3  # noqa: F401
    except Exception:
        has_browser_cookie3 = False

    print("== YuKiKo Cookie Selftest ==")
    print(f"browser={browser}")
    print(f"force_close={force_close}")
    print(f"verify_url={url or '(skip)'}")
    print(f"rookiepy_installed={has_rookiepy}")
    print(f"browser_cookie3_installed={has_browser_cookie3}")
    print(f"browser_running_before={is_browser_running(browser)}")


async def _verify_douyin_with_tool(url: str, cookie: str) -> tuple[bool, str]:
    if not url.strip():
        return True, "skip"
    config = {
        "video_resolver": {
            "enable": True,
            "download_timeout_seconds": 28,
            "resolve_total_timeout_seconds": 40,
            "download_max_mb": 64,
        },
        "video_analysis": {
            "douyin": {"enable": True, "cookie": cookie},
            "kuaishou": {"enable": True, "cookie": ""},
            "bilibili": {"enable": True, "sessdata": "", "bili_jct": ""},
        },
        "tool_interface": {
            "enable": True,
            "browser_enable": True,
            "auto_method_enable": True,
            "github_enable": True,
        },
    }
    search = SearchEngine({"enable": True})
    image = ImageEngine({"enable": False}, model_client=None)
    tools = ToolExecutor(
        search_engine=search,
        image_engine=image,
        plugin_runner=lambda *_a, **_k: None,
        config=config,
    )
    result = await tools._method_browser_resolve_video(
        "browser.resolve_video",
        {"url": url},
        "",
    )
    if result.ok:
        video_url = str(result.payload.get("video_url", "") or "")
        return True, f"ok video_url={video_url[:120]}"
    diag = str(result.payload.get("diagnostic", "") or "")
    return False, f"error={result.error} diagnostic={diag[:200]}"


async def _run(args: argparse.Namespace) -> int:
    _print_header(args.browser, args.force_close, args.verify_url)

    bili = extract_bilibili_cookies(args.browser, auto_close=args.force_close)
    dy = extract_douyin_cookie(args.browser, auto_close=args.force_close)
    ks = extract_kuaishou_cookie(args.browser, auto_close=args.force_close)

    print(f"browser_running_after_extract={is_browser_running(args.browser)}")
    print(f"bilibili.sessdata={bool(bili.get('sessdata'))}")
    print(f"bilibili.bili_jct={bool(bili.get('bili_jct'))}")
    print(f"douyin.cookie_len={len(dy)}")
    print(f"kuaishou.cookie_len={len(ks)}")

    if args.verify_url:
        ok, info = await _verify_douyin_with_tool(args.verify_url, dy)
        print(f"douyin.resolve={ok} {info}")
        if not ok:
            return 2

    # 提取完全失败时返回非 0
    if not bili.get("sessdata") and not dy and not ks:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo Cookie 提取与抖音解析本地自检")
    parser.add_argument("--browser", default="edge", help="浏览器: edge/chrome/brave/firefox")
    parser.add_argument(
        "--force-close",
        action="store_true",
        help="检测到浏览器运行时自动关闭后继续提取",
    )
    parser.add_argument(
        "--verify-url",
        default="",
        help="可选：抖音视频链接，附加执行一次 resolve_video 实测",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
