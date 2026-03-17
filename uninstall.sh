#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

has_scope=0
for arg in "$@"; do
  case "$arg" in
    --purge-runtime|--purge-state|--purge-env|--purge-data|--purge-all)
      has_scope=1
      ;;
  esac
done

default_scope=()
if [[ "$has_scope" -eq 0 ]]; then
  default_scope=(--purge-all)
fi

case "$(uname -s)" in
  Linux*)
    exec bash "$ROOT_DIR/scripts/yukiko_manager.sh" uninstall "${default_scope[@]}" "$@"
    ;;
  Darwin*)
    exec bash "$ROOT_DIR/scripts/uninstall_unix.sh" "${default_scope[@]}" "$@"
    ;;
  *)
    echo "[ERROR] Unsupported platform for uninstall.sh: $(uname -s)" >&2
    exit 1
    ;;
esac
