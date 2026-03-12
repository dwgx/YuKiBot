# Plugin Guide

## Overview

YuKiKo supports hot-pluggable plugins in the `plugins/` directory. Each plugin has its own config file in `plugins/config/`.

## Config Location

| Type | Path |
|------|------|
| Plugin code | `plugins/<name>.py` |
| Plugin config | `plugins/config/<name>.yml` |
| Plugin template | See `plugins/example_plugin.py` |

WebUI's plugin page reads each plugin's schema and renders editable fields. Saving writes back to the correct config file.

## Built-in Plugins

| Plugin | Config File | Description |
|--------|-------------|-------------|
| NewAPI | `plugins/config/newapi.yml` | Payment/topup integration |
| Wayback | `plugins/config/wayback.yml` | Internet Archive lookup |
| ConnectCLI | `plugins/config/connect_cli.yml` | External CLI tool integration |

## Plugin Lifecycle

```
needs_setup() → interactive_setup() → setup() → handle() → teardown()
```

Plugins can:
- Register Agent tools
- Provide prompt hints
- Inject context into conversations
- Define their own config schema for WebUI rendering

## Writing a Plugin

See [`plugins/example_plugin.py`](../plugins/example_plugin.py) for a complete template with comments.

Key points:
- Inherit from the plugin base class
- Define `config_schema` for WebUI integration
- Implement `handle()` for message processing
- Use `plugins/config/<your_plugin>.yml` for configuration
