from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
REQ_FILE = ROOT / "requirements.txt"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    print(f"[deploy] run: {' '.join(cmd)}")
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

    code = _run([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=ROOT)
    if code != 0:
        return code
    return _run([str(py), "-m", "pip", "install", "-r", str(REQ_FILE)], cwd=ROOT)


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


def bootstrap() -> int:
    code = ensure_venv()
    if code != 0:
        return code
    code = health_check()
    if code == 0:
        return 0
    print("[deploy] venv unhealthy or missing deps, installing requirements...")
    code = ensure_requirements()
    if code != 0:
        return code
    return health_check()


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo deploy helper")
    parser.add_argument("--run", action="store_true", help="bootstrap then run main.py")
    parser.add_argument(
        "main_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to main.py (use after --run)",
    )
    args = parser.parse_args()

    code = bootstrap()
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
