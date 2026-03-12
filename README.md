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

## 当前版本重点修复

- Linux 下载兼容：`smart_download` 在 NapCat 临时目录不可读时，会自动回退到本地 HTTP 下载，不再因为 `/root/.config/QQ/NapCat/temp/...` 权限问题直接失败。
- Wayback 回退：`wayback_lookup` 在 CDX 查询失败或被限流时，会继续尝试最近可用快照，不再只返回空结果。
- NewAPI 支付修正：
  - `/api topup` 现在只负责“充值信息 / 兑换码”。
  - 如果用户误输成 `/api topup 200 支付宝` 这类在线支付格式，会自动转给 `/api pay`。
  - `/api pay` 支持 `200M`、`1w` 这类金额写法。
- 多会话并发更稳：新增 `queue.smart_interrupt_same_user_enable`，默认关闭“同用户新任务自动打断旧任务”。
- WebUI 体验更新：
  - Thinking Island 取消滚轮改尺寸，改为右侧边缘拖拽调宽。
  - Plugins 页面现在会显示真实配置入口、写回文件、向导提示和按组整理后的字段编辑面板。
  - Dashboard 现在支持查看 GitHub 最新版本状态、拉取最新代码、查看多会话 AI 并发状态，并提供 VPS/Windows 部署下载入口。
  - Database 页面新增当前库导出 / 导入按钮；导入前会自动备份旧库。

## 插件配置位置

YuKiKo 现在同时支持两种插件配置写法：

- 统一配置：`config/plugins.yml`
- 插件独立配置：`plugins/config/<plugin>.yml`

WebUI 会根据插件实际配置模式写回正确位置：

- 支持独立配置/向导型插件：优先写到 `plugins/config/<plugin>.yml`
- 普通插件：写到 `config/plugins.yml`

如果你之前觉得 “插件配置没了”，先检查这两个位置。

## 推荐队列配置

如果你希望同一个用户在群里连续发多个任务时不要互相打断，确认配置里包含：

```yaml
queue:
  smart_interrupt_enable: true
  smart_interrupt_cross_user_enable: true
  smart_interrupt_same_user_enable: false
```

这样可以保留“跨用户抢占保护”，同时允许同用户的多个任务继续并行排队/执行。

## WebUI 运维入口

现在 WebUI 里新增了几组直接可用的运维能力：

- Dashboard：
  - 检查当前仓库是否落后于 GitHub
  - 一键拉取最新代码、同步 Python 依赖、重建 WebUI
  - 显示“多会话 AI 并发”是否启用，以及当前活跃会话数
  - 提供 Linux VPS 一键部署命令复制按钮
  - 提供 Windows ZIP 下载入口
- Database：
  - 导出当前数据库文件
  - 导入 SQLite 数据库文件
  - 导入前自动备份原库到 `storage/backups/db/`

说明：

- WebUI 的“拉取最新”只会更新到当前 Git 仓库上游的最新版本，不支持切换到别的仓库或别的分支。
- 代码拉取完成后，Python 新代码仍然建议手动重启服务后再正式生效。
- 数据库导入成功后，也建议手动重启服务，以便长期持有连接的模块完全刷新。

Release and operations deep dive (CN, 1000+ lines checklist + SOP):

- [docs/zh-CN/RELEASE_PLAYBOOK.md](docs/zh-CN/RELEASE_PLAYBOOK.md)

## 快速启动（Linux）

1. 克隆项目并进入目录：

```bash
git clone https://github.com/dwgx/YuKiKo.git
cd YuKiKo
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
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

Pass installer args (for non-interactive deploy):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
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

