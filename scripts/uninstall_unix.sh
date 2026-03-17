#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_BACKUP_DIR="$ROOT_DIR/backups"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

confirm_yes() {
  local prompt="$1"
  local confirm
  read -r -p "$prompt [yes/no]: " confirm
  [[ "${confirm,,}" == "yes" ]]
}

create_backup_archive() {
  local output_dir="$1"
  local prefix="$2"
  local ts archive_path
  ts="$(date +%Y%m%d_%H%M%S)"
  archive_path="${output_dir%/}/${prefix}_${ts}.tar.gz"
  mkdir -p "$output_dir"

  local -a include_paths=()
  [[ -f "$ROOT_DIR/.env" ]] && include_paths+=(".env")
  [[ -f "$ROOT_DIR/.env.prod" ]] && include_paths+=(".env.prod")
  [[ -d "$ROOT_DIR/config" ]] && include_paths+=("config")
  [[ -d "$ROOT_DIR/plugins/config" ]] && include_paths+=("plugins/config")
  [[ -d "$ROOT_DIR/storage" ]] && include_paths+=("storage")

  if [[ "${#include_paths[@]}" -eq 0 ]]; then
    return 1
  fi

  (
    cd "$ROOT_DIR"
    tar -czf "$archive_path" "${include_paths[@]}"
  )
  printf '%s' "$archive_path"
}

main() {
  local purge_runtime=0
  local purge_state=0
  local purge_env=0
  local purge_data=0
  local remove_cli=1
  local assume_yes=0
  local backup_before_purge=1
  local backup_dir="$DEFAULT_BACKUP_DIR"
  local backup_name="uninstall_backup"
  local cli_path="/usr/local/bin/yukiko"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --purge-runtime)
        purge_runtime=1
        shift
        ;;
      --purge-state)
        purge_state=1
        shift
        ;;
      --purge-env)
        purge_env=1
        shift
        ;;
      --purge-data)
        purge_data=1
        shift
        ;;
      --purge-all)
        purge_runtime=1
        purge_state=1
        purge_env=1
        purge_data=1
        shift
        ;;
      --backup-dir)
        backup_dir="${2:-}"
        shift 2
        ;;
      --backup-name)
        backup_name="${2:-}"
        shift 2
        ;;
      --no-backup)
        backup_before_purge=0
        shift
        ;;
      --keep-cli)
        remove_cli=0
        shift
        ;;
      --yes)
        assume_yes=1
        shift
        ;;
      *)
        error "Unknown option for uninstall.sh: $1"
        exit 1
        ;;
    esac
  done

  local destructive_purge=0
  if [[ "$purge_runtime" -eq 1 || "$purge_state" -eq 1 || "$purge_env" -eq 1 || "$purge_data" -eq 1 ]]; then
    destructive_purge=1
  fi

  if [[ "$assume_yes" -eq 0 ]]; then
    echo "About to uninstall YuKiKo local artifacts from: $ROOT_DIR"
    [[ "$purge_runtime" -eq 1 ]] && echo "- purge runtime: .venv, webui/node_modules, webui/dist"
    [[ "$purge_state" -eq 1 ]] && echo "- purge state: caches, sandboxes, coverage artifacts, temp files"
    [[ "$purge_env" -eq 1 ]] && echo "- purge env: .env, .env.prod"
    [[ "$purge_data" -eq 1 ]] && echo "- purge data: storage, logs, runtime and generated local data"
    if [[ "$remove_cli" -eq 1 ]]; then
      echo "- remove CLI: $cli_path (if linked to this repo)"
    fi
    if [[ "$destructive_purge" -eq 1 && "$backup_before_purge" -eq 1 ]]; then
      echo "- safety backup: ${backup_dir%/}/${backup_name}_<timestamp>.tar.gz"
    fi
    if ! confirm_yes "Continue uninstall?"; then
      warn "Uninstall cancelled."
      exit 0
    fi
  fi

  if [[ "$destructive_purge" -eq 1 && "$backup_before_purge" -eq 1 ]]; then
    local uninstall_backup
    uninstall_backup="$(create_backup_archive "$backup_dir" "$backup_name" || true)"
    if [[ -n "$uninstall_backup" ]]; then
      info "Created safety backup before uninstall: $uninstall_backup"
    else
      warn "Unable to create pre-uninstall backup, continuing."
    fi
  fi

  if command -v pkill >/dev/null 2>&1; then
    pkill -f "$ROOT_DIR.*main.py" >/dev/null 2>&1 || true
  fi

  if [[ "$remove_cli" -eq 1 && -f "$cli_path" ]]; then
    if grep -q "$ROOT_DIR" "$cli_path" 2>/dev/null; then
      rm -f "$cli_path"
      info "Removed CLI command: $cli_path"
    fi
  fi

  if [[ "$purge_runtime" -eq 1 ]]; then
    rm -rf "$ROOT_DIR/.venv" "$ROOT_DIR/webui/node_modules" "$ROOT_DIR/webui/dist"
    info "Runtime artifacts removed."
  fi

  if [[ "$purge_state" -eq 1 ]]; then
    rm -rf \
      "$ROOT_DIR/storage/cache" \
      "$ROOT_DIR/storage/sandbox" \
      "$ROOT_DIR/__pycache__" \
      "$ROOT_DIR/.pytest_cache" \
      "$ROOT_DIR/.mypy_cache" \
      "$ROOT_DIR/.ruff_cache" \
      "$ROOT_DIR/.hypothesis" \
      "$ROOT_DIR/.coverage" \
      "$ROOT_DIR/coverage.xml" \
      "$ROOT_DIR/htmlcov" \
      "$ROOT_DIR/tmp"
    info "Local state/cache artifacts removed."
  fi

  if [[ "$purge_env" -eq 1 ]]; then
    rm -f "$ROOT_DIR/.env" "$ROOT_DIR/.env.prod"
    info "Environment files removed."
  fi

  if [[ "$purge_data" -eq 1 ]]; then
    rm -rf "$ROOT_DIR/storage" "$ROOT_DIR/logs" "$ROOT_DIR/runtime" "$ROOT_DIR/tmp"
    info "Runtime data directories removed."
  fi

  info "Uninstall flow completed."
}

main "$@"
