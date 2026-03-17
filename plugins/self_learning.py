"""SelfLearning 插件 - 让 Agent 自我学习、自我编写技能、自我测试。

功能:
- 让 Agent 在网上学习新知识
- 自己写代码实现新功能
- 给自己编写新的 SKILL 工具
- 在沙盒环境测试代码
- 在群里发送 DEVLOG 日志（白话文）

版本: 1.0.0
作者: YuKiKo Team
许可: MIT
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from plugins.self_learning_runtime import BaseCodeExecutionBackend, create_code_execution_backend
from utils.text import normalize_text

_log = logging.getLogger("yukiko.plugin.self_learning")

# 版本信息
__version__ = "1.0.0"
__author__ = "YuKiKo Team"
__license__ = "MIT"

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "storage" / "self_created_skills"
_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "storage" / "sandbox"
_SAFE_IMPORT_MODULES = frozenset({
    "collections",
    "datetime",
    "decimal",
    "fractions",
    "functools",
    "heapq",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "string",
    "textwrap",
})
_BLOCKED_IMPORT_MODULES = frozenset({
    "builtins",
    "ctypes",
    "importlib",
    "io",
    "os",
    "pathlib",
    "resource",
    "shutil",
    "signal",
    "socket",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "multiprocessing",
})
_BLOCKED_CALL_NAMES = frozenset({
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "vars",
})
_BLOCKED_ATTR_NAMES = frozenset({
    "check_call",
    "check_output",
    "chmod",
    "chown",
    "execve",
    "hardlink_to",
    "kill",
    "mkdir",
    "open",
    "popen",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "run",
    "spawn",
    "symlink_to",
    "system",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
})
_BLOCKED_PATH_SNIPPETS = (
    "..\\",
    "../",
    "/etc/",
    "/proc/",
    "/sys/",
    "c:\\",
    "\\\\",
)


def _safe_input(prompt: str, default: str = "") -> str:
    """安全的 input，处理 KeyboardInterrupt 和 EOFError。"""
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return input().strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  [已取消配置]")
        return default
    except Exception:
        return default


def _stdin_is_tty() -> bool:
    try:
        stdin = getattr(sys, "stdin", None)
        return bool(stdin and hasattr(stdin, "isatty") and stdin.isatty())
    except Exception:
        return False


def _default_plugin_config(enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "allow_code_execution": False,
        "acknowledge_unsafe_execution": False,
        "execution_backend": "disabled",
        "super_admin_only": True,
        "sandbox_mode": "isolated",
        "auto_test": False,
        "devlog_broadcast": True,
        "learning_source": "both",
        "save_skills": False,
        "max_learning_time_seconds": 300,
        "max_code_lines": 500,
        "test_timeout_seconds": 15,
        "devlog_cooldown_seconds": 30,
    }


@dataclass
class LearningSession:
    """学习会话记录

    用于跟踪 Agent 的学习过程，包括学习内容、编写的代码、测试结果等。

    Attributes:
        session_id: 会话唯一标识符
        topic: 学习主题
        start_time: 开始时间
        end_time: 结束时间（可选）
        status: 当前状态 (learning/testing/completed/failed)
        learned_content: 学习到的内容
        code_written: 编写的代码
        test_results: 测试结果列表
        devlog: 开发日志列表
        metrics: 性能指标
    """
    session_id: str
    topic: str
    start_time: datetime
    end_time: datetime | None = None
    status: str = "learning"
    learned_content: str = ""
    code_written: str = ""
    test_results: list[str] = field(default_factory=list)
    devlog: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def duration(self) -> float:
        """计算会话持续时间（秒）"""
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "session_id": self.session_id,
            "topic": self.topic,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "status": self.status,
            "duration_seconds": self.duration(),
            "metrics": self.metrics,
        }


class Plugin:
    name = "self_learning"
    description = "Agent 自我学习系统: 让 AI 自己学习、写代码、创建技能、测试和改进。"
    agent_tool = True
    internal_only = True
    rules: list[str] = []
    args_schema: dict[str, str] = {}

    # ── 首次配置向导 ──

    @staticmethod
    def needs_setup() -> bool:
        """配置文件不存在时需要交互式初始化。"""
        return not (_CONFIG_DIR / "self_learning.yml").exists() and _stdin_is_tty()

    @staticmethod
    def interactive_setup() -> dict[str, Any]:
        """交互式向导，生成 plugins/config/self_learning.yml。"""
        print("\n┌─ SelfLearning 插件配置 ─┐")

        # 1. 启用?
        ans = _safe_input("  启用自我学习功能? (y/N): ", "n").lower()
        if ans in ("n", "no"):
            cfg = _default_plugin_config(enabled=False)
            _write_plugin_config(cfg)
            print("  self_learning 已禁用。")
            return cfg

        print("\n  [安全提示]")
        print("    当前跨平台实现不是强隔离沙盒，不能把它当成安全边界。")
        print("    只有在你明确理解风险、且仅执行受信任代码时才应开启代码执行。")
        allow_code_execution = _safe_input("  允许执行自生成 Python 代码? (y/N): ", "n").lower() in ("y", "yes")
        acknowledge_unsafe = False
        if allow_code_execution:
            acknowledge_unsafe = _safe_input("  我已知晓“非强隔离，仅限受信任代码”风险? (y/N): ", "n").lower() in ("y", "yes")
            if not acknowledge_unsafe:
                allow_code_execution = False
                print("  未确认风险，已自动关闭代码执行。")

        # 2. 沙盒模式（仅在显式允许执行代码时保留）
        print("\n  沙盒模式:")
        print("    1. isolated - 最佳努力隔离（非强沙盒）")
        print("    2. restricted - 受限运行（非强沙盒）")
        print("    3. full - 完全访问（危险，仅用于受信任开发）")
        ans = _safe_input("  选择 [1-3，默认 1]: ", "1")
        sandbox_mode = {"1": "isolated", "2": "restricted", "3": "full"}.get(ans, "isolated")

        # 3. 自动测试
        print("\n  自动测试:")
        print("    开启后 Agent 编写代码会自动在沙盒中测试")
        ans = _safe_input("  启用自动测试? (y/N): ", "n").lower()
        auto_test = allow_code_execution and ans in ("y", "yes")

        super_admin_only = _safe_input("  危险功能仅允许 super_admin 使用? (Y/n): ", "y").lower() not in ("n", "no")

        # 4. DEVLOG 广播
        print("\n  DEVLOG 广播:")
        print("    开启后 Agent 会在群里发送学习日志（白话文）")
        ans = _safe_input("  启用 DEVLOG 广播? (Y/n): ", "y").lower()
        devlog_broadcast = ans not in ("n", "no")

        # 5. 学习源
        print("\n  学习源:")
        print("    1. web - 从网络搜索学习（需要搜索工具）")
        print("    2. docs - 从文档学习")
        print("    3. both - 两者都用（默认）")
        ans = _safe_input("  选择 [1-3，默认 3]: ", "3")
        learning_source = {"1": "web", "2": "docs", "3": "both"}.get(ans, "both")

        # 6. 技能保存
        print("\n  技能保存:")
        print("    开启后成功的技能会自动保存并可重用")
        ans = _safe_input("  启用技能保存? (y/N): ", "n").lower()
        save_skills = allow_code_execution and ans in ("y", "yes")

        cfg = _default_plugin_config(enabled=True)
        cfg.update({
            "allow_code_execution": allow_code_execution,
            "acknowledge_unsafe_execution": acknowledge_unsafe,
            "execution_backend": "local_subprocess" if allow_code_execution and acknowledge_unsafe else "disabled",
            "super_admin_only": super_admin_only,
            "sandbox_mode": sandbox_mode,
            "auto_test": auto_test,
            "devlog_broadcast": devlog_broadcast,
            "learning_source": learning_source,
            "save_skills": save_skills,
        })
        _write_plugin_config(cfg)
        print(f"  配置已保存到 plugins/config/self_learning.yml")
        print("└──────────────────────┘\n")
        return cfg

    def __init__(self) -> None:
        """初始化插件实例"""
        # 配置参数
        self._enabled: bool = False
        self._sandbox_mode: str = "isolated"
        self._allow_code_execution: bool = False
        self._acknowledge_unsafe_execution: bool = False
        self._execution_backend_name: str = "disabled"
        self._super_admin_only: bool = True
        self._auto_test: bool = True
        self._devlog_broadcast: bool = True
        self._learning_source: str = "both"
        self._save_skills: bool = True
        self._max_learning_time: int = 300
        self._max_code_lines: int = 500
        self._test_timeout: int = 60
        self._devlog_cooldown: int = 30

        # 运行时状态
        self._registry: Any = None
        self._sessions: dict[str, LearningSession] = {}
        self._last_devlog_time: float = 0
        self._api_call: Any = None
        self._code_runner: BaseCodeExecutionBackend = create_code_execution_backend(
            "disabled",
            sandbox_root=_SANDBOX_DIR,
        )

        # 性能统计
        self._stats = {
            "total_sessions": 0,
            "successful_skills": 0,
            "failed_skills": 0,
            "total_tests": 0,
            "passed_tests": 0,
            "devlogs_sent": 0,
        }

        # 技能缓存
        self._skill_cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded: bool = False

    async def setup(self, config: dict[str, Any], context: Any) -> None:
        if not config.get("enabled", False):
            _log.info("self_learning disabled")
            return

        self._enabled = True
        self._sandbox_mode = str(config.get("sandbox_mode", "isolated"))
        self._allow_code_execution = bool(config.get("allow_code_execution", False))
        self._acknowledge_unsafe_execution = bool(
            config.get("acknowledge_unsafe_execution", False)
        )
        backend_name = normalize_text(str(config.get("execution_backend", ""))).lower()
        if not backend_name:
            backend_name = (
                "local_subprocess"
                if self._allow_code_execution and self._acknowledge_unsafe_execution
                else "disabled"
            )
        self._execution_backend_name = backend_name
        self._super_admin_only = bool(config.get("super_admin_only", True))
        self._auto_test = bool(config.get("auto_test", False))
        self._devlog_broadcast = bool(config.get("devlog_broadcast", True))
        self._learning_source = str(config.get("learning_source", "both"))
        self._save_skills = bool(config.get("save_skills", False))
        self._max_learning_time = int(config.get("max_learning_time_seconds", 300))
        self._max_code_lines = int(config.get("max_code_lines", 500))
        self._test_timeout = int(config.get("test_timeout_seconds", 15))
        self._devlog_cooldown = int(config.get("devlog_cooldown_seconds", 30))

        # 创建必要的目录
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
        self._code_runner = create_code_execution_backend(
            self._execution_backend_name,
            sandbox_root=_SANDBOX_DIR,
        )

        self._registry = getattr(context, "agent_tool_registry", None)
        if self._registry is not None:
            self._register_tools()

        _log.info(
            "self_learning setup | sandbox=%s | allow_code_execution=%s | execution_backend=%s | super_admin_only=%s | auto_test=%s | devlog=%s",
            self._sandbox_mode,
            self._code_execution_enabled(),
            self._execution_backend_name,
            self._super_admin_only,
            self._auto_test,
            self._devlog_broadcast,
        )
        if self._allow_code_execution and self._acknowledge_unsafe_execution and not self._code_runner.is_available:
            _log.warning(
                "self_learning execution backend unavailable | backend=%s | reason=%s",
                self._execution_backend_name,
                self._code_runner.unavailable_reason(),
            )

    def _code_execution_enabled(self) -> bool:
        return (
            self._allow_code_execution
            and self._acknowledge_unsafe_execution
            and self._code_runner.is_available
        )

    def _register_tools(self) -> None:
        from core.agent_tools import PromptHint, ToolSchema

        # 1. learn_from_web - 从网上学习
        self._registry.register(
            ToolSchema(
                name="learn_from_web",
                description=(
                    "从网上学习新知识或技术。Agent 可以搜索、阅读文档、学习新的编程技巧。"
                    "学习完成后会生成学习报告和可能的代码实现。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "要学习的主题或技术"},
                        "goal": {"type": "string", "description": "学习目标，想要实现什么功能"},
                        "context": {"type": "string", "description": "可选：额外的上下文信息"},
                    },
                    "required": ["topic", "goal"],
                },
                category="learning",
            ),
            self._handle_learn_from_web,
        )

        dangerous_tools_enabled = self._code_execution_enabled()
        if dangerous_tools_enabled:
            self._registry.register(
                ToolSchema(
                    name="create_skill",
                    description=(
                        "创建一个新的技能工具。仅限受信任代码场景，且默认只允许 super_admin。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "skill_name": {"type": "string", "description": "技能名称（英文，下划线分隔）"},
                            "description": {"type": "string", "description": "技能描述"},
                            "code": {"type": "string", "description": "技能的 Python 代码实现"},
                            "test_code": {"type": "string", "description": "可选：测试代码"},
                        },
                        "required": ["skill_name", "description", "code"],
                    },
                    category="learning",
                ),
                self._handle_create_skill,
            )

            self._registry.register(
                ToolSchema(
                    name="test_in_sandbox",
                    description=(
                        "最佳努力的受限代码测试，仅适用于受信任代码，不能视为强隔离沙盒。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "要测试的 Python 代码"},
                            "test_input": {"type": "string", "description": "可选：测试输入数据"},
                        },
                        "required": ["code"],
                    },
                    category="learning",
                ),
                self._handle_test_in_sandbox,
            )

        # 4. send_devlog - 发送开发日志
        self._registry.register(
            ToolSchema(
                name="send_devlog",
                description=(
                    "在群里发送开发日志（DEVLOG），用白话文告诉大家你学到了什么、"
                    "做了什么改进。让用户了解你的学习和成长过程。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "日志内容（白话文）"},
                        "log_type": {
                            "type": "string",
                            "description": "日志类型: learning/coding/testing/success/failed",
                        },
                    },
                    "required": ["message"],
                },
                category="learning",
            ),
            self._handle_send_devlog,
        )

        # 5. list_my_skills - 列出已创建的技能
        self._registry.register(
            ToolSchema(
                name="list_my_skills",
                description="列出所有已创建的技能，查看自己学会了什么。",
                parameters={"type": "object", "properties": {}},
                category="learning",
            ),
            self._handle_list_skills,
        )

        self_learning_tools = (
            ("learn_from_web", "send_devlog", "list_my_skills")
            if not dangerous_tools_enabled
            else ("learn_from_web", "create_skill", "test_in_sandbox", "send_devlog", "list_my_skills")
        )

        # 注入规则提示
        self._registry.register_prompt_hint(PromptHint(
            source="self_learning",
            section="rules",
            content=(
                "你可以通过 learn_from_web 学习新知识。"
                + (
                    "如管理员已明确开启受信任代码执行，也可使用 create_skill / test_in_sandbox。"
                    if dangerous_tools_enabled
                    else "默认不开放任意代码执行与自建技能，请优先给出方案、文档和人工执行建议。"
                )
                + " 学习和改进时，记得用 send_devlog 告诉用户你的进展（用白话文，不要太技术化）。"
            ),
            priority=70,
            tool_names=self_learning_tools,
        ))

        guidance_steps = (
            "  2. 如管理员已开启受信任代码执行，可使用 create_skill 编写功能\n"
            "  3. 使用 test_in_sandbox 做最佳努力受限测试\n"
            "  4. 测试通过后，使用 send_devlog 告诉用户你学会了什么\n"
            "  5. 使用 list_my_skills 查看自己已经掌握的技能\n"
            if dangerous_tools_enabled
            else
            "  2. 默认不要执行或落盘任意 Python，只输出方案/补丁建议\n"
            "  3. 使用 send_devlog 告诉用户你学会了什么\n"
            "  4. 使用 list_my_skills 查看自己已经掌握的技能\n"
        )

        # 注入工具使用指南
        self._registry.register_prompt_hint(PromptHint(
            source="self_learning",
            section="tools_guidance",
            content=(
                "自我学习流程:\n"
                "  1. 遇到不会的问题时，使用 learn_from_web 学习相关知识\n"
                + guidance_steps
                + "DEVLOG 要用白话文，像朋友聊天一样，例如：\n"
                "  '我刚学会了怎么解析 JSON 数据！现在可以帮你处理 API 返回的数据了~'\n"
                "  '测试了一下新写的图片处理代码，成功了！以后可以帮你压缩图片啦'"
            ),
            priority=10,
            tool_names=self_learning_tools,
        ))
        self._registry.register_context_provider(
            "self_learning_state",
            self._build_dynamic_context,
            priority=35,
            tool_names=self_learning_tools,
        )

    def _dangerous_tool_gate_error(
        self, context: dict[str, Any], tool_name: str
    ) -> str:
        if not self._allow_code_execution or not self._acknowledge_unsafe_execution:
            return (
                f"{tool_name} 已关闭：当前配置未显式开启受信任代码执行。"
                "如需启用，请在 self_learning 配置中同时打开 allow_code_execution 和 acknowledge_unsafe_execution。"
            )
        if tool_name == "test_in_sandbox" and not self._code_runner.is_available:
            return (
                f"{tool_name} 已关闭：{self._code_runner.unavailable_reason()}"
                "当前项目只把本地 subprocess 视为开发用途，不视为安全边界。"
            )
        if self._super_admin_only:
            permission_level = normalize_text(
                str(context.get("permission_level", ""))
            ).lower() or "user"
            if permission_level != "super_admin":
                return f"{tool_name} 仅允许 super_admin 使用。"
        return ""

    async def _handle_learn_from_web(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        topic = str(args.get("topic", "")).strip()
        goal = str(args.get("goal", "")).strip()
        ctx = str(args.get("context", "")).strip()

        if not topic or not goal:
            return ToolCallResult(ok=False, display="需要提供 topic 和 goal")

        session_id = f"learn_{int(time.time())}"
        session = LearningSession(
            session_id=session_id,
            topic=topic,
            start_time=datetime.now(),
            status="learning",
        )
        self._sessions[session_id] = session
        self._stats["total_sessions"] += 1

        # 构建学习提示
        learning_prompt = f"学习主题: {topic}\n学习目标: {goal}"
        if ctx:
            learning_prompt += f"\n背景: {ctx}"

        session.devlog.append(f"开始学习: {topic}")
        session.metrics.update({
            "goal": goal,
            "context": ctx,
            "learning_source": self._learning_source,
        })

        # 这里应该调用搜索工具或其他学习资源
        # 简化实现：返回学习指导
        dangerous_tools_enabled = self._code_execution_enabled()
        plan_steps = [
            "1. 使用 web_search 搜索相关文档和教程",
            "2. 阅读官方文档了解 API 和用法",
            "3. 查看示例代码理解实现方式",
        ]
        if dangerous_tools_enabled:
            plan_steps.extend([
                "4. 使用 create_skill 编写自己的实现",
                "5. 使用 test_in_sandbox 测试代码",
            ])
        else:
            plan_steps.extend([
                "4. 先整理方案、补丁建议或人工操作步骤，不要执行任意 Python",
                "5. 使用 send_devlog 汇报你学到了什么以及下一步建议",
            ])
        learned = f"""
已开始学习 '{topic}'。

学习目标: {goal}

建议步骤:
{chr(10).join(plan_steps)}

学习会话 ID: {session_id}
"""

        session.learned_content = learned
        session.devlog.append("学习资料收集完成")
        session.status = "completed"
        session.end_time = datetime.now()
        session.metrics["result"] = "guidance_generated"

        return ToolCallResult(
            ok=True,
            data={"session_id": session_id, "topic": topic},
            display=learned,
        )

    async def _handle_create_skill(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        skill_name = str(args.get("skill_name", "")).strip()
        description = str(args.get("description", "")).strip()
        code = str(args.get("code", "")).strip()
        test_code = str(args.get("test_code", "")).strip()

        if not skill_name or not description or not code:
            return ToolCallResult(ok=False, display="需要提供 skill_name, description 和 code")

        gate_error = self._dangerous_tool_gate_error(context, "create_skill")
        if gate_error:
            return ToolCallResult(ok=False, error="permission_denied", display=gate_error)

        # 验证技能名称
        if not re.match(r"^[a-z][a-z0-9_]*$", skill_name):
            return ToolCallResult(
                ok=False,
                display="技能名称必须是小写字母开头，只能包含字母、数字和下划线",
            )

        # 检查代码行数
        code_lines = len(code.splitlines())
        if code_lines > self._max_code_lines:
            return ToolCallResult(
                ok=False,
                display=f"代码行数超过限制 ({code_lines} > {self._max_code_lines})",
            )

        try:
            code = self._sanitize_code(code)
            if test_code:
                test_code = self._sanitize_code(test_code)
        except ValueError as exc:
            self._stats["failed_skills"] += 1
            return ToolCallResult(ok=False, error="unsafe_code", display=str(exc))

        self._load_skill_cache()
        is_duplicate, duplicate_name = self._is_duplicate_skill(code)
        if is_duplicate:
            self._stats["failed_skills"] += 1
            return ToolCallResult(
                ok=False,
                error="duplicate_skill",
                display=f"检测到重复技能：与现有技能 '{duplicate_name}' 代码相同",
            )

        # 自动测试
        if self._auto_test:
            test_result = await self._test_code_in_sandbox(code, test_code)
            if not test_result["ok"]:
                self._stats["failed_skills"] += 1
                return ToolCallResult(
                    ok=False,
                    data={"test_failed": True, "error": test_result.get("error")},
                    display=f"代码测试失败: {test_result.get('error', '未知错误')}",
                )

        # 保存技能
        if self._save_skills:
            skill_file = _SKILLS_DIR / f"{skill_name}.py"
            skill_meta_file = _SKILLS_DIR / f"{skill_name}.yml"

            # 保存代码
            with open(skill_file, "w", encoding="utf-8") as f:
                f.write(f'"""{description}"""\n\n')
                f.write(code)

            # 保存元数据
            meta = {
                "name": skill_name,
                "description": description,
                "created_at": datetime.now().isoformat(),
                "test_passed": self._auto_test,
                "code_hash": self._get_skill_hash(code),
            }
            with open(skill_meta_file, "w", encoding="utf-8") as f:
                yaml.dump(meta, f, allow_unicode=True)
            self._skill_cache[skill_name] = meta
            self._cache_loaded = True

            _log.info("skill_created | name=%s | lines=%d", skill_name, code_lines)

        self._stats["successful_skills"] += 1
        result_msg = f"技能 '{skill_name}' 创建成功！\n描述: {description}\n代码行数: {code_lines}"
        if self._auto_test:
            result_msg += "\n测试: 通过 ✓"

        return ToolCallResult(
            ok=True,
            data={"skill_name": skill_name, "saved": self._save_skills},
            display=result_msg,
        )

    async def _handle_test_in_sandbox(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        code = str(args.get("code", "")).strip()
        test_input = str(args.get("test_input", "")).strip()

        if not code:
            return ToolCallResult(ok=False, display="需要提供要测试的代码")

        gate_error = self._dangerous_tool_gate_error(context, "test_in_sandbox")
        if gate_error:
            return ToolCallResult(ok=False, error="permission_denied", display=gate_error)

        result = await self._test_code_in_sandbox(code, test_input)

        if result["ok"]:
            return ToolCallResult(
                ok=True,
                data=result,
                display=f"测试通过 ✓\n输出:\n{result.get('output', '')}",
            )
        else:
            return ToolCallResult(
                ok=False,
                data=result,
                display=f"测试失败 ✗\n错误:\n{result.get('error', '')}",
            )

    async def _handle_send_devlog(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        message = str(args.get("message", "")).strip()
        log_type = str(args.get("log_type", "learning")).strip()

        if not message:
            return ToolCallResult(ok=False, display="需要提供日志内容")

        # 检查冷却时间
        now = time.time()
        if now - self._last_devlog_time < self._devlog_cooldown:
            return ToolCallResult(
                ok=False,
                display=f"DEVLOG 发送太频繁，请等待 {self._devlog_cooldown} 秒",
            )

        # 格式化 DEVLOG
        emoji_map = {
            "learning": "📚",
            "coding": "💻",
            "testing": "🧪",
            "success": "✅",
            "failed": "❌",
        }
        emoji = emoji_map.get(log_type, "📝")

        devlog_msg = f"{emoji} DEVLOG | {message}"

        if not self._devlog_broadcast:
            self._last_devlog_time = now
            self._stats["devlogs_sent"] += 1
            return ToolCallResult(
                ok=True,
                data={"message": devlog_msg, "type": log_type, "broadcast": False},
                display=f"DEVLOG 已生成（广播关闭）: {devlog_msg}",
            )

        # 发送到群（如果启用）
        # 这里需要从 context 获取 api_call 和 group_id
        api_call = context.get("api_call")
        group_id = context.get("group_id")

        if api_call and group_id:
            try:
                await api_call("send_group_msg", group_id=group_id, message=devlog_msg)
                self._last_devlog_time = now
                self._stats["devlogs_sent"] += 1
                _log.info("devlog_sent | type=%s | group=%s", log_type, group_id)
            except Exception as e:
                _log.warning("devlog_send_failed | error=%s", e)
                return ToolCallResult(
                    ok=False,
                    display=f"DEVLOG 发送失败: {e}",
                )
        else:
            return ToolCallResult(
                ok=False,
                display="无法发送 DEVLOG: 缺少 API 调用或群 ID",
            )

        return ToolCallResult(
            ok=True,
            data={"message": devlog_msg, "type": log_type, "broadcast": True},
            display=f"DEVLOG 已发送: {devlog_msg}",
        )

    async def _handle_list_skills(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        self._load_skill_cache()

        if not self._skill_cache and not _SKILLS_DIR.exists():
            return ToolCallResult(ok=True, display="还没有创建任何技能")

        skills = sorted(
            self._skill_cache.values(),
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        )

        if not skills:
            return ToolCallResult(ok=True, display="还没有创建任何技能")

        # 格式化技能列表
        lines = ["已创建的技能:\n"]
        for i, skill in enumerate(skills, 1):
            name = skill.get("name", "unknown")
            desc = skill.get("description", "")
            created = skill.get("created_at", "")
            lines.append(f"{i}. {name}")
            lines.append(f"   描述: {desc}")
            lines.append(f"   创建时间: {created}")
            lines.append("")

        return ToolCallResult(
            ok=True,
            data={"skills": skills, "count": len(skills)},
            display="\n".join(lines),
        )

    async def _test_code_in_sandbox(self, code: str, test_input: str = "") -> dict[str, Any]:
        """在沙盒环境中测试代码

        Args:
            code: 要测试的 Python 代码
            test_input: 可选的测试输入数据

        Returns:
            包含测试结果的字典:
            - ok: 是否成功
            - output: 标准输出
            - error: 错误信息
            - returncode: 返回码
            - execution_time: 执行时间（秒）
        """
        if not self._allow_code_execution or not self._acknowledge_unsafe_execution:
            return {
                "ok": False,
                "error": (
                    "当前配置未开启受信任代码执行。"
                    "请显式启用 allow_code_execution 和 acknowledge_unsafe_execution。"
                ),
                "execution_time": 0.0,
            }
        if not self._code_runner.is_available:
            return {
                "ok": False,
                "error": self._code_runner.unavailable_reason(),
                "execution_time": 0.0,
                "backend": self._execution_backend_name,
                "sandbox_mode": self._sandbox_mode,
            }

        try:
            code = self._sanitize_code(code)
        except ValueError as exc:
            self._stats["total_tests"] += 1
            return {
                "ok": False,
                "error": str(exc),
                "execution_time": 0.0,
            }

        try:
            result = await self._code_runner.run(
                code=code,
                test_input=test_input,
                timeout_seconds=self._test_timeout,
                sandbox_mode=self._sandbox_mode,
            )
            self._stats["total_tests"] += 1
            if result.get("ok"):
                self._stats["passed_tests"] += 1
            return result
        except Exception as exc:
            self._stats["total_tests"] += 1
            _log.exception("sandbox_test_exception")
            return {
                "ok": False,
                "error": f"测试执行失败: {exc}",
                "execution_time": 0.0,
                "backend": self._execution_backend_name,
                "sandbox_mode": self._sandbox_mode,
            }

    def _load_skill_cache(self) -> None:
        """加载技能缓存"""
        if self._cache_loaded:
            return

        if not _SKILLS_DIR.exists():
            self._cache_loaded = True
            return

        for meta_file in _SKILLS_DIR.glob("*.yml"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f)
                    if meta and "name" in meta:
                        self._skill_cache[meta["name"]] = meta
            except Exception as e:
                _log.warning("skill_cache_load_failed | file=%s | error=%s", meta_file, e)

        self._cache_loaded = True
        _log.info("skill_cache_loaded | count=%d", len(self._skill_cache))

    def _get_skill_hash(self, code: str) -> str:
        """计算代码哈希值，用于检测重复技能"""
        return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]

    def _is_duplicate_skill(self, code: str) -> tuple[bool, str]:
        """检查是否是重复的技能

        Returns:
            (是否重复, 重复的技能名称)
        """
        code_hash = self._get_skill_hash(code)

        for skill_name, meta in self._skill_cache.items():
            if meta.get("code_hash") == code_hash:
                return True, skill_name

        return False, ""

    def _active_sessions(self) -> list[LearningSession]:
        active_statuses = {"learning", "testing", "running"}
        return [
            session
            for session in self._sessions.values()
            if session.end_time is None and normalize_text(session.status).lower() in active_statuses
        ]

    def get_stats(self) -> dict[str, Any]:
        """获取插件统计信息"""
        self._load_skill_cache()
        return {
            **self._stats,
            "active_sessions": len(self._active_sessions()),
            "cached_skills": len(self._skill_cache),
            "success_rate": (
                self._stats["passed_tests"] / self._stats["total_tests"]
                if self._stats["total_tests"] > 0
                else 0.0
            ),
        }

    def _build_dynamic_context(self, runtime_info: dict[str, Any]) -> str:
        _ = runtime_info
        stats = self.get_stats()
        dangerous_tools_enabled = self._code_execution_enabled()
        lines = [
            "自学习状态:",
            f"- 执行后端: {self._execution_backend_name}",
            f"- 受信任代码执行: {'开启' if dangerous_tools_enabled else '关闭'}",
            f"- 危险工具可用: {'是' if dangerous_tools_enabled else '否'}",
            f"- 权限限制: {'仅 super_admin' if self._super_admin_only else '按当前用户权限开放'}",
            f"- 学习来源: {self._learning_source}",
            f"- 活跃学习会话: {stats['active_sessions']}",
            f"- 累计学习会话: {stats['total_sessions']}",
            f"- 技能产出: 成功 {stats['successful_skills']} / 失败 {stats['failed_skills']}",
            f"- 缓存技能数: {stats['cached_skills']}",
            f"- 沙盒测试通过率: {stats['passed_tests']}/{stats['total_tests']} ({stats['success_rate']:.0%})",
        ]
        if dangerous_tools_enabled:
            lines.append("- 注意: 当前本地 subprocess 仅用于受信任代码开发，不是强隔离沙盒。")
        else:
            lines.append("- 默认策略: 先学习、整理方案和补丁建议，不要执行任意 Python。")

        recent_sessions = sorted(
            self._sessions.values(),
            key=lambda item: item.start_time,
            reverse=True,
        )[:3]
        if recent_sessions:
            session_rows = []
            for session in recent_sessions:
                session_rows.append(
                    f"- {clip_text(normalize_text(session.topic), 48)} | {session.status} | {int(session.duration())}s"
                )
            if session_rows:
                lines.append("最近学习记录:\n" + "\n".join(session_rows))
        return "\n".join(lines)

    def _sanitize_code(self, code: str) -> str:
        """验证代码，只允许受限纯 Python 片段。"""
        cleaned = str(code or "").strip()
        if not cleaned:
            raise ValueError("代码不能为空。")

        for snippet in _BLOCKED_PATH_SNIPPETS:
            if snippet in cleaned.lower():
                _log.warning("unsafe_code_path_detected | snippet=%s", snippet)
                raise ValueError(f"拒绝执行包含路径逃逸/系统路径片段的代码: {snippet}")

        try:
            tree = ast.parse(cleaned, mode="exec")
        except SyntaxError as exc:
            raise ValueError(f"代码语法错误: {exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in _BLOCKED_IMPORT_MODULES or root not in _SAFE_IMPORT_MODULES:
                        _log.warning("unsafe_code_import_detected | module=%s", alias.name)
                        raise ValueError(f"拒绝执行包含不安全导入的代码: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "").split(".", 1)[0]
                if (
                    any(alias.name == "*" for alias in node.names)
                    or module in _BLOCKED_IMPORT_MODULES
                    or module not in _SAFE_IMPORT_MODULES
                ):
                    _log.warning("unsafe_code_importfrom_detected | module=%s", module)
                    raise ValueError(f"拒绝执行包含不安全导入的代码: {module or 'unknown'}")
            elif isinstance(node, ast.Name):
                if node.id in _BLOCKED_CALL_NAMES or node.id.startswith("__"):
                    _log.warning("unsafe_code_name_detected | name=%s", node.id)
                    raise ValueError(f"拒绝执行包含高风险调用的代码: {node.id}")
            elif isinstance(node, ast.Attribute):
                if node.attr in _BLOCKED_ATTR_NAMES or node.attr.startswith("__"):
                    _log.warning("unsafe_code_attr_detected | attr=%s", node.attr)
                    raise ValueError(f"拒绝执行包含高风险属性调用的代码: {node.attr}")

        return cleaned

    async def handle(self, message: str, context: dict) -> str:
        return "此插件通过 Agent 工具使用，不支持直接调用。"

    async def teardown(self) -> None:
        self._sessions.clear()
        _log.info("self_learning teardown")


# ── 向导辅助函数 ──

def _write_plugin_config(cfg: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _CONFIG_DIR / "self_learning.yml"
    with open(path, "w", encoding="utf-8") as f:
        f.write("# SelfLearning 插件配置 (自动生成，可手动编辑)\n")
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
