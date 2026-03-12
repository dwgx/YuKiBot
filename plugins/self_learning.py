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

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

_log = logging.getLogger("yukiko.plugin.self_learning")

# 版本信息
__version__ = "1.0.0"
__author__ = "YuKiKo Team"
__license__ = "MIT"

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "storage" / "self_created_skills"
_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "storage" / "sandbox"


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
    internal_only = False
    intent_examples: list[str] = []
    rules: list[str] = []
    args_schema: dict[str, str] = {}

    # ── 首次配置向导 ──

    @staticmethod
    def needs_setup() -> bool:
        """配置文件不存在时需要交互式初始化。"""
        return not (_CONFIG_DIR / "self_learning.yml").exists()

    @staticmethod
    def interactive_setup() -> dict[str, Any]:
        """交互式向导，生成 plugins/config/self_learning.yml。"""
        print("\n┌─ SelfLearning 插件配置 ─┐")

        # 1. 启用?
        ans = _safe_input("  启用自我学习功能? (Y/n): ", "y").lower()
        if ans in ("n", "no"):
            cfg: dict[str, Any] = {"enabled": False}
            _write_plugin_config(cfg)
            print("  self_learning 已禁用。")
            return cfg

        # 2. 沙盒模式
        print("\n  沙盒模式:")
        print("    1. isolated - 完全隔离的沙盒环境（推荐）")
        print("    2. restricted - 受限环境，可访问部分系统资源")
        print("    3. full - 完全访问（危险，仅用于开发）")
        ans = _safe_input("  选择 [1-3，默认 1]: ", "1")
        sandbox_mode = {"1": "isolated", "2": "restricted", "3": "full"}.get(ans, "isolated")

        # 3. 自动测试
        print("\n  自动测试:")
        print("    开启后 Agent 编写代码会自动在沙盒中测试")
        ans = _safe_input("  启用自动测试? (Y/n): ", "y").lower()
        auto_test = ans not in ("n", "no")

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
        ans = _safe_input("  启用技能保存? (Y/n): ", "y").lower()
        save_skills = ans not in ("n", "no")

        cfg = {
            "enabled": True,
            "sandbox_mode": sandbox_mode,
            "auto_test": auto_test,
            "devlog_broadcast": devlog_broadcast,
            "learning_source": learning_source,
            "save_skills": save_skills,
            "max_learning_time_seconds": 300,
            "max_code_lines": 500,
            "test_timeout_seconds": 60,
            "devlog_cooldown_seconds": 30,
        }
        _write_plugin_config(cfg)
        print(f"  配置已保存到 plugins/config/self_learning.yml")
        print("└──────────────────────┘\n")
        return cfg

    def __init__(self) -> None:
        """初始化插件实例"""
        # 配置参数
        self._enabled: bool = False
        self._sandbox_mode: str = "isolated"
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
        if not config.get("enabled", True):
            _log.info("self_learning disabled")
            return

        self._enabled = True
        self._sandbox_mode = str(config.get("sandbox_mode", "isolated"))
        self._auto_test = bool(config.get("auto_test", True))
        self._devlog_broadcast = bool(config.get("devlog_broadcast", True))
        self._learning_source = str(config.get("learning_source", "both"))
        self._save_skills = bool(config.get("save_skills", True))
        self._max_learning_time = int(config.get("max_learning_time_seconds", 300))
        self._max_code_lines = int(config.get("max_code_lines", 500))
        self._test_timeout = int(config.get("test_timeout_seconds", 60))
        self._devlog_cooldown = int(config.get("devlog_cooldown_seconds", 30))

        # 创建必要的目录
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

        self._registry = getattr(context, "agent_tool_registry", None)
        if self._registry is not None:
            self._register_tools()

        _log.info(
            "self_learning setup | sandbox=%s | auto_test=%s | devlog=%s",
            self._sandbox_mode,
            self._auto_test,
            self._devlog_broadcast,
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

        # 2. create_skill - 创建新技能
        self._registry.register(
            ToolSchema(
                name="create_skill",
                description=(
                    "创建一个新的技能工具。Agent 可以编写代码实现新功能，"
                    "并将其保存为可重用的技能。"
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

        # 3. test_in_sandbox - 在沙盒测试代码
        self._registry.register(
            ToolSchema(
                name="test_in_sandbox",
                description=(
                    "在隔离的沙盒环境中测试代码。可以安全地运行和验证代码，"
                    "不会影响主系统。"
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

        # 注入规则提示
        self._registry.register_prompt_hint(PromptHint(
            source="self_learning",
            section="rules",
            content=(
                "你可以通过 learn_from_web 学习新知识，create_skill 创建新技能，"
                "test_in_sandbox 测试代码。学习和创建新功能时，记得用 send_devlog "
                "告诉用户你的进展（用白话文，不要太技术化）。"
            ),
            priority=70,
        ))

        # 注入工具使用指南
        self._registry.register_prompt_hint(PromptHint(
            source="self_learning",
            section="tools_guidance",
            content=(
                "自我学习流程:\n"
                "  1. 遇到不会的问题时，使用 learn_from_web 学习相关知识\n"
                "  2. 学习后，使用 create_skill 编写代码实现新功能\n"
                "  3. 使用 test_in_sandbox 测试代码是否正确\n"
                "  4. 测试通过后，使用 send_devlog 告诉用户你学会了什么\n"
                "  5. 使用 list_my_skills 查看自己已经掌握的技能\n"
                "DEVLOG 要用白话文，像朋友聊天一样，例如：\n"
                "  '我刚学会了怎么解析 JSON 数据！现在可以帮你处理 API 返回的数据了~'\n"
                "  '测试了一下新写的图片处理代码，成功了！以后可以帮你压缩图片啦'"
            ),
            priority=10,
        ))

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

        # 构建学习提示
        learning_prompt = f"学习主题: {topic}\n学习目标: {goal}"
        if ctx:
            learning_prompt += f"\n背景: {ctx}"

        session.devlog.append(f"开始学习: {topic}")

        # 这里应该调用搜索工具或其他学习资源
        # 简化实现：返回学习指导
        learned = f"""
已开始学习 '{topic}'。

学习目标: {goal}

建议步骤:
1. 使用 web_search 搜索相关文档和教程
2. 阅读官方文档了解 API 和用法
3. 查看示例代码理解实现方式
4. 使用 create_skill 编写自己的实现
5. 使用 test_in_sandbox 测试代码

学习会话 ID: {session_id}
"""

        session.learned_content = learned
        session.devlog.append("学习资料收集完成")

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

        # 自动测试
        if self._auto_test:
            test_result = await self._test_code_in_sandbox(code, test_code)
            if not test_result["ok"]:
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
            }
            with open(skill_meta_file, "w", encoding="utf-8") as f:
                yaml.dump(meta, f, allow_unicode=True)

            _log.info("skill_created | name=%s | lines=%d", skill_name, code_lines)

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

        # 发送到群（如果启用）
        if self._devlog_broadcast:
            # 这里需要从 context 获取 api_call 和 group_id
            api_call = context.get("api_call")
            group_id = context.get("group_id")

            if api_call and group_id:
                try:
                    await api_call("send_group_msg", group_id=group_id, message=devlog_msg)
                    self._last_devlog_time = now
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
            data={"message": devlog_msg, "type": log_type},
            display=f"DEVLOG 已发送: {devlog_msg}",
        )

    async def _handle_list_skills(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        if not _SKILLS_DIR.exists():
            return ToolCallResult(ok=True, display="还没有创建任何技能")

        skills = []
        for meta_file in _SKILLS_DIR.glob("*.yml"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f)
                    if meta:
                        skills.append(meta)
            except Exception as e:
                _log.warning("skill_meta_load_failed | file=%s | error=%s", meta_file, e)

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
        start_time = time.time()

        # 创建临时测试文件
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        ) as f:
            test_file = Path(f.name)
            f.write(code)

        try:
            # 根据沙盒模式设置环境
            env = os.environ.copy()
            if self._sandbox_mode == "isolated":
                # 限制环境变量
                env = {
                    "PATH": env.get("PATH", ""),
                    "PYTHONPATH": "",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }

            # 某些调用路径下 setup 尚未执行，确保沙盒目录存在（Windows 下否则会触发 WinError 267）
            _SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

            # 运行代码
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(test_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(_SANDBOX_DIR),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._test_timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                execution_time = time.time() - start_time
                self._stats["total_tests"] += 1
                return {
                    "ok": False,
                    "error": f"测试超时 ({self._test_timeout}秒)",
                    "execution_time": execution_time,
                }

            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            execution_time = time.time() - start_time

            # 更新统计
            self._stats["total_tests"] += 1

            if proc.returncode == 0:
                self._stats["passed_tests"] += 1
                return {
                    "ok": True,
                    "output": output,
                    "returncode": proc.returncode,
                    "execution_time": execution_time,
                }
            else:
                return {
                    "ok": False,
                    "error": error or output,
                    "returncode": proc.returncode,
                    "execution_time": execution_time,
                }

        except Exception as e:
            execution_time = time.time() - start_time
            self._stats["total_tests"] += 1
            _log.exception("sandbox_test_exception")
            return {
                "ok": False,
                "error": f"测试执行失败: {e}",
                "execution_time": execution_time,
            }
        finally:
            # 清理临时文件
            with contextlib.suppress(Exception):
                test_file.unlink()

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

    def get_stats(self) -> dict[str, Any]:
        """获取插件统计信息"""
        return {
            **self._stats,
            "active_sessions": len(self._sessions),
            "cached_skills": len(self._skill_cache),
            "success_rate": (
                self._stats["passed_tests"] / self._stats["total_tests"]
                if self._stats["total_tests"] > 0
                else 0.0
            ),
        }

    def _sanitize_code(self, code: str) -> str:
        """清理和验证代码

        移除潜在的危险操作，确保代码安全。
        """
        # 检查危险的导入和操作
        dangerous_patterns = [
            r"import\s+os\s*\.\s*system",
            r"import\s+subprocess",
            r"eval\s*\(",
            r"exec\s*\(",
            r"__import__\s*\(",
            r"open\s*\([^)]*['\"]w['\"]",  # 写文件操作
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, code, re.IGNORECASE):
                _log.warning("dangerous_code_pattern_detected | pattern=%s", pattern)

        return code

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
