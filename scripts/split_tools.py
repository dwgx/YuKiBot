"""自动拆分 tools.py 的模块级辅助函数为独立文件。

ToolExecutor 类内部的方法暂不拆分（需要 mixin 方案，风险更高）。
先把类外的、以及不依赖 self 的模块级辅助函数提取出去。

运行方式: python scripts/split_tools.py
"""
from __future__ import annotations
import re
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "core"
SRC = CORE / "tools.py"
lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)
total = len(lines)
print(f"[split_tools] 读取 {SRC.name}: {total} 行")

# ── Step 1: 提取 ToolResult 和模块级 helpers 到 tools_types.py ──
# ToolResult (L127-L205) + 模块级函数 (L62-126, L135-204)
# ToolResult + _SilentYTDLPLogger + _find_ffmpeg + _write_netscape_cookie_file + _tool_trace_tag + _prompt_cues

# 找 ToolExecutor 类的起始行
class_start = None
for i, line in enumerate(lines, 1):
    if line.startswith("class ToolExecutor:"):
        class_start = i
        break

print(f"[split_tools] ToolExecutor 类起始行: {class_start}")

# 提取类之前的所有内容作为 tools_types.py 的内容
pre_class_content = "".join(lines[:class_start - 1])

# 写入 tools_types.py
types_header = '''"""tools.py 公共类型和辅助函数。

从 core/tools.py 拆分。包含:
- ToolResult: 工具执行结果
- _SilentYTDLPLogger: yt-dlp 静默日志
- _find_ffmpeg: FFmpeg 定位
- _write_netscape_cookie_file: cookie 文件写入
- _tool_trace_tag / _prompt_cues: 工具追踪辅助
"""
'''

types_path = CORE / "tools_types.py"
types_path.write_text(types_header + pre_class_content, encoding="utf-8")
types_lines = (types_header + pre_class_content).count("\n") + 1
print(f"[split_tools] 写入 tools_types.py: {types_lines} 行")

# ── Step 2: 提取 ToolExecutor 类内的纯辅助方法到 tools_helpers.py ──
# 这些方法虽然在类内，但实际上是 staticmethod 或只用参数不用 self
# 暂时不动——太危险了，等下一轮做 mixin

# ── Step 3: 修改 tools.py，让它从 tools_types import ──
# 替换类之前的内容为 import 语句

# 找到原始的 import 区域结束位置（在 _tool_trace_tag 定义之前）
# 我们需要保留原始 imports + 从 tools_types 重新导入

# 简单做法：保留 tools.py 原样，但在头部加一个 re-export
# 更好的做法：把 pre-class 内容替换为 import

# 获取原始 imports (从文件头到第一个非-import/非-空行)
import_end = 0
for i, line in enumerate(lines):
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("import ") or \
       stripped.startswith("from ") or stripped.startswith('"""') or stripped.startswith("'''") or \
       stripped.startswith("_TOOLS_") or stripped.startswith("_tool_log"):
        import_end = i + 1
    elif stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("@"):
        # 遇到第一个定义，停止
        break
    else:
        import_end = i + 1

# 构建新的 tools.py
new_header = "".join(lines[:import_end])

# 追加 from tools_types import
new_header += "\n# ── Re-imports from tools_types (拆分后兼容) ──\n"
new_header += "from core.tools_types import (  # noqa: F401\n"
new_header += "    ToolResult,\n"
new_header += "    _find_ffmpeg,\n"
new_header += "    _SilentYTDLPLogger,\n"
new_header += "    _tool_trace_tag,\n"
new_header += "    _prompt_cues,\n"
new_header += "    _write_netscape_cookie_file,\n"
new_header += ")\n\n"

# 把 ToolExecutor 类及之后的内容追加
new_body = "".join(lines[class_start - 1:])
new_content = new_header + new_body

# 备份 + 写入
import shutil
backup = CORE / "tools.py.bak"
shutil.copy2(SRC, backup)
SRC.write_text(new_content, encoding="utf-8")
new_line_count = new_content.count("\n") + 1
print(f"[split_tools] tools.py 更新: {new_line_count} 行 (原 {total} 行)")
print(f"[split_tools] 原文件备份到 {backup.name}")

# ── Step 4: 确保外部 `from core.tools import ToolResult` 依然可用 ──
# ToolResult 现在在 tools_types.py 中定义，但 tools.py 通过 `from core.tools_types import ToolResult` 重新导入
# 所以所有 `from core.tools import ToolResult` 依然正常

print("[split_tools] 完成！请运行 pytest tests/ 验证。")
