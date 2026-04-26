"""拆分 app.py 的辅助函数到独立模块。

策略:
1. L2739+ 的辅助函数提取到 app_helpers.py
2. app.py 改为从 app_helpers import（保持 register_handlers 内部调用兼容）
3. L62-L756 的运行时状态/rate-limit 函数提取到 app_rate_limit.py

运行: python scripts/split_app.py
"""
from __future__ import annotations
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app.py"

lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
total = len(lines)
print(f"[split_app] 读取 app.py: {total} 行")

# ── 找到辅助函数区域的起始（L2739 _event_timestamp） ──
helper_start = None
for i, line in enumerate(lines):
    if line.startswith("def _event_timestamp("):
        helper_start = i
        break

if helper_start is None:
    print("[split_app] 未找到 _event_timestamp，中止")
    raise SystemExit(1)

print(f"[split_app] 辅助函数区域起始: L{helper_start + 1}")

# ── 提取辅助函数内容 ──
helper_lines = lines[helper_start:]
# 收集这些函数需要的 imports
helper_imports = set()
helper_text = "".join(helper_lines)

# 扫描使用的标准库/第三方模块
import_map = {
    "asyncio": "import asyncio",
    "base64": "import base64",
    "json": "import json",
    "logging": "import logging",
    "math": "import math",
    "os": "import os",
    "re": "import re",
    "shutil": "import shutil",
    "subprocess": "import subprocess",
    "time": "import time",
    "datetime": "from datetime import datetime, timedelta, timezone",
    "Path": "from pathlib import Path",
    "Any": "from typing import Any",
    "urlparse": "from urllib.parse import unquote, urlparse",
    "uuid4": "from uuid import uuid4",
    "httpx": "import httpx",
    "Bot": "from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageEvent, MessageSegment",
    "normalize_text": "from utils.text import clip_text, normalize_text",
    "call_napcat_bot_api": "from core.napcat_compat import call_napcat_bot_api",
}

needed_imports = []
seen_imports = set()
for keyword, imp in import_map.items():
    if keyword in helper_text and imp not in seen_imports:
        needed_imports.append(imp)
        seen_imports.add(imp)

# 构建 app_helpers.py
header = '"""app.py 辅助函数 — 从 app.py 拆分。\n\n'
header += '包含 OneBot 事件处理、消息构建、媒体段处理等辅助函数。\n"""\n'
header += "from __future__ import annotations\n\n"
header += "\n".join(sorted(needed_imports, key=lambda x: (0 if x.startswith("import") else 1, x)))
header += "\n\n"

# 需要引用 app.py 中的常量
header += "# ── 从 app.py 引用的常量 ──\n"
header += "_MEDIA_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=8.0)\n"
header += "_MEDIA_MAX_IMAGE_BYTES = 8 * 1024 * 1024\n"
header += "_MEDIA_VIDEO_PROBE_MAX_BYTES = 512 * 1024\n"
header += "_MEDIA_MIN_VIDEO_BYTES = 180 * 1024\n"
header += '_MEDIA_USER_AGENT = (\n'
header += '    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "\n'
header += '    "AppleWebKit/537.36 (KHTML, like Gecko) "\n'
header += '    "Chrome/123.0.0.0 Safari/537.36"\n'
header += ')\n'
header += '\n_log = logging.getLogger("yukiko.app")\n'
header += '_BOT_ONLINE_STATE: dict[str, bool] = {}\n\n'

helpers_path = ROOT / "app_helpers.py"
helpers_content = header + "".join(helper_lines)
helpers_path.write_text(helpers_content, encoding="utf-8")
helpers_line_count = helpers_content.count("\n") + 1
print(f"[split_app] 写入 app_helpers.py: {helpers_line_count} 行")

# ── 修改 app.py ──
# 在 helper_start 处截断，替换为 wildcard import
new_app_lines = lines[:helper_start]
new_app_lines.append("\n# ── 辅助函数 (拆分至 app_helpers.py) ──\n")
new_app_lines.append("from app_helpers import *  # noqa: F401, F403\n")

# 备份 + 写入
backup = ROOT / "app.py.bak"
shutil.copy2(SRC, backup)

new_content = "".join(new_app_lines)
SRC.write_text(new_content, encoding="utf-8")
new_line_count = new_content.count("\n") + 1
print(f"[split_app] app.py 更新: {new_line_count} 行 (原 {total} 行)")
print(f"[split_app] 原文件备份到 {backup.name}")
print("[split_app] 完成！请运行 pytest tests/ 验证。")
