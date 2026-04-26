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
DEFAULT_BACKUP_DIR="$ROOT_DIR/backups"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

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
  local pip_index_url="${1:-}"
  local pip_extra_index_url="${2:-}"
  local pip_find_links="${3:-}"
  local pip_cache_dir="${4:-}"
  local pip_timeout="${5:-}"
  local pip_retries="${6:-}"
  local npm_registry="${7:-}"
  local npm_cache_dir="${8:-}"
  local use_uv="${9:-}"

  export_if_nonempty "YUKIKO_PIP_INDEX_URL" "$pip_index_url"
  export_if_nonempty "YUKIKO_PIP_EXTRA_INDEX_URL" "$pip_extra_index_url"
  export_if_nonempty "YUKIKO_PIP_FIND_LINKS" "$pip_find_links"
  export_if_nonempty "YUKIKO_PIP_CACHE_DIR" "$pip_cache_dir"
  export_if_nonempty "YUKIKO_PIP_TIMEOUT" "$pip_timeout"
  export_if_nonempty "YUKIKO_PIP_RETRIES" "$pip_retries"
  export_if_nonempty "YUKIKO_NPM_REGISTRY" "$npm_registry"
  export_if_nonempty "YUKIKO_NPM_CACHE_DIR" "$npm_cache_dir"
  if [[ -n "$npm_cache_dir" ]]; then
    mkdir -p "$npm_cache_dir" >/dev/null 2>&1 || true
  fi
  if [[ -n "$use_uv" ]]; then
    export YUKIKO_USE_UV="$use_uv"
  fi
}

confirm_yes() {
  local prompt="$1"
  local confirm
  read -r -p "$prompt [yes/no]: " confirm
  [[ "${confirm,,}" == "yes" ]]
}

resolve_bootstrap_python() {
  if command_exists python3; then
    printf 'python3'
    return 0
  fi
  if command_exists python; then
    printf 'python'
    return 0
  fi
  return 1
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

create_backup_archive() {
  local output_dir="$1"
  local prefix="$2"
  local ts archive_name archive_path
  ts="$(date +%Y%m%d_%H%M%S)"
  archive_name="${prefix}_${ts}.tar.gz"
  archive_path="${output_dir%/}/$archive_name"

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

restore_backup_archive() {
  local archive_file="$1"
  if [[ ! -f "$archive_file" ]]; then
    return 1
  fi
  (
    cd "$ROOT_DIR"
    tar -xzf "$archive_file"
  )
}

infer_update_tasks_from_diff() {
  local from_commit="$1"
  local to_commit="$2"
  local changed
  changed="$(git -C "$ROOT_DIR" diff --name-only "${from_commit}..${to_commit}" || true)"

  local needs_python=0
  local needs_webui=0
  local needs_webui_deps=0

  if printf '%s\n' "$changed" | grep -Eiq '(^|/)requirements(\.txt|/)|(^|/)pyproject\.toml$|(^|/)poetry\.lock$|(^|/)Pipfile(\.lock)?$'; then
    needs_python=1
  fi

  if printf '%s\n' "$changed" | grep -Eiq '^webui/'; then
    needs_webui=1
  fi

  if printf '%s\n' "$changed" | grep -Eiq '^webui/(package(-lock)?\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock)$'; then
    needs_webui_deps=1
  fi

  printf 'needs_python=%s needs_webui=%s needs_webui_deps=%s\n' "$needs_python" "$needs_webui" "$needs_webui_deps"
}

rollback_update() {
  local rollback_commit="$1"
  local service_name="$2"

  warn "Attempting rollback to commit: $rollback_commit"
  if ! git -C "$ROOT_DIR" reset --hard "$rollback_commit"; then
    error "Rollback failed: unable to reset to $rollback_commit"
    return 1
  fi

  if command_exists systemctl; then
    local svc_path
    svc_path="$(service_path "$service_name")"
    if run_root_nonfatal test -f "$svc_path"; then
      if run_root_nonfatal systemctl restart "$service_name"; then
        info "Service restarted after rollback: $service_name"
      else
        warn "Rollback succeeded but service restart failed: $service_name"
      fi
    fi
  fi

  wait_webui_health "$DEFAULT_HEALTH_WAIT_SECONDS" || true
  warn "Rollback completed."
  return 0
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

cmd_napcat_inject() {
  local dry_run=0
  local extra_args=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        dry_run=1
        shift
        ;;
      --port|--token|--host)
        extra_args+=("$1" "${2:-}")
        shift 2
        ;;
      *)
        error "Unknown option for napcat-inject: $1"
        exit 1
        ;;
    esac
  done

  local py_cmd=""
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    py_cmd="$ROOT_DIR/.venv/bin/python"
  else
    py_cmd="$(resolve_bootstrap_python || true)"
  fi
  if [[ -z "$py_cmd" ]]; then
    error "No python executable found. Run install first."
    exit 1
  fi

  local helper_script="$ROOT_DIR/scripts/napcat_config_helper.py"
  if [[ ! -f "$helper_script" ]]; then
    error "napcat_config_helper.py not found: $helper_script"
    exit 1
  fi

  local cmd_args=("$py_cmd" "$helper_script" "--inject")
  if [[ "$dry_run" -eq 1 ]]; then
    cmd_args+=("--dry-run")
  fi
  if [[ ${#extra_args[@]} -gt 0 ]]; then
    cmd_args+=("${extra_args[@]}")
  fi

  info "Running: ${cmd_args[*]}"
  "${cmd_args[@]}"
}

cmd_doctor() {
  local service_name="$DEFAULT_SERVICE_NAME"
  local timeout_seconds=8
  local strict_mode=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --timeout-seconds)
        timeout_seconds="${2:-}"
        shift 2
        ;;
      --strict)
        strict_mode=1
        shift
        ;;
      *)
        error "Unknown option for doctor: $1"
        exit 1
        ;;
    esac
  done

  if [[ ! "$timeout_seconds" =~ ^[0-9]+$ ]] || (( timeout_seconds < 2 || timeout_seconds > 120 )); then
    error "--timeout-seconds must be between 2 and 120"
    exit 1
  fi

  local pass_count=0
  local warn_count=0
  local fail_count=0
  local napcat_method=""
  local host port health_host webui_token onebot_token

  host="$(get_env_value HOST)"
  port="$(get_env_value PORT)"
  webui_token="$(get_env_value WEBUI_TOKEN)"
  onebot_token="$(get_env_value ONEBOT_ACCESS_TOKEN)"
  host="${host:-0.0.0.0}"
  port="${port:-8081}"
  health_host="$(normalize_health_host "$host")"

  if [[ -f "$ENV_FILE" ]]; then
    info "[doctor][PASS] .env exists: $ENV_FILE"
    pass_count=$((pass_count + 1))
  else
    error "[doctor][FAIL] .env not found: $ENV_FILE"
    fail_count=$((fail_count + 1))
  fi

  if validate_port "$port"; then
    info "[doctor][PASS] PORT valid: $port"
    pass_count=$((pass_count + 1))
  else
    error "[doctor][FAIL] PORT invalid: $port"
    fail_count=$((fail_count + 1))
  fi

  if [[ -n "$webui_token" ]]; then
    if [[ "$webui_token" == "change_me"* || "$webui_token" == "replace_with_"* ]]; then
      warn "[doctor][WARN] WEBUI_TOKEN still has placeholder value — change it to a random string"
      warn_count=$((warn_count + 1))
    else
      info "[doctor][PASS] WEBUI_TOKEN configured"
      pass_count=$((pass_count + 1))
    fi
  else
    error "[doctor][FAIL] WEBUI_TOKEN is empty"
    fail_count=$((fail_count + 1))
  fi

  if [[ -n "$onebot_token" ]]; then
    if [[ "$onebot_token" == "replace_with_"* || "$onebot_token" == "change_me"* ]]; then
      warn "[doctor][WARN] ONEBOT_ACCESS_TOKEN still has placeholder value — set a real token"
      warn_count=$((warn_count + 1))
    else
      info "[doctor][PASS] ONEBOT_ACCESS_TOKEN configured"
      pass_count=$((pass_count + 1))
    fi
  else
    error "[doctor][FAIL] ONEBOT_ACCESS_TOKEN is empty"
    fail_count=$((fail_count + 1))
  fi

  if command_exists ffmpeg; then
    info "[doctor][PASS] ffmpeg available"
    pass_count=$((pass_count + 1))
  else
    warn "[doctor][WARN] ffmpeg not found"
    warn_count=$((warn_count + 1))
  fi

  if command_exists ffprobe; then
    info "[doctor][PASS] ffprobe available"
    pass_count=$((pass_count + 1))
  else
    warn "[doctor][WARN] ffprobe not found (video/audio features limited)"
    warn_count=$((warn_count + 1))
  fi

  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    info "[doctor][PASS] python venv ready: $ROOT_DIR/.venv/bin/python"
    pass_count=$((pass_count + 1))
  else
    warn "[doctor][WARN] python venv missing: $ROOT_DIR/.venv/bin/python"
    warn_count=$((warn_count + 1))
  fi

  if [[ -f "$ROOT_DIR/webui/dist/index.html" ]]; then
    info "[doctor][PASS] WebUI dist exists: $ROOT_DIR/webui/dist/index.html"
    pass_count=$((pass_count + 1))
  else
    warn "[doctor][WARN] WebUI dist missing (run webui build)"
    warn_count=$((warn_count + 1))
  fi

  napcat_method="$(detect_napcat)"
  if [[ -n "$napcat_method" ]]; then
    info "[doctor][PASS] NapCat detected: $napcat_method"
    pass_count=$((pass_count + 1))
  else
    warn "[doctor][WARN] NapCat not detected"
    warn_count=$((warn_count + 1))
  fi

  # NapCat systemd service 活跃状态
  if command_exists systemctl; then
    local napcat_svc=""
    napcat_svc="$(systemctl list-units --type=service --state=active --no-legend 2>/dev/null | awk '{print $1}' | grep -Ei 'napcat' | head -1 || true)"
    if [[ -n "$napcat_svc" ]]; then
      info "[doctor][PASS] NapCat systemd service active: $napcat_svc"
      pass_count=$((pass_count + 1))
    elif [[ -n "$napcat_method" ]]; then
      # NapCat detected but not via systemd
      info "[doctor][INFO] NapCat found ($napcat_method) but not as systemd service"
    fi
  fi

  if command_exists systemctl; then
    local svc_path
    svc_path="$(service_path "$service_name")"
    if run_root_nonfatal test -f "$svc_path"; then
      if run_root_nonfatal systemctl is-active --quiet "$service_name"; then
        info "[doctor][PASS] service active: $service_name"
        pass_count=$((pass_count + 1))
      else
        warn "[doctor][WARN] service not active: $service_name"
        warn_count=$((warn_count + 1))
      fi
      if run_root_nonfatal systemctl is-enabled --quiet "$service_name"; then
        info "[doctor][PASS] service enabled at boot: $service_name"
        pass_count=$((pass_count + 1))
      else
        warn "[doctor][WARN] service not enabled at boot: $service_name"
        warn_count=$((warn_count + 1))
      fi
    else
      warn "[doctor][WARN] service file not found: $svc_path"
      warn_count=$((warn_count + 1))
    fi
  else
    warn "[doctor][WARN] systemctl not available"
    warn_count=$((warn_count + 1))
  fi

  if command_exists curl; then
    local health_url napcat_url
    health_url="http://${health_host}:${port}${DEFAULT_HEALTH_PATH}"
    if curl -fsS --connect-timeout 2 --max-time "$timeout_seconds" "$health_url" >/dev/null 2>&1; then
      info "[doctor][PASS] WebUI health reachable: $health_url"
      pass_count=$((pass_count + 1))
    else
      warn "[doctor][WARN] WebUI health failed: $health_url"
      warn_count=$((warn_count + 1))
    fi

    if [[ -n "$webui_token" ]]; then
      napcat_url="http://${health_host}:${port}/api/webui/napcat/status"
      if curl -fsS --connect-timeout 2 --max-time "$timeout_seconds" -H "Authorization: Bearer $webui_token" "$napcat_url" >/dev/null 2>&1; then
        info "[doctor][PASS] NapCat diagnostics API reachable"
        pass_count=$((pass_count + 1))
      else
        warn "[doctor][WARN] NapCat diagnostics API failed (check WEBUI_TOKEN / service)"
        warn_count=$((warn_count + 1))
      fi
    fi
  else
    warn "[doctor][WARN] curl not available; skip HTTP diagnostics"
    warn_count=$((warn_count + 1))
  fi

  echo "[doctor] summary: pass=${pass_count} warn=${warn_count} fail=${fail_count}"
  if (( strict_mode == 1 && warn_count > 0 )); then
    error "[doctor] strict mode enabled: warnings are treated as failures."
    return 1
  fi
  if (( fail_count > 0 )); then
    return 1
  fi
  return 0
}

cmd_backup() {
  local output_dir="$DEFAULT_BACKUP_DIR"
  local name_prefix="yukiko_backup"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output-dir)
        output_dir="${2:-}"
        shift 2
        ;;
      --name)
        name_prefix="${2:-}"
        shift 2
        ;;
      *)
        error "Unknown option for backup: $1"
        exit 1
        ;;
    esac
  done

  if [[ -z "$output_dir" || -z "$name_prefix" ]]; then
    error "backup requires non-empty --output-dir and --name"
    exit 1
  fi

  local archive_path
  archive_path="$(create_backup_archive "$output_dir" "$name_prefix" || true)"
  if [[ -z "$archive_path" ]]; then
    error "Backup failed: no files available to archive."
    exit 1
  fi
  info "Backup created: $archive_path"
}

cmd_restore() {
  local backup_file=""
  local service_name="$DEFAULT_SERVICE_NAME"
  local restart_service=1
  local assume_yes=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --file)
        backup_file="${2:-}"
        shift 2
        ;;
      --service-name)
        service_name="${2:-}"
        shift 2
        ;;
      --no-restart)
        restart_service=0
        shift
        ;;
      --yes)
        assume_yes=1
        shift
        ;;
      *)
        error "Unknown option for restore: $1"
        exit 1
        ;;
    esac
  done

  if [[ -z "$backup_file" ]]; then
    error "restore requires --file <backup.tar.gz>"
    exit 1
  fi
  if [[ ! -f "$backup_file" ]]; then
    error "Backup file not found: $backup_file"
    exit 1
  fi

  if [[ "$assume_yes" -eq 0 ]]; then
    echo "About to restore backup archive:"
    echo "- file: $backup_file"
    echo "- target: $ROOT_DIR"
    echo "- restart service: $([[ "$restart_service" -eq 1 ]] && echo yes || echo no)"
    read -r -p "Continue restore? [yes/no]: " confirm
    if [[ "${confirm,,}" != "yes" ]]; then
      warn "Restore cancelled."
      exit 0
    fi
  fi

  local pre_backup
  pre_backup="$(create_backup_archive "$DEFAULT_BACKUP_DIR" "pre_restore" || true)"
  if [[ -n "$pre_backup" ]]; then
    info "Created safety backup before restore: $pre_backup"
  else
    warn "Unable to create pre-restore backup, continuing."
  fi

  if command_exists systemctl; then
    local svc_path
    svc_path="$(service_path "$service_name")"
    if run_root_nonfatal test -f "$svc_path"; then
      run_root_nonfatal systemctl stop "$service_name" || true
    fi
  fi

  if ! restore_backup_archive "$backup_file"; then
    error "Restore failed while extracting archive: $backup_file"
    exit 1
  fi
  info "Restore completed: $backup_file"

  if [[ "$restart_service" -eq 1 ]] && command_exists systemctl; then
    local svc_path
    svc_path="$(service_path "$service_name")"
    if run_root_nonfatal test -f "$svc_path"; then
      if run_root_nonfatal systemctl restart "$service_name"; then
        info "Service restarted after restore: $service_name"
        wait_service_active "$service_name" "$DEFAULT_SERVICE_WAIT_SECONDS" || true
        wait_webui_health "$DEFAULT_HEALTH_WAIT_SECONDS" || true
      else
        warn "Service restart failed after restore: $service_name"
      fi
    fi
  fi
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
  doctor [options]                  Deployment health diagnostics
  backup [options]                  Backup env/config/storage
  restore --file FILE [options]     Restore backup archive
  napcat-status [--method-only|--quiet]
                                    Detect NapCat installation status
  napcat-inject [--dry-run]         Auto-inject YuKiKo connection into NapCat config
  set-port --port N [--host H]      Update HOST/PORT in .env
  uninstall [options]               Perfect uninstall helper

Uninstall options:
  --service-name NAME               Service name (default: ${DEFAULT_SERVICE_NAME})
  --purge-runtime                   Remove .venv, webui/node_modules, webui/dist
  --purge-state                     Remove caches, sandboxes, coverage and temp files
  --purge-env                       Remove .env and .env.prod
  --purge-data                      Remove storage/, logs/, tmp/ and generated runtime data
  --purge-all                       Shortcut for --purge-runtime --purge-state --purge-env --purge-data
  --backup-dir DIR                  Backup output dir before destructive purge (default: ${DEFAULT_BACKUP_DIR})
  --backup-name PREFIX              Backup filename prefix (default: uninstall_backup)
  --no-backup                       Skip safety backup before purge
  --keep-cli                        Keep /usr/local/bin/yukiko
  --keep-napcat                     Keep NapCat (skip NapCat uninstall)
  --yes                             No confirmation prompt

Doctor options:
  --service-name NAME               Service name (default: ${DEFAULT_SERVICE_NAME})
  --timeout-seconds N               Health-check timeout (default: 8)
  --strict                          Treat warnings as failures (strict acceptance)

Backup options:
  --output-dir DIR                  Backup output dir (default: ${DEFAULT_BACKUP_DIR})
  --name PREFIX                     Backup filename prefix (default: yukiko_backup)

Restore options:
  --file FILE                       Backup tar.gz file path (required)
  --service-name NAME               Service name (default: ${DEFAULT_SERVICE_NAME})
  --no-restart                      Do not restart service after restore
  --yes                             No confirmation prompt

Update options:
  --check-only                      Show update status only
  --allow-dirty                     Allow updating with dirty worktree
  --fast                            Skip optional dependency/build steps
  --force-python                    Force Python dependency sync
  --force-webui                     Force WebUI build
  --pip-index-url URL               Custom Python package index mirror
  --pip-extra-index-url URL         Secondary Python package index mirror
  --pip-find-links PATH             Local wheel dir / extra Python package source
  --pip-cache-dir DIR               Reuse pip cache directory
  --pip-timeout N                   pip network timeout seconds
  --pip-retries N                   pip retry count
  --npm-registry URL                Custom npm registry mirror
  --npm-cache-dir DIR               Reuse npm cache directory
  --use-uv                          Use uv for Python dependency sync when available
  --auto-rollback                   Enable rollback when health checks fail (default)
  --no-auto-rollback                Disable automatic rollback
  --hot-reload                      Restart service after update (default)
  --no-hot-reload                   Do not restart service

Examples:
  yukiko --help
  yukiko install --host 0.0.0.0 --port 18081
  yukiko update --check-only
  yukiko update --fast
  yukiko update --pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple --npm-registry https://registry.npmmirror.com
  yukiko update --no-auto-rollback
  yukiko update --no-hot-reload
  yukiko update --restart
  yukiko doctor
  yukiko doctor --strict
  yukiko backup
  yukiko restore --file backups/yukiko_backup_20260101_120000.tar.gz --yes
  yukiko register --service-name yukiko --user \$USER
  yukiko start
  yukiko logs --lines 200
  yukiko napcat-status
  yukiko napcat-inject
  yukiko napcat-inject --dry-run
  yukiko set-port --port 8088 --host 0.0.0.0
  yukiko uninstall --purge-runtime --purge-env --yes
  yukiko uninstall --purge-all --backup-dir /tmp/yukiko-backups --yes
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
  local fast_mode=0
  local auto_rollback=1
  local force_python=0
  local force_webui=0
  local pip_index_url="${YUKIKO_PIP_INDEX_URL:-}"
  local pip_extra_index_url="${YUKIKO_PIP_EXTRA_INDEX_URL:-}"
  local pip_find_links="${YUKIKO_PIP_FIND_LINKS:-}"
  local pip_cache_dir="${YUKIKO_PIP_CACHE_DIR:-}"
  local pip_timeout="${YUKIKO_PIP_TIMEOUT:-}"
  local pip_retries="${YUKIKO_PIP_RETRIES:-}"
  local npm_registry="${YUKIKO_NPM_REGISTRY:-}"
  local npm_cache_dir="${YUKIKO_NPM_CACHE_DIR:-}"
  local use_uv="${YUKIKO_USE_UV:-}"

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
      --fast)
        fast_mode=1
        shift
        ;;
      --auto-rollback)
        auto_rollback=1
        shift
        ;;
      --no-auto-rollback)
        auto_rollback=0
        shift
        ;;
      --force-python)
        force_python=1
        shift
        ;;
      --force-webui)
        force_webui=1
        shift
        ;;
      --pip-index-url)
        pip_index_url="${2:-}"
        shift 2
        ;;
      --pip-extra-index-url)
        pip_extra_index_url="${2:-}"
        shift 2
        ;;
      --pip-find-links)
        pip_find_links="${2:-}"
        shift 2
        ;;
      --pip-cache-dir)
        pip_cache_dir="${2:-}"
        shift 2
        ;;
      --pip-timeout)
        pip_timeout="${2:-}"
        shift 2
        ;;
      --pip-retries)
        pip_retries="${2:-}"
        shift 2
        ;;
      --npm-registry)
        npm_registry="${2:-}"
        shift 2
        ;;
      --npm-cache-dir)
        npm_cache_dir="${2:-}"
        shift 2
        ;;
      --use-uv)
        use_uv="auto"
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

  if ! validate_optional_integer "$pip_timeout"; then
    error "Invalid --pip-timeout: $pip_timeout"
    exit 1
  fi
  if ! validate_optional_integer "$pip_retries"; then
    error "Invalid --pip-retries: $pip_retries"
    exit 1
  fi

  apply_acceleration_env "$pip_index_url" "$pip_extra_index_url" "$pip_find_links" "$pip_cache_dir" "$pip_timeout" "$pip_retries" "$npm_registry" "$npm_cache_dir" "$use_uv"

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

  local pre_update_commit post_update_commit did_pull=0
  local changed_hints=""
  pre_update_commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  if [[ "$behind" -gt 0 ]]; then
    info "Pulling latest commits (ff-only)..."
    git -C "$ROOT_DIR" pull --ff-only
    did_pull=1
  else
    info "Already up to date with $upstream."
  fi
  post_update_commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"

  if [[ "$did_pull" -eq 1 ]]; then
    changed_hints="$(infer_update_tasks_from_diff "$pre_update_commit" "$post_update_commit")"
    info "Changed hints: ${changed_hints:-none}"
  fi

  local need_python=0
  local need_webui=0
  if [[ "$install_python" -eq 1 ]]; then
    if [[ "$force_python" -eq 1 ]]; then
      need_python=1
    elif [[ "$fast_mode" -eq 1 ]]; then
      need_python=0
    elif [[ "$did_pull" -eq 1 && "$changed_hints" == *"needs_python=1"* ]]; then
      need_python=1
    fi
  fi
  if [[ "$build_webui" -eq 1 ]]; then
    if [[ "$force_webui" -eq 1 ]]; then
      need_webui=1
    elif [[ "$fast_mode" -eq 1 ]]; then
      need_webui=0
    elif [[ "$did_pull" -eq 1 && "$changed_hints" == *"needs_webui=1"* ]]; then
      need_webui=1
    fi
  fi

  if [[ "$need_python" -eq 1 ]]; then
    local py_cmd=""
    py_cmd="$(resolve_bootstrap_python || true)"
    if [[ -n "$py_cmd" ]]; then
      info "Installing Python dependencies..."
      "$py_cmd" "$ROOT_DIR/scripts/deploy.py" --ensure-requirements
    else
      warn "No python executable found, skipped Python dependency sync."
    fi
  else
    info "Python dependency sync skipped (no dependency changes detected)."
  fi

  if [[ "$need_webui" -eq 1 ]]; then
    if [[ -f "$ROOT_DIR/webui/package.json" ]]; then
      if command_exists npm; then
        info "Building WebUI..."
        if [[ "$force_webui" -eq 1 || "$changed_hints" == *"needs_webui_deps=1"* ]]; then
          export YUKIKO_WEBUI_FORCE_INSTALL=1
        else
          export YUKIKO_WEBUI_FORCE_INSTALL=0
        fi
        bash "$ROOT_DIR/build-webui.sh"
      else
        warn "npm not found, skipped WebUI build."
      fi
    fi
  else
    info "WebUI build skipped (no webui changes detected)."
  fi

  local hot_reload_done=0
  local service_ready=0
  local health_ok=0
  local reload_expected=0
  local svc_path=""
  if [[ -n "$service_name" ]]; then
    svc_path="$(service_path "$service_name")"
  fi
  if [[ "$restart_service" -eq 1 ]]; then
    reload_expected=1
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
      if run_root_nonfatal test -f "$svc_path"; then
        reload_expected=1
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

  if [[ "$did_pull" -eq 1 && "$auto_rollback" -eq 1 && "$reload_expected" -eq 1 ]]; then
    if [[ "$hot_reload_done" -ne 1 || "$service_ready" -ne 1 || "$health_ok" -ne 1 ]]; then
      warn "Readiness checks failed after update. Auto rollback is enabled."
      rollback_update "$pre_update_commit" "$service_name" || true
      return 1
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

  if ! command_exists systemctl; then
    warn "systemctl not found, skipped service removal for: $service_name"
    return 0
  fi

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
  local purge_data=0
  local remove_cli=1
  local remove_napcat=1
  local assume_yes=0
  local cli_path="/usr/local/bin/yukiko"
  local backup_before_purge=1
  local backup_dir="$DEFAULT_BACKUP_DIR"
  local backup_name="uninstall_backup"

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

  if [[ -z "$backup_dir" || -z "$backup_name" ]]; then
    error "uninstall requires non-empty --backup-dir and --backup-name"
    exit 1
  fi

  local destructive_purge=0
  if [[ "$purge_runtime" -eq 1 || "$purge_state" -eq 1 || "$purge_env" -eq 1 || "$purge_data" -eq 1 ]]; then
    destructive_purge=1
  fi

  if [[ "$assume_yes" -eq 0 ]]; then
    echo "About to uninstall YuKiKo deployment bits from: $ROOT_DIR"
    echo "- service: $service_name (stop/disable/remove)"
    if [[ "$purge_runtime" -eq 1 ]]; then
      echo "- purge runtime: .venv, webui/node_modules, webui/dist"
    fi
    if [[ "$purge_state" -eq 1 ]]; then
      echo "- purge state: caches, sandboxes, coverage artifacts, temp files"
    fi
    if [[ "$purge_env" -eq 1 ]]; then
      echo "- purge env: .env, .env.prod"
    fi
    if [[ "$purge_data" -eq 1 ]]; then
      echo "- purge data: storage, logs, runtime and generated local data"
    fi
    if [[ "$remove_cli" -eq 1 ]]; then
      echo "- remove CLI: $cli_path (if linked to this repo)"
    fi
    if [[ "$remove_napcat" -eq 1 ]]; then
      echo "- remove NapCat: try official --uninstall and fallback cleanup"
    else
      echo "- keep NapCat: enabled (--keep-napcat)"
    fi
    if [[ "$destructive_purge" -eq 1 && "$backup_before_purge" -eq 1 ]]; then
      echo "- safety backup: ${backup_dir%/}/${backup_name}_<timestamp>.tar.gz"
    elif [[ "$destructive_purge" -eq 1 ]]; then
      echo "- safety backup: disabled (--no-backup)"
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
    rm -rf \
      "$ROOT_DIR/.venv" \
      "$ROOT_DIR/webui/node_modules" \
      "$ROOT_DIR/webui/dist"
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
    rm -rf \
      "$ROOT_DIR/storage" \
      "$ROOT_DIR/logs" \
      "$ROOT_DIR/runtime" \
      "$ROOT_DIR/tmp"
    info "Runtime data directories removed."
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
    doctor)
      cmd_doctor "$@"
      ;;
    backup)
      cmd_backup "$@"
      ;;
    restore)
      cmd_restore "$@"
      ;;
    napcat-status)
      cmd_napcat_status "$@"
      ;;
    napcat-inject)
      cmd_napcat_inject "$@"
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
