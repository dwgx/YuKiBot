#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${YUKIKO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$ROOT_DIR/.env"
SERVICE_TEMPLATE="$ROOT_DIR/deploy/systemd/yukiko.service.template"
DEFAULT_SERVICE_NAME="${YUKIKO_SERVICE_NAME:-yukiko}"
NAPCAT_INSTALLER_URL="https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh"
NAPCAT_GUIDE_URL="https://napneko.github.io/guide/boot/Shell"
DEFAULT_HEALTH_PATH="/api/webui/health"
DEFAULT_SERVICE_WAIT_SECONDS=40
DEFAULT_HEALTH_WAIT_SECONDS=35

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

run_root_nonfatal() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
    return $?
  fi
  if command_exists sudo; then
    sudo -n "$@"
    return $?
  fi
  return 127
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

get_env_value() {
  local key="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    return
  fi
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    printf '%s' "${line#*=}"
  fi
}

detect_napcat_custom_tree() {
  local -a candidate_roots=(
    "/root"
    "/opt"
    "/usr/local"
    "${HOME:-}"
    "$ROOT_DIR/.."
  )

  if [[ -n "${SUDO_USER:-}" ]]; then
    local sudo_home
    sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
    if [[ -n "$sudo_home" ]]; then
      candidate_roots+=("$sudo_home")
    fi
  fi

  local root
  for root in "${candidate_roots[@]}"; do
    [[ -n "$root" && -d "$root" ]] || continue

    if find "$root" -maxdepth 9 -type f -path "*/opt/QQ/resources/app/app_launcher/napcat/napcat.mjs" 2>/dev/null | grep -q .; then
      echo "custom_tree"
      return
    fi

    if find "$root" -maxdepth 9 -type f -path "*/opt/QQ/resources/app/napcat/napcat.mjs" 2>/dev/null | grep -q .; then
      echo "custom_tree"
      return
    fi
  done
}

detect_napcat_process() {
  command_exists pgrep || return 1

  if pgrep -fa napcat >/dev/null 2>&1; then
    echo "process"
    return 0
  fi

  if pgrep -fa "QQ/qq" 2>/dev/null | grep -Eiq '/[^ ]*napcat[^ ]*/opt/QQ/qq'; then
    echo "qq_process"
    return 0
  fi

  return 1
}

detect_napcat() {
  local custom_tree

  if command_exists napcat; then
    echo "binary"
    return
  fi

  if [[ -f /opt/QQ/resources/app/app_launcher/napcat/napcat.mjs ]]; then
    echo "shell_launcher"
    return
  fi

  if [[ -f /opt/QQ/resources/app/napcat/napcat.mjs ]]; then
    echo "shell_app"
    return
  fi

  if [[ -d "$ROOT_DIR/../NapCat.Shell.Windows.Node" ]]; then
    echo "local"
    return
  fi

  custom_tree="$(detect_napcat_custom_tree || true)"
  if [[ -n "$custom_tree" ]]; then
    echo "$custom_tree"
    return
  fi

  if find "$ROOT_DIR/.." -maxdepth 1 -type d -iname "*napcat*" 2>/dev/null | grep -q .; then
    echo "local"
    return
  fi

  if command_exists systemctl; then
    if systemctl list-units --all --type=service --no-legend 2>/dev/null | grep -Eiq 'napcat'; then
      echo "systemd_unit"
      return
    fi
    if systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -Eiq 'napcat'; then
      echo "systemd_file"
      return
    fi
  fi

  if command_exists docker && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eiq 'napcat'; then
    echo "docker"
    return
  fi

  local process_match
  process_match="$(detect_napcat_process || true)"
  if [[ -n "$process_match" ]]; then
    echo "$process_match"
    return
  fi

  echo ""
}

wait_service_active() {
  local service_name="$1"
  local timeout_seconds="${2:-$DEFAULT_SERVICE_WAIT_SECONDS}"
  local start_ts now elapsed
  start_ts="$(date +%s)"
  while true; do
    if run_root_nonfatal systemctl is-active --quiet "$service_name"; then
      return 0
    fi
    now="$(date +%s)"
    elapsed=$(( now - start_ts ))
    if (( elapsed >= timeout_seconds )); then
      return 1
    fi
    sleep 1
  done
}

normalize_health_host() {
  local host="$1"
  if [[ -z "$host" || "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    printf '127.0.0.1'
    return
  fi
  if [[ "$host" == "*" ]]; then
    printf '127.0.0.1'
    return
  fi
  printf '%s' "$host"
}

wait_webui_health() {
  local timeout_seconds="${1:-$DEFAULT_HEALTH_WAIT_SECONDS}"
  if ! command_exists curl; then
    warn "curl not found, skipped WebUI health check."
    return 2
  fi

  local host port health_host url start_ts now elapsed
  host="$(get_env_value HOST)"
  port="$(get_env_value PORT)"
  host="${host:-0.0.0.0}"
  port="${port:-8081}"
  health_host="$(normalize_health_host "$host")"
  url="http://${health_host}:${port}${DEFAULT_HEALTH_PATH}"

  start_ts="$(date +%s)"
  while true; do
    if curl -fsS --connect-timeout 2 --max-time 5 "$url" >/dev/null 2>&1; then
      info "WebUI health check passed: $url"
      return 0
    fi
    now="$(date +%s)"
    elapsed=$(( now - start_ts ))
    if (( elapsed >= timeout_seconds )); then
      warn "WebUI health check timed out: $url"
      return 1
    fi
    sleep 1
  done
}

cmd_napcat_status() {
  local method_only=0
  local quiet=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method-only)
        method_only=1
        shift
        ;;
      --quiet)
        quiet=1
        shift
        ;;
      *)
        error "Unknown option for napcat-status: $1"
        exit 1
        ;;
    esac
  done

  local method
  method="$(detect_napcat)"
  if [[ "$method_only" -eq 1 ]]; then
    printf '%s\n' "$method"
    [[ -n "$method" ]]
    return
  fi

  if [[ -n "$method" ]]; then
    if [[ "$quiet" -ne 1 ]]; then
      info "NapCat detected (method: $method)"
      if [[ -f /opt/QQ/resources/app/app_launcher/napcat/napcat.mjs ]]; then
        info "Detected shell path: /opt/QQ/resources/app/app_launcher/napcat/napcat.mjs"
      elif [[ -f /opt/QQ/resources/app/napcat/napcat.mjs ]]; then
        info "Detected shell path: /opt/QQ/resources/app/napcat/napcat.mjs"
      fi
    fi
    return 0
  fi

  if [[ "$quiet" -ne 1 ]]; then
    warn "NapCat not detected."
    warn "Guide: $NAPCAT_GUIDE_URL"
  fi
  return 1
}

uninstall_napcat() {
  local before_method
  before_method="$(detect_napcat)"
  if [[ -z "$before_method" ]]; then
    info "NapCat not detected, skip NapCat uninstall."
    return 0
  fi

  info "Attempting official NapCat uninstall..."
  local napcat_script=""
  local official_ok=0
  if command_exists curl; then
    napcat_script="$(mktemp)"
    if curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 10 --max-time 120 -o "$napcat_script" "$NAPCAT_INSTALLER_URL"; then
      if run_root_nonfatal bash "$napcat_script" --uninstall; then
        official_ok=1
      else
        warn "Official NapCat uninstall command returned non-zero."
      fi
    else
      warn "Failed to download NapCat installer for uninstall."
    fi
  else
    warn "curl not found, skip official NapCat uninstall."
  fi
  if [[ -n "$napcat_script" ]]; then
    rm -f "$napcat_script"
  fi

  if [[ "$official_ok" -ne 1 ]]; then
    warn "Falling back to manual NapCat cleanup."
  fi

  if command_exists systemctl; then
    local units
    units="$(run_root_nonfatal systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Ei 'napcat' || true)"
    if [[ -n "$units" ]]; then
      local unit
      while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        run_root_nonfatal systemctl stop "$unit" || true
        run_root_nonfatal systemctl disable "$unit" || true
      done <<< "$units"
      run_root_nonfatal systemctl daemon-reload || true
      run_root_nonfatal systemctl reset-failed || true
    fi
  fi

  if command_exists docker; then
    local docker_ids
    docker_ids="$(docker ps -a --format '{{.ID}} {{.Names}}' 2>/dev/null | awk 'tolower($0) ~ /napcat/ {print $1}' || true)"
    if [[ -n "$docker_ids" ]]; then
      local docker_id
      while IFS= read -r docker_id; do
        [[ -n "$docker_id" ]] || continue
        docker rm -f "$docker_id" >/dev/null 2>&1 || true
      done <<< "$docker_ids"
    fi
  fi

  local -a rm_paths=(
    "/opt/QQ/resources/app/app_launcher/napcat"
    "/opt/QQ/resources/app/napcat"
    "/opt/NapCat"
    "/usr/local/NapCat"
    "/usr/local/bin/napcat"
    "/usr/bin/napcat"
  )
  local p
  for p in "${rm_paths[@]}"; do
    if run_root_nonfatal test -e "$p"; then
      run_root_nonfatal rm -rf "$p" || true
    fi
  done

  local after_method
  after_method="$(detect_napcat)"
  if [[ -n "$after_method" ]]; then
    warn "NapCat still detected after cleanup (method: $after_method)."
    warn "Please check manually: $NAPCAT_GUIDE_URL"
    return 1
  fi

  info "NapCat uninstall cleanup completed."
  return 0
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
  update [options]                  Pull latest code and refresh runtime deps
  start [--service-name NAME]       systemctl start service
  stop [--service-name NAME]        systemctl stop service
  restart [--service-name NAME]     systemctl restart service
  status [--service-name NAME]      systemctl status service
  logs [--service-name NAME] [--lines N] [--no-follow]
                                    Show service logs
  register [--service-name NAME] [--user USER] [--enable-now|--no-enable-now]
                                    Register systemd service
  unregister [--service-name NAME]  Stop/disable and remove service file
  napcat-status [--method-only|--quiet]
                                    Detect NapCat installation status
  set-port --port N [--host H]      Update HOST/PORT in .env
  uninstall [options]               Perfect uninstall helper

Uninstall options:
  --service-name NAME               Service name (default: ${DEFAULT_SERVICE_NAME})
  --purge-runtime                   Remove .venv, webui/node_modules, webui/dist
  --purge-state                     Remove storage/cache, __pycache__, .pytest_cache
  --purge-env                       Remove .env and .env.prod
  --purge-all                       Shortcut for --purge-runtime --purge-state --purge-env
  --keep-cli                        Keep /usr/local/bin/yukiko
  --keep-napcat                     Keep NapCat (skip NapCat uninstall)
  --yes                             No confirmation prompt

Examples:
  yukiko --help
  yukiko install --host 0.0.0.0 --port 18081
  yukiko update --check-only
  yukiko update --no-hot-reload
  yukiko update --restart
  yukiko register --service-name yukiko --user \$USER
  yukiko start
  yukiko logs --lines 200
  yukiko napcat-status
  yukiko set-port --port 8088 --host 0.0.0.0
  yukiko uninstall --purge-runtime --purge-env --yes
  yukiko uninstall --purge-all --yes
EOF
}

cmd_update() {
  local service_name="$DEFAULT_SERVICE_NAME"
  local check_only=0
  local restart_service=0
  local install_python=1
  local build_webui=1
  local allow_dirty=0
  local hot_reload=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --check-only)
        check_only=1
        shift
        ;;
      --restart)
        restart_service=1
        shift
        ;;
      --no-python)
        install_python=0
        shift
        ;;
      --no-webui)
        build_webui=0
        shift
        ;;
      --allow-dirty)
        allow_dirty=1
        shift
        ;;
      --hot-reload)
        hot_reload=1
        shift
        ;;
      --no-hot-reload)
        hot_reload=0
        shift
        ;;
      *)
        error "Unknown option for update: $1"
        exit 1
        ;;
    esac
  done

  if ! command_exists git; then
    error "git not found. Cannot run update."
    exit 1
  fi

  if [[ ! -d "$ROOT_DIR/.git" ]]; then
    error "Current directory is not a git repository: $ROOT_DIR"
    exit 1
  fi

  info "Fetching remote changes..."
  git -C "$ROOT_DIR" fetch --prune --tags origin

  local branch upstream remote_head local_head status_line
  branch="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
  upstream="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null || true)"
  if [[ -z "$upstream" ]]; then
    upstream="origin/$branch"
  fi

  if ! git -C "$ROOT_DIR" rev-parse --verify "$upstream" >/dev/null 2>&1; then
    warn "Upstream ref not found: $upstream"
    status_line="branch=$branch upstream=$upstream status=unknown"
    echo "$status_line"
    if [[ "$check_only" -eq 1 ]]; then
      return 0
    fi
  fi

  local ahead=0 behind=0 dirty=0
  if git -C "$ROOT_DIR" rev-parse --verify "$upstream" >/dev/null 2>&1; then
    read -r ahead behind < <(git -C "$ROOT_DIR" rev-list --left-right --count "$upstream...HEAD")
  fi
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
    dirty=1
  fi

  local_head="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  remote_head="$(git -C "$ROOT_DIR" rev-parse --short "$upstream" 2>/dev/null || echo "unknown")"
  status_line="branch=$branch upstream=$upstream local=$local_head remote=$remote_head ahead=$ahead behind=$behind dirty=$dirty"
  echo "$status_line"

  if [[ "$check_only" -eq 1 ]]; then
    return 0
  fi

  if [[ "$dirty" -eq 1 && "$allow_dirty" -ne 1 ]]; then
    error "Working tree has local changes. Commit/stash first, or retry with --allow-dirty."
    exit 1
  fi

  if [[ "$behind" -gt 0 ]]; then
    info "Pulling latest commits (ff-only)..."
    git -C "$ROOT_DIR" pull --ff-only
  else
    info "Already up to date with $upstream."
  fi

  if [[ "$install_python" -eq 1 ]]; then
    local py_cmd=""
    if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
      py_cmd="$ROOT_DIR/.venv/bin/python"
    elif command_exists python3; then
      py_cmd="python3"
    elif command_exists python; then
      py_cmd="python"
    fi
    if [[ -n "$py_cmd" ]]; then
      info "Installing Python dependencies..."
      "$py_cmd" -m pip install -r "$ROOT_DIR/requirements.txt"
    else
      warn "No python executable found, skipped Python dependency sync."
    fi
  fi

  if [[ "$build_webui" -eq 1 ]]; then
    if [[ -f "$ROOT_DIR/webui/package.json" ]]; then
      if command_exists npm; then
        info "Building WebUI..."
        (
          cd "$ROOT_DIR/webui"
          if [[ -f package-lock.json ]]; then
            npm ci --no-audit --no-fund || npm install --no-audit --no-fund
          else
            npm install --no-audit --no-fund
          fi
          npm run build
        )
      else
        warn "npm not found, skipped WebUI build."
      fi
    fi
  fi

  local hot_reload_done=0
  local service_ready=0
  local health_ok=0
  if [[ "$restart_service" -eq 1 ]]; then
    if [[ -z "$service_name" ]]; then
      error "Service name cannot be empty."
      exit 1
    fi
    info "Restarting service: $service_name"
    run_root systemctl restart "$service_name"
    hot_reload_done=1
  elif [[ "$hot_reload" -eq 1 ]]; then
    if [[ -z "$service_name" ]]; then
      warn "Service name is empty, skipped automatic hot reload."
    elif ! command_exists systemctl; then
      warn "systemctl not found, skipped automatic hot reload."
    else
      local svc_path
      svc_path="$(service_path "$service_name")"
      if run_root_nonfatal test -f "$svc_path"; then
        info "Hot reloading service via systemd restart: $service_name"
        if run_root_nonfatal systemctl restart "$service_name"; then
          info "Hot reload completed: service restarted."
          hot_reload_done=1
        else
          warn "Hot reload failed: unable to restart service '$service_name'."
        fi
      else
        warn "Service file not found ($svc_path), skipped automatic hot reload."
      fi
    fi
  fi

  if [[ "$hot_reload_done" -eq 1 ]]; then
    if wait_service_active "$service_name" "$DEFAULT_SERVICE_WAIT_SECONDS"; then
      service_ready=1
      info "Service active check passed: $service_name"
    else
      warn "Service did not become active within ${DEFAULT_SERVICE_WAIT_SECONDS}s: $service_name"
    fi

    if wait_webui_health "$DEFAULT_HEALTH_WAIT_SECONDS"; then
      health_ok=1
    fi
  fi

  if [[ "$behind" -gt 0 ]]; then
    if [[ "$hot_reload_done" -eq 1 && "$service_ready" -eq 1 && "$health_ok" -eq 1 ]]; then
      info "Update flow completed. New code is active and WebUI health is OK."
    elif [[ "$hot_reload_done" -eq 1 ]]; then
      warn "Update completed and restart was triggered, but readiness checks were incomplete."
    else
      warn "Update completed, but automatic hot reload did not finish. Please restart service manually to apply Python code."
    fi
  else
    info "Update flow completed."
  fi
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
  local purge_state=0
  local purge_env=0
  local remove_cli=1
  local remove_napcat=1
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
      --purge-state)
        purge_state=1
        shift
        ;;
      --purge-env)
        purge_env=1
        shift
        ;;
      --purge-all)
        purge_runtime=1
        purge_state=1
        purge_env=1
        shift
        ;;
      --keep-cli)
        remove_cli=0
        shift
        ;;
      --keep-napcat)
        remove_napcat=0
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
    if [[ "$purge_state" -eq 1 ]]; then
      echo "- purge state: storage/cache, __pycache__, .pytest_cache"
    fi
    if [[ "$purge_env" -eq 1 ]]; then
      echo "- purge env: .env, .env.prod"
    fi
    if [[ "$remove_cli" -eq 1 ]]; then
      echo "- remove CLI: $cli_path (if linked to this repo)"
    fi
    if [[ "$remove_napcat" -eq 1 ]]; then
      echo "- remove NapCat: try official --uninstall and fallback cleanup"
    else
      echo "- keep NapCat: enabled (--keep-napcat)"
    fi
    read -r -p "Continue uninstall? [yes/no]: " confirm
    if [[ "${confirm,,}" != "yes" ]]; then
      warn "Uninstall cancelled."
      exit 0
    fi
  fi

  unregister_service "$service_name"

  if [[ "$remove_napcat" -eq 1 ]]; then
    uninstall_napcat || true
  fi

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

  if [[ "$purge_state" -eq 1 ]]; then
    rm -rf "$ROOT_DIR/storage/cache" "$ROOT_DIR/__pycache__" "$ROOT_DIR/.pytest_cache"
    info "Local state/cache artifacts removed."
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
    update)
      cmd_update "$@"
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
    napcat-status)
      cmd_napcat_status "$@"
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
