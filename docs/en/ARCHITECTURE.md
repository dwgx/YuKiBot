# YuKiKo Bot Architecture Notes

This file explains internal design decisions, not deployment steps.

## 1. Main Message Pipeline

1. OneBot event enters `app.py`
2. Event is normalized into `EngineMessage`
3. Queue layer (`core/queue.py`) schedules by conversation policy
4. Trigger + Router decide action intent
5. Self-check enforces local safety/consistency guards
6. Agent/Tools execute calls
7. Engine builds final response payload

## 2. Why Self-check Exists

Model routing is flexible but may over-reply in group chat.  
Self-check is a local guardrail layer to reduce false positives.

Typical blocks:

- Undirected group messages without enough confidence
- Message that @mentions someone else but not the bot
- Tool-required intent that incorrectly returned plain `reply`

## 3. Config Template System

Primary template: `config/templates/master.template.yml`  
Template merge/default logic: `core/config_templates.py`

Design goals:

- Missing fields are auto-filled
- Legacy configs are healed during upgrades
- Runtime and WebUI share one canonical config schema

## 4. Plugin Template Pattern

Plugin config files live in `plugins/config/*.yml`.  
WebUI plugin list reads plugin schema and renders editable fields.

Benefits:

- Per-plugin ownership and safer changes
- Easier rollback and review
- Avoids giant monolithic config pages

## 5. Music Source Stability Rules

Recommended flow:

1. `music_search`
2. `music_play_by_id`
3. Fallback only when needed

Keep stability by:

- Enabling artist guard
- Keeping unblock source list strict and clean
- Avoiding mixed-purpose sources in unblock list

## 6. Runtime Mode Layering

`main.py` supports layered startup:

- Normal mode
- Auto setup mode when config is missing
- Forced CLI setup (`--setup` / `setup`)
- WebUI setup fallback to CLI if frontend is not built

This prevents broken half-initialized boot states on new machines.
