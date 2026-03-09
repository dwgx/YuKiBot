#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBUI_DIR="$ROOT_DIR/webui"

echo "========================================"
echo "YuKiKo WebUI Build Tool"
echo "========================================"

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not found. Please install Node.js 18+ first."
  exit 1
fi

cd "$WEBUI_DIR"

if [[ ! -d node_modules ]]; then
  echo "[1/2] node_modules not found, installing dependencies..."
  npm install
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
