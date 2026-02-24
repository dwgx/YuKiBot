"""Cookie 自动获取模块 — B站扫码登录 + 浏览器 Cookie 提取。

B站: 使用 bilibili-api-python 的 QrCodeLogin，终端显示二维码扫码
抖音/快手: 多策略提取浏览器 cookie:
  1. rookiepy (需管理员权限，支持 Chrome v130+ App-Bound Encryption)
  2. Chrome DevTools Protocol (无需管理员，需关闭浏览器后重开)
  3. browser_cookie3 (仅 Firefox 可靠，Chrome/Edge 已失效)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("yukiko.cookie_auth")


def _print_compact_qr(data: str) -> None:
    """用 Unicode 半块字符渲染紧凑二维码（高度减半）。

    ▀ = 上黑下白, ▄ = 上白下黑, █ = 全黑, ' ' = 全白
    两行像素合并成一行显示。
    """
    try:
        import qrcode
    except ImportError:
        print(f"  扫码链接: {data}")
        return

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.modules

    rows = len(matrix)
    lines = []
    for y in range(0, rows, 2):
        line = []
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < rows else False
            if top and bot:
                line.append("█")
            elif top and not bot:
                line.append("▀")
            elif not top and bot:
                line.append("▄")
            else:
                line.append(" ")
        lines.append("  " + "".join(line))

    print("\n".join(lines))
    print()


# ═══════════════════════════════════════════════════════════
#  B站扫码登录
# ═══════════════════════════════════════════════════════════

async def bilibili_qr_login() -> dict[str, str] | None:
    """B站二维码扫码登录，返回 {sessdata, bili_jct, dedeuserid} 或 None。"""
    try:
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents, QrCodeLoginChannel
    except ImportError:
        print("  [错误] bilibili-api-python 未安装，无法扫码登录。")
        print("  pip install bilibili-api-python")
        return None

    qr = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()

    url = getattr(qr, "_QrCodeLogin__qr_link", "")
    if url:
        print("\n请用 B站 APP 扫描下方二维码登录:\n")
        _print_compact_qr(url)
    else:
        try:
            terminal_qr = qr.get_qrcode_terminal()
            print("\n请用 B站 APP 扫描下方二维码登录:\n")
            print(terminal_qr)
        except Exception:
            print("\n无法获取二维码，请检查网络连接。")
            return None

    print("等待扫码...")

    timeout = 120
    elapsed = 0
    interval = 2

    while elapsed < timeout:
        try:
            state = await qr.check_state()
        except Exception as exc:
            _log.debug("check_state error: %s", exc)
            await asyncio.sleep(interval)
            elapsed += interval
            continue

        if state == QrCodeLoginEvents.DONE:
            cred = qr.get_credential()
            print("  B站登录成功!")
            return {
                "sessdata": cred.sessdata or "",
                "bili_jct": cred.bili_jct or "",
                "dedeuserid": getattr(cred, "dedeuserid", "") or "",
            }
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("  二维码已过期。")
            return None
        elif state == QrCodeLoginEvents.CONF:
            print("  已扫码，请在手机上确认...")

        await asyncio.sleep(interval)
        elapsed += interval

    print("  等待超时。")
    return None


def bilibili_qr_login_sync() -> dict[str, str] | None:
    """同步包装，供 setup 向导调用。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, bilibili_qr_login())
                return future.result(timeout=180)
        return loop.run_until_complete(bilibili_qr_login())
    except RuntimeError:
        return asyncio.run(bilibili_qr_login())


# ═══════════════════════════════════════════════════════════
#  浏览器 Cookie 提取（多策略）
# ═══════════════════════════════════════════════════════════

_BROWSER_DISPLAY = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "brave": "Brave",
}

_BROWSER_PROCESS_NAMES = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "brave": "brave.exe",
    "firefox": "firefox.exe",
    "opera": "opera.exe",
}

# Windows 下浏览器可执行文件路径
_BROWSER_PATHS: dict[str, list[str]] = {
    "chrome": [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ],
    "edge": [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "brave": [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
}

# 浏览器 User Data 目录
_BROWSER_USER_DATA: dict[str, str] = {
    "chrome": os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data"),
    "edge": os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\User Data"),
    "brave": os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\User Data"),
}

_CHROMIUM_BROWSERS = {"chrome", "edge", "brave", "opera", "chromium"}


def _find_browser_exe(browser: str) -> str | None:
    """查找浏览器可执行文件。"""
    for path in _BROWSER_PATHS.get(browser, []):
        if os.path.isfile(path):
            return path
    # 尝试 shutil.which
    names = {"chrome": "chrome", "edge": "msedge", "brave": "brave"}
    found = shutil.which(names.get(browser, browser))
    return found


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _extract_via_cdp(browser: str, domain: str, auto_close: bool = False) -> dict[str, str]:
    """通过 Chrome DevTools Protocol 提取 cookie。

    启动浏览器（带 --remote-debugging-port），用 CDP 获取 cookie，然后关闭。
    这种方式不需要管理员权限，能绕过 App-Bound Encryption。
    注意：浏览器正在运行时无法使用同一 user-data-dir，需先关闭浏览器。
    """
    exe = _find_browser_exe(browser)
    if not exe:
        _log.debug("CDP: 未找到 %s 可执行文件", browser)
        return {}

    user_data = _BROWSER_USER_DATA.get(browser, "")
    if not user_data or not os.path.isdir(user_data):
        _log.debug("CDP: 未找到 %s User Data 目录", browser)
        return {}

    # 浏览器正在运行且占用 profile 时，无法用同一 user-data-dir 启动第二个实例。
    if _is_browser_running(browser):
        total, foreground = _get_browser_process_state(browser)
        if not auto_close:
            if foreground <= 0:
                print(
                    f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 后台进程占用配置目录，"
                    "请允许自动关闭后重试。"
                )
            else:
                print(f"  {_BROWSER_DISPLAY.get(browser, browser)} 正在运行，请先关闭浏览器再试。")
            return {}
        if foreground <= 0:
            print(
                f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 后台进程({total}个)，"
                "尝试自动关闭后继续提取..."
            )
        else:
            print(f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 正在运行，尝试自动关闭后继续提取...")
        if not _stop_browser_processes(browser):
            print(f"  自动关闭 {_BROWSER_DISPLAY.get(browser, browser)} 失败，请手动关闭后重试。")
            return {}
        time.sleep(1.2)
        if _is_browser_running(browser):
            print(f"  {_BROWSER_DISPLAY.get(browser, browser)} 仍在运行，请手动关闭后重试。")
            return {}

    # 找一个空闲端口
    port = 19222
    while not _is_port_free(port) and port < 19300:
        port += 1
    if port >= 19300:
        _log.debug("CDP: 无可用端口")
        return {}

    proc = None
    try:
        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--headless=new",
            "--disable-gpu",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        # 等待 CDP 端口就绪
        import httpx
        ready = False
        for _ in range(40):  # 最多等 8 秒
            time.sleep(0.2)
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
                if resp.status_code == 200:
                    ready = True
                    break
            except Exception:
                continue

        if not ready:
            _log.debug("CDP: 浏览器启动超时")
            return {}

        # 获取 cookie — 直接用 Network.getAllCookies，不需要先导航
        resp = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3)
        targets = resp.json()

        if not targets:
            _log.debug("CDP: 无可用 target")
            return {}

        ws_url = targets[0].get("webSocketDebuggerUrl", "")
        if not ws_url:
            _log.debug("CDP: 无 WebSocket URL")
            return {}

        cookies = _cdp_get_cookies(ws_url, domain)
        return cookies

    except Exception as exc:
        _log.debug("CDP 提取失败: %s", exc)
        return {}
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _is_browser_running(browser: str) -> bool:
    """检查浏览器是否处于“会占用 profile”的运行状态。"""
    total, foreground = _get_browser_process_state(browser)
    if total <= 0:
        return False

    # Chromium 系浏览器常驻后台进程较多；仅后台且未锁 profile 时不视为运行。
    if browser in _CHROMIUM_BROWSERS and foreground <= 0 and not _is_chromium_profile_locked(browser):
        return False
    return True


def _get_browser_process_state(browser: str) -> tuple[int, int]:
    """返回 (总进程数, 前台窗口进程数)。"""
    name = _BROWSER_PROCESS_NAMES.get(browser)
    if not name:
        return (0, 0)

    proc_name = name.replace(".exe", "")
    # 先用 PowerShell 拿更准确的 MainWindowHandle。
    try:
        ps_script = (
            f"$rows = Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue | "
            "Select-Object MainWindowHandle; "
            "$rows | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = (result.stdout or "").strip()
        if payload:
            rows = json.loads(payload)
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                total = len(rows)
                foreground = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        if int(row.get("MainWindowHandle", 0) or 0) != 0:
                            foreground += 1
                    except Exception:
                        continue
                return (total, foreground)
    except Exception:
        pass

    # 回退 tasklist（只计数，不区分前台）。
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        count = 0
        for line in lines:
            lower = line.lower()
            if "no tasks are running" in lower or "没有运行的任务" in lower or "info:" in lower:
                continue
            if lower.startswith(f'"{name.lower()}"'):
                count += 1
        return (count, 0)
    except Exception:
        return (0, 0)


def _is_chromium_profile_locked(browser: str) -> bool:
    """检查 Chromium User Data 是否存在锁文件。"""
    if browser not in _CHROMIUM_BROWSERS:
        return False
    user_data = _BROWSER_USER_DATA.get(browser, "")
    if not user_data:
        return False
    root = Path(user_data)
    if not root.exists():
        return False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            if (root / name).exists():
                return True
        except Exception:
            continue
    return False


def _stop_browser_processes(browser: str) -> bool:
    """尝试关闭目标浏览器进程。"""
    name = _BROWSER_PROCESS_NAMES.get(browser)
    if not name:
        return False
    if not _is_browser_running(browser):
        return True
    try:
        subprocess.run(
            ["taskkill", "/IM", name, "/F", "/T"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    for _ in range(8):
        time.sleep(0.3)
        if not _is_browser_running(browser):
            return True
    return False


def is_browser_running(browser: str) -> bool:
    """公开浏览器运行态检测（供管理命令和自检脚本使用）。"""
    return _is_browser_running(browser)


def _copy_cookie_profile(src_data_dir: str, dst_dir: str) -> None:
    """复制浏览器 profile 中的 cookie 相关文件到临时目录。"""
    # 只复制 Default profile 的 Cookies 和 Local State（解密需要）
    src = Path(src_data_dir)
    dst = Path(dst_dir)

    # Local State（包含加密密钥）
    local_state = src / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, dst / "Local State")

    # Default profile 的 Cookies
    for profile in ("Default", "Profile 1"):
        src_profile = src / profile
        if not src_profile.exists():
            continue
        dst_profile = dst / profile
        dst_profile.mkdir(parents=True, exist_ok=True)
        for fname in ("Cookies", "Cookies-journal", "Preferences", "Secure Preferences"):
            src_file = src_profile / fname
            if src_file.exists():
                try:
                    shutil.copy2(src_file, dst_profile / fname)
                except Exception:
                    pass
        break  # 只复制第一个找到的 profile


def _cdp_get_cookies(ws_url: str, domain: str) -> dict[str, str]:
    """通过 CDP WebSocket 获取指定域名的 cookie。"""
    import websockets.sync.client as ws_client

    cookies: dict[str, str] = {}
    msg_id = 1
    clean_domain = domain.lstrip(".")
    try:
        with ws_client.connect(ws_url, close_timeout=3) as ws:
            # 直接用 Network.getAllCookies 获取所有 cookie（不需要先导航）
            ws.send(json.dumps({
                "id": msg_id,
                "method": "Network.getAllCookies",
            }))

            for _ in range(10):
                raw = ws.recv(timeout=5)
                result = json.loads(raw)
                if result.get("id") == msg_id:
                    all_cookies = result.get("result", {}).get("cookies", [])
                    for c in all_cookies:
                        c_domain = c.get("domain", "").lstrip(".")
                        if clean_domain in c_domain or c_domain in clean_domain:
                            cookies[c["name"]] = c["value"]
                    break
    except ImportError:
        _log.debug("CDP: websockets 库未安装")
    except Exception as exc:
        _log.debug("CDP WebSocket 错误: %s", exc)

    return cookies


def _extract_via_rookiepy(browser: str, domain: str) -> dict[str, str]:
    """通过 rookiepy 提取 cookie（需管理员权限，支持 Chrome v130+）。"""
    try:
        import rookiepy
    except ImportError:
        return {}

    fn = getattr(rookiepy, browser, None)
    if not fn:
        return {}

    try:
        raw = fn([domain])
        return {c["name"]: c["value"] for c in raw if c.get("name")}
    except Exception as exc:
        _log.debug("rookiepy %s 失败: %s", browser, exc)
        return {}


def _extract_via_browser_cookie3(browser: str, domain: str) -> dict[str, str]:
    """通过 browser_cookie3 提取（仅 Firefox 可靠）。"""
    try:
        import browser_cookie3
    except ImportError:
        return {}

    fn = getattr(browser_cookie3, browser, None)
    if not fn:
        return {}

    try:
        cj = fn(domain_name=domain)
        return {c.name: c.value for c in cj if c.domain.endswith(domain)}
    except Exception as exc:
        _log.debug("browser_cookie3 %s 失败: %s", browser, exc)
        return {}


def extract_browser_cookies(browser: str, domain: str, auto_close: bool = False) -> dict[str, str]:
    """从指定浏览器提取指定域名的 cookie。

    策略优先级:
      1. rookiepy（需管理员，支持 Chrome v130+）
      2. CDP（无需管理员，headless 启动浏览器获取）
      3. browser_cookie3（仅 Firefox 可靠）
    """
    # 策略 1: rookiepy
    cookies = _extract_via_rookiepy(browser, domain)
    if cookies:
        _log.debug("rookiepy 提取成功: %s %s", browser, domain)
        return cookies

    # 策略 2: CDP（Chrome/Edge/Brave）
    if browser in ("chrome", "edge", "brave"):
        print(f"  正在通过 CDP 从 {_BROWSER_DISPLAY.get(browser, browser)} 提取...")
        print("  (如果浏览器正在运行，可能需要先关闭)")
        cookies = _extract_via_cdp(browser, domain, auto_close=auto_close)
        if cookies:
            _log.debug("CDP 提取成功: %s %s", browser, domain)
            return cookies

    # 策略 3: browser_cookie3（Firefox 回退）
    cookies = _extract_via_browser_cookie3(browser, domain)
    if cookies:
        _log.debug("browser_cookie3 提取成功: %s %s", browser, domain)
        return cookies

    return {}


def extract_douyin_cookie(browser: str = "edge", auto_close: bool = False) -> str:
    """从浏览器提取抖音 cookie，返回 cookie 字符串。"""
    cookies = extract_browser_cookies(browser, ".douyin.com", auto_close=auto_close)
    if not cookies:
        return ""
    important = ["sessionid", "passport_csrf_token", "ttwid", "msToken", "odin_tt"]
    parts = []
    for key in important:
        if key in cookies:
            parts.append(f"{key}={cookies[key]}")
    for k, v in cookies.items():
        if k not in important:
            parts.append(f"{k}={v}")
    return "; ".join(parts)


def extract_kuaishou_cookie(browser: str = "edge", auto_close: bool = False) -> str:
    """从浏览器提取快手 cookie，返回 cookie 字符串。"""
    cookies = extract_browser_cookies(browser, ".kuaishou.com", auto_close=auto_close)
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def extract_bilibili_cookies(browser: str = "edge", auto_close: bool = False) -> dict[str, str]:
    """从浏览器提取 B站 cookie，返回 {sessdata, bili_jct}。"""
    cookies = extract_browser_cookies(browser, ".bilibili.com", auto_close=auto_close)
    return {
        "sessdata": cookies.get("SESSDATA", ""),
        "bili_jct": cookies.get("bili_jct", ""),
    }


# ═══════════════════════════════════════════════════════════
#  交互式获取（供 setup.py 调用）
# ═══════════════════════════════════════════════════════════

def _detect_installed_browsers() -> list[str]:
    """检测本机安装的浏览器。"""
    available = []
    for name in ("edge", "chrome", "firefox", "brave"):
        if name == "firefox":
            if shutil.which("firefox"):
                available.append(name)
        else:
            if _find_browser_exe(name):
                available.append(name)
    return available or ["edge", "chrome", "firefox"]


def _pick_browser() -> str:
    """让用户选择浏览器。"""
    available = _detect_installed_browsers()

    print("  可用浏览器:")
    for i, b in enumerate(available):
        marker = " *" if i == 0 else ""
        print(f"    {i + 1}. {_BROWSER_DISPLAY.get(b, b)}{marker}")

    val = input(f"  选择 [1-{len(available)}，默认 1]: ").strip()
    try:
        idx = int(val) - 1
        if 0 <= idx < len(available):
            return available[idx]
    except (ValueError, IndexError):
        pass
    return available[0]


def _ask_auto_close_for_running_browser(browser: str) -> bool:
    """浏览器运行中时，询问是否自动关闭。"""
    total, foreground = _get_browser_process_state(browser)
    if total <= 0:
        return False

    if browser in _CHROMIUM_BROWSERS and foreground <= 0 and not _is_chromium_profile_locked(browser):
        # 仅后台残留进程且未锁 profile，不需要关闭。
        return False

    if foreground <= 0:
        answer = input(
            f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 后台进程，是否自动关闭后继续提取? (Y/n): "
        ).strip().lower()
        if not answer:
            return True
        return answer not in {"n", "no", "0"}

    answer = input(
        f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 正在运行，是否自动关闭后继续提取? (y/N): "
    ).strip().lower()
    return answer in {"y", "yes", "1"}


def interactive_bilibili_cookie() -> dict[str, str]:
    """交互式获取 B站 cookie。"""
    print("\n  B站 Cookie 获取方式:")
    print("    1. 扫码登录（推荐，最可靠）")
    print("    2. 从浏览器自动提取")
    print("    3. 手动输入")
    print("    4. 跳过")

    choice = input("  选择 [1-4，默认 1]: ").strip()
    if not choice or choice == "1":
        result = bilibili_qr_login_sync()
        if result and result.get("sessdata"):
            return result
        print("  扫码登录未成功，可选择其他方式。")
        return interactive_bilibili_cookie()

    elif choice == "2":
        browser = _pick_browser()
        auto_close = _ask_auto_close_for_running_browser(browser)
        result = extract_bilibili_cookies(browser, auto_close=auto_close)
        if result.get("sessdata"):
            print(f"  从 {_BROWSER_DISPLAY.get(browser, browser)} 提取成功!")
            return result
        print(f"  未在 {_BROWSER_DISPLAY.get(browser, browser)} 中找到 B站登录信息。")
        print("  请确保已在该浏览器中登录 bilibili.com")
        return {"sessdata": "", "bili_jct": ""}

    elif choice == "3":
        sessdata = input("  SESSDATA: ").strip()
        bili_jct = input("  bili_jct: ").strip()
        return {"sessdata": sessdata, "bili_jct": bili_jct}

    return {"sessdata": "", "bili_jct": ""}


def interactive_douyin_cookie() -> str:
    """交互式获取抖音 cookie。"""
    print("\n  抖音 Cookie 获取方式:")
    print("    1. 从浏览器自动提取（需已登录 douyin.com）")
    print("    2. 手动输入")
    print("    3. 跳过")

    choice = input("  选择 [1-3，默认 1]: ").strip()
    if not choice or choice == "1":
        browser = _pick_browser()
        auto_close = _ask_auto_close_for_running_browser(browser)
        cookie = extract_douyin_cookie(browser, auto_close=auto_close)
        if cookie:
            print(f"  从 {_BROWSER_DISPLAY.get(browser, browser)} 提取成功!")
            return cookie
        print(f"  未在 {_BROWSER_DISPLAY.get(browser, browser)} 中找到抖音登录信息。")
        return ""

    elif choice == "2":
        return input("  抖音 Cookie 字符串: ").strip()

    return ""


def interactive_kuaishou_cookie() -> str:
    """交互式获取快手 cookie。"""
    print("\n  快手 Cookie 获取方式:")
    print("    1. 从浏览器自动提取（需已登录 kuaishou.com）")
    print("    2. 手动输入")
    print("    3. 跳过")

    choice = input("  选择 [1-3，默认 1]: ").strip()
    if not choice or choice == "1":
        browser = _pick_browser()
        auto_close = _ask_auto_close_for_running_browser(browser)
        cookie = extract_kuaishou_cookie(browser, auto_close=auto_close)
        if cookie:
            print(f"  从 {_BROWSER_DISPLAY.get(browser, browser)} 提取成功!")
            return cookie
        print(f"  未在 {_BROWSER_DISPLAY.get(browser, browser)} 中找到快手登录信息。")
        return ""

    elif choice == "2":
        return input("  快手 Cookie 字符串: ").strip()

    return ""


# ---------------------------------------------------------------------------
# Cookie 有效性验证
# ---------------------------------------------------------------------------

async def check_bilibili_cookie(sessdata: str) -> bool:
    """验证 B站 sessdata 是否有效。"""
    if not sessdata:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10, headers={
            "Cookie": f"SESSDATA={sessdata}",
            "User-Agent": "Mozilla/5.0",
        }) as client:
            resp = await client.get("https://api.bilibili.com/x/web-interface/nav")
            data = resp.json()
            return data.get("code") == 0
    except Exception:
        return False


async def check_douyin_cookie(cookie: str) -> bool:
    """验证抖音 cookie 是否有效。"""
    if not cookie:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.douyin.com/",
        }) as client:
            resp = await client.get("https://www.douyin.com/")
            return resp.status_code == 200
    except Exception:
        return False


async def check_all_cookies(cfg: dict) -> dict[str, bool]:
    """启动时验证所有平台 cookie 有效性，返回 {platform: is_valid}。"""
    va = cfg.get("video_analysis", {}) or {}
    results: dict[str, bool] = {}

    # B站
    bili_cfg = va.get("bilibili", {}) or {}
    sessdata = str(bili_cfg.get("sessdata", "")).strip()
    if sessdata:
        results["bilibili"] = await check_bilibili_cookie(sessdata)
        status = "有效" if results["bilibili"] else "已失效"
        _log.info("cookie_check | bilibili | %s", status)

    # 抖音
    dy_cfg = va.get("douyin", {}) or {}
    dy_cookie = str(dy_cfg.get("cookie", "")).strip()
    if dy_cookie:
        results["douyin"] = await check_douyin_cookie(dy_cookie)
        status = "有效" if results["douyin"] else "已失效"
        _log.info("cookie_check | douyin | %s", status)

    # 快手
    ks_cfg = va.get("kuaishou", {}) or {}
    ks_cookie = str(ks_cfg.get("cookie", "")).strip()
    results["kuaishou"] = bool(ks_cookie)
    if not ks_cookie:
        _log.info("cookie_check | kuaishou | 未配置")

    return results
