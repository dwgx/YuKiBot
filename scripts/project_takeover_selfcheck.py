from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_CHECK = ROOT / "scripts" / "agent_deep_selfcheck.py"
TEST_TARGETS = [
    "tests/test_self_learning_plugin.py",
    "tests/test_thinking_engine_regression.py",
    "tests/test_webui_auth_regression.py",
    "tests/test_webui_chat_media_regression.py",
    "tests/test_image_nsfw_guard_regression.py",
    "tests/test_safety_profile_regression.py",
]


def _run(label: str, args: list[str]) -> bool:
    print(f"[RUN] {label}")
    print("      " + " ".join(args))
    started = time.monotonic()
    result = subprocess.run(args, cwd=ROOT)
    elapsed = time.monotonic() - started
    mark = "PASS" if result.returncode == 0 else "FAIL"
    print(f"[{mark}] {label} ({elapsed:.1f}s)")
    return result.returncode == 0


def main() -> int:
    steps: list[tuple[str, list[str]]] = [
        ("agent_deep_selfcheck", [sys.executable, str(AGENT_CHECK)]),
        (
            "targeted_regressions",
            [sys.executable, "-m", "pytest", "-q", *TEST_TARGETS],
        ),
    ]

    failed: list[str] = []
    for label, cmd in steps:
        if not _run(label, cmd):
            failed.append(label)

    print("\n== Takeover Selfcheck Summary ==")
    if failed:
        print("status=FAIL")
        print("failed_steps=" + ",".join(failed))
        return 1
    print("status=PASS")
    print("checked_steps=" + ",".join(label for label, _ in steps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
