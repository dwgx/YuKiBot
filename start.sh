#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
NEED_DEPLOY=0
WEBUI_DIR="$ROOT_DIR/webui"
WEBUI_DIST_INDEX="$WEBUI_DIR/dist/index.html"
WEBUI_AUTOBUILD="${YUKIKO_WEBUI_AUTOBUILD:-1}"

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
