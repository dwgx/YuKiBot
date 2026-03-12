"""ConnectCLI 插件 - 让 Agent 调用外部 CLI 工具 (Claude CLI / Codex CLI)。

安全: 仅限 Agent 内部调用，不暴露给用户。
平台: 仅支持 Windows。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.plugin.connect_cli")

_IS_WINDOWS = platform.system() == "Windows"

_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _safe_input(prompt: str, default: str = "") -> str:
    """安全的 input，处理 KeyboardInterrupt 和 EOFError。"""
    import sys
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
class CLIProvider:
    name: str
    command: str
    binary_path: str
    enabled: bool
    api_key: str
    model: str
    extra_args: list[str]
    env_overrides: dict[str, str]
    timeout_seconds: int
    health_checked: bool = False
    health_ok: bool = False
    health_note: str = "not_checked"


class Plugin:
    name = "connect_cli"
    description = "Agent 内部工具: 调用外部 CLI (Claude / Codex) 执行复杂推理、搜索等任务。"
    agent_tool = True
    internal_only = True
    intent_examples: list[str] = []
    rules: list[str] = []
    args_schema: dict[str, str] = {}

    # ── 首次配置向导 ──

    @staticmethod
    def needs_setup() -> bool:
        """配置文件不存在时需要交互式初始化。"""
        return not (_CONFIG_DIR / "connect_cli.yml").exists()

    @staticmethod
    def interactive_setup() -> dict[str, Any]:
        """交互式向导，生成 plugins/config/connect_cli.yml。"""
        if not _IS_WINDOWS:
            print("  [connect_cli] 仅支持 Windows，跳过配置。")
            cfg = {"enabled": False}
            _write_plugin_config(cfg)
            return cfg

        print("\n┌─ ConnectCLI 插件配置 ─┐")

        # 1. 启用?
        ans = _safe_input("  启用 CLI 工具调用? (Y/n): ", "y").lower()
        if ans in ("n", "no"):
            cfg: dict[str, Any] = {"enabled": False}
            _write_plugin_config(cfg)
            print("  connect_cli 已禁用。")
            return cfg

        # 2. 检测可用 CLI
        providers: dict[str, dict[str, Any]] = {}
        claude_bin = shutil.which("claude")
        codex_bin = shutil.which("codex")

        if claude_bin:
            print(f"  检测到 Claude CLI: {claude_bin}")
            ans = _safe_input("  启用 Claude CLI? (Y/n): ", "n").lower()
            if ans not in ("n", "no"):
                model = _ask_claude_model()
                providers["claude_cli"] = {
                    "enabled": True,
                    "command": "claude",
                    "model": model,
                    "api_key": "",
                    "extra_args": [],
                }
        else:
            print("  未检测到 Claude CLI (claude 命令不在 PATH 中)")

        if codex_bin:
            print(f"  检测到 Codex CLI: {codex_bin}")
            ans = _safe_input("  启用 Codex CLI? (Y/n): ", "y").lower()
            if ans not in ("n", "no"):
                model = _ask_codex_model()
                providers["codex_cli"] = {
                    "enabled": True,
                    "command": "codex",
                    "model": model,
                    "api_key": "",
                    "extra_args": ["--skip-git-repo-check"],
                }
        else:
            print("  未检测到 Codex CLI (codex 命令不在 PATH 中)")

        if not providers:
            print("  没有可用的 CLI 工具，插件将禁用。")
            cfg = {"enabled": False}
            _write_plugin_config(cfg)
            return cfg

        # 3. 默认 provider
        pnames = list(providers.keys())
        default = pnames[0]
        if len(pnames) > 1:
            print(f"\n  默认 CLI 工具:")
            for i, p in enumerate(pnames, 1):
                mark = " *" if i == 1 else ""
                print(f"    {i}. {p}{mark}")
            ans = _safe_input(f"  选择 [1-{len(pnames)}，默认 1]: ", "1")
            if ans.isdigit() and 1 <= int(ans) <= len(pnames):
                default = pnames[int(ans) - 1]

        # 4. 运行模式
        print("\n  CLI 运行模式:")
        print("    1. embedded — 后台运行，输出返回给 Agent (默认)")
        print("    2. cmd — 打开独立 CMD 窗口 (调试用)")
        ans = _safe_input("  选择 [1-2，默认 1]: ", "1")
        open_mode = "cmd" if ans == "2" else "embedded"

        # 5. 节省 token
        print("\n  节省 Token 模式:")
        print("    开启后 Claude 降级为 haiku+low effort，Codex 降级为 gpt-5.3")
        ans = _safe_input("  启用节省 Token? (y/N): ", "n").lower()
        token_saving = ans in ("y", "yes")

        # 6. 安全模式
        print("\n  安全模式:")
        print("    开启: Claude 只规划不执行，Codex 只读沙箱")
        print("    关闭: Claude 跳过权限，Codex 全自动")
        ans = _safe_input("  启用安全模式? (Y/n): ", "y").lower()
        safety_mode = ans not in ("n", "no")

        # 7. 上下文注入
        print("\n  上下文注入:")
        print("    开启后 Agent 调用 CLI 时会自动附带用户消息和对话上下文")
        ans = _safe_input("  启用上下文注入? (Y/n): ", "y").lower()
        inject_context = ans not in ("n", "no")

        # 8. 输出过滤
        print("\n  输出过滤:")
        print("    开启后 CLI 返回的内容会经过安全过滤再给用户")
        ans = _safe_input("  启用输出过滤? (Y/n): ", "y").lower()
        filter_output = ans not in ("n", "no")

        cfg = {
            "enabled": True,
            "default_provider": default,
            "timeout_seconds": 120,
            "max_output_chars": 8000,
            "token_saving": token_saving,
            "safety_mode": safety_mode,
            "inject_context": inject_context,
            "filter_output": filter_output,
            "open_mode": open_mode,
            "self_check_on_setup": True,
            "probe_before_invoke": False,
            "emit_provider_tag": True,
            "show_status_in_context": True,
            "providers": providers,
        }
        _write_plugin_config(cfg)
        print(f"  配置已保存到 plugins/config/connect_cli.yml")
        print("└──────────────────────┘\n")
        return cfg

    def __init__(self) -> None:
        self._providers: dict[str, CLIProvider] = {}
        self._default: str = ""
        self._timeout: int = 120
        self._max_output: int = 8000
        self._token_saving: bool = False
        self._safety_mode: bool = True
        self._inject_context: bool = True
        self._filter_output: bool = True
        self._open_mode: str = "embedded"
        self._self_check_on_setup: bool = True
        self._probe_before_invoke: bool = False
        self._emit_provider_tag: bool = True
        self._show_status_in_context: bool = True
        self._registry: Any = None
        self._self_check_task: asyncio.Task[None] | None = None

    async def setup(self, config: dict[str, Any], context: Any) -> None:
        if not _IS_WINDOWS:
            _log.info("connect_cli skipped on unsupported platform: %s", platform.system())
            return
        if not config.get("enabled", True):
            _log.info("connect_cli disabled")
            return

        self._timeout = int(config.get("timeout_seconds", 120))
        self._max_output = int(config.get("max_output_chars", 8000))
        self._default = str(config.get("default_provider", "claude_cli"))
        self._token_saving = bool(config.get("token_saving", False))
        self._safety_mode = bool(config.get("safety_mode", True))
        self._inject_context = bool(config.get("inject_context", True))
        self._filter_output = bool(config.get("filter_output", True))
        self._open_mode = str(config.get("open_mode", "embedded")).strip().lower()
        self._self_check_on_setup = bool(config.get("self_check_on_setup", True))
        self._probe_before_invoke = bool(config.get("probe_before_invoke", False))
        self._emit_provider_tag = bool(config.get("emit_provider_tag", True))
        self._show_status_in_context = bool(config.get("show_status_in_context", True))

        for pname, pcfg in (config.get("providers") or {}).items():
            if not isinstance(pcfg, dict) or not pcfg.get("enabled", True):
                continue
            command = str(pcfg.get("command", pname))
            binary = shutil.which(command)
            if binary and _IS_WINDOWS:
                binary = self._prefer_windows_executable(binary)
            if not binary:
                _log.warning("cli_not_found | provider=%s cmd=%s", pname, command)
                continue

            api_key = str(pcfg.get("api_key", "") or "")
            model = str(pcfg.get("model", "") or "")
            extra = pcfg.get("extra_args", [])
            if not isinstance(extra, list):
                extra = []

            env: dict[str, str] = {}
            if api_key:
                if "claude" in pname.lower() or "anthropic" in pname.lower():
                    env["ANTHROPIC_API_KEY"] = api_key
                elif "openai" in pname.lower() or "codex" in pname.lower():
                    env["OPENAI_API_KEY"] = api_key
            if "claude" in pname.lower():
                bash_path, bash_note = self._resolve_claude_git_bash_path(base_env=env)
                if bash_path:
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = bash_path
                if bash_note:
                    _log.info("claude_git_bash | provider=%s | %s", pname, bash_note)

            self._providers[pname] = CLIProvider(
                name=pname, command=command, binary_path=binary, enabled=True,
                api_key=api_key, model=model, extra_args=extra,
                env_overrides=env, timeout_seconds=self._timeout,
            )
            _log.info("cli_provider_ready | %s -> %s", pname, binary)

        if not self._providers:
            _log.warning("connect_cli: no providers available")
            return

        self._registry = getattr(context, "agent_tool_registry", None)
        if self._registry is not None:
            self._register_tools()

        if self._self_check_on_setup:
            self._self_check_task = asyncio.create_task(self._run_startup_self_check())
            _log.info("cli_self_check_started | background=true")

        mode_info = (
            f"token_saving={self._token_saving} "
            f"safety={self._safety_mode} "
            f"open_mode={self._open_mode}"
        )
        _log.info("connect_cli setup | providers=%d | %s", len(self._providers), mode_info)

    def _register_tools(self) -> None:
        from core.agent_tools import PromptHint, ToolSchema

        names = list(self._providers.keys())
        provider_desc = ", ".join(names)

        self._registry.register(
            ToolSchema(
                name="cli_invoke",
                description=(
                    f"调用外部 CLI 工具执行复杂任务。可用: {provider_desc}。"
                    "使用场景: 需要深度代码分析、复杂推理、联网搜索等超出内置工具能力的任务。"
                    "执行较慢(最多2分钟)，仅在必要时使用。"
                    "prompt 参数应包含完整的问题描述和必要上下文。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "发送给 CLI 的提示/问题，应包含完整上下文"},
                        "provider": {
                            "type": "string",
                            "description": f"CLI provider, 可选: {provider_desc}, 默认: {self._default}",
                        },
                        "context_hint": {
                            "type": "string",
                            "description": "可选: 补充背景信息，如用户画像、对话历史摘要等",
                        },
                    },
                    "required": ["prompt"],
                },
                category="cli",
            ),
            self._handle_invoke,
        )

        # cli_status: 让 Agent 查询 CLI 可用状态
        self._registry.register(
            ToolSchema(
                name="cli_status",
                description="查询外部 CLI 工具的当前状态和可用性。",
                parameters={"type": "object", "properties": {}},
                category="cli",
            ),
            self._handle_status,
        )

        # 注入规则提示: 告诉 Agent 什么时候该用 cli_invoke
        self._registry.register_prompt_hint(PromptHint(
            source="connect_cli",
            section="rules",
            content=(
                "cli_invoke 仅用于深度联网搜索、复杂代码/数学推理、"
                "你无法回答的专业问题。日常闲聊和普通搜索勿用(执行慢)"
            ),
            priority=80,
        ))

        # 注入工具使用指南: 详细告诉 Agent 如何正确使用
        self._registry.register_prompt_hint(PromptHint(
            source="connect_cli",
            section="tools_guidance",
            content=(
                "当你遇到以下情况时，使用 cli_invoke:\n"
                "  1. 用户问题超出你的知识范围，需要联网搜索最新信息\n"
                "  2. 复杂的代码分析、数学推理、逻辑推导\n"
                "  3. 你不确定答案的准确性，需要外部验证\n"
                "  4. 用户明确要求深度搜索或专业分析\n"
                "使用时，在 prompt 中包含: 用户的原始问题 + 你已知的上下文 + 你需要 CLI 帮你做什么。"
                f"可用 provider: {provider_desc}。"
            ),
            priority=10,
        ))

        # 注册动态上下文: 让 Agent 知道 CLI 当前状态
        if self._show_status_in_context:
            self._registry.register_context_provider(
                "cli_status",
                lambda info: self._context_status_line(),
                priority=90,
            )

    async def _handle_invoke(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolCallResult(ok=False, data={}, display="prompt 不能为空")

        pname = str(args.get("provider", self._default)).strip()
        provider = self._providers.get(pname)
        if not provider:
            avail = ", ".join(self._providers.keys())
            return ToolCallResult(ok=False, data={}, display=f"未知 provider '{pname}', 可用: {avail}")

        if self._probe_before_invoke:
            ok, note = await self._probe_provider(provider)
            provider.health_checked = True
            provider.health_ok = ok
            provider.health_note = note
            if not ok:
                return ToolCallResult(
                    ok=False,
                    data={"provider": pname, "health": note},
                    display=f"CLI 自检失败({pname}): {note}",
                )

        context_hint = str(args.get("context_hint", "")).strip()
        enhanced_prompt = self._build_enhanced_prompt(prompt, context_hint, context)

        try:
            if self._open_mode == "cmd":
                self._open_cmd_window(provider, enhanced_prompt)
                return ToolCallResult(
                    ok=True,
                    data={"provider": pname, "mode": "cmd"},
                    display=f"已在独立 CMD 窗口打开 {pname}（CLI）",
                )
            output = await self._run_embedded(provider, enhanced_prompt)
            if self._filter_output:
                output = self._sanitize_output(output)
            if self._emit_provider_tag and output:
                output = f"【CLI:{pname}】\n{output}"
            if len(output) > self._max_output:
                output = output[: self._max_output] + f"\n...(截断, 共{len(output)}字符)"
            return ToolCallResult(
                ok=True,
                data={"provider": pname, "output": output, "cli_used": True},
                display=output[:2000],
            )
        except asyncio.TimeoutError:
            return ToolCallResult(
                ok=False, data={"provider": pname},
                display=f"CLI 执行超时 ({provider.timeout_seconds}s)，任务可能过于复杂，建议拆分或简化问题。",
            )
        except Exception as e:
            _log.exception("cli_invoke_error")
            return ToolCallResult(ok=False, data={}, display=f"CLI 错误: {e}")

    async def _handle_status(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        """返回 CLI 工具的当前状态。"""
        from core.agent_tools import ToolCallResult
        return ToolCallResult(
            ok=True,
            data={"status": self.status_text()},
            display=self.status_text(),
        )

    def _build_enhanced_prompt(
        self, prompt: str, context_hint: str, tool_context: dict[str, Any],
    ) -> str:
        """构建增强 prompt，注入对话上下文让 CLI 更好理解任务。"""
        parts = []

        # 基本任务描述
        parts.append(f"任务: {prompt}")

        # Agent 提供的补充上下文
        if context_hint:
            parts.append(f"\n背景信息: {context_hint}")

        # 从 tool_context 提取有用信息 (仅在 inject_context 开启时)
        if self._inject_context:
            user_name = tool_context.get("user_name", "")
            message_text = tool_context.get("message_text", "")
            if user_name and message_text:
                parts.append(f"\n用户 {user_name} 的原始消息: {message_text[:500]}")

        # 安全提示
        if self._safety_mode:
            parts.append("\n注意: 请只提供信息和分析，不要执行任何破坏性操作。")

        return "\n".join(parts)

    def _build_cmd(self, provider: CLIProvider, prompt: str) -> list[str]:
        """根据 provider 类型 + 全局模式构建命令行参数。"""
        exe = provider.binary_path or provider.command
        cmd = [exe]
        is_claude = "claude" in provider.name.lower()
        is_codex = "codex" in provider.name.lower()

        if is_claude:
            # 非交互模式: --print
            cmd.append("--print")

            # 模型: token_saving 时降级
            model = provider.model
            if self._token_saving:
                model = "haiku"
                cmd.extend(["--effort", "low"])
            if model:
                cmd.extend(["--model", model])

            # 安全模式
            if self._safety_mode:
                cmd.extend(["--permission-mode", "plan"])
            else:
                cmd.append("--dangerously-skip-permissions")

            cmd.extend(provider.extra_args)
            cmd.append(prompt)

        elif is_codex:
            # 非交互模式: exec
            cmd.append("exec")

            # 模型: token_saving 时降级
            model = provider.model
            if self._token_saving:
                model = "gpt-5.3"  # 使用 gpt-5.3 作为节省模式
            if model:
                cmd.extend(["--model", model])

            # 安全模式
            if self._safety_mode:
                cmd.extend(["--sandbox", "read-only"])
            else:
                cmd.append("--full-auto")

            # 自动添加 --skip-git-repo-check（如果 extra_args 中没有）
            if "--skip-git-repo-check" not in provider.extra_args:
                cmd.append("--skip-git-repo-check")

            cmd.extend(provider.extra_args)
            cmd.append(prompt)

        else:
            cmd.extend(provider.extra_args)
            cmd.append(prompt)

        return cmd

    def _build_interactive_cmd(self, provider: CLIProvider) -> list[str]:
        """构建交互式 CMD 窗口的命令 (不带 --print / exec)。"""
        exe = provider.binary_path or provider.command
        cmd = [exe]
        is_claude = "claude" in provider.name.lower()
        is_codex = "codex" in provider.name.lower()

        if is_claude:
            model = provider.model
            if self._token_saving:
                model = "haiku"
                cmd.extend(["--effort", "low"])
            if model:
                cmd.extend(["--model", model])
            if self._safety_mode:
                cmd.extend(["--permission-mode", "plan"])
            cmd.extend(provider.extra_args)

        elif is_codex:
            model = provider.model
            if self._token_saving:
                model = "gpt-5.3"  # 使用 gpt-5.3 作为节省模式
            if model:
                cmd.extend(["--model", model])
            if self._safety_mode:
                cmd.extend(["--sandbox", "read-only"])
            cmd.extend(provider.extra_args)

        return cmd

    @staticmethod
    async def _terminate_process(proc: asyncio.subprocess.Process | None, wait_timeout: float = 1.5) -> None:
        """尝试终止子进程，并限制等待时长，避免 Windows 下 kill 后卡住。"""
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=max(0.2, float(wait_timeout)))

    async def _run_embedded(self, provider: CLIProvider, prompt: str) -> str:
        """后台运行 CLI，捕获输出返回。超时时返回已收集的部分输出。"""
        cmd = self._build_cmd(provider, prompt)

        env = os.environ.copy()
        env.update(provider.env_overrides)

        _log.info("cli_exec | provider=%s | cmd=%s", provider.name, " ".join(cmd[:6]))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=provider.timeout_seconds,
            )
        except asyncio.TimeoutError:
            # 尝试读取已有的部分输出
            partial = ""
            try:
                if proc.stdout:
                    partial_bytes = await asyncio.wait_for(proc.stdout.read(self._max_output), timeout=1.0)
                    partial = partial_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            await self._terminate_process(proc, wait_timeout=1.2)
            if partial:
                _log.info("cli_timeout_with_partial | provider=%s | len=%d", provider.name, len(partial))
                return f"(超时，以下为部分输出)\n{partial}"
            raise

        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not output:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"CLI exit {proc.returncode}: {err[:500]}")
        return output

    @staticmethod
    def _sanitize_output(text: str) -> str:
        """过滤 CLI 输出中的敏感信息和潜在注入内容。"""
        if not text:
            return text
        import re
        # 移除 ANSI 转义序列
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
        # 移除可能的 API key 泄露 (常见格式)
        text = re.sub(r"(sk-|key-|token-)[a-zA-Z0-9]{20,}", r"\1***", text)
        # 移除文件路径中的用户名 (Windows)
        text = re.sub(r"C:\\Users\\[^\\]+", r"C:\\Users\\***", text)
        return text

    def _context_status_line(self) -> str:
        if not self._providers:
            return "外部CLI工具当前不可用。"
        healthy = [p.name for p in self._providers.values() if p.health_checked and p.health_ok]
        checking = [
            p.name for p in self._providers.values()
            if (not p.health_checked) and normalize_text(p.health_note).lower() == "checking"
        ]
        unknown = [
            p.name for p in self._providers.values()
            if (not p.health_checked) and normalize_text(p.health_note).lower() != "checking"
        ]
        unhealthy = [p.name for p in self._providers.values() if p.health_checked and not p.health_ok]
        parts = []
        if healthy:
            parts.append(f"可用: {', '.join(healthy)}")
        if checking:
            parts.append(f"探测中: {', '.join(checking)}")
        if unknown:
            parts.append(f"未探测: {', '.join(unknown)}")
        if unhealthy:
            parts.append(f"异常: {', '.join(unhealthy)}")
        body = " | ".join(parts) if parts else "状态未知"
        return f"外部CLI状态({body})，复杂任务可调用 cli_invoke。"

    async def _run_startup_self_check(self) -> None:
        providers = list(self._providers.values())
        if not providers:
            return

        async def _check_one(provider: CLIProvider) -> tuple[bool, str]:
            provider.health_checked = False
            provider.health_ok = False
            provider.health_note = "checking"
            _log.info("cli_provider_health_check_start | %s", provider.name)
            ok, note = await self._probe_provider(provider)
            provider.health_checked = True
            provider.health_ok = ok
            provider.health_note = note
            level = _log.info if ok else _log.warning
            level("cli_provider_health | %s | ok=%s | %s", provider.name, ok, note)
            return ok, note

        tasks = [asyncio.create_task(_check_one(p)) for p in providers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok_count = 0
        fail_count = 0
        abnormal: list[str] = []
        for item in results:
            if isinstance(item, Exception):
                fail_count += 1
                abnormal.append(f"internal_error:{type(item).__name__}")
                continue
            if item[0]:
                ok_count += 1
            else:
                fail_count += 1
                # note 已在 _check_one 中写回 provider.health_note
        for provider in providers:
            if provider.health_checked and not provider.health_ok:
                abnormal.append(f"{provider.name}:{provider.health_note}")
        _log.info(
            "cli_provider_health_done | total=%d | ok=%d | fail=%d",
            len(providers),
            ok_count,
            fail_count,
        )
        if abnormal:
            _log.warning("cli_provider_health_abnormal | %s", " | ".join(abnormal))
        else:
            _log.info("cli_provider_health_all_ok")

    async def _probe_provider(self, provider: CLIProvider) -> tuple[bool, str]:
        env = os.environ.copy()
        env.update(provider.env_overrides)
        exe = provider.binary_path or provider.command
        checks = (
            [exe, "--version"],
            [exe, "--help"],
        )
        last_note = "probe_failed"
        for cmd in checks:
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=4)
            except asyncio.TimeoutError:
                last_note = f"{cmd[-1]}:timeout"
                await self._terminate_process(proc, wait_timeout=0.8)
                continue
            except Exception as exc:
                last_note = f"{cmd[-1]}:{type(exc).__name__}"
                continue
            out = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()
            if proc.returncode == 0:
                version_brief = out.splitlines()[0].strip() if out else "ok"
                # 运行时探测（实际调用 API）作为可选增强：
                # 超时不影响 provider 可用性，仅记录警告。
                runtime_ok, runtime_note = await self._probe_runtime(provider=provider, env=env)
                if not runtime_ok:
                    _log.warning(
                        "cli_provider_runtime_probe_soft_fail | %s | %s (binary ok, marking usable)",
                        provider.name, runtime_note,
                    )
                    return True, f"{version_brief} (runtime_probe:{runtime_note})"
                if runtime_note and runtime_note != "ok":
                    return True, runtime_note[:120]
                return True, version_brief[:120]
            last_note = f"{cmd[-1]} exit={proc.returncode}"
        return False, last_note[:120]

    async def _probe_runtime(self, provider: CLIProvider, env: dict[str, str]) -> tuple[bool, str]:
        cmd = self._build_cmd(provider, "__YUKIKO_CLI_HEALTHCHECK__: 回复 OK")
        timeout_seconds = max(6, min(int(provider.timeout_seconds or 20), 30))
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self._terminate_process(proc, wait_timeout=1.0)
            return False, "runtime_timeout"
        except Exception as exc:
            return False, f"runtime_{type(exc).__name__}"

        output = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()
        if proc.returncode == 0:
            brief = output.splitlines()[0].strip() if output else "ok"
            return True, brief[:120]
        return False, self._summarize_runtime_probe_error(output=output, returncode=proc.returncode)

    @staticmethod
    def _summarize_runtime_probe_error(output: str, returncode: int) -> str:
        text = str(output or "").strip()
        lower = text.lower()
        if "claude_code_git_bash_path" in lower and "unable to find" in lower:
            return "invalid_claude_git_bash_path"
        if "not logged in" in lower:
            return "not_logged_in"
        if "not inside a trusted directory" in lower:
            return "codex_repo_not_trusted(--skip-git-repo-check)"
        if "model is not supported" in lower:
            return "model_not_supported_by_current_account"
        if "api key" in lower and ("missing" in lower or "invalid" in lower):
            return "api_key_invalid_or_missing"
        if "unauthorized" in lower or "forbidden" in lower:
            return "auth_failed"
        if text:
            return clip_text(text.splitlines()[0].strip(), 120)
        return f"runtime_exit={returncode}"

    def status_text(self) -> str:
        if not _IS_WINDOWS:
            return "connect_cli: unsupported_platform"
        if not self._providers:
            return "connect_cli: disabled_or_no_provider"
        lines = [
            "connect_cli:",
            f"- default={self._default}",
            f"- mode={self._open_mode}",
        ]
        for p in self._providers.values():
            if p.health_checked and p.health_ok:
                health = "ok"
            elif p.health_checked:
                health = "fail"
            elif normalize_text(p.health_note).lower() == "checking":
                health = "checking"
            else:
                health = "unknown"
            lines.append(f"- {p.name}: {health} | {p.health_note}")
        return "\n".join(lines)

    @staticmethod
    def _prefer_windows_executable(path: str) -> str:
        resolved = str(path or "").strip()
        if not resolved:
            return ""
        p = Path(resolved)
        if p.suffix.lower() in {".exe", ".cmd", ".bat", ".ps1"}:
            return str(p)
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            candidate = Path(f"{resolved}{ext}")
            if candidate.exists():
                return str(candidate)
        return resolved

    def _resolve_claude_git_bash_path(self, base_env: dict[str, str] | None = None) -> tuple[str, str]:
        env = base_env or {}
        configured = str(
            env.get("CLAUDE_CODE_GIT_BASH_PATH")
            or os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
        ).strip()
        if configured:
            if Path(configured).exists():
                return configured, f"use_configured={configured}"
            configured_note = f"configured_missing={configured}"
        else:
            configured_note = ""

        candidates: list[str] = []
        for var_name in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            root = str(os.environ.get(var_name, "")).strip()
            if not root:
                continue
            candidates.extend(
                [
                    str(Path(root) / "Git" / "bin" / "bash.exe"),
                    str(Path(root) / "Git" / "usr" / "bin" / "bash.exe"),
                    str(Path(root) / "Programs" / "Git" / "bin" / "bash.exe"),
                    str(Path(root) / "Programs" / "Git" / "usr" / "bin" / "bash.exe"),
                ]
            )

        # 兼容用户历史路径（如果存在则继续使用）
        candidates.extend(
            [
                r"D:\Software\Dev\Git\bin\bash.exe",
                r"D:\Software\Dev\Git\usr\bin\bash.exe",
            ]
        )

        # 从 PATH 推测，排除 WSL 的 system32\bash.exe
        for cmd in ("git-bash", "bash"):
            found = shutil.which(cmd)
            if not found:
                continue
            found_path = Path(found)
            normalized = str(found_path).lower().replace("/", "\\")
            if normalized.endswith(r"\windows\system32\bash.exe"):
                continue
            if found_path.suffix.lower() in {".exe", ".cmd", ".bat", ".ps1"}:
                candidates.append(str(found_path))

        seen: set[str] = set()
        for raw in candidates:
            item = str(raw or "").strip()
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            if Path(item).exists():
                note = f"auto_detected={item}"
                if configured_note:
                    note = f"{configured_note}; {note}"
                return item, note

        if configured_note:
            return "", configured_note
        return "", "git_bash_not_found"

    def _open_cmd_window(self, provider: CLIProvider, prompt: str) -> None:
        """打开独立 CMD 窗口运行 CLI (仅 Windows，调试用)。"""
        cmd = self._build_interactive_cmd(provider)
        title = f"YuKiKo - {provider.name}"

        # 使用 list 形式避免 shell 注入
        _log.info("cli_open_cmd | provider=%s", provider.name)
        subprocess.Popen(
            ["cmd", "/C", "start", title, "cmd", "/K"] + cmd,
            env={**os.environ, **provider.env_overrides},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def handle(self, message: str, context: dict) -> str:
        return "此插件仅供内部 Agent 使用，不支持直接调用。"

    async def teardown(self) -> None:
        task = self._self_check_task
        self._self_check_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        self._providers.clear()
        _log.info("connect_cli teardown")


# ── 向导辅助函数 ──

def _ask_claude_model() -> str:
    print("  Claude CLI 模型:")
    print("    1. claude-sonnet-4-20250514 (默认)")
    print("    2. claude-opus-4-6")
    print("    3. haiku")
    print("    4. 自定义")
    ans = _safe_input("  选择 [1-4，默认 1]: ", "1")
    if ans == "2":
        return "claude-opus-4-6"
    if ans == "3":
        return "haiku"
    if ans == "4":
        return _safe_input("  输入模型名: ", "claude-sonnet-4-20250514") or "claude-sonnet-4-20250514"
    return "claude-sonnet-4-20250514"


def _ask_codex_model() -> str:
    print("  Codex CLI 模型:")
    print("    1. gpt-5.4 (默认，推荐)")
    print("    2. gpt-5.3-codex")
    print("    3. gpt-5.3")
    print("    4. 自定义")
    ans = _safe_input("  选择 [1-4，默认 1]: ", "1")
    if ans == "2":
        return "gpt-5.3-codex"
    if ans == "3":
        return "gpt-5.3"
    if ans == "4":
        return _safe_input("  输入模型名: ", "gpt-5.4") or "gpt-5.4"
    return "gpt-5.4"


def _write_plugin_config(cfg: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _CONFIG_DIR / "connect_cli.yml"
    with open(path, "w", encoding="utf-8") as f:
        f.write("# ConnectCLI 插件配置 (自动生成，可手动编辑)\n")
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
