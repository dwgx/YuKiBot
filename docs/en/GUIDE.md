# YuKiKo Bot — Deployment & Usage Guide

Complete guide covering setup from scratch on Linux, Windows, and macOS.

---

## Table of Contents

- [1. Prerequisites](#1-prerequisites)
- [2. Installation](#2-installation)
- [3. Connecting NapCat](#3-connecting-napcat)
- [4. First Run & WebUI](#4-first-run--webui)
- [5. Configuration](#5-configuration)
- [6. Runtime Modes](#6-runtime-modes)
- [7. Operations](#7-operations)
- [8. Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.10+ | 3.11 / 3.12 recommended |
| Node.js | 18+ | For building WebUI |
| ffmpeg | Latest | Audio/video processing |
| Git | Latest | For cloning the repo |
| OneBot V11 | — | [NapCat](https://github.com/NapNeko/NapCatQQ) recommended |

### Linux (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm ffmpeg git curl
```

### Windows

1. **Python**: https://www.python.org/downloads/ — check "Add to PATH" during install
2. **Node.js**: https://nodejs.org/ — LTS version
3. **ffmpeg**: https://www.gyan.dev/ffmpeg/builds/ — add `bin` folder to system PATH
4. **Git**: https://git-scm.com/download/win

### macOS

```bash
brew install python@3.12 node ffmpeg git
```

---

## 2. Installation

### Linux — One-Click Deploy (Recommended)

No manual clone needed:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

The script handles everything: system deps, Python venv, WebUI build, systemd service.

<details>
<summary>Non-interactive mode</summary>

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh) \
  -- --non-interactive --host 0.0.0.0 --port 8081 --service-name yukiko --open-firewall
```
</details>

<details>
<summary>Already cloned? Use local installer</summary>

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
bash install.sh
```
</details>

### Windows

```powershell
git clone https://github.com/dwgx/YuKiKo.git
cd YuKiKo
Copy-Item .env.example .env
# Edit .env — set ONEBOT_ACCESS_TOKEN and WEBUI_TOKEN
.\start.bat
```

### macOS

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
cp .env.example .env
# Edit .env — set ONEBOT_ACCESS_TOKEN and WEBUI_TOKEN
bash start.sh
```

### Manual Setup (All Platforms)

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit env file
cp .env.example .env

# Build WebUI (optional but recommended)
cd webui && npm install && npm run build && cd ..

# Start
python main.py
```

---

## 3. Connecting NapCat

YuKiKo connects to QQ via [NapCat](https://github.com/NapNeko/NapCatQQ) using the OneBot V11 protocol.

In NapCat's OneBot V11 settings:

| Setting | Value |
|---------|-------|
| Connection mode | Reverse WebSocket |
| WS URL | `ws://<YuKiKo-host>:<PORT>/onebot/v11/ws` |
| Access Token | Must match `ONEBOT_ACCESS_TOKEN` in `.env` |

```text
# Same machine
ws://127.0.0.1:8081/onebot/v11/ws

# Cross-machine (use YuKiKo host's LAN IP)
ws://192.168.1.50:8081/onebot/v11/ws
```

> The Linux installer auto-detects and offers to install NapCat. Manual install:
> ```bash
> curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
> ```

---

## 4. First Run & WebUI

On first launch (when `config/config.yml` doesn't exist):
- If WebUI is built → setup wizard at `/webui/setup`
- If not built → falls back to CLI setup wizard

**Build WebUI:**

| Platform | Command |
|----------|---------|
| Linux / macOS | `bash build-webui.sh` |
| Windows | `build-webui.bat` |
| Manual | `cd webui && npm install && npm run build` |

**Access WebUI:** `http://<HOST>:<PORT>/webui/login` — log in with your `WEBUI_TOKEN`.

---

## 5. Configuration

YuKiKo uses a three-layer config system:

| Layer | File | Purpose |
|-------|------|---------|
| Environment | `.env` | Ports, secrets, API keys |
| Global config | `config/config.yml` | Bot behavior, model params, routing |
| Plugin configs | `plugins/config/*.yml` | Per-plugin settings |

### .env Key Fields

| Field | Description | Required |
|-------|-------------|----------|
| `HOST` | Listen address (default `127.0.0.1`) | Yes |
| `PORT` | Listen port (default `8081`) | Yes |
| `ONEBOT_ACCESS_TOKEN` | NapCat auth token | Yes |
| `WEBUI_TOKEN` | WebUI admin token | Yes |
| `SKIAPI_KEY` | Default AI model API key | As needed |
| `OPENAI_API_KEY` | OpenAI API key | As needed |

Full reference: [`.env.example`](../../.env.example)

### config.yml Sections

Auto-generated from template on first run. Editable via WebUI.

| Section | Description |
|---------|-------------|
| `bot` | Name, nicknames, reply format, message splitting |
| `api` | Model provider, model name, base_url, temperature, max_tokens |
| `agent` | Max steps, timeouts, tool call strategy |
| `routing` | Confidence thresholds, routing mode |
| `queue` | Concurrency, smart interrupt, message TTL |
| `music` | Music sources, unlock sources, artist guard |
| `search` | Web scraping, video parsing, vision analysis |

Missing fields are auto-filled from the template (self-healing).

---

## 6. Runtime Modes

| Mode | Command | Description |
|------|---------|-------------|
| Normal | `python main.py` | Standard run |
| Quick start | `start.bat` / `bash start.sh` | Auto-checks env, repairs if needed |
| First-time setup | Automatic | Triggered when config is missing |
| Force CLI setup | `python main.py --setup` | Skip WebUI, use CLI wizard |
| Deploy only | `python scripts/deploy.py` | Install deps without starting |
| Deploy & run | `python scripts/deploy.py --run` | Install deps then start |

---

## 7. Operations

### Linux systemd (via `yukiko` CLI)

```bash
yukiko status              # Service status
yukiko logs --lines 200    # Recent logs
yukiko restart             # Restart service
yukiko stop / start        # Stop / start
yukiko update --check-only # Check for updates
yukiko update --restart    # Pull updates and restart
yukiko uninstall           # Uninstall
```

---

## 8. Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError` | Run `python scripts/deploy.py` |
| WebUI 503 | Build frontend: `cd webui && npm install && npm run build` |
| NapCat won't connect | Check token match and WS URL format |
| Plugin config not applied | Check `plugins/config/<name>.yml` or re-save in WebUI |
| ffmpeg not found | Install it and add to PATH |
| pip timeout | Use mirror: `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| Port in use | Change `PORT` in `.env` or find the process using it |
| Broken config | Delete `config/config.yml` and restart (auto-rebuilds from template) |
| Broken venv | Delete `.venv` and re-run `start.sh` / `start.bat` |

---

## Further Reading

- [Architecture Notes](ARCHITECTURE.md) — message pipeline, Router, Agent, Self-check design
- [简体中文指南](../zh-CN/GUIDE.md) — Chinese deployment guide
- [Plugin Guide](../PLUGIN_GUIDE.md) — plugin development
