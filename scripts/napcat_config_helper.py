"""NapCat OneBot V11 配置助手。

从 YuKiKo 的 .env 自动生成 NapCat 所需的 onebot11 反向 WebSocket 配置，
支持自动探测 NapCat 配置目录并注入/更新连接配置。

用法:
  python napcat_config_helper.py                    # 打印配置 + 探测
  python napcat_config_helper.py --detect            # 仅探测 NapCat 路径
  python napcat_config_helper.py --inject            # 自动注入到 NapCat 配置
  python napcat_config_helper.py --inject --dry-run  # 预览注入效果，不写入
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"

# ── NapCat 配置文件常见目录 (优先级从高到低) ──
NAPCAT_CONFIG_SEARCH_PATHS: list[Path] = [
    # NapCat Shell 标准安装 (Linux)
    Path("/opt/QQ/resources/app/app_launcher/napcat/config"),
    Path("/opt/QQ/resources/app/napcat/config"),
    # 用户级安装
    Path.home() / ".config" / "QQ" / "NapCat" / "config",
    # NapCat.Shell (手动部署)
    Path.home() / "NapCat.Shell" / "napcat" / "config",
]

# ── 扩展搜索: 在常见父目录下递归查找 napcat/config ──
_EXTRA_SEARCH_ROOTS = ["/opt", "/root", "/home", str(Path.home())]


def _read_env(key: str, fallback: str = "") -> str:
    """从 .env 文件读取值。"""
    if not ENV_FILE.exists():
        return fallback
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return fallback


def _log(msg: str) -> None:
    print(f"[napcat-config] {msg}")


def _warn(msg: str) -> None:
    print(f"[napcat-config][WARN] {msg}", file=sys.stderr)


# ──────────────────────────────────────────────────
# 配置生成
# ──────────────────────────────────────────────────

def generate_onebot11_config(
    *,
    port: str = "",
    token: str = "",
    host: str = "127.0.0.1",
) -> dict:
    """生成 NapCat onebot11 反向 WebSocket 完整配置。"""
    if not port:
        port = _read_env("PORT", "8081")
    if not token:
        token = _read_env("ONEBOT_ACCESS_TOKEN", "")

    ws_url = f"ws://{host}:{port}/onebot/v11/ws"

    return {
        "httpServers": [],
        "httpClients": [],
        "wsServers": [],
        "wsClients": [
            {
                "enable": True,
                "url": ws_url,
                "token": token,
                "reconnectInterval": 5000,
            }
        ],
        "enableLocalFile2Url": True,
        "debug": False,
        "heartInterval": 30000,
        "messagePostFormat": "array",
        "token": token,
        "musicSignUrl": "",
        "reportSelfMessage": False,
        "GroupLocalTime": {"Record": False, "RecordList": []},
    }


def generate_ws_client_entry(
    *,
    port: str = "",
    token: str = "",
    host: str = "127.0.0.1",
) -> dict:
    """生成单条 wsClients 条目（用于注入已有配置）。"""
    if not port:
        port = _read_env("PORT", "8081")
    if not token:
        token = _read_env("ONEBOT_ACCESS_TOKEN", "")
    return {
        "enable": True,
        "url": f"ws://{host}:{port}/onebot/v11/ws",
        "token": token,
        "reconnectInterval": 5000,
    }


# ──────────────────────────────────────────────────
# NapCat 路径探测
# ──────────────────────────────────────────────────

def find_napcat_config_dir() -> Path | None:
    """查找 NapCat 配置目录。优先检查常见路径，然后递归搜索。"""
    # 1) 常见固定路径
    for path in NAPCAT_CONFIG_SEARCH_PATHS:
        if path.is_dir():
            return path

    # 2) 递归搜索 (maxdepth=6 的等效实现)
    for root in _EXTRA_SEARCH_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        # 使用 glob 搜索 napcat/config 目录
        for pattern in [
            "**/napcat/config",
            "**/NapCat/config",
            "**/NapCat.Shell/napcat/config",
        ]:
            try:
                for match in root_path.glob(pattern):
                    if match.is_dir() and _depth(root_path, match) <= 6:
                        return match
            except (PermissionError, OSError):
                continue

    return None


def _depth(root: Path, target: Path) -> int:
    """计算目录深度差。"""
    try:
        return len(target.relative_to(root).parts)
    except ValueError:
        return 999


def find_all_onebot11_configs(config_dir: Path | None = None) -> list[Path]:
    """查找所有 onebot11_*.json 配置文件。"""
    search_dir = config_dir or find_napcat_config_dir()
    if not search_dir or not search_dir.is_dir():
        return []
    results = []
    for f in sorted(search_dir.iterdir()):
        if f.name.startswith("onebot11_") and f.name.endswith(".json") and f.is_file():
            results.append(f)
    # 也检查 onebot11.json (v4.5.3+ 格式)
    generic = search_dir / "onebot11.json"
    if generic.is_file() and generic not in results:
        results.insert(0, generic)
    return results


def find_existing_onebot11_config(config_dir: Path | None = None) -> Path | None:
    """查找第一个 onebot11_*.json 配置文件。"""
    configs = find_all_onebot11_configs(config_dir)
    return configs[0] if configs else None


# ──────────────────────────────────────────────────
# 配置注入
# ──────────────────────────────────────────────────

def _backup_config(config_path: Path) -> Path | None:
    """创建配置文件备份。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = config_path.with_suffix(f".bak.{ts}")
    try:
        shutil.copy2(config_path, backup)
        return backup
    except OSError as e:
        _warn(f"Failed to create backup: {e}")
        return None


def _yukiko_ws_url(port: str, host: str = "127.0.0.1") -> str:
    """生成 YuKiKo 的 WebSocket URL。"""
    return f"ws://{host}:{port}/onebot/v11/ws"


def inject_into_existing_config(
    config_path: Path,
    *,
    port: str = "",
    token: str = "",
    host: str = "127.0.0.1",
    dry_run: bool = False,
) -> bool:
    """向已有 NapCat onebot11 配置注入/更新 YuKiKo 的反向 WebSocket 连接。

    逻辑:
    - 如果 wsClients 中已有指向 YuKiKo 的条目 → 更新 token 和 enable
    - 如果没有 → 追加一条新的 wsClient 条目
    - 同时更新顶层 token 字段

    Returns:
        True if changes were made (or would be made in dry_run), False otherwise.
    """
    if not port:
        port = _read_env("PORT", "8081")
    if not token:
        token = _read_env("ONEBOT_ACCESS_TOKEN", "")

    try:
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"Failed to read {config_path}: {e}")
        return False

    original = copy.deepcopy(config)
    ws_url = _yukiko_ws_url(port, host)
    new_entry = generate_ws_client_entry(port=port, token=token, host=host)

    # ── 查找/更新 wsClients ──
    ws_clients: list[dict] = config.get("wsClients", [])
    found_idx = -1
    for i, client in enumerate(ws_clients):
        url = client.get("url", "")
        # 匹配条件: 同端口同路径的 YuKiKo WebSocket
        if "/onebot/v11/ws" in url and f":{port}/" in url:
            found_idx = i
            break

    if found_idx >= 0:
        # 更新已有条目
        ws_clients[found_idx]["url"] = ws_url
        ws_clients[found_idx]["token"] = token
        ws_clients[found_idx]["enable"] = True
        if "reconnectInterval" not in ws_clients[found_idx]:
            ws_clients[found_idx]["reconnectInterval"] = 5000
        _log(f"Updated existing wsClient entry at index {found_idx}")
    else:
        # 追加新条目
        ws_clients.append(new_entry)
        _log(f"Added new wsClient entry: {ws_url}")

    config["wsClients"] = ws_clients

    # ── 更新顶层 token ──
    if "token" in config:
        config["token"] = token

    # ── 确保 messagePostFormat 正确 ──
    config.setdefault("messagePostFormat", "array")

    # ── 检查是否有实际变更 ──
    if config == original:
        _log("No changes needed — config already up to date.")
        return False

    if dry_run:
        _log("Dry run — would write:")
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return True

    # ── 备份并写入 ──
    backup = _backup_config(config_path)
    if backup:
        _log(f"Backup created: {backup}")

    try:
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _log(f"Config updated: {config_path}")
        return True
    except OSError as e:
        _warn(f"Failed to write config: {e}")
        return False


def inject_auto(
    *,
    port: str = "",
    token: str = "",
    host: str = "127.0.0.1",
    dry_run: bool = False,
) -> int:
    """自动探测 NapCat 配置目录并注入 YuKiKo 连接配置。

    Returns:
        0 = success, 1 = no config dir found, 2 = no config files found
    """
    config_dir = find_napcat_config_dir()
    if not config_dir:
        _warn("NapCat config directory not found on this system.")
        _warn("Please install NapCat first, then run this command again.")
        _warn("Or manually configure reverse WebSocket in NapCat WebUI (http://127.0.0.1:6099/webui)")
        return 1

    _log(f"Found NapCat config dir: {config_dir}")

    configs = find_all_onebot11_configs(config_dir)
    if not configs:
        # NapCat 已安装但还没有 onebot11 配置 (没登录过 QQ)
        _warn("No onebot11_*.json config files found.")
        _warn("Please log in to NapCat WebUI first to create a QQ session.")
        _warn("After login, run this command again to auto-inject YuKiKo connection.")

        # 提供一个 onebot11.json 作为默认配置的选项
        generic_path = config_dir / "onebot11.json"
        if not generic_path.exists():
            full_config = generate_onebot11_config(port=port, token=token, host=host)
            if dry_run:
                _log(f"Dry run — would create: {generic_path}")
                print(json.dumps(full_config, indent=2, ensure_ascii=False))
            else:
                generic_path.write_text(
                    json.dumps(full_config, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                _log(f"Created default config: {generic_path}")
                _log("This config will be used by all QQ accounts unless overridden.")
            return 0
        return 2

    # 对所有找到的配置文件执行注入
    updated = 0
    for config_path in configs:
        _log(f"Processing: {config_path.name}")
        if inject_into_existing_config(
            config_path,
            port=port,
            token=token,
            host=host,
            dry_run=dry_run,
        ):
            updated += 1

    if updated > 0:
        action = "would update" if dry_run else "updated"
        _log(f"Done — {action} {updated}/{len(configs)} config file(s).")
    else:
        _log("All config files are already up to date.")

    return 0


# ──────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="NapCat OneBot V11 配置助手 — 自动探测并注入 YuKiKo 连接配置",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python napcat_config_helper.py                     # 打印配置 + 探测路径
  python napcat_config_helper.py --detect            # 仅探测 NapCat 路径
  python napcat_config_helper.py --inject            # 自动注入到 NapCat 配置
  python napcat_config_helper.py --inject --dry-run  # 预览注入效果
  python napcat_config_helper.py --output /tmp/onebot11.json  # 导出配置文件
""",
    )
    parser.add_argument("--port", default="", help="YuKiKo 端口 (默认从 .env 读取)")
    parser.add_argument("--token", default="", help="OneBot Access Token (默认从 .env 读取)")
    parser.add_argument("--host", default="127.0.0.1", help="WebSocket 连接地址 (默认 127.0.0.1)")
    parser.add_argument("--output", default="", help="导出配置 JSON 到指定路径")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    parser.add_argument("--detect", action="store_true", help="仅检测 NapCat 配置路径")
    parser.add_argument("--inject", action="store_true", help="自动注入到已有 NapCat 配置")
    parser.add_argument("--dry-run", action="store_true", help="预览注入效果，不实际修改文件")
    args = parser.parse_args()

    # ── 探测模式 ──
    if args.detect:
        config_dir = find_napcat_config_dir()
        if config_dir:
            _log(f"NapCat config dir: {config_dir}")
            configs = find_all_onebot11_configs(config_dir)
            if configs:
                for c in configs:
                    _log(f"  onebot11 config: {c}")
            else:
                _log("  No onebot11_*.json found (QQ not logged in yet?)")
            return 0
        _log("NapCat config directory not found.")
        return 1

    # ── 注入模式 ──
    if args.inject:
        return inject_auto(
            port=args.port,
            token=args.token,
            host=args.host,
            dry_run=args.dry_run,
        )

    # ── 生成/导出模式 ──
    config = generate_onebot11_config(
        port=args.port,
        token=args.token,
        host=args.host,
    )
    config_json = json.dumps(config, indent=2, ensure_ascii=False)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(config_json + "\n", encoding="utf-8")
        _log(f"Config written to: {output_path}")
        return 0

    if args.quiet:
        return 0

    _log("Generated NapCat onebot11 config:")
    print(config_json)

    config_dir = find_napcat_config_dir()
    if config_dir:
        print(f"\n[napcat-config] Detected NapCat config dir: {config_dir}")
        configs = find_all_onebot11_configs(config_dir)
        if configs:
            for c in configs:
                print(f"[napcat-config]   {c.name}")
            print("[napcat-config] Run with --inject to auto-update these configs.")
        else:
            print("[napcat-config] No onebot11_*.json found (QQ not logged in yet?).")
            print("[napcat-config] Run with --inject to create a default config.")
    else:
        print("\n[napcat-config] NapCat config directory not found.")
        print("[napcat-config] Install NapCat first, then run with --inject.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
