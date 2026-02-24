from __future__ import annotations

import io
import os
import sys

# Windows GBK 环境下强制 UTF-8，防止中文日志/消息变成 ????
os.environ.setdefault("PYTHONUTF8", "1")
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from pathlib import Path

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from app import create_engine, register_handlers
from core.setup import needs_setup, run as run_setup


def _load_env_files() -> None:
    root = Path(__file__).resolve().parent
    # 显式加载环境变量，供配置解析使用
    load_dotenv(root / ".env", override=False, encoding="utf-8")
    load_dotenv(root / ".env.prod", override=False, encoding="utf-8")


_load_env_files()

# 首次运行向导：
#   config.yml 不存在 → 自动触发
#   python main.py --setup → 强制触发
#   python main.py setup   → 强制触发
_force_setup = "--setup" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "setup")
if needs_setup() or _force_setup:
    run_setup()

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

engine = create_engine()
register_handlers(engine)
app = nonebot.get_asgi()


if __name__ == "__main__":
    nonebot.run()
