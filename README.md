# YuKiKo Bot Documentation Hub

This repository currently focuses on runtime code and configuration templates.
To make onboarding easier, the full docs are split by language:

- 简体中文: [docs/zh-CN/GUIDE.md](docs/zh-CN/GUIDE.md)
- 繁體中文: [docs/zh-TW/GUIDE.md](docs/zh-TW/GUIDE.md)
- English: [docs/en/GUIDE.md](docs/en/GUIDE.md)

Architecture and design notes:

- 简体中文原理文档: [docs/zh-CN/ARCHITECTURE.md](docs/zh-CN/ARCHITECTURE.md)
- 简体中文深度总览与维护手册（700+ 行）: [docs/zh-CN/PROJECT_DEEP_SUMMARY.md](docs/zh-CN/PROJECT_DEEP_SUMMARY.md)
- 繁體中文原理文件: [docs/zh-TW/ARCHITECTURE.md](docs/zh-TW/ARCHITECTURE.md)
- English architecture notes: [docs/en/ARCHITECTURE.md](docs/en/ARCHITECTURE.md)

If you are new, start from the GUIDE file in your language first.

Release and operations deep dive (CN, 1000+ lines checklist + SOP):

- [docs/zh-CN/RELEASE_PLAYBOOK.md](docs/zh-CN/RELEASE_PLAYBOOK.md)

## 快速启动（Linux）

1. 克隆项目并进入目录：

```bash
git clone https://github.com/dwgx/YuKiBot.git
cd YuKiBot
```

2. 一键交互式部署（推荐）：

```bash
bash install.sh
```

3. 部署完成后访问 WebUI（默认地址）：

```bash
http://127.0.0.1:8081/webui/login
```

4. 常用管理命令：

```bash
yukiko --help
yukiko status
yukiko logs --lines 200
yukiko restart
```

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
yukiko restart
yukiko stop
yukiko start
yukiko uninstall --purge-runtime --purge-env
```

## NapCat (QQ Adapter)

YuKiKo uses [NapCat](https://github.com/NapNeko/NapCatQQ) as the OneBot V11 adapter for QQ.

The Linux installer (`install.sh`) will auto-detect NapCat and offer to install it if missing. To skip:

```bash
bash install.sh --skip-napcat
```

Manual install:

```bash
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
```

After NapCat is running, set `ONEBOT_ACCESS_TOKEN` in `.env` to match NapCat's token.

### NapCat WS 参数配置（Linux / Windows 通用）

YuKiKo 的 OneBot V11 WS 接入路径是：

```text
ws://<YuKiKo主机>:<PORT>/onebot/v11/ws
```

示例（同机部署）：

```text
ws://127.0.0.1:8081/onebot/v11/ws
```

在 NapCat 的 OneBot V11 配置里，按下面填写：

1. 连接模式：`反向 WebSocket (Reverse WS)`
2. WS 上报地址：`ws://<YuKiKo主机>:<PORT>/onebot/v11/ws`
3. Access Token：`与你 .env 中 ONEBOT_ACCESS_TOKEN 完全一致`
4. 启用连接并保存

`.env` 对应最小配置示例：

```env
HOST=0.0.0.0
PORT=8081
ONEBOT_ACCESS_TOKEN=replace_with_napcat_token
```

说明：

1. Linux 和 Windows 参数完全一样，只是 `<YuKiKo主机>` 不同。
2. 同一台机器可用 `127.0.0.1`；跨机器请填 YuKiKo 所在机器的局域网 IP。
3. YuKiKo 监听的 WS 路径是 `/onebot/v11/ws`（也兼容 `/onebot/v11/`，推荐前者）。
