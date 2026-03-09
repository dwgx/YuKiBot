#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
NEED_DEPLOY=0

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
  "$VENV_PY" main.py "$@"
fi
