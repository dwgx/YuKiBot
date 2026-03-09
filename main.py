from __future__ import annotations

import io
import os
import sys

# Windows GBK 环境下强制 UTF-8，防止中文日志/消息变成 ????
os.environ.setdefault("PYTHONUTF8", "1")
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from pathlib import Path

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from app import create_engine, register_handlers
from core.setup import needs_setup, run as run_setup


def _load_env_files() -> None:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", override=False, encoding="utf-8")
    load_dotenv(root / ".env.prod", override=False, encoding="utf-8")


_load_env_files()

# 首次运行向导：
#   config.yml 不存在 → 启动 WebUI 配置页面
#   python main.py --setup → 强制 CLI 向导
#   python main.py setup   → 强制 CLI 向导
_force_cli_setup = "--setup" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "setup")
if _force_cli_setup:
    run_setup()
    sys.exit(0)
elif needs_setup():
    # WebUI 配置向导模式
    _webui_dist = Path(__file__).resolve().parent / "webui" / "dist"
    if _webui_dist.is_dir():
        from core.webui import run_setup_server
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8081"))
        run_setup_server(host=host, port=port)
        # setup server 退出后检查是否已生成 config
        if not needs_setup():
            print("配置已完成，setup 模式结束。")
            sys.exit(0)
        else:
            print("配置未完成，退出。")
            sys.exit(0)
    else:
        # webui/dist 不存在，回退到 CLI 向导
        print("WebUI 未构建，使用 CLI 向导...")
        run_setup()
        sys.exit(0)

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

engine = create_engine()
register_handlers(engine)

# WebUI 管理面板 API
from core.webui import init_webui
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse, RedirectResponse, Response

app = nonebot.get_asgi()
app.include_router(init_webui(engine))

# SPA 静态文件 + 路由回退
_webui_dist = Path(__file__).resolve().parent / "webui" / "dist"
_webui_index = _webui_dist / "index.html"
_webui_assets = _webui_dist / "assets"
if _webui_assets.is_dir():
    app.mount("/webui/assets", StaticFiles(directory=str(_webui_assets)), name="webui-assets")


def _webui_missing_response() -> Response:
    return Response(
        (
            "WebUI 静态页面未构建。请先执行前端构建后重启服务：\n"
            "Windows: build-webui.bat\n"
            "Linux/macOS: bash build-webui.sh\n"
            "或手动执行: cd webui && npm install && npm run build"
        ),
        status_code=503,
        media_type="text/plain; charset=utf-8",
    )


@app.get("/webui/{path:path}")
async def _webui_spa(path: str):
    if ".." in path:
        return Response("Not found", status_code=404)
    if path.lower().startswith("setup"):
        if _webui_index.exists():
            return RedirectResponse(url="/webui/", status_code=307)
        return _webui_missing_response()
    fp = _webui_dist / path
    if fp.is_file():
        return FileResponse(fp)
    if _webui_index.exists():
        return FileResponse(_webui_index)
    return _webui_missing_response()


@app.get("/webui")
async def _webui_root():
    if _webui_index.exists():
        return FileResponse(_webui_index)
    return _webui_missing_response()


@app.get("/login")
async def _login_alias():
    return RedirectResponse(url="/webui/login", status_code=307)


@app.get("/")
async def _root_redirect():
    if _webui_index.exists():
        return RedirectResponse(url="/webui/", status_code=307)
    return _webui_missing_response()


if __name__ == "__main__":
    nonebot.run()
