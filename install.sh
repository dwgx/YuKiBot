#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
SERVICE_TEMPLATE="$ROOT_DIR/deploy/systemd/yukiko.service.template"

NON_INTERACTIVE=0
AUTO_INSTALL_SERVICE=1
AUTO_OPEN_FIREWALL=0
SKIP_WEBUI_BUILD=0

HOST_INPUT=""
PORT_INPUT=""
WEBUI_TOKEN_INPUT=""
SERVICE_NAME_INPUT="yukiko"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --host <host>             Bind host written to .env (default: keep current or 0.0.0.0)
  --port <port>             Bind port written to .env (default: keep current or 8081)
  --webui-token <token>     WebUI token written to .env
  --service-name <name>     systemd service name (default: yukiko)
  --service                 Enable systemd install (default)
  --no-service              Skip systemd install
  --open-firewall           Try opening selected port in firewall
  --no-firewall             Do not touch firewall (default)
  --skip-webui-build        Skip npm build step
  --non-interactive         Use defaults and CLI arguments, no prompts
  -h, --help                Show this help

Examples:
  bash install.sh
  bash install.sh --host 0.0.0.0 --port 18081 --open-firewall
  bash install.sh --non-interactive --port 9000 --no-service
EOF
}

require_linux() {
  if [[ "${OSTYPE:-}" != linux* ]]; then
    error "This installer is for Linux only."
    exit 1
  fi
}

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

run_root_shell() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    bash -lc "$*"
  elif command_exists sudo; then
    sudo bash -lc "$*"
  else
    error "Need root privileges for: $* (sudo not found)."
    exit 1
  fi
}

detect_pkg_manager() {
  if command_exists apt-get; then
    echo "apt"
    return
  fi
  if command_exists dnf; then
    echo "dnf"
    return
  fi
  if command_exists yum; then
    echo "yum"
    return
  fi
  if command_exists pacman; then
    echo "pacman"
    return
  fi
  if command_exists zypper; then
    echo "zypper"
    return
  fi
  echo ""
}

install_system_packages() {
  local pm="$1"
  info "Installing system dependencies..."
  case "$pm" in
    apt)
      run_root apt-get update
      run_root apt-get install -y \
        python3 python3-venv python3-pip curl git ca-certificates ffmpeg nodejs npm
      ;;
    dnf)
      run_root dnf install -y python3 python3-pip curl git ca-certificates ffmpeg nodejs npm
      ;;
    yum)
      run_root yum install -y python3 python3-pip curl git ca-certificates ffmpeg nodejs npm
      ;;
    pacman)
      run_root pacman -Sy --noconfirm python python-pip curl git ca-certificates ffmpeg nodejs npm
      ;;
    zypper)
      run_root zypper --non-interactive refresh
      run_root zypper --non-interactive install python3 python3-pip curl git ca-certificates ffmpeg nodejs npm
      ;;
    *)
      warn "Unknown package manager. Please ensure Python3/venv/pip, Node.js 18+, npm, git, curl, ffmpeg are installed."
      ;;
  esac
}

node_major_version() {
  if ! command_exists node; then
    echo 0
    return
  fi
  local ver
  ver="$(node -v 2>/dev/null || true)"
  ver="${ver#v}"
  echo "${ver%%.*}"
}

ensure_node_18_plus() {
  local pm="$1"
  local major
  major="$(node_major_version)"
  if [[ "$major" -ge 18 ]]; then
    return
  fi

  if [[ "$pm" == "apt" ]]; then
    warn "Detected Node.js < 18. Trying NodeSource 20.x..."
    run_root_shell "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
    run_root apt-get install -y nodejs
    major="$(node_major_version)"
  fi

  if [[ "$major" -lt 18 ]]; then
    error "Node.js 18+ is required for WebUI build. Current: $(node -v 2>/dev/null || echo 'not installed')"
    exit 1
  fi
}

random_token() {
  if command_exists openssl; then
    openssl rand -hex 24
  else
    printf 'yukiko_%s_%s' "$(date +%s)" "$RANDOM"
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

upsert_env() {
  local key="$1"
  local value="$2"
  local escaped="$value"
  escaped="${escaped//\\/\\\\}"
  escaped="${escaped//&/\\&}"
  escaped="${escaped//|/\\|}"

  if grep -q -E "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|g" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
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

ask_input() {
  local prompt="$1"
  local default_value="$2"
  local value
  read -r -p "$prompt [$default_value]: " value
  if [[ -z "$value" ]]; then
    value="$default_value"
  fi
  printf '%s' "$value"
}

ask_yes_no() {
  local prompt="$1"
  local default_value="$2"
  local answer
  while true; do
    read -r -p "$prompt [$default_value]: " answer
    answer="${answer:-$default_value}"
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

write_env_values() {
  local host="$1"
  local port="$2"
  local token="$3"
  upsert_env "HOST" "$host"
  upsert_env "PORT" "$port"
  upsert_env "WEBUI_TOKEN" "$token"
  info "Updated .env: HOST=$host PORT=$port WEBUI_TOKEN=***"
}

bootstrap_python() {
  if ! command_exists python3; then
    error "python3 not found after dependency installation."
    exit 1
  fi
  info "Bootstrapping python environment..."
  python3 "$ROOT_DIR/scripts/deploy.py"
}

build_webui() {
  if [[ "$SKIP_WEBUI_BUILD" -eq 1 ]]; then
    warn "Skipping WebUI build (--skip-webui-build)."
    return
  fi
  if ! command_exists npm; then
    error "npm not found, cannot build WebUI."
    exit 1
  fi

  info "Building WebUI..."
  pushd "$ROOT_DIR/webui" >/dev/null
  if [[ -f package-lock.json ]]; then
    npm ci --no-fund --no-audit || npm install --no-fund --no-audit
  else
    npm install --no-fund --no-audit
  fi
  npm run build
  popd >/dev/null
}

open_firewall_port() {
  local port="$1"
  if command_exists ufw && run_root ufw status | grep -q "Status: active"; then
    run_root ufw allow "${port}/tcp"
    info "UFW rule added: ${port}/tcp"
    return
  fi

  if command_exists firewall-cmd && run_root systemctl is-active --quiet firewalld; then
    run_root firewall-cmd --permanent --add-port="${port}/tcp"
    run_root firewall-cmd --reload
    info "firewalld rule added: ${port}/tcp"
    return
  fi

  warn "No active ufw/firewalld detected, skipped firewall changes."
}

install_systemd_service() {
  local service_name="$1"
  local service_user="$2"
  local workdir="$3"
  local service_path="/etc/systemd/system/${service_name}.service"
  local rendered
  rendered="$(mktemp)"

  if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
    error "Service template missing: $SERVICE_TEMPLATE"
    exit 1
  fi

  sed \
    -e "s|{{SERVICE_NAME}}|${service_name}|g" \
    -e "s|{{SERVICE_USER}}|${service_user}|g" \
    -e "s|{{WORKDIR}}|${workdir}|g" \
    "$SERVICE_TEMPLATE" >"$rendered"

  run_root mkdir -p /etc/systemd/system
  run_root cp "$rendered" "$service_path"
  rm -f "$rendered"

  run_root systemctl daemon-reload
  run_root systemctl enable --now "$service_name"
  info "systemd service ready: $service_name"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)
        HOST_INPUT="${2:-}"
        shift 2
        ;;
      --port)
        PORT_INPUT="${2:-}"
        shift 2
        ;;
      --webui-token)
        WEBUI_TOKEN_INPUT="${2:-}"
        shift 2
        ;;
      --service-name)
        SERVICE_NAME_INPUT="${2:-}"
        shift 2
        ;;
      --service)
        AUTO_INSTALL_SERVICE=1
        shift
        ;;
      --no-service)
        AUTO_INSTALL_SERVICE=0
        shift
        ;;
      --open-firewall)
        AUTO_OPEN_FIREWALL=1
        shift
        ;;
      --no-firewall)
        AUTO_OPEN_FIREWALL=0
        shift
        ;;
      --skip-webui-build)
        SKIP_WEBUI_BUILD=1
        shift
        ;;
      --non-interactive)
        NON_INTERACTIVE=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        error "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done
}

main() {
  require_linux
  parse_args "$@"
  cd "$ROOT_DIR"

  if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
      cp "$ENV_EXAMPLE" "$ENV_FILE"
      info "Created .env from .env.example"
    else
      touch "$ENV_FILE"
      warn ".env.example not found, created empty .env"
    fi
  fi

  local current_host current_port current_token
  current_host="$(get_env_value HOST)"
  current_port="$(get_env_value PORT)"
  current_token="$(get_env_value WEBUI_TOKEN)"

  local host_default port_default token_default
  host_default="${current_host:-0.0.0.0}"
  port_default="${current_port:-8081}"
  token_default="${current_token:-$(random_token)}"

  local host port webui_token service_name install_service open_firewall
  host="${HOST_INPUT:-$host_default}"
  port="${PORT_INPUT:-$port_default}"
  webui_token="${WEBUI_TOKEN_INPUT:-$token_default}"
  service_name="${SERVICE_NAME_INPUT:-yukiko}"
  install_service="$AUTO_INSTALL_SERVICE"
  open_firewall="$AUTO_OPEN_FIREWALL"

  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    echo "========================================"
    echo "YuKiKo Linux One-Click Deploy"
    echo "========================================"
    echo "Like 1Panel flow: fill config -> install -> start service."
    echo

    host="$(ask_input "Bind HOST" "$host")"

    while true; do
      port="$(ask_input "Bind PORT" "$port")"
      if validate_port "$port"; then
        break
      fi
      warn "Invalid port: $port (must be 1-65535)"
    done

    webui_token="$(ask_input "WEBUI_TOKEN" "$webui_token")"
    service_name="$(ask_input "systemd service name" "$service_name")"

    if ask_yes_no "Install and start systemd service now?" "yes"; then
      install_service=1
    else
      install_service=0
    fi

    if ask_yes_no "Open firewall port ${port}/tcp automatically?" "no"; then
      open_firewall=1
    else
      open_firewall=0
    fi
  fi

  if ! validate_port "$port"; then
    error "Invalid PORT: $port"
    exit 1
  fi
  if [[ -z "$service_name" ]]; then
    error "Service name cannot be empty."
    exit 1
  fi

  write_env_values "$host" "$port" "$webui_token"

  local pm
  pm="$(detect_pkg_manager)"
  install_system_packages "$pm"
  ensure_node_18_plus "$pm"
  bootstrap_python
  build_webui

  if [[ "$open_firewall" -eq 1 ]]; then
    open_firewall_port "$port"
  fi

  local service_user
  service_user="${SUDO_USER:-$USER}"
  if [[ "${EUID:-$(id -u)}" -eq 0 && -z "${service_user:-}" ]]; then
    service_user="root"
  fi

  if [[ "$install_service" -eq 1 ]]; then
    install_systemd_service "$service_name" "$service_user" "$ROOT_DIR"
  fi

  echo
  echo "========================================"
  echo "Deploy completed."
  echo "========================================"
  echo "Host: $host"
  echo "Port: $port"
  echo "WebUI: http://${host}:${port}/webui/login"
  if [[ "$install_service" -eq 1 ]]; then
    echo "Service: $service_name"
    echo "Status:  sudo systemctl status $service_name"
    echo "Logs:    sudo journalctl -u $service_name -f"
    echo "Restart: sudo systemctl restart $service_name"
  else
    echo "Run manually: bash start.sh"
  fi
}

main "$@"
