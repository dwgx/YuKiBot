#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
NEED_DEPLOY=0
WEBUI_DIR="$ROOT_DIR/webui"
WEBUI_DIST_INDEX="$WEBUI_DIR/dist/index.html"
WEBUI_AUTOBUILD="${YUKIKO_WEBUI_AUTOBUILD:-1}"

# ── .env 必填项校验 ──
validate_env_essentials() {
  local env_file="$ROOT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    echo "[YuKiKo] WARN: .env file not found. Run install.sh first."
    return 1
  fi
  local missing=0
  local key val
  for key in ONEBOT_ACCESS_TOKEN WEBUI_TOKEN PORT; do
    val="$(grep -E "^${key}=" "$env_file" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    if [[ -z "$val" ]]; then
      echo "[YuKiKo] WARN: $key is empty in .env"
      missing=$((missing + 1))
    elif [[ "$val" == "replace_with_"* || "$val" == "change_me"* ]]; then
      echo "[YuKiKo] WARN: $key still has placeholder value in .env — please set a real value"
      missing=$((missing + 1))
    fi
  done
  if [[ "$missing" -gt 0 ]]; then
    echo "[YuKiKo] WARN: $missing essential .env variable(s) need attention"
  fi
  return 0
}

# ── NapCat 连接前置自检 ──
check_napcat_readiness() {
  # 检查 systemd 服务
  if command -v systemctl >/dev/null 2>&1; then
    local napcat_svc=""
    napcat_svc="$(systemctl list-units --type=service --state=active --no-legend 2>/dev/null | awk '{print $1}' | grep -Ei 'napcat' | head -1 || true)"
    if [[ -n "$napcat_svc" ]]; then
      echo "[YuKiKo] NapCat service active: $napcat_svc ✓"
      return 0
    fi
    # 检查是否有 napcat service 但未启动
    local napcat_inactive=""
    napcat_inactive="$(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Ei 'napcat' | head -1 || true)"
    if [[ -n "$napcat_inactive" ]]; then
      echo "[YuKiKo] WARN: NapCat service found but not active: $napcat_inactive"
      echo "[YuKiKo] WARN: Start it with: sudo systemctl start ${napcat_inactive%.service}"
      return 1
    fi
  fi
  # 检查进程
  if command -v pgrep >/dev/null 2>&1 && pgrep -fa napcat >/dev/null 2>&1; then
    echo "[YuKiKo] NapCat process detected ✓"
    return 0
  fi
  # Docker 容器
  if command -v docker >/dev/null 2>&1; then
    local napcat_container=""
    napcat_container="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -Ei 'napcat' | head -1 || true)"
    if [[ -n "$napcat_container" ]]; then
      echo "[YuKiKo] NapCat Docker container running: $napcat_container ✓"
      return 0
    fi
  fi
  echo "[YuKiKo] INFO: NapCat not detected. Bot will start but won't receive QQ messages until NapCat connects."
  echo "[YuKiKo] INFO: Guide: https://napneko.github.io/guide/boot/Shell"
  return 0  # 不阻止启动，只是提示
}

build_webui_if_needed() {
  if [[ "$WEBUI_AUTOBUILD" != "1" ]]; then
    return 0
  fi
  if [[ ! -d "$WEBUI_DIR" || ! -f "$WEBUI_DIR/package.json" ]]; then
    return 0
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "[YuKiKo] npm not found, skip webui auto-build."
    return 0
  fi

  local need_build=0
  if [[ ! -f "$WEBUI_DIST_INDEX" ]]; then
    need_build=1
  elif find "$WEBUI_DIR/src" -type f -newer "$WEBUI_DIST_INDEX" -print -quit 2>/dev/null | grep -q .; then
    need_build=1
  elif [[ "$WEBUI_DIR/package.json" -nt "$WEBUI_DIST_INDEX" || "$WEBUI_DIR/package-lock.json" -nt "$WEBUI_DIST_INDEX" ]]; then
    need_build=1
  fi

  if [[ "$need_build" -eq 1 ]]; then
    echo "[YuKiKo] webui dist is missing or outdated, running npm build..."
    if ! npm --prefix "$WEBUI_DIR" run build; then
      echo "[YuKiKo] WARN: webui build failed, continue starting backend with existing/static assets."
    fi
  fi
}

# ── 启动前检查 ──
validate_env_essentials || true
check_napcat_readiness || true

if [[ -x "$VENV_PY" ]]; then
  "$VENV_PY" -c "import pydantic_core._pydantic_core; import nonebot" >/dev/null 2>&1 || NEED_DEPLOY=1
else
  NEED_DEPLOY=1
fi

if [[ "$NEED_DEPLOY" -eq 1 ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
  else
    echo "ERROR: python/python3 not found in PATH, and local venv is missing or broken."
    exit 1
  fi
  echo "[YuKiKo] local venv missing or unhealthy, running deploy helper..."
  "$PY_BIN" scripts/deploy.py --run "$@"
else
  build_webui_if_needed
  "$VENV_PY" main.py "$@"
fi
