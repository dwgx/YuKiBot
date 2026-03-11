#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${YUKIKO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$ROOT_DIR/.env"
SERVICE_TEMPLATE="$ROOT_DIR/deploy/systemd/yukiko.service.template"
DEFAULT_SERVICE_NAME="${YUKIKO_SERVICE_NAME:-yukiko}"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command_exists sudo; then
    sudo "$@"
  else
    error "Need root privileges for: $* (sudo not found)."
    exit 1
  fi
}

service_path() {
  local service_name="$1"
  printf '/etc/systemd/system/%s.service' "$service_name"
}

service_exists() {
  local service_name="$1"
  local path
  path="$(service_path "$service_name")"
  if run_root test -f "$path"; then
    return 0
  fi
  return 1
}

validate_port() {
  local port="$1"
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if (( port < 1 || port > 65535 )); then
    return 1
  fi
  return 0
}

upsert_env() {
  local key="$1"
  local value="$2"
  local escaped="$value"
  escaped="${escaped//\\/\\\\}"
  escaped="${escaped//&/\\&}"
  escaped="${escaped//|/\\|}"

  if [[ ! -f "$ENV_FILE" ]]; then
    touch "$ENV_FILE"
  fi

  if grep -q -E "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|g" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

usage() {
  cat <<EOF
YuKiKo CLI Manager

Usage:
  yukiko <command> [options]
  yukiko --help

Commands:
  help                              Show this help
  install [install.sh options]      Run interactive/non-interactive installer
  run [main.py args]                Run app in foreground (same as start.sh)
  start [--service-name NAME]       systemctl start service
  stop [--service-name NAME]        systemctl stop service
  restart [--service-name NAME]     systemctl restart service
  status [--service-name NAME]      systemctl status service
  logs [--service-name NAME] [--lines N] [--no-follow]
                                    Show service logs
  register [--service-name NAME] [--user USER] [--enable-now|--no-enable-now]
                                    Register systemd service
  unregister [--service-name NAME]  Stop/disable and remove service file
  set-port --port N [--host H]      Update HOST/PORT in .env
  uninstall [options]               Perfect uninstall helper

Uninstall options:
  --service-name NAME               Service name (default: ${DEFAULT_SERVICE_NAME})
  --purge-runtime                   Remove .venv, webui/node_modules, webui/dist
  --purge-env                       Remove .env and .env.prod
  --keep-cli                        Keep /usr/local/bin/yukiko
  --yes                             No confirmation prompt

Examples:
  yukiko --help
  yukiko install --host 0.0.0.0 --port 18081
  yukiko register --service-name yukiko --user \$USER
  yukiko start
  yukiko logs --lines 200
  yukiko set-port --port 8088 --host 0.0.0.0
  yukiko uninstall --purge-runtime --purge-env --yes
EOF
}

register_service() {
  local service_name="$1"
  local service_user="$2"
  local enable_now="$3"
  local path
  path="$(service_path "$service_name")"

  if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
    error "Service template missing: $SERVICE_TEMPLATE"
    exit 1
  fi

  local rendered
  rendered="$(mktemp)"
  sed \
    -e "s|{{SERVICE_NAME}}|${service_name}|g" \
    -e "s|{{SERVICE_USER}}|${service_user}|g" \
    -e "s|{{WORKDIR}}|${ROOT_DIR}|g" \
    "$SERVICE_TEMPLATE" >"$rendered"

  run_root mkdir -p /etc/systemd/system
  run_root cp "$rendered" "$path"
  rm -f "$rendered"

  run_root systemctl daemon-reload
  run_root systemctl enable "$service_name"
  if [[ "$enable_now" -eq 1 ]]; then
    run_root systemctl restart "$service_name"
  fi
  info "Service registered: $service_name"
}

unregister_service() {
  local service_name="$1"
  local path
  path="$(service_path "$service_name")"

  if service_exists "$service_name"; then
    run_root systemctl stop "$service_name" || true
    run_root systemctl disable "$service_name" || true
    run_root rm -f "$path"
    run_root systemctl daemon-reload
    run_root systemctl reset-failed || true
    info "Service removed: $service_name"
  else
    warn "Service not found: $service_name"
  fi
}

cmd_install() {
  exec bash "$ROOT_DIR/install.sh" "$@"
}

cmd_run() {
  exec bash "$ROOT_DIR/start.sh" "$@"
}

cmd_service_action() {
  local action="$1"
  shift
  local service_name="$DEFAULT_SERVICE_NAME"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      *)
        error "Unknown option for $action: $1"
        exit 1
        ;;
    esac
  done

  if [[ -z "$service_name" ]]; then
    error "Service name cannot be empty."
    exit 1
  fi

  run_root systemctl "$action" "$service_name"
}

cmd_status() {
  local service_name="$DEFAULT_SERVICE_NAME"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      *)
        error "Unknown option for status: $1"
        exit 1
        ;;
    esac
  done
  if [[ -z "$service_name" ]]; then
    error "Service name cannot be empty."
    exit 1
  fi
  run_root systemctl status "$service_name" --no-pager
}

cmd_logs() {
  local service_name="$DEFAULT_SERVICE_NAME"
  local lines=200
  local follow=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --lines)
        lines="${2:-}"
        shift 2
        ;;
      --no-follow)
        follow=0
        shift
        ;;
      *)
        error "Unknown option for logs: $1"
        exit 1
        ;;
    esac
  done

  if [[ ! "$lines" =~ ^[0-9]+$ ]]; then
    error "--lines must be a positive number."
    exit 1
  fi

  if [[ "$follow" -eq 1 ]]; then
    run_root journalctl -u "$service_name" -n "$lines" -f
  else
    run_root journalctl -u "$service_name" -n "$lines" --no-pager
  fi
}

cmd_register() {
  local service_name="$DEFAULT_SERVICE_NAME"
  local service_user="${SUDO_USER:-${USER:-root}}"
  local enable_now=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --user)
        service_user="${2:-}"
        shift 2
        ;;
      --enable-now)
        enable_now=1
        shift
        ;;
      --no-enable-now)
        enable_now=0
        shift
        ;;
      *)
        error "Unknown option for register: $1"
        exit 1
        ;;
    esac
  done

  if [[ -z "$service_name" || -z "$service_user" ]]; then
    error "Service name and user cannot be empty."
    exit 1
  fi

  register_service "$service_name" "$service_user" "$enable_now"
}

cmd_unregister() {
  local service_name="$DEFAULT_SERVICE_NAME"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      *)
        error "Unknown option for unregister: $1"
        exit 1
        ;;
    esac
  done
  unregister_service "$service_name"
}

cmd_set_port() {
  local host=""
  local port=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)
        host="${2:-}"
        shift 2
        ;;
      --port)
        port="${2:-}"
        shift 2
        ;;
      *)
        error "Unknown option for set-port: $1"
        exit 1
        ;;
    esac
  done

  if [[ -z "$port" ]]; then
    error "set-port requires --port"
    exit 1
  fi
  if ! validate_port "$port"; then
    error "Invalid port: $port (must be 1-65535)"
    exit 1
  fi

  upsert_env "PORT" "$port"
  if [[ -n "$host" ]]; then
    upsert_env "HOST" "$host"
  fi
  info "Updated .env -> HOST=${host:-unchanged}, PORT=$port"
}

cmd_uninstall() {
  local service_name="$DEFAULT_SERVICE_NAME"
  local purge_runtime=0
  local purge_env=0
  local remove_cli=1
  local assume_yes=0
  local cli_path="/usr/local/bin/yukiko"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --purge-runtime)
        purge_runtime=1
        shift
        ;;
      --purge-env)
        purge_env=1
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
        error "Unknown option for uninstall: $1"
        exit 1
        ;;
    esac
  done

  if [[ "$assume_yes" -eq 0 ]]; then
    echo "About to uninstall YuKiKo deployment bits from: $ROOT_DIR"
    echo "- service: $service_name (stop/disable/remove)"
    if [[ "$purge_runtime" -eq 1 ]]; then
      echo "- purge runtime: .venv, webui/node_modules, webui/dist"
    fi
    if [[ "$purge_env" -eq 1 ]]; then
      echo "- purge env: .env, .env.prod"
    fi
    if [[ "$remove_cli" -eq 1 ]]; then
      echo "- remove CLI: $cli_path (if linked to this repo)"
    fi
    read -r -p "Continue uninstall? [yes/no]: " confirm
    if [[ "${confirm,,}" != "yes" ]]; then
      warn "Uninstall cancelled."
      exit 0
    fi
  fi

  unregister_service "$service_name"

  if [[ "$remove_cli" -eq 1 ]]; then
    if run_root test -f "$cli_path"; then
      if run_root grep -q "$ROOT_DIR/scripts/yukiko_manager.sh" "$cli_path"; then
        run_root rm -f "$cli_path"
        info "Removed CLI command: $cli_path"
      else
        warn "$cli_path exists but does not point to current repo, skipped."
      fi
    else
      warn "CLI command not found at $cli_path"
    fi
  fi

  if [[ "$purge_runtime" -eq 1 ]]; then
    rm -rf "$ROOT_DIR/.venv" "$ROOT_DIR/webui/node_modules" "$ROOT_DIR/webui/dist"
    info "Runtime artifacts removed."
  fi

  if [[ "$purge_env" -eq 1 ]]; then
    rm -f "$ROOT_DIR/.env" "$ROOT_DIR/.env.prod"
    info "Environment files removed."
  fi

  info "Uninstall flow completed."
}

main() {
  local cmd="${1:-help}"
  shift || true

  case "$cmd" in
    help|-h|--help)
      usage
      ;;
    install)
      cmd_install "$@"
      ;;
    run)
      cmd_run "$@"
      ;;
    start|stop|restart)
      cmd_service_action "$cmd" "$@"
      ;;
    status)
      cmd_status "$@"
      ;;
    logs)
      cmd_logs "$@"
      ;;
    register)
      cmd_register "$@"
      ;;
    unregister)
      cmd_unregister "$@"
      ;;
    set-port)
      cmd_set_port "$@"
      ;;
    uninstall)
      cmd_uninstall "$@"
      ;;
    *)
      error "Unknown command: $cmd"
      usage
      exit 1
      ;;
  esac
}

main "$@"
