# YuKiKo Bot Documentation Hub

This repository currently focuses on runtime code and configuration templates.
To make onboarding easier, the full docs are split by language:

- 简体中文: [docs/zh-CN/GUIDE.md](docs/zh-CN/GUIDE.md)
- 繁體中文: [docs/zh-TW/GUIDE.md](docs/zh-TW/GUIDE.md)
- English: [docs/en/GUIDE.md](docs/en/GUIDE.md)

Architecture and design notes:

- 简体中文原理文档: [docs/zh-CN/ARCHITECTURE.md](docs/zh-CN/ARCHITECTURE.md)
- 繁體中文原理文件: [docs/zh-TW/ARCHITECTURE.md](docs/zh-TW/ARCHITECTURE.md)
- English architecture notes: [docs/en/ARCHITECTURE.md](docs/en/ARCHITECTURE.md)

If you are new, start from the GUIDE file in your language first.

Release and operations deep dive (CN, 1000+ lines checklist + SOP):

- [docs/zh-CN/RELEASE_PLAYBOOK.md](docs/zh-CN/RELEASE_PLAYBOOK.md)

## Linux One-Click Deploy

For a 1Panel-like interactive deploy flow (custom host/port + optional systemd):

```bash
bash install.sh
```

Remote bootstrap directly from GitHub:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh)
```

Pass installer args (for non-interactive deploy):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

Non-interactive example:

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

After install, use the unified manager command:

```bash
yukiko --help
yukiko update --check-only
yukiko update --restart
yukiko status
yukiko logs --lines 200
yukiko stop
yukiko start
yukiko uninstall --purge-runtime --purge-env
```
