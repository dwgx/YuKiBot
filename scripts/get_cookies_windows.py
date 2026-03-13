#!/usr/bin/env python3
"""Windows Cookie 一键获取工具

支持从 Chrome/Edge/Firefox 浏览器提取 B站/抖音/快手等网站的 Cookie。
使用多种策略确保兼容性：
1. rookiepy (需管理员权限，支持 Chrome v130+ App-Bound Encryption)
2. Chrome DevTools Protocol (无需管理员，需关闭浏览器后重开)
3. browser_cookie3 (仅 Firefox 可靠)

使用方法:
    python scripts/get_cookies_windows.py --site bilibili
    python scripts/get_cookies_windows.py --site douyin --browser chrome
    python scripts/get_cookies_windows.py --all
"""
import argparse
import json
import logging
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.cookie_auth import (
    bilibili_qr_login,
    extract_browser_cookies_with_source,
    get_cookie_runtime_capabilities,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger(__name__)


SUPPORTED_SITES = {
    "bilibili": [".bilibili.com", "bilibili.com"],
    "douyin": [".douyin.com", "douyin.com"],
    "kuaishou": [".kuaishou.com", "kuaishou.com"],
    "zhihu": [".zhihu.com", "zhihu.com"],
    "weibo": [".weibo.com", "weibo.com"],
}
SUPPORTED_BROWSERS = ("edge", "chrome", "firefox")


def format_cookies_for_display(cookies: dict[str, str]) -> str:
    """格式化 Cookie 用于显示"""
    if not cookies:
        return "无"

    lines = []
    for key, value in cookies.items():
        display_value = value[:40] + "..." if len(value) > 40 else value
        lines.append(f"  {key}: {display_value}")
    return "\n".join(lines)


def save_cookies_to_file(site: str, cookies: dict[str, str], output_dir: Path) -> None:
    """保存 Cookie 到文件"""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{site}_cookies.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    _log.info(f"✓ {site} Cookie 已保存到: {output_file}")


async def get_cookies_for_site(
    site: str,
    browser: str | None = None,
    use_qr: bool = False,
    auto_close: bool = False,
) -> dict[str, str]:
    """获取指定网站的 Cookie"""
    _log.info(f"正在获取 {site} 的 Cookie...")

    # B站支持扫码登录
    if site == "bilibili" and use_qr:
        _log.info("使用 B站扫码登录...")
        try:
            cookies = await bilibili_qr_login()
            if cookies:
                _log.info(f"✓ B站扫码登录成功，获取到 {len(cookies)} 个 Cookie")
                return cookies
            _log.warning("B站扫码登录失败")
            return {}
        except Exception as e:
            _log.error(f"B站扫码登录异常: {e}")
            return {}

    # 从浏览器提取 Cookie
    domains = SUPPORTED_SITES.get(site, [site])
    browser_candidates = _pick_browser_candidates(browser)
    try:
        for browser_name in browser_candidates:
            cookies = _extract_site_cookies(
                site=site,
                domains=domains,
                browser=browser_name,
                auto_close=auto_close,
            )
            if cookies:
                _log.info(
                    f"✓ 成功从 {browser_name} 提取 {site} Cookie，共 {len(cookies)} 个"
                )
                return cookies

        _log.warning(
            "未能从浏览器提取 %s Cookie，已尝试: %s",
            site,
            ", ".join(browser_candidates),
        )
        return {}
    except Exception as e:
        _log.error(f"提取 {site} Cookie 失败: {e}")
        return {}


def _pick_browser_candidates(preferred: str | None) -> list[str]:
    if preferred:
        return [preferred]

    caps = get_cookie_runtime_capabilities()
    browser_info = caps.get("browsers", {}) if isinstance(caps, dict) else {}
    recommended = str(browser_info.get("recommended", "") or "").strip().lower()
    installed = browser_info.get("installed", []) if isinstance(browser_info, dict) else []

    candidates: list[str] = []
    for name in [recommended, *installed, *SUPPORTED_BROWSERS]:
        normalized = str(name or "").strip().lower()
        if normalized in SUPPORTED_BROWSERS and normalized not in candidates:
            candidates.append(normalized)
    return candidates or list(SUPPORTED_BROWSERS)


def _extract_site_cookies(
    *,
    site: str,
    domains: list[str],
    browser: str,
    auto_close: bool,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    domain_sources: list[str] = []

    for domain in domains:
        cookies, source = extract_browser_cookies_with_source(
            browser=browser,
            domain=domain,
            auto_close=auto_close,
        )
        if cookies:
            merged.update(cookies)
        domain_sources.append(f"{domain}:{source}")

    _log.info(
        "浏览器提取结果 | site=%s | browser=%s | domains=%s | cookies=%d",
        site,
        browser,
        ", ".join(domain_sources),
        len(merged),
    )
    return merged


async def main_async() -> None:
    parser = argparse.ArgumentParser(
        description="Windows Cookie 一键获取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--site",
        choices=list(SUPPORTED_SITES.keys()),
        help="指定要获取 Cookie 的网站",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="获取所有支持网站的 Cookie",
    )
    parser.add_argument(
        "--browser",
        choices=["chrome", "edge", "firefox"],
        help="指定浏览器（默认自动检测）",
    )
    parser.add_argument(
        "--qr",
        action="store_true",
        help="使用扫码登录（仅 B站支持）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/cookies"),
        help="Cookie 保存目录（默认: data/cookies）",
    )
    parser.add_argument(
        "--display-only",
        action="store_true",
        help="仅显示 Cookie，不保存到文件",
    )
    parser.add_argument(
        "--auto-close",
        action="store_true",
        help="必要时关闭浏览器再提取 Cookie",
    )

    args = parser.parse_args()

    if not args.site and not args.all:
        parser.error("请指定 --site 或 --all")

    sites_to_process = list(SUPPORTED_SITES.keys()) if args.all else [args.site]

    results = {}
    for site in sites_to_process:
        cookies = await get_cookies_for_site(
            site=site,
            browser=args.browser,
            use_qr=(args.qr and site == "bilibili"),
            auto_close=args.auto_close,
        )
        results[site] = cookies

        if cookies:
            print(f"\n【{site} Cookie】")
            print(format_cookies_for_display(cookies))

            if not args.display_only:
                save_cookies_to_file(site, cookies, args.output)
        else:
            print(f"\n【{site}】未获取到 Cookie")

        print()

    # 总结
    success_count = sum(1 for cookies in results.values() if cookies)
    total_count = len(results)

    print("=" * 60)
    print(f"完成！成功获取 {success_count}/{total_count} 个网站的 Cookie")

    if not args.display_only and success_count > 0:
        print(f"Cookie 已保存到: {args.output.absolute()}")

    print("\n提示:")
    print("  - 如果提取失败，请确保浏览器已登录目标网站")
    print("  - Chrome/Edge 可能需要管理员权限或关闭浏览器")
    print("  - B站可以使用 --qr 参数进行扫码登录")
    print("=" * 60)


def main() -> None:
    import asyncio
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
