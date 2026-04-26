"""自动拆分 agent_tools.py 为多个子模块的脚本。

运行方式: python scripts/split_agent_tools.py
"""
from __future__ import annotations
import re
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "core"
SRC = CORE / "agent_tools.py"

# ── 读取原文件 ──
lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
total = len(lines)
print(f"[split] 读取 {SRC.name}: {total} 行")


def extract(start: int, end: int) -> str:
    """提取 start..end (1-indexed, inclusive) 行。"""
    return "".join(lines[start - 1 : end])


# ── 确定各 _register_ 函数的行号 ──
register_funcs: list[tuple[int, str]] = []
for i, line in enumerate(lines, 1):
    m = re.match(r"^def (_register_\w+|register_\w+)\(", line)
    if m:
        register_funcs.append((i, m.group(1)))

print(f"[split] 找到 {len(register_funcs)} 个注册函数:")
for ln, name in register_funcs:
    print(f"  L{ln}: {name}")


# ── 定义拆分映射 ──
# 每个子模块需要哪些 _register_ 函数，以及额外需要的 import
SHARED_IMPORTS = '''\
"""Auto-split from core/agent_tools.py — {desc}"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from core.agent_tools_types import PromptHint, ToolCallResult, ToolSchema
from core.agent_tools_registry import AgentToolRegistry
from core.napcat_compat import call_napcat_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning, looks_like_preferred_name_knowledge
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")

'''

# 子模块定义: (filename, description, list of register function names)
MODULES = [
    ("agent_tools_napcat.py", "NapCat OneBot V11 API 工具",
     ["_register_napcat_tools", "_register_napcat_extended_tools"]),
    ("agent_tools_search.py", "搜索工具",
     ["_register_search_tools"]),
    ("agent_tools_media.py", "媒体工具（图片/视频/音乐/语音）",
     ["_register_media_tools"]),
    ("agent_tools_admin.py", "管理员工具",
     ["_register_admin_tools"]),
    ("agent_tools_utility.py", "实用工具 (final_answer, think) + 表情系统",
     ["_register_utility_tools"]),
    ("agent_tools_memory.py", "记忆管理工具",
     ["_register_memory_tools"]),
    ("agent_tools_knowledge.py", "知识库 + 爬虫工具",
     ["_register_crawler_tools"]),
    ("agent_tools_social.py", "社交功能工具 (日报/QZone)",
     ["_register_daily_report_tools", "_register_qzone_tools"]),
    ("agent_tools_web.py", "AI Method / 爬虫 LLM 工具",
     ["_register_ai_method_tools", "_register_scrapy_llm_tools"]),
]

# register_sticker_tools 是公共函数，需要特殊处理
# 它在 _register_utility_tools 之后、_register_memory_tools 之前
# 查找它的位置
sticker_register_line = None
for ln, name in register_funcs:
    if name == "register_sticker_tools":
        sticker_register_line = ln
        break

# 构建行号范围映射
func_line_map: dict[str, int] = {name: ln for ln, name in register_funcs}

# 找每个函数的结束行(下一个同级函数的起始行-1，或文件末尾)
# 额外收集所有顶层函数/类的行号
toplevel_starts: list[int] = []
for i, line in enumerate(lines, 1):
    if re.match(r"^(def |async def |class )", line):
        toplevel_starts.append(i)
toplevel_starts.append(total + 1)  # sentinel
toplevel_starts.sort()


def find_section_end(start_line: int) -> int:
    """找 start_line 所在顶层函数之后，到下一个 _register_* 函数之前的所有代码。"""
    # 需要包含该 register 函数以及它注册的所有 handler
    idx = toplevel_starts.index(start_line) if start_line in toplevel_starts else -1
    if idx < 0:
        # fallback: 搜索最近的
        for j, s in enumerate(toplevel_starts):
            if s >= start_line:
                idx = j
                break
    # 查找这个 register 函数之后的下一个 register 函数
    all_register_lines = sorted(func_line_map.values())
    next_register = total + 1
    for rl in all_register_lines:
        if rl > start_line:
            next_register = rl
            break
    return next_register - 1


# ── 执行拆分 ──
for filename, desc, func_names in MODULES:
    # 收集该模块的所有行
    sections: list[str] = []
    for fn in func_names:
        if fn not in func_line_map:
            print(f"  [WARN] {fn} not found, skipping")
            continue
        start = func_line_map[fn]
        end = find_section_end(start)
        section_text = extract(start, end)
        sections.append(section_text)

    # 如果是 utility 模块，还要包含 register_sticker_tools
    if filename == "agent_tools_utility.py" and sticker_register_line:
        sticker_end = find_section_end(sticker_register_line)
        # sticker tools 区域在 utility 和 memory 之间
        # 包含从 _register_utility_tools 到 _register_memory_tools 之前的全部内容
        utility_start = func_line_map["_register_utility_tools"]
        memory_start = func_line_map["_register_memory_tools"]
        # 重写: 直接取 utility_start 到 memory_start-1 的所有内容
        sections = [extract(utility_start, memory_start - 1)]

    # 额外: 为 media 模块添加 image_gen import
    extra_imports = ""
    if filename == "agent_tools_media.py":
        extra_imports = """from core.image_gen import (
    IMAGE_PROMPT_BLOCKED_MESSAGE,
    assess_prompt_qq_ban_risk,
    detect_custom_prompt_risk_reason,
    detect_qq_ban_risk_reason,
)

"""

    content = SHARED_IMPORTS.format(desc=desc) + extra_imports + "\n".join(sections)
    out_path = CORE / filename
    out_path.write_text(content, encoding="utf-8")
    line_count = content.count("\n") + 1
    print(f"[split] 写入 {filename}: {line_count} 行")


# ── 生成 re-export hub ──
# 扫描所有子模块的公共符号（以 _ 开头的也要 re-export 因为测试直接 import 它们）
print("\n[split] 生成 re-export hub...")

# 收集所有被外部 import 的符号
# 从之前的 grep 结果我们知道测试代码 import 了这些:
KNOWN_EXTERNAL_SYMBOLS = {
    # types
    "ToolSchema", "ToolCallResult", "PromptHint", "ToolHandler", "ContextProvider",
    # registry
    "AgentToolRegistry", "register_builtin_tools",
    # sticker
    "register_sticker_tools", "_handle_send_emoji", "_make_learn_sticker_handler",
    # napcat
    "_napcat_api_call", "_handle_send_group_message", "_handle_set_group_ban",
    "_build_onebot_message_segments", "_handle_recall_recent_messages",
    "_handle_get_group_member_list", "_handle_get_group_info", "_handle_get_user_info",
    "_handle_get_message", "_handle_delete_message", "_handle_send_private_message",
    "_handle_set_group_card", "_handle_set_group_kick", "_handle_set_group_special_title",
    "_handle_get_group_honor_info", "_handle_upload_group_file",
    "_handle_get_group_notice", "_handle_send_group_notice",
    "_handle_get_friend_list", "_handle_get_group_list", "_handle_send_like",
    "_handle_set_group_whole_ban", "_handle_set_group_admin", "_handle_set_group_sign",
    "_handle_set_group_name", "_handle_get_login_info", "_handle_forward_message",
    "_handle_get_group_history", "_handle_get_chat_history",
    "_handle_get_group_files", "_handle_get_group_file_url",
    "_handle_get_muted_list", "_handle_generic_napcat_api",
    # media
    "_handle_analyze_local_video", "_handle_analyze_voice", "_make_image_gen_handler",
    # knowledge
    "_handle_learn_knowledge", "_handle_search_knowledge",
    # admin
    "_handle_smart_download",
    # social
    "_make_qzone_handler",
    # memory
    "_handle_memory_list",
    # web
    "_handle_fetch_webpage", "_handle_github_search", "_handle_github_readme",
    # utility
    "_handle_final_answer", "_handle_think",
}

hub = '''\
"""Agent 工具注册表 — re-export hub。

原始代码已拆分为以下子模块:
- agent_tools_types: 公共类型 (ToolSchema, ToolCallResult, PromptHint)
- agent_tools_registry: AgentToolRegistry + register_builtin_tools
- agent_tools_napcat: NapCat OneBot V11 API 工具
- agent_tools_search: 搜索工具
- agent_tools_media: 媒体工具
- agent_tools_admin: 管理员工具
- agent_tools_utility: 实用工具 + 表情系统
- agent_tools_memory: 记忆管理工具
- agent_tools_knowledge: 知识库 + 爬虫工具
- agent_tools_social: 社交功能工具
- agent_tools_web: AI Method / 爬虫 LLM 工具

所有 `from core.agent_tools import X` 保持向后兼容。
"""
# ── Types ──
from core.agent_tools_types import (  # noqa: F401
    ContextProvider,
    PromptHint,
    ToolCallResult,
    ToolHandler,
    ToolSchema,
)

# ── Registry ──
from core.agent_tools_registry import (  # noqa: F401
    AgentToolRegistry,
    register_builtin_tools,
)

# ── NapCat handlers (re-exported for test compatibility) ──
from core.agent_tools_napcat import (  # noqa: F401
    _build_onebot_message_segments,
    _handle_send_group_message,
    _handle_send_private_message,
    _napcat_api_call,
    _handle_generic_napcat_api,
    _handle_get_group_member_list,
    _handle_get_group_info,
    _handle_get_user_info,
    _handle_get_message,
    _handle_delete_message,
    _handle_recall_recent_messages,
    _handle_set_group_ban,
    _handle_set_group_card,
    _handle_set_group_kick,
    _handle_set_group_special_title,
    _handle_get_group_honor_info,
    _handle_upload_group_file,
    _handle_get_group_notice,
    _handle_send_group_notice,
    _handle_get_friend_list,
    _handle_get_group_list,
    _handle_send_like,
    _handle_set_group_whole_ban,
    _handle_set_group_admin,
    _handle_set_group_sign,
    _handle_set_group_name,
    _handle_get_login_info,
    _handle_forward_message,
    _handle_get_group_history,
    _handle_get_chat_history,
    _handle_get_group_files,
    _handle_get_group_file_url,
    _handle_get_muted_list,
)

# ── Utility + Sticker ──
from core.agent_tools_utility import (  # noqa: F401
    _handle_final_answer,
    _handle_think,
    register_sticker_tools,
    _handle_send_emoji,
    _make_learn_sticker_handler,
)

# ── Media ──
try:
    from core.agent_tools_media import (  # noqa: F401
        _handle_analyze_local_video,
        _handle_analyze_voice,
        _make_image_gen_handler,
    )
except ImportError:
    pass

# ── Memory ──
from core.agent_tools_memory import _register_memory_tools  # noqa: F401

# ── Knowledge ──
from core.agent_tools_knowledge import (  # noqa: F401
    _handle_learn_knowledge,
    _handle_search_knowledge,
)

# ── Social ──
from core.agent_tools_social import (  # noqa: F401
    _register_daily_report_tools,
    _register_qzone_tools,
    _make_qzone_handler,
)

# ── Web ──
from core.agent_tools_web import (  # noqa: F401
    _register_ai_method_tools,
    _register_scrapy_llm_tools,
)
'''

# 备份原文件
backup = CORE / "agent_tools.py.bak"
import shutil
shutil.copy2(SRC, backup)
print(f"[split] 原文件备份到 {backup.name}")

# 写入 re-export hub
SRC.write_text(hub, encoding="utf-8")
print(f"[split] agent_tools.py 已替换为 re-export hub ({hub.count(chr(10))+1} 行)")
print("[split] 完成！请运行 pytest tests/ 验证。")
