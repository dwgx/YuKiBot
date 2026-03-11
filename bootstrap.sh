#!/usr/bin/env bash
set -euo pipefail

# GitHub remote bootstrap installer for YuKiKo.
# Usage examples:
#   bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh)
#   bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081

REPO_URL_DEFAULT="https://github.com/dwgx/YuKiBot.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT=""
KEEP_EXISTING=0

info() { printf '[BOOTSTRAP] %s\n' "$*"; }
warn() { printf '[BOOTSTRAP][WARN] %s\n' "$*" >&2; }
error() { printf '[BOOTSTRAP][ERROR] %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
Usage: bash bootstrap.sh [bootstrap-options] [-- install-options]

Bootstrap options:
  --repo-url <url>         Git repository URL (default: https://github.com/dwgx/YuKiBot.git)
  --branch <name>          Git branch/tag to checkout (default: main)
  --install-dir <path>     Target directory for repo (default: /opt/yukiko-bot if root, else $HOME/yukiko-bot)
  --keep-existing          If install dir exists and is not empty, do not delete it
  -h, --help               Show this help

All arguments after `--` are forwarded to install.sh.

Examples:
  bash bootstrap.sh
  bash bootstrap.sh -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko
  bash bootstrap.sh --install-dir /opt/yukiko-bot --branch main -- --open-firewall
EOF
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

resolve_default_install_dir() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    printf '/opt/yukiko-bot'
  else
    printf '%s/yukiko-bot' "${HOME:-$PWD}"
  fi
}

ensure_dir_parent() {
  local dir="$1"
  local parent
  parent="$(dirname "$dir")"
  mkdir -p "$parent"
}

parse_args() {
  FORWARD_ARGS=()
  local mode="bootstrap"
  while [[ $# -gt 0 ]]; do
    if [[ "$mode" == "forward" ]]; then
      FORWARD_ARGS+=("$1")
      shift
      continue
    fi
    case "$1" in
      --repo-url)
        REPO_URL="${2:-}"
        shift 2
        ;;
      --branch)
        BRANCH="${2:-}"
        shift 2
        ;;
      --install-dir)
        INSTALL_DIR="${2:-}"
        shift 2
        ;;
      --keep-existing)
        KEEP_EXISTING=1
        shift
        ;;
      --)
        mode="forward"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        # Treat unknown args as install.sh args for convenience.
        mode="forward"
        ;;
    esac
  done
}

prepare_repo() {
  local repo_url="$1"
  local branch="$2"
  local install_dir="$3"

  if [[ -d "$install_dir/.git" ]]; then
    info "Existing git repo detected: $install_dir"
    git -C "$install_dir" fetch --prune origin
    git -C "$install_dir" checkout "$branch"
    git -C "$install_dir" pull --ff-only origin "$branch"
    return
  fi

  if [[ -d "$install_dir" && -n "$(ls -A "$install_dir" 2>/dev/null || true)" ]]; then
    if [[ "$KEEP_EXISTING" -eq 1 ]]; then
      error "Install dir is not empty and --keep-existing is set: $install_dir"
      error "Please ensure it contains a valid YuKiKo git repo."
      exit 1
    fi
    warn "Install dir exists and is not empty, removing: $install_dir"
    rm -rf "$install_dir"
  fi

  ensure_dir_parent "$install_dir"
  info "Cloning $repo_url -> $install_dir"
  git clone --depth 1 --branch "$branch" "$repo_url" "$install_dir"
}

main() {
  REPO_URL="$REPO_URL_DEFAULT"
  BRANCH="$BRANCH_DEFAULT"
  INSTALL_DIR="$INSTALL_DIR_DEFAULT"
  FORWARD_ARGS=()

  parse_args "$@"

  if [[ -z "$INSTALL_DIR" ]]; then
    INSTALL_DIR="$(resolve_default_install_dir)"
  fi
  if [[ -z "$REPO_URL" || -z "$BRANCH" || -z "$INSTALL_DIR" ]]; then
    error "Invalid bootstrap arguments."
    usage
    exit 1
  fi

  if ! command_exists git; then
    error "git is required but not found. Install git first."
    exit 1
  fi

  prepare_repo "$REPO_URL" "$BRANCH" "$INSTALL_DIR"

  local installer="$INSTALL_DIR/install.sh"
  if [[ ! -f "$installer" ]]; then
    error "install.sh not found in repo: $installer"
    exit 1
  fi

  info "Running installer: $installer ${FORWARD_ARGS[*]:-}"
  exec bash "$installer" "${FORWARD_ARGS[@]}"
}

main "$@"

