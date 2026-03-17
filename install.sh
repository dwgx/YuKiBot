#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
SERVICE_TEMPLATE="$ROOT_DIR/deploy/systemd/yukiko.service.template"
MANAGER_SCRIPT="$ROOT_DIR/scripts/yukiko_manager.sh"
NAPCAT_INSTALLER_URL="https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh"
NAPCAT_GUIDE_URL="https://napneko.github.io/guide/boot/Shell"

NON_INTERACTIVE=0
AUTO_INSTALL_SERVICE=1
AUTO_OPEN_FIREWALL=1
SKIP_WEBUI_BUILD=0
SKIP_CLI_INSTALL=0
SKIP_NAPCAT=0
FAST_DEPLOY=0
SKIP_POST_CHECK=0
POST_CHECK_TIMEOUT=20
PIP_INDEX_URL_INPUT="${YUKIKO_PIP_INDEX_URL:-}"
PIP_EXTRA_INDEX_URL_INPUT="${YUKIKO_PIP_EXTRA_INDEX_URL:-}"
PIP_FIND_LINKS_INPUT="${YUKIKO_PIP_FIND_LINKS:-}"
PIP_CACHE_DIR_INPUT="${YUKIKO_PIP_CACHE_DIR:-}"
PIP_TIMEOUT_INPUT="${YUKIKO_PIP_TIMEOUT:-}"
PIP_RETRIES_INPUT="${YUKIKO_PIP_RETRIES:-}"
NPM_REGISTRY_INPUT="${YUKIKO_NPM_REGISTRY:-}"
NPM_CACHE_DIR_INPUT="${YUKIKO_NPM_CACHE_DIR:-}"
USE_UV_INPUT="${YUKIKO_USE_UV:-}"

HOST_INPUT=""
PORT_INPUT=""
WEBUI_TOKEN_INPUT=""
ONEBOT_ACCESS_TOKEN_INPUT=""
SERVICE_NAME_INPUT="yukiko"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

is_tty_session() {
  [[ -t 0 && -t 1 ]]
}

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --host <host>             Bind host written to .env (default: keep current or 0.0.0.0)
  --port <port>             Bind port written to .env (default: keep current or 8081)
  --webui-token <token>     WebUI token written to .env
  --onebot-access-token <token>
                            OneBot Access Token written to .env
  --service-name <name>     systemd service name (default: yukiko)
  --service                 Enable systemd install (default)
  --no-service              Skip systemd install
  --open-firewall           Try opening selected port in firewall (default)
  --no-firewall             Do not touch firewall
  --skip-webui-build        Skip npm build step
  --skip-cli-install        Skip installing /usr/local/bin/yukiko
  --skip-napcat             Skip NapCat detection and install
  --skip-post-check         Skip strict post-deploy health checks
  --post-check-timeout <s>  Post-deploy check timeout in seconds (default: 20)
  --fast                    Fast deploy: skip webui build + skip NapCat auto-install
  --pip-index-url <url>     Custom Python package index mirror
  --pip-extra-index-url <url>
                            Secondary Python package index mirror
  --pip-find-links <path>   Local wheel dir / extra Python package source
  --pip-cache-dir <dir>     Reuse pip cache directory for faster deploys
  --pip-timeout <s>         pip network timeout seconds
  --pip-retries <n>         pip retry count
  --npm-registry <url>      Custom npm registry mirror
  --npm-cache-dir <dir>     Reuse npm cache directory for faster deploys
  --use-uv                  Use uv for Python dependency sync when available
  --non-interactive         Use defaults and CLI arguments, no prompts
  -h, --help                Show this help

Examples:
  bash install.sh
  bash install.sh --host 0.0.0.0 --port 18081 --open-firewall
  bash install.sh --fast --non-interactive --port 8081
  bash install.sh --pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple --npm-registry https://registry.npmmirror.com
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

export_if_nonempty() {
  local key="$1"
  local value="${2:-}"
  if [[ -n "$value" ]]; then
    export "$key=$value"
  fi
}

validate_optional_integer() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    return 0
  fi
  [[ "$value" =~ ^[0-9]+$ ]]
}

apply_acceleration_env() {
  export_if_nonempty "YUKIKO_PIP_INDEX_URL" "$PIP_INDEX_URL_INPUT"
  export_if_nonempty "YUKIKO_PIP_EXTRA_INDEX_URL" "$PIP_EXTRA_INDEX_URL_INPUT"
  export_if_nonempty "YUKIKO_PIP_FIND_LINKS" "$PIP_FIND_LINKS_INPUT"
  export_if_nonempty "YUKIKO_PIP_CACHE_DIR" "$PIP_CACHE_DIR_INPUT"
  export_if_nonempty "YUKIKO_PIP_TIMEOUT" "$PIP_TIMEOUT_INPUT"
  export_if_nonempty "YUKIKO_PIP_RETRIES" "$PIP_RETRIES_INPUT"
  export_if_nonempty "YUKIKO_NPM_REGISTRY" "$NPM_REGISTRY_INPUT"
  export_if_nonempty "YUKIKO_NPM_CACHE_DIR" "$NPM_CACHE_DIR_INPUT"
  if [[ -n "$USE_UV_INPUT" ]]; then
    export YUKIKO_USE_UV="$USE_UV_INPUT"
  fi
}

normalize_health_host() {
  local host="$1"
  if [[ -z "$host" || "$host" == "0.0.0.0" || "$host" == "::" || "$host" == "*" ]]; then
    printf '127.0.0.1'
    return
  fi
  printf '%s' "$host"
}

port_in_use() {
  local port="$1"
  if command_exists ss; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null 2>&1; then
      return 0
    fi
  fi
  if command_exists lsof; then
    if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
  fi
  if command_exists netstat; then
    if netstat -ltn 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

wait_service_active() {
  local service_name="$1"
  local timeout_seconds="${2:-40}"
  local start_ts now elapsed
  start_ts="$(date +%s)"
  while true; do
    if run_root systemctl is-active --quiet "$service_name"; then
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

wait_webui_health() {
  local host="$1"
  local port="$2"
  local timeout_seconds="${3:-20}"
  if ! command_exists curl; then
    warn "curl not found, skipped WebUI health check."
    return 2
  fi

  local health_host url start_ts now elapsed
  health_host="$(normalize_health_host "$host")"
  url="http://${health_host}:${port}/api/webui/health"
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

run_post_deploy_checks() {
  local service_name="$1"
  local host="$2"
  local port="$3"
  local timeout_seconds="$4"

  if [[ "$SKIP_POST_CHECK" -eq 1 ]]; then
    warn "Skipping post-deploy checks (--skip-post-check)."
    return 0
  fi

  info "Running strict post-deploy checks..."
  if ! wait_service_active "$service_name" "$timeout_seconds"; then
    error "Service did not become active in ${timeout_seconds}s: $service_name"
    run_root systemctl status "$service_name" --no-pager || true
    return 1
  fi
  if ! wait_webui_health "$host" "$port" "$timeout_seconds"; then
    error "WebUI health check failed."
    run_root journalctl -u "$service_name" --no-pager -n 120 || true
    return 1
  fi

  if [[ -f "$MANAGER_SCRIPT" ]]; then
    if ! YUKIKO_ROOT="$ROOT_DIR" YUKIKO_SERVICE_NAME="$service_name" bash "$MANAGER_SCRIPT" doctor --service-name "$service_name" --timeout-seconds "$timeout_seconds" --strict; then
      error "Strict doctor failed after deployment."
      return 1
    fi
  fi

  info "Post-deploy checks passed."
  return 0
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
      if [[ "$FAST_DEPLOY" -eq 1 ]]; then
        warn "Fast mode enabled: skip apt-get update."
      else
        run_root apt-get update
      fi
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
  local onebot_access_token="$4"
  upsert_env "HOST" "$host"
  upsert_env "PORT" "$port"
  upsert_env "WEBUI_TOKEN" "$token"
  upsert_env "ONEBOT_ACCESS_TOKEN" "$onebot_access_token"
  info "Updated .env: HOST=$host PORT=$port WEBUI_TOKEN=*** ONEBOT_ACCESS_TOKEN=***"
}

bootstrap_python() {
  if ! command_exists python3; then
    error "python3 not found after dependency installation."
    exit 1
  fi
  info "Bootstrapping python environment..."
  apply_acceleration_env
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
  apply_acceleration_env
  export YUKIKO_WEBUI_FORCE_INSTALL=1
  bash "$ROOT_DIR/build-webui.sh"
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

install_cli_command() {
  local service_name="$1"
  local cli_path="/usr/local/bin/yukiko"
  local wrapper
  wrapper="$(mktemp)"

  if [[ ! -f "$MANAGER_SCRIPT" ]]; then
    error "Manager script missing: $MANAGER_SCRIPT"
    exit 1
  fi

  cat >"$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export YUKIKO_ROOT="$ROOT_DIR"
export YUKIKO_SERVICE_NAME="$service_name"
exec /usr/bin/env bash "$MANAGER_SCRIPT" "\$@"
EOF

  run_root install -m 0755 "$wrapper" "$cli_path"
  rm -f "$wrapper"
  info "CLI installed: yukiko -> $MANAGER_SCRIPT"
}

print_napcat_manual_hint() {
  warn "See: $NAPCAT_GUIDE_URL"
  warn "Manual install:"
  warn "  curl -o napcat.sh $NAPCAT_INSTALLER_URL && bash napcat.sh"
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

    if find "$root" -maxdepth 4 -type f \( -path "*/Nap[Cc]at*/opt/QQ/qq" -o -path "*/napcat*/opt/QQ/qq" \) 2>/dev/null | grep -q .; then
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

  # Check common NapCat install locations
  if command_exists napcat; then
    echo "binary"
    return
  fi

  # Check for NapCat Shell installation (most common)
  if [[ -f /opt/QQ/resources/app/app_launcher/napcat/napcat.mjs ]]; then
    echo "shell"
    return
  fi

  if [[ -f /opt/QQ/resources/app/napcat/napcat.mjs ]]; then
    echo "shell"
    return
  fi

  # Check for NapCat in current project directory (Windows-style structure)
  if [[ -d "$ROOT_DIR/../NapCat.Shell.Windows.Node" ]]; then
    echo "local"
    return
  fi

  custom_tree="$(detect_napcat_custom_tree || true)"
  if [[ -n "$custom_tree" ]]; then
    echo "$custom_tree"
    return
  fi

  # Check for any NapCat directory in parent
  if find "$ROOT_DIR/.." -maxdepth 1 -type d -iname "*napcat*" 2>/dev/null | grep -q .; then
    echo "local"
    return
  fi

  # Check systemd service
  if command_exists systemctl; then
    if systemctl list-units --all --type=service --no-legend 2>/dev/null | grep -Eiq 'napcat'; then
      echo "systemd"
      return
    fi
    if systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -Eiq 'napcat'; then
      echo "systemd"
      return
    fi
  fi

  # Check docker
  if command_exists docker && docker ps -a 2>/dev/null | grep -qi napcat; then
    echo "docker"
    return
  fi

  # Check running process
  local process_match
  process_match="$(detect_napcat_process || true)"
  if [[ -n "$process_match" ]]; then
    echo "$process_match"
    return
  fi

  # Check for node_modules with napcat
  if [[ -d "$ROOT_DIR/../NapCat.Shell.Windows.Node/napcat/node_modules" ]]; then
    echo "local"
    return
  fi

  echo ""
}

install_napcat() {
  if [[ "$SKIP_NAPCAT" -eq 1 ]]; then
    warn "Skipping NapCat install (--skip-napcat)."
    return
  fi

  local napcat_status
  napcat_status="$(detect_napcat)"

  if [[ -n "$napcat_status" ]]; then
    info "NapCat already detected (method: $napcat_status). Skipping install."
    return
  fi

  local do_install=0
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    info "NapCat not detected. Installing in non-interactive mode..."
    do_install=1
  elif ! is_tty_session; then
    info "NapCat not detected and no TTY available. Falling back to non-interactive install..."
    do_install=1
  else
    if ask_yes_no "NapCat (QQ adapter) not detected. Install NapCat now?" "yes"; then
      do_install=1
    fi
  fi

  if [[ "$do_install" -eq 0 ]]; then
    warn "Skipping NapCat install. You will need to set it up manually."
    print_napcat_manual_hint
    return
  fi

  if ! command_exists curl; then
    warn "curl is missing, cannot download NapCat installer."
    print_napcat_manual_hint
    return
  fi

  info "Downloading NapCat installer..."
  local napcat_script
  napcat_script="$(mktemp)"
  if ! curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 10 --max-time 120 -o "$napcat_script" "$NAPCAT_INSTALLER_URL"; then
    warn "Failed to download NapCat installer. Please install manually:"
    print_napcat_manual_hint
    rm -f "$napcat_script"
    return
  fi

  info "Running NapCat installer..."
  if [[ "$NON_INTERACTIVE" -eq 1 ]] || ! is_tty_session; then
    bash "$napcat_script" --docker n --cli n --proxy 0 --force || {
      warn "NapCat auto-install failed. Please install manually after deployment."
      print_napcat_manual_hint
    }
  else
    bash "$napcat_script" --tui || {
      warn "NapCat install exited. You can retry later with:"
      print_napcat_manual_hint
    }
  fi
  rm -f "$napcat_script"

  local napcat_after
  napcat_after="$(detect_napcat)"
  if [[ -n "$napcat_after" ]]; then
    info "NapCat install check: detected ($napcat_after)."
  else
    warn "NapCat install check: still not detected."
    print_napcat_manual_hint
  fi
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
      --onebot-access-token)
        ONEBOT_ACCESS_TOKEN_INPUT="${2:-}"
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
      --skip-cli-install)
        SKIP_CLI_INSTALL=1
        shift
        ;;
      --skip-napcat)
        SKIP_NAPCAT=1
        shift
        ;;
      --skip-post-check)
        SKIP_POST_CHECK=1
        shift
        ;;
      --post-check-timeout)
        POST_CHECK_TIMEOUT="${2:-}"
        shift 2
        ;;
      --fast)
        FAST_DEPLOY=1
        SKIP_WEBUI_BUILD=1
        SKIP_NAPCAT=1
        shift
        ;;
      --pip-index-url)
        PIP_INDEX_URL_INPUT="${2:-}"
        shift 2
        ;;
      --pip-extra-index-url)
        PIP_EXTRA_INDEX_URL_INPUT="${2:-}"
        shift 2
        ;;
      --pip-find-links)
        PIP_FIND_LINKS_INPUT="${2:-}"
        shift 2
        ;;
      --pip-cache-dir)
        PIP_CACHE_DIR_INPUT="${2:-}"
        shift 2
        ;;
      --pip-timeout)
        PIP_TIMEOUT_INPUT="${2:-}"
        shift 2
        ;;
      --pip-retries)
        PIP_RETRIES_INPUT="${2:-}"
        shift 2
        ;;
      --npm-registry)
        NPM_REGISTRY_INPUT="${2:-}"
        shift 2
        ;;
      --npm-cache-dir)
        NPM_CACHE_DIR_INPUT="${2:-}"
        shift 2
        ;;
      --use-uv)
        USE_UV_INPUT="auto"
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

  local current_host current_port current_token current_onebot_access_token
  current_host="$(get_env_value HOST)"
  current_port="$(get_env_value PORT)"
  current_token="$(get_env_value WEBUI_TOKEN)"
  current_onebot_access_token="$(get_env_value ONEBOT_ACCESS_TOKEN)"

  local host_default port_default token_default onebot_access_token_default
  host_default="0.0.0.0"
  port_default="${current_port:-8081}"
  token_default="${current_token:-$(random_token)}"
  onebot_access_token_default="${current_onebot_access_token:-$(random_token)}"

  local host port webui_token onebot_access_token service_name install_service open_firewall
  host="${HOST_INPUT:-$host_default}"
  port="${PORT_INPUT:-$port_default}"
  webui_token="${WEBUI_TOKEN_INPUT:-$token_default}"
  onebot_access_token="${ONEBOT_ACCESS_TOKEN_INPUT:-$onebot_access_token_default}"
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
    onebot_access_token="$(ask_input "ONEBOT_ACCESS_TOKEN (NapCat OneBot token)" "$onebot_access_token")"
    service_name="$(ask_input "systemd service name" "$service_name")"

    if ask_yes_no "Install and start systemd service now?" "yes"; then
      install_service=1
    else
      install_service=0
    fi

    if ask_yes_no "Open firewall port ${port}/tcp automatically?" "yes"; then
      open_firewall=1
    else
      open_firewall=0
    fi
  fi

  if ! validate_port "$port"; then
    error "Invalid PORT: $port"
    exit 1
  fi
  if [[ ! "$POST_CHECK_TIMEOUT" =~ ^[0-9]+$ ]] || (( POST_CHECK_TIMEOUT < 5 || POST_CHECK_TIMEOUT > 120 )); then
    error "Invalid --post-check-timeout: $POST_CHECK_TIMEOUT (must be 5-120)"
    exit 1
  fi
  if ! validate_optional_integer "$PIP_TIMEOUT_INPUT"; then
    error "Invalid --pip-timeout: $PIP_TIMEOUT_INPUT"
    exit 1
  fi
  if ! validate_optional_integer "$PIP_RETRIES_INPUT"; then
    error "Invalid --pip-retries: $PIP_RETRIES_INPUT"
    exit 1
  fi
  if [[ -z "$service_name" ]]; then
    error "Service name cannot be empty."
    exit 1
  fi

  apply_acceleration_env

  if port_in_use "$port"; then
    local service_path_existing="/etc/systemd/system/${service_name}.service"
    if run_root test -f "$service_path_existing" && run_root systemctl is-active --quiet "$service_name"; then
      warn "Port ${port} is currently occupied, but ${service_name} service is active. Reusing this port."
    else
      if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
        error "Port ${port} is already in use. Choose another port or stop conflicting service."
        exit 1
      fi
      warn "Port ${port} appears to be in use."
      if ! ask_yes_no "Continue deployment and reuse this port anyway?" "no"; then
        error "Deployment cancelled due to port conflict."
        exit 1
      fi
    fi
  fi

  write_env_values "$host" "$port" "$webui_token" "$onebot_access_token"

  local pm
  pm="$(detect_pkg_manager)"
  install_system_packages "$pm"
  if [[ "$SKIP_WEBUI_BUILD" -eq 0 ]]; then
    ensure_node_18_plus "$pm"
  else
    warn "Node.js version check skipped because WebUI build is disabled."
  fi
  bootstrap_python
  build_webui
  install_napcat

  if [[ "$SKIP_CLI_INSTALL" -eq 0 ]]; then
    install_cli_command "$service_name"
  else
    warn "Skipping CLI install (--skip-cli-install)."
  fi

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
    run_post_deploy_checks "$service_name" "$host" "$port" "$POST_CHECK_TIMEOUT"
  fi

  echo
  echo "========================================"
  echo "Deploy completed."
  echo "========================================"
  echo "Host: $host"
  echo "Port: $port"
  echo "WebUI: http://${host}:${port}/webui/login"
  echo "CLI: yukiko --help"
  local napcat_final
  napcat_final="$(detect_napcat)"
  if [[ -n "$napcat_final" ]]; then
    echo "NapCat: installed ($napcat_final)"
  else
    echo "NapCat: not installed (set up manually: $NAPCAT_GUIDE_URL)"
  fi
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
