#!/usr/bin/env python3
"""Windows Cookie 一键获取工具

支持从 Chrome/Edge/Firefox 浏览器提取 B站/抖音/QQ空间等网站的 Cookie，
并直接导出成可复制到 VPS 的 YuKiKo 配置片段。

使用多种策略确保兼容性：
1. rookiepy (需管理员权限，支持 Chrome v130+ App-Bound Encryption)
2. Chrome DevTools Protocol (无需管理员，需关闭浏览器后重开)
3. browser_cookie3 (仅 Firefox 可靠)

使用方法:
    python scripts/get_cookies_windows.py --site bilibili
    python scripts/get_cookies_windows.py --site douyin --browser chrome
    python scripts/get_cookies_windows.py --site q
    python scripts/get_cookies_windows.py --all
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.cookie_auth import (
    bilibili_qr_login,
    extract_bilibili_cookies,
    extract_browser_cookies_with_source,
    extract_douyin_cookie,
    extract_qzone_cookies,
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
    "qzone": [".qq.com", ".i.qq.com", ".qzone.qq.com"],
    "kuaishou": [".kuaishou.com", "kuaishou.com"],
    "zhihu": [".zhihu.com", "zhihu.com"],
    "weibo": [".weibo.com", "weibo.com"],
}
SUPPORTED_BROWSERS = ("edge", "chrome", "firefox")
SITE_ALIASES = {
    "b": "bilibili",
    "bili": "bilibili",
    "bilibili": "bilibili",
    "d": "douyin",
    "dy": "douyin",
    "douyin": "douyin",
    "k": "kuaishou",
    "ks": "kuaishou",
    "kuaishou": "kuaishou",
    "q": "qzone",
    "qq": "qzone",
    "qqzone": "qzone",
    "qzone": "qzone",
    "zhihu": "zhihu",
    "weibo": "weibo",
}
YUKIKO_IMPORT_FILE = "yukiko_cookie_import.yml"


def normalize_site_name(raw: str) -> str:
    """支持更短的站点别名，如 q -> qzone。"""
    site = str(raw or "").strip().lower()
    return SITE_ALIASES.get(site, site)


def cookie_dict_to_string(cookies: dict[str, Any]) -> str:
    """把 {k:v} 形式转成 k=v; k2=v2 形式，方便 VPS 直接粘贴。"""
    parts: list[str] = []
    for key, value in cookies.items():
        k = str(key or "").strip()
        v = str(value or "").strip()
        if not k or not v:
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def cookie_string_to_dict(cookie: str) -> dict[str, str]:
    """把 cookie 字符串还原成 dict，便于保存 raw json。"""
    out: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        k = key.strip()
        v = value.strip()
        if k and v:
            out[k] = v
    return out


def normalize_cookie_payload(site: str, payload: dict[str, Any] | str) -> tuple[dict[str, str], str]:
    """统一成 dict + cookie 字符串，方便同时输出 JSON/TXT/YAML。"""
    if isinstance(payload, dict):
        cookie_dict = {
            str(k): str(v)
            for k, v in payload.items()
            if str(k or "").strip() and str(v or "").strip()
        }
        return cookie_dict, cookie_dict_to_string(cookie_dict)
    cookie_string = str(payload or "").strip()
    return cookie_string_to_dict(cookie_string), cookie_string


def build_yukiko_site_payload(site: str, cookies: dict[str, str], cookie_string: str) -> dict[str, Any]:
    """转成 YuKiKo 运行配置所需格式。"""
    if site == "bilibili":
        sessdata = str(cookies.get("SESSDATA", "") or cookies.get("sessdata", "")).strip()
        bili_jct = str(cookies.get("bili_jct", "") or cookies.get("BILI_JCT", "")).strip()
        if not sessdata:
            return {}
        payload: dict[str, Any] = {
            "enable": True,
            "sessdata": sessdata,
        }
        if bili_jct:
            payload["bili_jct"] = bili_jct
        return payload

    if site in {"douyin", "kuaishou", "qzone"}:
        final_cookie = cookie_string.strip() or cookie_dict_to_string(cookies)
        if not final_cookie:
            return {}
        return {
            "enable": True,
            "cookie": final_cookie,
        }

    return {}


def format_cookies_for_display(cookies: dict[str, str]) -> str:
    """格式化 Cookie 用于显示"""
    if not cookies:
        return "无"

    lines = []
    for key, value in cookies.items():
        display_value = value[:40] + "..." if len(value) > 40 else value
        lines.append(f"  {key}: {display_value}")
    return "\n".join(lines)


def save_cookies_to_file(site: str, cookies: dict[str, str], output_dir: Path) -> Path:
    """保存 raw Cookie JSON 到文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{site}_cookies.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    _log.info(f"✓ {site} Cookie 已保存到: {output_file}")
    return output_file


def save_cookie_string_file(site: str, cookie_string: str, output_dir: Path) -> Path | None:
    """保存整串 cookie，适合 VPS 直接粘贴。"""
    final_cookie = str(cookie_string or "").strip()
    if not final_cookie:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{site}_cookie.txt"
    output_file.write_text(final_cookie, encoding="utf-8")
    _log.info("✓ %s Cookie 串已保存到: %s", site, output_file)
    return output_file


def save_yukiko_site_snippet(site: str, payload: dict[str, Any], output_dir: Path) -> Path | None:
    """按单站点导出 YuKiKo 配置片段。"""
    if not payload:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{site}_for_yukiko.yml"
    content = {
        "video_analysis": {
            site: payload,
        }
    }
    output_file.write_text(
        yaml.safe_dump(content, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _log.info("✓ %s YuKiKo 配置片段已保存到: %s", site, output_file)
    return output_file


def save_yukiko_import_config(export_payload: dict[str, Any], output_dir: Path) -> Path | None:
    """导出聚合后的 YuKiKo 配置文件，可直接传到 VPS 参考/合并。"""
    if not export_payload.get("video_analysis"):
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / YUKIKO_IMPORT_FILE
    output_file.write_text(
        yaml.safe_dump(export_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _log.info("✓ YuKiKo 导入配置已保存到: %s", output_file)
    return output_file


async def get_cookies_for_site(
    site: str,
    browser: str | None = None,
    use_qr: bool = False,
    auto_close: bool = False,
) -> dict[str, str] | str:
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

    if site == "bilibili":
        browser_candidates = _pick_browser_candidates(browser)
        for browser_name in browser_candidates:
            cookies = extract_bilibili_cookies(browser_name, auto_close=auto_close)
            if cookies and cookies.get("sessdata"):
                _log.info("✓ 成功从 %s 提取 bilibili Cookie", browser_name)
                return cookies
        _log.warning("未能从浏览器提取 bilibili Cookie，已尝试: %s", ", ".join(browser_candidates))
        return {}

    if site == "douyin":
        browser_candidates = _pick_browser_candidates(browser)
        for browser_name in browser_candidates:
            cookie = extract_douyin_cookie(browser_name, auto_close=auto_close)
            if cookie:
                _log.info("✓ 成功从 %s 提取 douyin Cookie", browser_name)
                return cookie
        _log.warning("未能从浏览器提取 douyin Cookie，已尝试: %s", ", ".join(browser_candidates))
        return ""

    if site == "qzone":
        browser_candidates = _pick_browser_candidates(browser)
        for browser_name in browser_candidates:
            cookie = extract_qzone_cookies(browser_name, auto_close=auto_close)
            if cookie:
                _log.info("✓ 成功从 %s 提取 qzone Cookie", browser_name)
                return cookie
        _log.warning("未能从浏览器提取 qzone Cookie，已尝试: %s", ", ".join(browser_candidates))
        return ""

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
        help="指定要获取 Cookie 的网站/别名，如 bilibili / douyin / qzone / q",
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

    if args.site:
        args.site = normalize_site_name(args.site)
        if args.site not in SUPPORTED_SITES:
            parser.error(
                f"不支持的网站: {args.site}，可选: {', '.join(sorted(SUPPORTED_SITES))}，别名 q= qzone"
            )

    sites_to_process = list(SUPPORTED_SITES.keys()) if args.all else [args.site]

    results: dict[str, tuple[dict[str, str], str]] = {}
    yukiko_export: dict[str, Any] = {"video_analysis": {}}
    for site in sites_to_process:
        raw_payload = await get_cookies_for_site(
            site=site,
            browser=args.browser,
            use_qr=(args.qr and site == "bilibili"),
            auto_close=args.auto_close,
        )
        cookie_dict, cookie_string = normalize_cookie_payload(site, raw_payload)
        results[site] = (cookie_dict, cookie_string)
        site_payload = build_yukiko_site_payload(site, cookie_dict, cookie_string)
        if site_payload:
            yukiko_export["video_analysis"][site] = site_payload

        if cookie_dict or cookie_string:
            print(f"\n【{site} Cookie】")
            print(format_cookies_for_display(cookie_dict))

            if not args.display_only:
                save_cookies_to_file(site, cookie_dict, args.output)
                save_cookie_string_file(site, cookie_string, args.output)
                save_yukiko_site_snippet(site, site_payload, args.output)
        else:
            print(f"\n【{site}】未获取到 Cookie")

        print()

    if not args.display_only:
        save_yukiko_import_config(yukiko_export, args.output)

    # 总结
    success_count = sum(
        1
        for cookie_dict, cookie_string in results.values()
        if cookie_dict or cookie_string
    )
    total_count = len(results)

    print("=" * 60)
    print(f"完成！成功获取 {success_count}/{total_count} 个网站的 Cookie")

    if not args.display_only and success_count > 0:
        print(f"Cookie 已保存到: {args.output.absolute()}")
        print(
            f"可直接带到 VPS 的 YuKiKo 配置文件: {(args.output / YUKIKO_IMPORT_FILE).absolute()}"
        )

    print("\n提示:")
    print("  - 如果提取失败，请确保浏览器已登录目标网站")
    print("  - Chrome/Edge 可能需要管理员权限或关闭浏览器")
    print("  - B站可以使用 --qr 参数进行扫码登录")
    print("  - q / qq / qqzone 都等价于 qzone")
    print("=" * 60)


def main() -> None:
    import asyncio
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
