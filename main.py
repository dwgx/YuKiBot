from __future__ import annotations

from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from dotenv import load_dotenv

from app import create_engine, register_handlers


def _load_env_files() -> None:
    root = Path(__file__).resolve().parent
    # 显式加载环境变量，供配置解析使用
    load_dotenv(root / ".env", override=False, encoding="utf-8")
    load_dotenv(root / ".env.prod", override=False, encoding="utf-8")


_load_env_files()
nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

engine = create_engine()
register_handlers(engine)
app = nonebot.get_asgi()


if __name__ == "__main__":
    nonebot.run()
