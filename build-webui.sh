#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBUI_DIR="$ROOT_DIR/webui"
FORCE_INSTALL="${YUKIKO_WEBUI_FORCE_INSTALL:-0}"

echo "========================================"
echo "YuKiKo WebUI Build Tool"
echo "========================================"

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not found. Please install Node.js 18+ first."
  exit 1
fi

if [[ -n "${YUKIKO_NPM_REGISTRY:-}" ]]; then
  export NPM_CONFIG_REGISTRY="$YUKIKO_NPM_REGISTRY"
  export npm_config_registry="$YUKIKO_NPM_REGISTRY"
fi

if [[ -n "${YUKIKO_NPM_CACHE_DIR:-}" ]]; then
  mkdir -p "$YUKIKO_NPM_CACHE_DIR"
  export NPM_CONFIG_CACHE="$YUKIKO_NPM_CACHE_DIR"
  export npm_config_cache="$YUKIKO_NPM_CACHE_DIR"
fi

export NPM_CONFIG_UPDATE_NOTIFIER=false

cd "$WEBUI_DIR"

NEED_INSTALL=0
if [[ ! -d node_modules ]]; then
  NEED_INSTALL=1
elif [[ "$FORCE_INSTALL" == "1" ]]; then
  NEED_INSTALL=1
elif [[ -f package-lock.json && ! -f node_modules/.package-lock.json ]]; then
  NEED_INSTALL=1
elif [[ -f package-lock.json && package-lock.json -nt node_modules/.package-lock.json ]]; then
  NEED_INSTALL=1
elif [[ -f package.json && package.json -nt node_modules ]]; then
  NEED_INSTALL=1
fi

if [[ "$NEED_INSTALL" == "1" ]]; then
  echo "[1/2] syncing WebUI dependencies..."
  if [[ -f package-lock.json ]]; then
    npm ci --prefer-offline --no-audit --no-fund || npm install --prefer-offline --no-audit --no-fund
  else
    npm install --prefer-offline --no-audit --no-fund
  fi
else
  echo "[1/2] dependencies already installed."
fi

echo "[2/2] building webui..."
npm run build

echo
echo "========================================"
echo "Build completed!"
echo "========================================"
echo "WebUI URL: http://127.0.0.1:8080/webui/"
