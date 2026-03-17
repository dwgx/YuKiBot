from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class BaseCodeExecutionBackend:
    name = "disabled"
    is_available = False

    async def run(
        self,
        *,
        code: str,
        test_input: str,
        timeout_seconds: int,
        sandbox_mode: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def unavailable_reason(self) -> str:
        return f"execution_backend={self.name} 不可用。"


class DisabledCodeExecutionBackend(BaseCodeExecutionBackend):
    def __init__(self, reason: str = "") -> None:
        self._reason = reason or "当前未启用代码执行后端。"

    def unavailable_reason(self) -> str:
        return self._reason

    async def run(
        self,
        *,
        code: str,
        test_input: str,
        timeout_seconds: int,
        sandbox_mode: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self._reason,
            "execution_time": 0.0,
            "backend": self.name,
            "sandbox_mode": sandbox_mode,
        }


class LocalSubprocessCodeExecutionBackend(BaseCodeExecutionBackend):
    name = "local_subprocess"
    is_available = True

    def __init__(self, sandbox_root: Path) -> None:
        self._sandbox_root = sandbox_root

    async def run(
        self,
        *,
        code: str,
        test_input: str,
        timeout_seconds: int,
        sandbox_mode: str,
    ) -> dict[str, Any]:
        start_time = time.time()
        self._sandbox_root.mkdir(parents=True, exist_ok=True)
        sandbox_run_dir = Path(tempfile.mkdtemp(prefix="self_learning_", dir=self._sandbox_root))
        test_file = sandbox_run_dir / "snippet.py"
        test_file.write_text(code, encoding="utf-8")

        try:
            env = {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
            if test_input:
                env["SELF_LEARNING_TEST_INPUT"] = test_input

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(test_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(sandbox_run_dir),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=max(1, int(timeout_seconds)),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "ok": False,
                    "error": f"测试超时 ({timeout_seconds}秒)",
                    "execution_time": time.time() - start_time,
                    "backend": self.name,
                    "sandbox_mode": sandbox_mode,
                }

            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            return {
                "ok": proc.returncode == 0,
                "output": output,
                "error": "" if proc.returncode == 0 else (error or output),
                "returncode": proc.returncode,
                "execution_time": time.time() - start_time,
                "backend": self.name,
                "sandbox_mode": sandbox_mode,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"测试执行失败: {exc}",
                "execution_time": time.time() - start_time,
                "backend": self.name,
                "sandbox_mode": sandbox_mode,
            }
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(sandbox_run_dir, ignore_errors=True)


def create_code_execution_backend(name: str, *, sandbox_root: Path) -> BaseCodeExecutionBackend:
    normalized = str(name or "").strip().lower()
    if normalized == "local_subprocess":
        return LocalSubprocessCodeExecutionBackend(sandbox_root)
    if normalized in {"", "disabled"}:
        return DisabledCodeExecutionBackend("当前未启用代码执行后端。")
    return DisabledCodeExecutionBackend(f"不支持的 execution_backend: {normalized}")
