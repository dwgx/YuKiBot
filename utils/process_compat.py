"""Subprocess compatibility helpers."""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any


def macos_subprocess_kwargs() -> dict[str, Any]:
    """Prefer posix_spawn-compatible subprocess settings on macOS."""
    if sys.platform == "darwin":
        # CPython falls back to fork_exec when close_fds=True. In YuKiKo the
        # parent process is heavily threaded, and macOS can crash in the child
        # before exec. Disabling close_fds lets subprocess use posix_spawn when
        # the other call arguments are compatible.
        return {"close_fds": False}
    return {}


def resolve_executable_for_spawn(executable: str) -> str:
    """Resolve PATH executables so CPython can use posix_spawn on macOS."""
    if not executable or os.path.dirname(executable):
        return executable
    return shutil.which(executable) or executable
