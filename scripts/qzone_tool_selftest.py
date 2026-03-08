from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _bootstrap_path() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_path()

from core.agent_tools import _make_qzone_handler  # noqa: E402
from core.config_manager import ConfigManager  # noqa: E402
from core.qzone import parse_cookie_string  # noqa: E402


def _load_runtime_config(root: Path, cookie_override: str) -> dict[str, Any]:
    cm = ConfigManager(root / "config", root / "storage")
    cfg = cm.raw if isinstance(cm.raw, dict) else {}
    runtime_cfg = dict(cfg)

    va = runtime_cfg.get("video_analysis", {})
    va = dict(va) if isinstance(va, dict) else {}
    qz = va.get("qzone", {})
    qz = dict(qz) if isinstance(qz, dict) else {}
    if cookie_override.strip():
        qz["cookie"] = cookie_override.strip()
    va["qzone"] = qz
    runtime_cfg["video_analysis"] = va
    return runtime_cfg


def _resolve_target_qq(qq_arg: str, config: dict[str, Any]) -> str:
    qq = str(qq_arg or "").strip()
    if qq:
        return qq

    va = config.get("video_analysis", {})
    va = va if isinstance(va, dict) else {}
    qz = va.get("qzone", {})
    qz = qz if isinstance(qz, dict) else {}
    cookie_str = str(qz.get("cookie", "")).strip()
    cookies = parse_cookie_string(cookie_str)
    raw_uin = str(cookies.get("uin", "") or cookies.get("p_uin", "") or "")
    if raw_uin.startswith("o"):
        raw_uin = raw_uin[1:]
    return raw_uin.strip()


def _build_tool_args(ns: argparse.Namespace, qq: str) -> dict[str, Any]:
    args: dict[str, Any] = {"qq_number": qq}
    if ns.mode == "moods":
        args["count"] = ns.count
    if ns.mode == "analyze":
        args["mood_count"] = ns.count
        args["include_moods"] = not ns.no_moods
        args["include_albums"] = not ns.no_albums
    return args


async def _run(ns: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    config = _load_runtime_config(root, ns.cookie)
    qq = _resolve_target_qq(ns.qq, config)
    if not qq or not qq.isdigit():
        print("目标 QQ 号无效，请使用 --qq 指定，例如: --qq ***REMOVED***")
        return 2

    handler = _make_qzone_handler(ns.mode, config)
    tool_args = _build_tool_args(ns, qq)
    result = await handler(tool_args, {"config": config})

    print("== QZone Tool Selftest ==")
    print(f"mode={ns.mode}")
    print(f"qq={qq}")
    print(f"ok={result.ok}")
    if result.error:
        print(f"error={result.error}")
    if result.display:
        print("---- display ----")
        print(result.display)
    if ns.print_data:
        print("---- data(json) ----")
        print(json.dumps(result.data, ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="YuKiKo QZone 工具本地自测")
    parser.add_argument(
        "--mode",
        default="analyze",
        choices=["profile", "moods", "albums", "analyze"],
        help="要测试的工具模式",
    )
    parser.add_argument("--qq", default="", help="目标 QQ 号，不填时尝试从 cookie 中读取本人 uin")
    parser.add_argument("--count", type=int, default=8, help="moods/analyze 的说说条数")
    parser.add_argument("--cookie", default="", help="可选：临时覆盖 video_analysis.qzone.cookie")
    parser.add_argument("--no-moods", action="store_true", help="analyze 模式下不拉取说说")
    parser.add_argument("--no-albums", action="store_true", help="analyze 模式下不拉取相册")
    parser.add_argument("--print-data", action="store_true", help="打印返回 data JSON")
    ns = parser.parse_args()
    return asyncio.run(_run(ns))


if __name__ == "__main__":
    raise SystemExit(main())
