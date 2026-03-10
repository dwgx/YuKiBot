"""Cookie 自动获取模块 — B站扫码登录 + 浏览器 Cookie 提取。

B站: 使用 bilibili-api-python 的 QrCodeLogin，终端显示二维码扫码
抖音/快手: 多策略提取浏览器 cookie:
  1. rookiepy (需管理员权限，支持 Chrome v130+ App-Bound Encryption)
  2. Chrome DevTools Protocol (无需管理员，需关闭浏览器后重开)
  3. browser_cookie3 (仅 Firefox 可靠，Chrome/Edge 已失效)
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("yukiko.cookie_auth")


def _local_httpx_client(**kwargs: Any) -> "httpx.Client":
    """创建不走系统代理的 httpx Client，用于 CDP 本地回环请求。"""
    import httpx as _httpx
    kwargs.setdefault("proxy", None)
    return _httpx.Client(**kwargs)


def _safe_input(prompt: str) -> str:
    """确保 prompt 先刷新再读取，兼容 PyCharm / 非 TTY。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        return input().strip()
    except EOFError:
        return ""


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

async def bilibili_qr_create_session() -> dict[str, Any] | None:
    """创建 B站二维码会话，返回可供 WebUI 展示和轮询的对象。"""
    try:
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginChannel
    except ImportError:
        _log.warning("bilibili_qr unavailable: bilibili-api-python not installed")
        return None

    qr = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()

    image_data_uri = ""
    with contextlib.suppress(Exception):
        pic = qr.get_qrcode_picture()
        content = bytes(getattr(pic, "content", b"") or b"")
        if content:
            image_type = str(getattr(pic, "imageType", "") or "").strip().lower()
            mime = "image/png"
            if image_type in {"jpg", "jpeg"}:
                mime = "image/jpeg"
            elif image_type == "gif":
                mime = "image/gif"
            image_data_uri = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"

    terminal = ""
    with contextlib.suppress(Exception):
        terminal = str(qr.get_qrcode_terminal() or "")

    url = getattr(qr, "_QrCodeLogin__qr_link", "")
    return {
        "qr": qr,
        "qr_url": str(url or ""),
        "qr_image_data_uri": image_data_uri,
        "qr_terminal": terminal,
        "timeout_seconds": 120,
    }


async def bilibili_qr_check_state(qr: Any) -> dict[str, Any]:
    """检查 B站二维码登录状态。"""
    from bilibili_api.login_v2 import QrCodeLoginEvents

    try:
        state = await qr.check_state()
    except Exception as exc:
        _log.debug("bilibili_qr check_state error: %s", exc)
        return {"ok": False, "status": "error", "message": "状态检查失败，请重试"}

    if state == QrCodeLoginEvents.DONE:
        cred = qr.get_credential()
        return {
            "ok": True,
            "status": "done",
            "data": {
                "sessdata": cred.sessdata or "",
                "bili_jct": cred.bili_jct or "",
                "dedeuserid": getattr(cred, "dedeuserid", "") or "",
            },
            "message": "登录成功",
        }
    if state == QrCodeLoginEvents.TIMEOUT:
        return {"ok": False, "status": "expired", "message": "二维码已过期，请重新获取"}
    if state == QrCodeLoginEvents.CONF:
        return {"ok": True, "status": "confirm", "message": "已扫码，请在手机上确认"}
    if state == QrCodeLoginEvents.SCAN:
        return {"ok": True, "status": "scanned", "message": "已扫码，等待确认"}
    return {"ok": True, "status": "pending", "message": "等待扫码"}


async def bilibili_qr_login() -> dict[str, str] | None:
    """B站二维码扫码登录，返回 {sessdata, bili_jct, dedeuserid} 或 None。"""
    session = await bilibili_qr_create_session()
    if not session:
        print("  [错误] bilibili-api-python 未安装，无法扫码登录。")
        print("  pip install bilibili-api-python")
        return None

    qr = session["qr"]
    url = str(session.get("qr_url", "") or "")
    terminal = str(session.get("qr_terminal", "") or "")
    if url:
        print("\n请用 B站 APP 扫描下方二维码登录:\n")
        _print_compact_qr(url)
    elif terminal:
        print("\n请用 B站 APP 扫描下方二维码登录:\n")
        print(terminal)
    else:
        print("\n无法获取二维码，请检查网络连接。")
        return None

    print("等待扫码...")

    timeout = 120
    elapsed = 0
    interval = 2

    while elapsed < timeout:
        state = await bilibili_qr_check_state(qr)
        status = str(state.get("status", "") or "")
        if status == "done":
            print("  B站登录成功!")
            data = state.get("data", {}) if isinstance(state, dict) else {}
            return {
                "sessdata": str(data.get("sessdata", "") or ""),
                "bili_jct": str(data.get("bili_jct", "") or ""),
                "dedeuserid": str(data.get("dedeuserid", "") or ""),
            }
        elif status == "expired":
            print("  二维码已过期。")
            return None
        elif status in {"confirm", "scanned"}:
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
    "chromium": "Chromium",
    "qzone": "QQ 空间",
}

_BROWSER_PROCESS_NAMES = {
    "chrome": "chrome",
    "edge": "msedge",
    "brave": "brave",
    "firefox": "firefox",
    "chromium": "chromium",
    "opera": "opera",
}

if sys.platform == "darwin":
    # macOS
    _BROWSER_PATHS: dict[str, list[str]] = {
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
        "firefox": ["/Applications/Firefox.app/Contents/MacOS/firefox"],
    }
    _BROWSER_USER_DATA: dict[str, str] = {
        "chrome": str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome"),
        "edge": str(Path.home() / "Library" / "Application Support" / "Microsoft Edge"),
        "brave": str(Path.home() / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser"),
        "chromium": str(Path.home() / "Library" / "Application Support" / "Chromium"),
    }
elif os.name == "nt":
    # Windows
    _BROWSER_PATHS = {
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
        "firefox": [
            os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
        ],
    }
    _BROWSER_USER_DATA = {
        "chrome": os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data"),
        "edge": os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\User Data"),
        "brave": os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\User Data"),
    }
else:
    # Linux
    _BROWSER_PATHS = {
        "chrome": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"],
        "edge": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
        "brave": ["/usr/bin/brave-browser", "/usr/bin/brave"],
        "chromium": ["/usr/bin/chromium", "/usr/bin/chromium-browser"],
        "firefox": ["/usr/bin/firefox"],
    }
    _BROWSER_USER_DATA = {
        "chrome": str(Path.home() / ".config" / "google-chrome"),
        "edge": str(Path.home() / ".config" / "microsoft-edge"),
        "brave": str(Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"),
        "chromium": str(Path.home() / ".config" / "chromium"),
    }

_CHROMIUM_BROWSERS = {"chrome", "edge", "brave", "opera", "chromium"}
_ROOKIEPY_PREFETCH_DOMAINS = [
    ".bilibili.com",
    ".douyin.com",
    ".kuaishou.com",
    ".qq.com",
    ".qzone.qq.com",
]
_ROOKIEPY_ELEVATED_CACHE: dict[str, dict[str, dict[str, str]]] = {}
_ROOKIEPY_ELEVATED_SKIP_UNTIL: dict[str, float] = {}

_COOKIE_PLATFORM_LOGIN_GUIDES: dict[str, dict[str, Any]] = {
    "bilibili": {
        "display_name": "Bilibili",
        "site": "bilibili.com",
        "login_url": "https://passport.bilibili.com/login",
        "after_login_url": "https://www.bilibili.com/",
        "instructions": [
            "The browser will open Bilibili's official login page. You can scan with the Bilibili app or sign in manually.",
            "After login, wait until the page returns to bilibili.com, then come back and extract cookies.",
        ],
        "notes": [
            "If you only need Bilibili cookies, WebUI also supports the native QR login flow.",
        ],
    },
    "douyin": {
        "display_name": "Douyin",
        "site": "douyin.com",
        "login_url": "https://login.douyin.com/",
        "after_login_url": "https://www.douyin.com/",
        "instructions": [
            "The browser will open Douyin's official login page. Scan with the Douyin app.",
            "After login, wait until the page reaches douyin.com, then come back and extract cookies.",
        ],
        "notes": [
            "If extraction fails, enable auto-close retry. Chromium v130+ may also need admin privileges.",
        ],
    },
    "kuaishou": {
        "display_name": "Kuaishou",
        "site": "kuaishou.com",
        "login_url": "https://www.kuaishou.com/account/login",
        "after_login_url": "https://www.kuaishou.com/",
        "instructions": [
            "The browser will open Kuaishou's official login page. Scan with the Kuaishou app.",
            "After login, make sure the page has entered kuaishou.com, then come back and extract cookies.",
        ],
        "notes": [
            "If you are already logged in with this browser profile, you can skip the scan-login step and extract directly.",
        ],
    },
    "qzone": {
        "display_name": "QZone",
        "site": "qzone.qq.com",
        "login_url": "https://qzone.qq.com/",
        "after_login_url": "https://user.qzone.qq.com/",
        "instructions": [
            "The browser will open QZone's official login page. Scan with QQ.",
            "After login, make sure the browser enters your own QZone home page before extracting cookies.",
        ],
        "notes": [
            "Do not stay on someone else's QZone page. The browser must reach your own QZone.",
            "If the browser reaches user.qzone.qq.com/<your-qq>, the login state is ready.",
        ],
    },
}


def _normalize_cookie_platform_name(platform_name: str) -> str:
    key = str(platform_name or "").strip().lower()
    if key == "qq":
        return "qzone"
    return key


def get_cookie_login_guide(platform: str) -> dict[str, Any] | None:
    """Return browser scan-login guidance for a cookie platform."""
    key = _normalize_cookie_platform_name(platform)
    guide = _COOKIE_PLATFORM_LOGIN_GUIDES.get(key)
    if not guide:
        return None
    return {
        "platform": key,
        "display_name": str(guide.get("display_name", key)),
        "site": str(guide.get("site", "") or ""),
        "login_url": str(guide.get("login_url", "") or ""),
        "after_login_url": str(guide.get("after_login_url", "") or ""),
        "instructions": [str(item) for item in (guide.get("instructions") or []) if str(item).strip()],
        "notes": [str(item) for item in (guide.get("notes") or []) if str(item).strip()],
    }


def _build_browser_login_command(browser: str, url: str) -> tuple[list[str], str]:
    exe = _find_browser_exe(browser)
    if not exe:
        raise FileNotFoundError(f"Browser executable not found: {_BROWSER_DISPLAY.get(browser, browser)}")

    cmd = [exe]
    profile_hint = ""
    if browser in _CHROMIUM_BROWSERS:
        user_data = _BROWSER_USER_DATA.get(browser, "")
        if user_data and os.path.isdir(user_data):
            profiles = _list_chromium_profiles(user_data)
            if profiles:
                profile_hint = profiles[0]
                cmd.append(f"--profile-directory={profile_hint}")
        cmd.append("--new-tab")
    elif browser == "firefox":
        cmd.append("-new-tab")

    cmd.append(url)
    return cmd, profile_hint


def prepare_browser_cookie_login(platform: str, browser: str = "edge") -> dict[str, Any]:
    """Open the platform login page in the selected browser for scan-login cookie extraction."""
    key = _normalize_cookie_platform_name(platform)
    browser_name = str(browser or "edge").strip().lower() or "edge"
    guide = get_cookie_login_guide(key)
    if not guide:
        return {"ok": False, "message": f"Unsupported platform: {platform}"}

    try:
        cmd, profile_hint = _build_browser_login_command(browser_name, str(guide.get("login_url", "") or ""))
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {
            "ok": False,
            "platform": key,
            "browser": browser_name,
            "message": f"Failed to open {guide['display_name']} login page: {exc}",
        }

    message = (
        f"Opened {guide['display_name']} login page in {_BROWSER_DISPLAY.get(browser_name, browser_name)}. "
        "Finish scan login, then come back to extract cookies."
    )
    return {
        "ok": True,
        "platform": key,
        "browser": browser_name,
        "browser_display": _BROWSER_DISPLAY.get(browser_name, browser_name),
        "profile_directory": profile_hint,
        "message": message,
        **guide,
    }

def _find_browser_exe(browser: str) -> str | None:
    """查找浏览器可执行文件。"""
    for path in _BROWSER_PATHS.get(browser, []):
        if os.path.isfile(path):
            return path
    # 尝试 shutil.which
    names = {
        "chrome": "google-chrome" if os.name != "nt" else "chrome",
        "edge": "microsoft-edge" if os.name != "nt" else "msedge",
        "brave": "brave-browser" if os.name != "nt" else "brave",
        "chromium": "chromium",
        "firefox": "firefox",
    }
    found = shutil.which(names.get(browser, browser))
    if not found and browser in {"chrome", "edge", "brave"}:
        # 补充常见二进制名
        fallback_names = {
            "chrome": ["chrome", "google-chrome-stable"],
            "edge": ["msedge", "microsoft-edge-stable"],
            "brave": ["brave"],
        }
        for name in fallback_names.get(browser, []):
            found = shutil.which(name)
            if found:
                break
    return found


def _list_chromium_profiles(user_data: str) -> list[str]:
    """列出 Chromium 的可用 profile，优先返回最近使用的 profile。"""
    root = Path(user_data)
    if not root.exists():
        return ["Default"]

    profiles: list[str] = []
    if (root / "Default").exists():
        profiles.append("Default")
    for child in sorted(root.glob("Profile *")):
        if child.is_dir():
            profiles.append(child.name)

    # 优先使用 Local State 记录的 last_used profile
    last_used = ""
    try:
        local_state_path = root / "Local State"
        if local_state_path.exists():
            payload = json.loads(local_state_path.read_text(encoding="utf-8"))
            last_used = str(
                ((payload.get("profile") or {}) if isinstance(payload, dict) else {}).get("last_used", "")
                or ""
            ).strip()
    except Exception:
        last_used = ""

    if last_used and last_used in profiles:
        profiles = [last_used] + [p for p in profiles if p != last_used]

    return profiles or ["Default"]




def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _extract_via_cdp(browser: str, domain: str, auto_close: bool = False) -> dict[str, str]:
    """通过 Chrome DevTools Protocol 提取 cookie。

    策略优先级:
      1. 尝试连接已运行浏览器的 debug 端口（无需关闭浏览器）
      2. 复制 cookie 数据库到临时目录，用新 profile 启动 headless 解密
      3. 如果以上都失败且 auto_close=True，关闭浏览器后用原 profile 启动
    """
    exe = _find_browser_exe(browser)
    if not exe:
        _log.debug("CDP: 未找到 %s 可执行文件", browser)
        return {}

    user_data = _BROWSER_USER_DATA.get(browser, "")
    if not user_data or not os.path.isdir(user_data):
        _log.debug("CDP: 未找到 %s User Data 目录", browser)
        return {}
    profile_candidates = _list_chromium_profiles(user_data)

    # ── 策略 1：尝试连接已运行浏览器的 debug 端口 ──
    # 扫描常见 debug 端口（用户可能已带 --remote-debugging-port 启动）
    for probe_port in (9222, 9229, 19222):
        cookies = _try_connect_existing_debug_port(probe_port, domain)
        if cookies:
            _log.debug("CDP: 通过已有 debug 端口 %d 提取成功", probe_port)
            return cookies

    # ── 策略 2：复制 cookie 文件到临时目录，用新 profile 解密 ──
    # 这种方式不需要关闭浏览器！
    for profile_dir in profile_candidates:
        cookies = _extract_via_temp_profile(exe, user_data, domain, profile_dir=profile_dir)
        if cookies:
            _log.debug("CDP: 通过临时 profile 提取成功 | profile=%s", profile_dir)
            return cookies

    # ── 策略 3：关闭浏览器后用原 profile（最后手段）──
    if not _is_browser_running(browser):
        # 浏览器没在运行，直接用原 profile
        for profile_dir in profile_candidates:
            cookies = _extract_via_cdp_original_profile(exe, user_data, domain, profile_dir=profile_dir)
            if cookies:
                _log.debug("CDP: 原始 profile 提取成功 | profile=%s", profile_dir)
                return cookies
        return {}

    if not auto_close:
        _log.debug("CDP 策略 1&2 失败，浏览器运行中，需 auto_close 才能继续")
        return {}

    total, foreground = _get_browser_process_state(browser)
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

    for profile_dir in profile_candidates:
        cookies = _extract_via_cdp_original_profile(exe, user_data, domain, profile_dir=profile_dir)
        if cookies:
            _log.debug("CDP: 关闭后原始 profile 提取成功 | profile=%s", profile_dir)
            return cookies
    return {}


def _try_connect_existing_debug_port(port: int, domain: str) -> dict[str, str]:
    """尝试连接已运行浏览器的 debug 端口获取 cookie。"""
    try:
        with _local_httpx_client(timeout=1.5) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/version")
            if resp.status_code != 200:
                return {}
    except Exception:
        return {}

    try:
        with _local_httpx_client(timeout=2) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/list")
            targets = resp.json()
        if not targets:
            return {}
        ws_url = targets[0].get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {}
        return _cdp_get_cookies(ws_url, domain)
    except Exception:
        return {}


def _extract_via_temp_profile(
    exe: str,
    user_data: str,
    domain: str,
    profile_dir: str = "Default",
) -> dict[str, str]:
    """复制 cookie 数据库到临时目录，启动 headless 浏览器解密。

    关键：使用临时 user-data-dir，不与正在运行的浏览器冲突。
    浏览器会用 Local State 中的加密密钥解密临时目录中的 cookie 文件。
    """
    tmp_dir = None
    proc = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="yukiko_cookie_")
        _copy_cookie_profile(user_data, tmp_dir)

        # 检查 cookie 文件是否存在（Chromium 新版通常在 Network/Cookies）
        tmp_root = Path(tmp_dir)
        cookie_exists = any(
            p.exists()
            for p in (
                *tmp_root.glob("*/Network/Cookies"),
                *tmp_root.glob("*/Cookies"),
            )
        )
        if not cookie_exists:
            _log.debug("临时 profile 中无 Cookies 文件")
            return {}

        # 找空闲端口
        port = 19222
        while not _is_port_free(port) and port < 19300:
            port += 1
        if port >= 19300:
            return {}

        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_dir}",
            f"--profile-directory={profile_dir}",
            "--no-first-run",
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--no-default-browser-check",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        # 等待 CDP 就绪
        ready = False
        for _ in range(40):
            time.sleep(0.2)
            try:
                with _local_httpx_client(timeout=1) as client:
                    resp = client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    ready = True
                    break
            except Exception:
                continue

        if not ready:
            _log.debug("临时 profile headless 启动超时 | profile=%s", profile_dir)
            return {}

        with _local_httpx_client(timeout=3) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/list")
        targets = resp.json()
        if not targets:
            return {}
        ws_url = targets[0].get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {}

        return _cdp_get_cookies(ws_url, domain)

    except Exception as exc:
        _log.debug("临时 profile 提取失败 | profile=%s | %s", profile_dir, exc)
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
        if tmp_dir:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


def _extract_via_cdp_original_profile(
    exe: str,
    user_data: str,
    domain: str,
    profile_dir: str = "Default",
) -> dict[str, str]:
    """用原始 profile 启动 headless 浏览器提取 cookie（需浏览器未运行）。"""
    port = 19222
    while not _is_port_free(port) and port < 19300:
        port += 1
    if port >= 19300:
        return {}

    proc = None
    try:
        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            f"--profile-directory={profile_dir}",
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

        ready = False
        for _ in range(40):
            time.sleep(0.2)
            try:
                with _local_httpx_client(timeout=1) as client:
                    resp = client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    ready = True
                    break
            except Exception:
                continue

        if not ready:
            return {}

        with _local_httpx_client(timeout=3) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/list")
        targets = resp.json()
        if not targets:
            return {}
        ws_url = targets[0].get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {}

        return _cdp_get_cookies(ws_url, domain)

    except Exception as exc:
        _log.debug("CDP 原始 profile 提取失败 | profile=%s | %s", profile_dir, exc)
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

    if os.name != "nt":
        # Linux/macOS：使用 pgrep，无法可靠区分前台窗口，foreground 取 total 以便后续逻辑工作。
        try:
            result = subprocess.run(
                ["pgrep", "-x", proc_name],
                capture_output=True,
                text=True,
                timeout=4,
            )
            pids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            total = len(pids)
            return (total, total)
        except Exception:
            return (0, 0)

    # Windows：先用 PowerShell 拿更准确的 MainWindowHandle。
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
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        count = 0
        for line in lines:
            lower = line.lower()
            if "no tasks are running" in lower or "没有运行的任务" in lower or "info:" in lower:
                continue
            if lower.startswith(f'"{proc_name.lower()}.exe"'):
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

    proc_name = name.replace(".exe", "")
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", f"{proc_name}.exe", "/F", "/T"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.run(["pkill", "-TERM", "-x", proc_name], capture_output=True, text=True, timeout=6)
    except Exception:
        return False
    for _ in range(8):
        time.sleep(0.3)
        if not _is_browser_running(browser):
            return True
    if os.name != "nt":
        with contextlib.suppress(Exception):
            subprocess.run(["pkill", "-KILL", "-x", proc_name], capture_output=True, text=True, timeout=5)
        for _ in range(5):
            time.sleep(0.2)
            if not _is_browser_running(browser):
                return True
    return False


def is_browser_running(browser: str) -> bool:
    """公开浏览器运行态检测（供管理命令和自检脚本使用）。"""
    return _is_browser_running(browser)


def _copy_cookie_profile(src_data_dir: str, dst_dir: str) -> None:
    """复制浏览器 profile 中的 cookie 相关文件到临时目录。"""
    # 复制 Local State + 常见 profile 的 Cookies（含 Network/Cookies）
    src = Path(src_data_dir)
    dst = Path(dst_dir)

    # Local State（包含加密密钥）
    local_state = src / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, dst / "Local State")

    profile_names: list[str] = []
    if (src / "Default").exists():
        profile_names.append("Default")
    for child in sorted(src.glob("Profile *")):
        if child.is_dir():
            profile_names.append(child.name)
    if not profile_names:
        profile_names = ["Default", "Profile 1"]

    for profile in profile_names:
        src_profile = src / profile
        if not src_profile.exists():
            continue
        dst_profile = dst / profile
        dst_profile.mkdir(parents=True, exist_ok=True)

        for fname in ("Preferences", "Secure Preferences"):
            src_file = src_profile / fname
            if src_file.exists():
                try:
                    shutil.copy2(src_file, dst_profile / fname)
                except Exception:
                    pass

        # Chromium 老版本可能在 profile 根目录；新版本常在 profile/Network 目录。
        cookie_candidates = [
            (src_profile / "Cookies", dst_profile / "Cookies"),
            (src_profile / "Cookies-journal", dst_profile / "Cookies-journal"),
            (src_profile / "Cookies-wal", dst_profile / "Cookies-wal"),
            (src_profile / "Cookies-shm", dst_profile / "Cookies-shm"),
            (src_profile / "Network" / "Cookies", dst_profile / "Network" / "Cookies"),
            (src_profile / "Network" / "Cookies-journal", dst_profile / "Network" / "Cookies-journal"),
            (src_profile / "Network" / "Cookies-wal", dst_profile / "Network" / "Cookies-wal"),
            (src_profile / "Network" / "Cookies-shm", dst_profile / "Network" / "Cookies-shm"),
        ]
        for src_file, dst_file in cookie_candidates:
            if not src_file.exists():
                continue
            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
            except Exception:
                pass


def _cdp_send_and_recv(ws: Any, msg_id: int, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
    """CDP 发送命令并等待对应 id 的响应。"""
    payload: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = ws.recv(timeout=1)
        except TimeoutError:
            continue
        result = json.loads(raw)
        if result.get("id") == msg_id:
            return result.get("result", {})
    return {}


# QZone 域名 → 导航 URL 映射，用于 CDP 先导航再取 cookie
_DOMAIN_NAV_URLS: dict[str, list[str]] = {
    "qq.com": ["https://user.qzone.qq.com", "https://i.qq.com"],
    "qzone.qq.com": ["https://user.qzone.qq.com"],
    "i.qq.com": ["https://i.qq.com"],
}


def _cdp_get_cookies(ws_url: str, domain: str) -> dict[str, str]:
    """通过 CDP WebSocket 获取指定域名的 cookie。

    策略:
      1. 先尝试 Network.getAllCookies（快速路径）
      2. 如果目标域名是 QZone 相关且结果不足，导航到目标页面后重试
      3. 用 Network.getCookies + 具体 URL 作为补充
    """
    import websockets.sync.client as ws_client

    cookies: dict[str, str] = {}
    msg_id = 0
    clean_domain = domain.lstrip(".")

    def _collect(cookie_list: list[dict]) -> None:
        for c in cookie_list:
            c_domain = c.get("domain", "").lstrip(".")
            if clean_domain in c_domain or c_domain in clean_domain:
                name = c.get("name", "")
                if name:
                    cookies[name] = c.get("value", "")

    try:
        with ws_client.connect(ws_url, close_timeout=5) as ws:
            # ── 快速路径: Network.getAllCookies ──
            msg_id += 1
            result = _cdp_send_and_recv(ws, msg_id, "Network.getAllCookies")
            _collect(result.get("cookies", []))

            # 对 QZone 相关域名，如果快速路径没拿到关键 cookie，导航后重试
            is_qzone = _is_qzone_related_domain(domain)
            if is_qzone and not _has_qzone_signal(cookies):
                nav_urls = _DOMAIN_NAV_URLS.get(clean_domain, [])
                for nav_url in nav_urls:
                    # 启用 Page 域
                    msg_id += 1
                    _cdp_send_and_recv(ws, msg_id, "Page.enable", timeout=2)
                    # 导航到目标页面
                    msg_id += 1
                    _cdp_send_and_recv(ws, msg_id, "Page.navigate", {"url": nav_url}, timeout=8)
                    # 等待页面加载（cookie 设置需要时间）
                    time.sleep(2.0)
                    # 重新获取 cookie
                    msg_id += 1
                    result = _cdp_send_and_recv(ws, msg_id, "Network.getAllCookies")
                    _collect(result.get("cookies", []))
                    if _has_qzone_signal(cookies):
                        break

                # 补充: 用 Network.getCookies + 具体 URL
                if not _has_qzone_signal(cookies):
                    all_urls = []
                    for urls in _DOMAIN_NAV_URLS.values():
                        all_urls.extend(urls)
                    msg_id += 1
                    result = _cdp_send_and_recv(
                        ws, msg_id, "Network.getCookies",
                        {"urls": list(dict.fromkeys(all_urls))},
                        timeout=5,
                    )
                    _collect(result.get("cookies", []))

    except ImportError:
        _log.debug("CDP: websockets 库未安装")
    except Exception as exc:
        _log.debug("CDP WebSocket 错误: %s", exc)

    return cookies


def _extract_via_rookiepy(browser: str, domain: str) -> dict[str, str]:
    """通过 rookiepy 提取 cookie（需管理员权限，支持 Chrome v130+）。"""
    cached = _ROOKIEPY_ELEVATED_CACHE.get(browser, {}).get(domain, {})
    if cached:
        return dict(cached)

    try:
        import rookiepy
    except ImportError:
        return {}

    fn = getattr(rookiepy, browser, None)
    if not fn:
        return {}

    domain_candidates = [domain]
    stripped = domain.lstrip(".")
    if stripped and stripped != domain:
        domain_candidates.append(stripped)

    try:
        raw = fn(domain_candidates)
        if not raw and len(domain_candidates) > 1:
            # 部分环境对前导点域名不兼容，回退重试无前导点写法
            raw = fn([stripped])
        return {c["name"]: c["value"] for c in raw if c.get("name")}
    except Exception as exc:
        msg = str(exc)
        lower = msg.lower()
        if "appbound encryption" in lower or "running as admin" in lower:
            print(
                "  提示: Chromium v130+ Cookie 解密可能需要管理员权限。"
                "请以管理员身份运行，或关闭浏览器后重试。"
            )
            domains = list(dict.fromkeys([domain, *_ROOKIEPY_PREFETCH_DOMAINS]))
            elevated = _extract_via_rookiepy_elevated_prefetch(browser, domains).get(domain, {})
            if elevated:
                _log.debug(
                    "rookiepy elevated 提取成功: %s %s | cookies=%d",
                    browser,
                    domain,
                    len(elevated),
                )
                return elevated
        _log.debug("rookiepy %s 失败: %s", browser, exc)
        return {}


def _extract_via_sqlite_direct(browser: str, domain: str) -> dict[str, str]:
    """直接读取 Chromium SQLite cookie 数据库（无需关闭浏览器）。

    策略:
      1. 复制 Cookies 数据库到临时文件（避免锁冲突）
      2. 读取 SQLite 数据库中的加密 cookie
      3. 用 DPAPI 解密（Windows）或 keyring（Linux/Mac）
      4. 仅适用于 Chrome < v130（v130+ 需要 App-Bound Encryption）
    """
    if os.name != "nt":
        return {}  # 目前仅支持 Windows DPAPI

    user_data = _BROWSER_USER_DATA.get(browser, "")
    if not user_data or not os.path.isdir(user_data):
        return {}

    try:
        import sqlite3
        import win32crypt  # pywin32
    except ImportError:
        return {}

    # 读取 Local State 获取加密密钥
    local_state_path = Path(user_data) / "Local State"
    if not local_state_path.exists():
        return {}

    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key", "")
        if not encrypted_key_b64:
            return {}
        import base64
        encrypted_key = base64.b64decode(encrypted_key_b64)
        # 去掉 DPAPI 前缀 "DPAPI"
        if encrypted_key[:5] == b"DPAPI":
            encrypted_key = encrypted_key[5:]
        # DPAPI 解密密钥
        key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except Exception as exc:
        _log.debug("SQLite: 无法读取加密密钥: %s", exc)
        return {}

    # 查找 Cookies 数据库（优先 Network/Cookies，回退到根目录 Cookies）
    cookie_db_paths = []
    for profile in ["Default", "Profile 1", "Profile 2"]:
        profile_path = Path(user_data) / profile
        if not profile_path.exists():
            continue
        cookie_db_paths.append(profile_path / "Network" / "Cookies")
        cookie_db_paths.append(profile_path / "Cookies")

    cookies: dict[str, str] = {}
    clean_domain = domain.lstrip(".").lower()

    for cookie_db in cookie_db_paths:
        if not cookie_db.exists():
            continue

        # 复制到临时文件避免锁冲突
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
                tmp_path = tmp.name
            shutil.copy2(cookie_db, tmp_path)
        except Exception:
            continue

        try:
            conn = sqlite3.connect(tmp_path, timeout=5)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (f"%{clean_domain}%",)
            )
            rows = cursor.fetchall()
            conn.close()

            for host_key, name, encrypted_value in rows:
                if not name or not encrypted_value:
                    continue
                try:
                    # Chrome v80+ 使用 AES-GCM 加密，前缀 "v10" 或 "v11"
                    if encrypted_value[:3] == b"v10" or encrypted_value[:3] == b"v11":
                        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                        nonce = encrypted_value[3:15]
                        ciphertext = encrypted_value[15:]
                        aesgcm = AESGCM(key)
                        decrypted = aesgcm.decrypt(nonce, ciphertext, None)
                        value = decrypted.decode("utf-8", errors="ignore")
                    else:
                        # 老版本 DPAPI 加密
                        decrypted = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
                        value = decrypted.decode("utf-8", errors="ignore")
                    if value:
                        cookies[name] = value
                except Exception:
                    continue
        except Exception as exc:
            _log.debug("SQLite: 读取 %s 失败: %s", cookie_db, exc)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return cookies


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
        target = domain.lstrip(".").lower()
        cookies: dict[str, str] = {}
        for c in cj:
            c_domain = str(c.domain or "").lstrip(".").lower()
            if not c_domain:
                continue
            if c_domain == target or c_domain.endswith(f".{target}") or target.endswith(f".{c_domain}"):
                cookies[c.name] = c.value
        return cookies
    except Exception as exc:
        _log.debug("browser_cookie3 %s 失败: %s", browser, exc)
        return {}


_QZONE_RELATED_DOMAINS = {"qq.com", "qzone.qq.com", "i.qq.com"}
_QZONE_SIGNAL_KEYS = {"p_skey", "skey", "media_p_skey", "p_uin", "uin", "media_p_uin", "pt2gguin"}


def _is_qzone_related_domain(domain: str) -> bool:
    clean = str(domain or "").strip().lstrip(".").lower()
    return clean in _QZONE_RELATED_DOMAINS


def _has_qzone_signal(cookies: dict[str, str]) -> bool:
    if not cookies:
        return False
    for key in _QZONE_SIGNAL_KEYS:
        value = str(cookies.get(key, "") or "").strip()
        if value:
            return True
    return False


def extract_browser_cookies_with_source(
    browser: str,
    domain: str,
    auto_close: bool = False,
) -> tuple[dict[str, str], str]:
    """从指定浏览器提取指定域名 cookie，并返回命中来源。

    策略优先级:
      1. rookiepy（需管理员，支持 Chrome v130+）
      2. CDP 三级策略:
         a. 连接已有 debug 端口（无需关闭浏览器）
         b. 复制 cookie 到临时 profile 解密（无需关闭浏览器）
         c. 关闭浏览器后用原 profile（最后手段）
      2.5. SQLite 直接读取 + DPAPI 解密（Chromium 系，无需关闭浏览器）
      3. browser_cookie3（仅 Firefox 可靠）

    QZone 特殊处理:
      - CDP 会先导航到 user.qzone.qq.com 触发 cookie 加载
      - 使用 Network.getCookies + 具体 URL 补充提取
      - 检查关键 cookie (p_skey/skey) 是否存在
    """
    qzone_related = _is_qzone_related_domain(domain)
    partial_candidates: list[tuple[str, dict[str, str]]] = []

    # 策略 1: rookiepy
    cookies = _extract_via_rookiepy(browser, domain)
    if cookies:
        if not qzone_related or _has_qzone_signal(cookies):
            _log.debug("rookiepy 提取成功: %s %s", browser, domain)
            return cookies, "rookiepy"
        # QZone 场景下，若 rookiepy 只有无用残片，继续回退 CDP。
        partial_candidates.append(("rookiepy", cookies))
        _log.debug("rookiepy 提取到 qzone 残片，继续回退: %s %s", browser, domain)

    # 策略 2: CDP（Chrome/Edge/Brave）— 优先不关闭浏览器
    if browser in ("chrome", "edge", "brave", "chromium"):
        print(f"  正在从 {_BROWSER_DISPLAY.get(browser, browser)} 提取 Cookie...")
        cookies = _extract_via_cdp(browser, domain, auto_close=auto_close)
        if cookies:
            if not qzone_related or _has_qzone_signal(cookies):
                _log.debug("CDP 提取成功: %s %s", browser, domain)
                return cookies, "cdp"
            partial_candidates.append(("cdp", cookies))
            _log.debug("CDP 提取到 qzone 残片: %s %s", browser, domain)

    # 策略 2.5: 直接读取 SQLite cookie 数据库（Chromium 系浏览器，无需关闭）
    if browser in ("chrome", "edge", "brave", "chromium"):
        cookies = _extract_via_sqlite_direct(browser, domain)
        if cookies:
            if not qzone_related or _has_qzone_signal(cookies):
                _log.debug("SQLite 直接提取成功: %s %s", browser, domain)
                return cookies, "sqlite_direct"
            partial_candidates.append(("sqlite_direct", cookies))
            _log.debug("SQLite 提取到 qzone 残片: %s %s", browser, domain)

    # 策略 3: browser_cookie3（Firefox 回退）
    cookies = _extract_via_browser_cookie3(browser, domain)
    if cookies:
        if not qzone_related or _has_qzone_signal(cookies):
            _log.debug("browser_cookie3 提取成功: %s %s", browser, domain)
            return cookies, "browser_cookie3"
        partial_candidates.append(("browser_cookie3", cookies))

    # QZone 场景回退：都不达标时，返回 cookie 最多的一份，方便前端展示诊断信息。
    if partial_candidates:
        best_source, best = max(partial_candidates, key=lambda item: len(item[1] or {}))
        return best, f"{best_source}_partial"

    return {}, "none"


def extract_browser_cookies(browser: str, domain: str, auto_close: bool = False) -> dict[str, str]:
    """从指定浏览器提取指定域名的 cookie。"""
    cookies, _ = extract_browser_cookies_with_source(
        browser=browser,
        domain=domain,
        auto_close=auto_close,
    )
    return cookies


def smart_extract_all_cookies_no_restart(
    browser: str = "edge",
    domains: list[str] | None = None,
    *,
    include_meta: bool = False,
) -> Any:
    """无重启提取所有平台 Cookie（默认策略）。

    说明:
      - 不会关闭/重开浏览器。
      - 每个域独立提取，优先 rookiepy/CDP，必要时 browser_cookie3 回退。
    """
    if domains is None:
        domains = [".bilibili.com", ".douyin.com", ".kuaishou.com", ".qq.com", ".qzone.qq.com"]

    result: dict[str, dict[str, str]] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []

    for domain in domains:
        cookies, source = extract_browser_cookies_with_source(
            browser=browser,
            domain=domain,
            auto_close=False,
        )
        sources[domain] = source
        if cookies:
            result[domain] = cookies
            _log.info(
                "smart_extract_no_restart | browser=%s | domain=%s | source=%s | cookies=%d",
                browser,
                domain,
                source,
                len(cookies),
            )
        else:
            _log.info(
                "smart_extract_no_restart | browser=%s | domain=%s | source=%s | cookies=0",
                browser,
                domain,
                source,
            )
        if source == "browser_cookie3":
            warnings.append(
                f"{domain}: 使用 browser_cookie3 回退，若缺少 HttpOnly 字段可切换到 CDP/rookiepy。"
            )

    meta = {
        "browser": browser,
        "sources": sources,
        "warnings": warnings,
    }
    if include_meta:
        return result, meta
    return result


def smart_extract_all_cookies(
    browser: str = "edge",
    setup_url: str = "http://127.0.0.1:8081/webui/setup",
    domains: list[str] | None = None,
) -> dict[str, dict[str, str]]:
    """智能重启浏览器提取所有平台 Cookie（v130+ 唯一可靠方案）。

    流程:
      1. 关闭目标浏览器
      2. 用 --remote-debugging-port 重新启动（保留原 profile，自动恢复标签页）
      3. 通过 CDP Network.getAllCookies 一次性拿到所有 cookie
      4. 保持浏览器运行（用户继续使用，debug 端口无害）

    返回: {domain: {cookie_name: cookie_value}}
    """
    if domains is None:
        domains = [".bilibili.com", ".douyin.com", ".kuaishou.com", ".qq.com", ".qzone.qq.com"]

    exe = _find_browser_exe(browser)
    if not exe:
        _log.warning("smart_extract: 未找到 %s", browser)
        return {}

    user_data = _BROWSER_USER_DATA.get(browser, "")
    if not user_data or not os.path.isdir(user_data):
        _log.warning("smart_extract: 未找到 %s User Data", browser)
        return {}

    was_running_before = _is_browser_running(browser)
    closed_for_extract = False

    def _restore_browser_if_needed(stage: str) -> None:
        if not was_running_before:
            return
        if not closed_for_extract:
            return
        if _is_browser_running(browser):
            return
        try:
            restore_cmd = [
                exe,
                f"--user-data-dir={user_data}",
                "--restore-last-session",
                setup_url,
            ]
            subprocess.Popen(
                restore_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _log.warning(
                "smart_extract: 阶段 %s 失败，已尝试恢复启动 %s",
                stage,
                browser,
            )
        except Exception as exc:
            _log.warning(
                "smart_extract: 阶段 %s 失败，恢复启动 %s 失败 | %s",
                stage,
                browser,
                exc,
            )

    # ── 步骤 1: 关闭浏览器 ──
    if was_running_before:
        _log.info("smart_extract: 正在关闭 %s ...", browser)
        if not _stop_browser_processes(browser):
            _log.warning("smart_extract: 无法关闭 %s", browser)
            return {}
        # 等待进程完全退出
        for _ in range(15):
            time.sleep(0.3)
            if not _is_browser_running(browser):
                break
        else:
            _log.warning("smart_extract: %s 未能完全退出", browser)
            return {}
        closed_for_extract = True
        time.sleep(0.5)

    # ── 步骤 2: 带 debug 端口重启 ──
    port = 19222
    while not _is_port_free(port) and port < 19300:
        port += 1
    if port >= 19300:
        _log.warning("smart_extract: 无可用端口")
        _restore_browser_if_needed("port")
        return {}

    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--restore-last-session",
        setup_url,
    ]
    _log.info("smart_extract: 启动 %s (port=%d)", browser, port)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # ── 步骤 3: 等待 CDP 就绪 ──
    ready = False
    for _ in range(60):  # 最多等 12 秒
        time.sleep(0.2)
        try:
            with _local_httpx_client(timeout=1) as client:
                resp = client.get(f"http://127.0.0.1:{port}/json/version")
            if resp.status_code == 200:
                ready = True
                break
        except Exception:
            continue

    if not ready:
        _log.warning("smart_extract: CDP 未就绪")
        _restore_browser_if_needed("cdp_ready")
        return {}

    # ── 步骤 4: 获取 WebSocket URL ──
    try:
        with _local_httpx_client(timeout=3) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/list")
        targets = resp.json()
    except Exception:
        targets = []

    ws_url = ""
    if targets:
        ws_url = targets[0].get("webSocketDebuggerUrl", "")

    if not ws_url:
        # 尝试从 /json/version 获取
        try:
            with _local_httpx_client(timeout=3) as client:
                resp = client.get(f"http://127.0.0.1:{port}/json/version")
            ver = resp.json()
            ws_url = ver.get("webSocketDebuggerUrl", "")
        except Exception:
            pass

    if not ws_url:
        _log.warning("smart_extract: 无法获取 WebSocket URL")
        _restore_browser_if_needed("ws_url")
        return {}

    # ── 步骤 5: 通过 CDP 提取所有 cookie ──
    _log.info("smart_extract: 正在通过 CDP 提取 cookie ...")
    all_cookies = _cdp_get_all_cookies(ws_url)

    # ── 步骤 6: 按域名分组 ──
    result: dict[str, dict[str, str]] = {}
    for domain in domains:
        clean = domain.lstrip(".")
        matched: dict[str, str] = {}
        for c_domain, c_name, c_value in all_cookies:
            c_clean = c_domain.lstrip(".")
            if clean in c_clean or c_clean in clean:
                matched[c_name] = c_value
        if matched:
            result[domain] = matched

    _log.info("smart_extract: 提取完成，%d 个域名有 cookie", len(result))
    # 注意：不关闭浏览器，用户继续使用
    return result


def _cdp_get_all_cookies(ws_url: str) -> list[tuple[str, str, str]]:
    """通过 CDP 获取浏览器所有 cookie，返回 [(domain, name, value), ...]。

    会先导航到 QZone 页面以确保 QZone cookie 被加载。
    """
    import websockets.sync.client as ws_client

    cookies: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    msg_id = 0

    def _collect(cookie_list: list[dict]) -> None:
        for c in cookie_list:
            key = (c.get("domain", ""), c.get("name", ""))
            if key not in seen and key[1]:
                seen.add(key)
                cookies.append((c.get("domain", ""), c["name"], c.get("value", "")))

    try:
        with ws_client.connect(ws_url, close_timeout=5) as ws:
            # 先导航到 QZone 触发 cookie 加载
            msg_id += 1
            _cdp_send_and_recv(ws, msg_id, "Page.enable", timeout=2)
            msg_id += 1
            _cdp_send_and_recv(ws, msg_id, "Page.navigate",
                               {"url": "https://user.qzone.qq.com"}, timeout=8)
            time.sleep(2.0)

            # 获取所有 cookie
            msg_id += 1
            result = _cdp_send_and_recv(ws, msg_id, "Network.getAllCookies", timeout=8)
            _collect(result.get("cookies", []))

            # 补充: getCookies with QZone URLs
            msg_id += 1
            result = _cdp_send_and_recv(ws, msg_id, "Network.getCookies", {
                "urls": ["https://user.qzone.qq.com", "https://qzone.qq.com",
                         "https://i.qq.com", "https://qq.com"]
            }, timeout=5)
            _collect(result.get("cookies", []))

    except Exception as exc:
        _log.debug("CDP getAllCookies 错误: %s", exc)

    return cookies


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


def extract_qzone_cookies(browser: str = "edge", auto_close: bool = False) -> str:
    """从浏览器提取 QQ空间 cookie，返回 cookie 字符串。

    需要合并 .qq.com 和 .qzone.qq.com 两个域的 cookie。
    关键字段: p_skey / skey, uin, p_uin

    策略:
      1. 分域提取 (.qq.com + .i.qq.com + .qzone.qq.com) 并合并
      2. 如果分域提取缺少关键 cookie，尝试 smart_extract 一次性提取
    """
    # ── 策略 1: 分域提取 ──
    qq_cookies = extract_browser_cookies(browser, ".qq.com", auto_close=auto_close)
    iqq_cookies = extract_browser_cookies(browser, ".i.qq.com", auto_close=auto_close)
    qzone_cookies = extract_browser_cookies(browser, ".qzone.qq.com", auto_close=auto_close)
    merged = {**qq_cookies, **iqq_cookies, **qzone_cookies}

    # ── 策略 2: 分域不够时，用 smart_extract 一次性 CDP 提取 ──
    if not (merged.get("p_skey") or merged.get("skey")):
        _log.debug("qzone 分域提取缺少关键 cookie，尝试 smart_extract_no_restart")
        try:
            all_result = smart_extract_all_cookies_no_restart(
                browser=browser,
                domains=[".qq.com", ".qzone.qq.com", ".i.qq.com"],
            )
            for domain_cookies in all_result.values():
                if isinstance(domain_cookies, dict):
                    # 不覆盖已有值，只补充缺失的
                    for k, v in domain_cookies.items():
                        if k not in merged or not merged[k]:
                            merged[k] = v
        except Exception as exc:
            _log.debug("smart_extract_no_restart 失败: %s", exc)

    if not (merged.get("p_skey") or merged.get("skey")):
        return ""

    # 只保留关键 cookie
    important = ["p_skey", "p_uin", "uin", "skey", "pt2gguin"]
    parts = []
    for key in important:
        if key in merged:
            parts.append(f"{key}={merged[key]}")
    for k, v in merged.items():
        if k not in important:
            parts.append(f"{k}={v}")
    return "; ".join(parts)


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
    for name in ("edge", "chrome", "brave", "chromium", "firefox"):
        if _find_browser_exe(name):
            available.append(name)
    # 保留原有默认顺序，避免旧环境中完全无检测结果时为空
    if "chromium" in available and "chrome" in available:
        # 已有 chrome 时通常不单独展示 chromium，避免 UI 选择过多
        available = [b for b in available if b != "chromium"]
    return available or ["edge", "chrome", "firefox"]


def _pick_browser() -> str:
    """让用户选择浏览器。"""
    available = _detect_installed_browsers()

    print("  可用浏览器:")
    for i, b in enumerate(available):
        marker = " *" if i == 0 else ""
        print(f"    {i + 1}. {_BROWSER_DISPLAY.get(b, b)}{marker}")

    val = _safe_input(f"  选择 [1-{len(available)}，默认 1]: ")
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
        answer = _safe_input(
            f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 后台进程，是否自动关闭后继续提取? (Y/n): "
        ).lower()
        if not answer:
            return True
        return answer not in {"n", "no", "0"}

    answer = _safe_input(
        f"  检测到 {_BROWSER_DISPLAY.get(browser, browser)} 正在运行，是否自动关闭后继续提取? (y/N): "
    ).lower()
    return answer in {"y", "yes", "1"}


def _interactive_prepare_browser_login(platform: str, browser: str) -> bool:
    result = prepare_browser_cookie_login(platform=platform, browser=browser)
    if not result.get("ok"):
        print(f"  {result.get('message', 'Failed to open login page.')}")
        return False

    print(f"  {result.get('message', '')}")
    for idx, line in enumerate(result.get("instructions", []), start=1):
        print(f"    {idx}. {line}")
    for line in result.get("notes", []):
        print(f"    - {line}")
    _safe_input("  Press Enter after scan login is complete and the page looks ready... ")
    return True

def interactive_bilibili_cookie() -> dict[str, str]:
    """交互式获取 B站 cookie。"""
    print("\n  B站 Cookie 获取方式:")
    print("    1. 扫码登录（推荐，最可靠）")
    print("    2. 从浏览器自动提取")
    print("    3. 手动输入")
    print("    4. 跳过")

    choice = _safe_input("  选择 [1-4，默认 1]: ")
    if not choice or choice == "1":
        result = bilibili_qr_login_sync()
        if result and result.get("sessdata"):
            return result
        print("  扫码登录未成功，可选择其他方式。")
        return interactive_bilibili_cookie()

    elif choice == "2":
        browser = _pick_browser()
        result = extract_bilibili_cookies(browser, auto_close=False)
        if result.get("sessdata"):
            print(f"  从 {_BROWSER_DISPLAY.get(browser, browser)} 提取成功!")
            return result
        # 无关闭模式失败，询问是否关闭浏览器重试
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            result = extract_bilibili_cookies(browser, auto_close=True)
            if result.get("sessdata"):
                print(f"  从 {_BROWSER_DISPLAY.get(browser, browser)} 提取成功!")
                return result
        print(f"  未在 {_BROWSER_DISPLAY.get(browser, browser)} 中找到 B站登录信息。")
        print("  请确保已在该浏览器中登录 bilibili.com")
        return {"sessdata": "", "bili_jct": ""}

    elif choice == "3":
        sessdata = _safe_input("  SESSDATA: ")
        bili_jct = _safe_input("  bili_jct: ")
        return {"sessdata": sessdata, "bili_jct": bili_jct}

    return {"sessdata": "", "bili_jct": ""}


def interactive_douyin_cookie() -> str:
    """Interactive Douyin cookie setup."""
    print("\n  Douyin Cookie setup:")
    print("    1. Open official login page -> scan login -> extract (recommended)")
    print("    2. Extract from a browser that is already logged in")
    print("    3. Paste cookie manually")
    print("    4. Skip")

    choice = _safe_input("  Select [1-4, default 1]: ")
    if not choice or choice == "1":
        browser = _pick_browser()
        if not _interactive_prepare_browser_login("douyin", browser):
            return ""
        cookie = extract_douyin_cookie(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_douyin_cookie(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No Douyin login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        return ""

    if choice == "2":
        browser = _pick_browser()
        cookie = extract_douyin_cookie(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_douyin_cookie(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No Douyin login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        return ""

    if choice == "3":
        return _safe_input("  Douyin cookie: ")

    return ""


def interactive_kuaishou_cookie() -> str:
    """Interactive Kuaishou cookie setup."""
    print("\n  Kuaishou Cookie setup:")
    print("    1. Open official login page -> scan login -> extract (recommended)")
    print("    2. Extract from a browser that is already logged in")
    print("    3. Paste cookie manually")
    print("    4. Skip")

    choice = _safe_input("  Select [1-4, default 1]: ")
    if not choice or choice == "1":
        browser = _pick_browser()
        if not _interactive_prepare_browser_login("kuaishou", browser):
            return ""
        cookie = extract_kuaishou_cookie(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_kuaishou_cookie(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No Kuaishou login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        return ""

    if choice == "2":
        browser = _pick_browser()
        cookie = extract_kuaishou_cookie(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_kuaishou_cookie(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No Kuaishou login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        return ""

    if choice == "3":
        return _safe_input("  Kuaishou cookie: ")

    return ""


def interactive_qzone_cookie() -> str:
    """Interactive QZone cookie setup."""
    print("\n  QZone Cookie setup:")
    print("    1. Open official login page -> scan login -> extract (recommended)")
    print("    2. Extract from a browser that is already logged in")
    print("    3. Paste manually: p_skey=xxx; uin=xxx; skey=xxx")
    print("    4. Skip")
    print("\n  Notes:")
    print("    - The browser should open https://qzone.qq.com/ and sign in with your QQ account.")
    print("    - Before extraction, confirm the browser reaches your own QZone home page.")
    print("    - Visiting someone else's QZone page is not enough.")

    choice = _safe_input("  Select [1-4, default 1]: ")
    if not choice or choice == "1":
        browser = _pick_browser()
        if not _interactive_prepare_browser_login("qzone", browser):
            return ""
        cookie = extract_qzone_cookies(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_qzone_cookies(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No QZone login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        print("  Make sure the browser reaches your own QZone home page after signing in.")
        return ""

    if choice == "2":
        browser = _pick_browser()
        cookie = extract_qzone_cookies(browser, auto_close=False)
        if cookie:
            print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
            return cookie
        auto_close = _ask_auto_close_for_running_browser(browser)
        if auto_close:
            cookie = extract_qzone_cookies(browser, auto_close=True)
            if cookie:
                print(f"  Extracted from {_BROWSER_DISPLAY.get(browser, browser)} successfully!")
                return cookie
        print(f"  No QZone login was found in {_BROWSER_DISPLAY.get(browser, browser)}.")
        print("  Make sure the browser reaches your own QZone home page after signing in.")
        return ""

    if choice == "3":
        return _safe_input("  QZone cookie: ")

    return ""


def get_cookie_runtime_capabilities() -> dict[str, Any]:
    """返回当前运行环境的 Cookie 自动提取能力，用于 WebUI 展示。"""
    system = platform.system().lower()
    if system == "darwin":
        system = "macos"

    def _has_module(module_name: str) -> bool:
        try:
            return importlib.util.find_spec(module_name) is not None
        except Exception:
            return False

    installed_browsers = [b for b in ("edge", "chrome", "brave", "chromium", "firefox") if _find_browser_exe(b)]
    recommended_browser = "firefox" if "firefox" in installed_browsers else (installed_browsers[0] if installed_browsers else "")

    cdp_supported_browsers = [b for b in ("edge", "chrome", "brave", "chromium") if b in installed_browsers]
    browser_cookie3_ok = _has_module("browser_cookie3")
    rookiepy_ok = _has_module("rookiepy")
    bilibili_qr_ok = _has_module("bilibili_api")

    notices: list[str] = []
    if system != "windows":
        notices.append(
            "Linux/macOS 下浏览器 Cookie 解密能力受系统密钥链和浏览器版本影响，自动提取可能失败。"
        )
    if not rookiepy_ok and not browser_cookie3_ok:
        notices.append("未检测到 rookiepy/browser_cookie3，浏览器自动提取能力有限。")
    if not bilibili_qr_ok:
        notices.append("未检测到 bilibili-api-python，B站扫码登录不可用。")

    browser_extract_ok = bool(installed_browsers and (rookiepy_ok or browser_cookie3_ok))
    browser_scan_login_ok = bool(installed_browsers)
    return {
        "os": system,
        "python_platform": sys.platform,
        "modules": {
            "rookiepy": rookiepy_ok,
            "browser_cookie3": browser_cookie3_ok,
            "bilibili_api": bilibili_qr_ok,
        },
        "browsers": {
            "installed": installed_browsers,
            "recommended": recommended_browser,
            "cdp_supported": cdp_supported_browsers,
            "scan_login_supported": installed_browsers,
        },
        "platforms": {
            "bilibili": {
                "qr_scan": bilibili_qr_ok,
                "browser_extract": browser_extract_ok,
                "browser_scan_login": browser_scan_login_ok,
            },
            "douyin": {"browser_extract": browser_extract_ok, "browser_scan_login": browser_scan_login_ok},
            "kuaishou": {"browser_extract": browser_extract_ok, "browser_scan_login": browser_scan_login_ok},
            "qzone": {"browser_extract": browser_extract_ok, "browser_scan_login": browser_scan_login_ok},
        },
        "notices": notices,
    }


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


async def check_qzone_cookie(cookie_str: str) -> bool:
    """验证 QZone cookie 是否有效（尝试获取自己的资料）。"""
    if not cookie_str:
        return False
    from core.qzone import QZoneClient, parse_cookie_string
    cookies = parse_cookie_string(cookie_str)
    if not (cookies.get("p_skey") or cookies.get("skey")):
        return False
    try:
        client = QZoneClient(cookies, timeout=10)
        if not client.self_uin:
            return False
        profile = await client.get_profile(client.self_uin)
        return bool(profile.nickname)
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

    # QQ空间
    qz_cfg = va.get("qzone", {}) or {}
    qz_cookie = str(qz_cfg.get("cookie", "")).strip()
    if qz_cookie:
        results["qzone"] = await check_qzone_cookie(qz_cookie)
        status = "有效" if results["qzone"] else "已失效"
        _log.info("cookie_check | qzone | %s", status)
    else:
        _log.info("cookie_check | qzone | 未配置")

    return results
