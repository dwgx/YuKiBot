# Project Structure

```
YuKiKo/
├── main.py                  # Entry point (startup + setup wizard)
├── app.py                   # OneBot event handlers, message pipeline
├── core/                    # Core engine
│   ├── engine.py            # Main orchestrator (YukikoEngine)
│   ├── queue.py             # Concurrency & conversation queue
│   ├── router.py            # Intent routing with confidence scoring
│   ├── tools.py             # Tool executor (50+ built-in tools)
│   ├── agent.py             # Agent loop with reasoning
│   ├── webui.py             # WebUI API backend
│   ├── config_templates.py  # Config template merge & self-healing
│   └── ...                  # Memory, search, music, safety, admin engines
├── plugins/                 # Plugin implementations
│   ├── config/              # Per-plugin config files (*.yml)
│   ├── newapi_plugin.py     # Payment/topup integration
│   ├── wayback_plugin.py    # Internet Archive lookup
│   ├── connect_cli.py       # External CLI tool integration
│   └── example_plugin.py    # Plugin template
├── config/
│   ├── templates/           # master.template.yml (canonical defaults)
│   └── prompts.yml          # System prompts & agent rules
├── services/                # Model clients & external service wrappers
├── webui/                   # React + Vite + TypeScript admin frontend
│   └── src/pages/           # Dashboard, Config, Plugins, Chat, Logs, etc.
├── scripts/                 # deploy.py, build helpers
├── utils/                   # Text processing, media utils, filters
├── storage/                 # Runtime data (cache, databases, backups)
├── deploy/                  # systemd service templates
├── tests/                   # Test suite
├── install.sh               # Linux interactive installer
├── bootstrap.sh             # Remote one-click deploy
├── start.sh / start.bat     # Quick start scripts
├── build-webui.sh / .bat    # WebUI build scripts
└── .env.example             # Environment variable template
```

## Key Modules

| Module | Role |
|--------|------|
| `core/engine.py` | Message processing orchestrator |
| `core/queue.py` | Per-group concurrency, smart interrupt, TTL |
| `core/router.py` | AI-based intent routing with confidence |
| `core/tools.py` | Tool registration & execution |
| `core/agent.py` | Multi-step reasoning agent loop |
| `core/webui.py` | REST API for the management panel |
| `plugins/` | Hot-pluggable plugin system |
| `services/` | LLM provider clients (OpenAI-compatible) |
| `webui/` | React admin dashboard |
