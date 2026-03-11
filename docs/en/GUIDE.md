# YuKiKo Bot Guide (English)

This guide is intentionally ordered for real usage:

1. Deploy and boot fast
2. Configure parameters
3. Understand all runtime modes

## 1. Prerequisites

- Python 3.10+ (3.11/3.12 recommended)
- Node.js 18+ (for WebUI build)
- npm
- A working OneBot V11 service (for example NapCat)

Clone:

```bash
git clone <your-repo-url> yukiko-bot
cd yukiko-bot
```

Copy env file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Minimum values to set first:

- `ONEBOT_ACCESS_TOKEN` (must match OneBot side)
- `WEBUI_TOKEN` (set a strong random token)
- `HOST` and `PORT`

## 2. Quick Deploy and Start

### 2.1 Linux one-click deploy (1Panel-like)

```bash
bash install.sh
```

Direct remote bootstrap from GitHub (no manual clone required):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh)
```

Non-interactive remote install example:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

The installer asks for:

- `HOST`
- `PORT` (fully custom)
- `WEBUI_TOKEN`
- systemd service name
- whether to open firewall port automatically

It then performs:

- system package install
- Python venv + requirements bootstrap
- WebUI build
- `.env` updates for `HOST`/`PORT`
- optional systemd create + start

Non-interactive example:

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

Service operations:

```bash
yukiko --help
yukiko update --check-only
yukiko update --restart
yukiko status
yukiko logs --lines 200
yukiko stop
yukiko start
yukiko register --service-name yukiko
yukiko unregister --service-name yukiko
yukiko uninstall --purge-runtime --purge-env
```

### 2.2 One-command startup scripts

Windows:

```bat
start.bat
```

Linux/macOS:

```bash
bash start.sh
```

Both scripts validate local `.venv`.  
If missing/unhealthy, they auto-run `scripts/deploy.py --run`.

### 2.3 Manual bootstrap

```bash
python scripts/deploy.py
python scripts/deploy.py --run
```

- `deploy.py`: bootstrap only
- `deploy.py --run`: bootstrap + run `main.py`

## 3. First Run and WebUI Setup

If `config/config.yml` does not exist:

- App enters setup flow
- If `webui/dist` exists, setup is served at `/webui/setup`
- If not built, it falls back to CLI setup wizard

Build WebUI:

Windows:

```bat
build-webui.bat
```

Linux/macOS:

```bash
bash build-webui.sh
```

Manual:

```bash
cd webui
npm install
npm run build
```

## 4. Parameter Configuration

Configuration is layered:

1. `.env`
2. `config/config.yml`
3. `plugins/config/*.yml`

### 4.1 `.env` (runtime and secrets)

Based on `.env.example`. Important keys:

- `HOST`, `PORT`
- `ONEBOT_API_TIMEOUT`
- `ONEBOT_ACCESS_TOKEN`
- `WEBUI_TOKEN`
- provider keys such as `SKIAPI_KEY`, `OPENAI_API_KEY`, `NEWAPI_API_KEY`

### 4.2 `config/config.yml` (global behavior)

Template source: `config/templates/master.template.yml`  
Defaults + healing logic: `core/config_templates.py`

Main sections:

- `bot`
- `api`
- `agent`
- `routing`
- `self_check`
- `queue`
- `music`
- `search`

### 4.3 `plugins/config/*.yml` (plugin templates)

This is the practical way to avoid one giant config page.  
Each plugin keeps its own file and can be managed in plugin list UI.

Example `plugins/config/newapi.yml`:

```yaml
enabled: true
display_name: skiapi
response:
  force_plain_text: true
  strip_markdown_chars: true
payment:
  auto_require_method_selection_when_multiple: true
  auto_prefer_methods:
    - alipay
  auto_fallback_method_when_info_unavailable: wxpay
  include_epay_submit_url: true
privacy_guard:
  enabled: true
  recall_message: true
  notify_group: true
  notify_private: true
```

Example `plugins/config/connect_cli.yml`:

```yaml
enabled: true
default_provider: codex_cli
timeout_seconds: 120
max_output_chars: 8000
token_saving: false
safety_mode: true
inject_context: true
filter_output: true
open_mode: embedded
providers:
  codex_cli:
    enabled: true
    command: codex
    model: gpt-5.4
    api_key: ""
```

## 5. Music Interface Maintenance

Focus on `config/config.yml -> music`:

- `local_source_enable`
- `unblock_enable`
- `unblock_sources`
- `artist_guard_enable`

Recommended stability strategy:

- Keep local source stable first
- Keep `unblock_sources` limited to true unblock providers
- Do not mix unrelated sources into unblock list
- Keep artist guard enabled to reduce wrong-song playback

## 6. All Runtime Modes

### 6.1 Normal mode

```bash
python main.py
```

or use startup scripts (`start.bat` / `start.sh`).

### 6.2 Auto setup mode

Triggered when `config/config.yml` is missing.  
Serves setup page at `/webui/setup` if frontend is built.

### 6.3 Forced CLI setup mode

```bash
python main.py --setup
```

or:

```bash
python main.py setup
```

### 6.4 Bootstrap only

```bash
python scripts/deploy.py
```

### 6.5 Bootstrap then run

```bash
python scripts/deploy.py --run
```

### 6.6 Frontend build only

```bash
bash build-webui.sh
```

Windows:

```bat
build-webui.bat
```

## 7. Quick Troubleshooting

- `ModuleNotFoundError`: run `python scripts/deploy.py`
- WebUI 503: build frontend (`npm run build`)
- OneBot connection issues: verify token and upstream endpoint
- Plugin config not applied: verify `plugins/config/<name>.yml` and save/reload in WebUI

## 8. Principles and Internals

See [ARCHITECTURE.md](ARCHITECTURE.md) for message flow, self-check logic, and template strategy.
