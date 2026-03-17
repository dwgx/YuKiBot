from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
REQ_FILE = ROOT / "requirements.txt"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _is_enabled(name: str) -> bool:
    value = _env(name).lower()
    return value in {"1", "true", "yes", "on", "auto", "always"}


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _mask_cmd(cmd: list[str]) -> str:
    masked: list[str] = []
    redact_next = False
    for part in cmd:
        if redact_next:
            masked.append("***")
            redact_next = False
            continue
        masked.append(part)
        if part in {"--index-url", "--extra-index-url"}:
            redact_next = True
    return " ".join(masked)


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    print(f"[deploy] run: {_mask_cmd(cmd)}")
    return subprocess.call(cmd, cwd=str(cwd or ROOT))


def ensure_venv() -> int:
    py = _venv_python()
    if py.exists():
        return 0
    print("[deploy] creating virtual environment...")
    return _run([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=ROOT)


def ensure_requirements() -> int:
    py = _venv_python()
    if not py.exists():
        print("[deploy] venv python not found after creation.")
        return 1
    if not REQ_FILE.exists():
        print(f"[deploy] requirements not found: {REQ_FILE}")
        return 1

    use_uv = _is_enabled("YUKIKO_USE_UV")
    if use_uv:
        uv_bin = shutil.which("uv")
        if uv_bin:
            cmd = [uv_bin, "pip", "install", "--python", str(py)]
            for env_name, flag in (
                ("YUKIKO_PIP_INDEX_URL", "--index-url"),
                ("YUKIKO_PIP_EXTRA_INDEX_URL", "--extra-index-url"),
                ("YUKIKO_PIP_FIND_LINKS", "--find-links"),
                ("YUKIKO_PIP_CACHE_DIR", "--cache-dir"),
            ):
                value = _env(env_name)
                if value:
                    cmd.extend([flag, value])
            cmd.extend(["-r", str(REQ_FILE)])
            print("[deploy] syncing requirements with uv...")
            code = _run(cmd, cwd=ROOT)
            if code == 0:
                return 0
            print("[deploy] uv sync failed, falling back to pip...")
        else:
            print("[deploy] uv requested but not found, falling back to pip...")

    cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--prefer-binary",
        "--upgrade-strategy",
        "only-if-needed",
    ]
    for env_name, flag in (
        ("YUKIKO_PIP_INDEX_URL", "--index-url"),
        ("YUKIKO_PIP_EXTRA_INDEX_URL", "--extra-index-url"),
        ("YUKIKO_PIP_FIND_LINKS", "--find-links"),
        ("YUKIKO_PIP_CACHE_DIR", "--cache-dir"),
        ("YUKIKO_PIP_TIMEOUT", "--timeout"),
        ("YUKIKO_PIP_RETRIES", "--retries"),
    ):
        value = _env(env_name)
        if value:
            cmd.extend([flag, value])
    cmd.extend(["-r", str(REQ_FILE)])
    return _run(cmd, cwd=ROOT)


def health_check() -> int:
    py = _venv_python()
    if not py.exists():
        return 1
    return _run(
        [
            str(py),
            "-c",
            "import pydantic_core._pydantic_core; import nonebot; print('ok')",
        ],
        cwd=ROOT,
    )


def run_main(extra_args: list[str]) -> int:
    py = _venv_python()
    if not py.exists():
        return 1
    return _run([str(py), "main.py", *extra_args], cwd=ROOT)


def bootstrap(*, ensure_requirements_sync: bool = False) -> int:
    code = ensure_venv()
    if code != 0:
        return code
    if not ensure_requirements_sync:
        code = health_check()
        if code == 0:
            return 0
        print("[deploy] venv unhealthy or missing deps, installing requirements...")
    else:
        print("[deploy] forcing requirements sync...")
    code = ensure_requirements()
    if code != 0:
        return code
    return health_check()


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo deploy helper")
    parser.add_argument("--run", action="store_true", help="bootstrap then run main.py")
    parser.add_argument(
        "--ensure-requirements",
        action="store_true",
        help="force a dependency sync even when the venv health check passes",
    )
    parser.add_argument(
        "main_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to main.py (use after --run)",
    )
    args = parser.parse_args()

    code = bootstrap(ensure_requirements_sync=args.ensure_requirements)
    if code != 0:
        print(f"[deploy] bootstrap failed with code {code}")
        return code

    if args.run:
        extra = list(args.main_args or [])
        if extra and extra[0] == "--":
            extra = extra[1:]
        return run_main(extra)
    print("[deploy] bootstrap done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
