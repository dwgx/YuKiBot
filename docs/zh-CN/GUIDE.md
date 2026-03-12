# YuKiKo Bot 部署与使用指南

本指南覆盖从零开始到稳定运行的完整流程，适用于 Linux / Windows / macOS。

---

## 目录

- [1. 环境准备](#1-环境准备)
  - [1.1 Linux（Ubuntu / Debian / CentOS）](#11-linuxubuntu--debian--centos)
  - [1.2 Windows](#12-windows)
  - [1.3 macOS](#13-macos)
- [2. 部署安装](#2-部署安装)
  - [2.1 Linux 一键部署](#21-linux-一键部署推荐)
  - [2.2 Windows 部署](#22-windows-部署)
  - [2.3 macOS 部署](#23-macos-部署)
  - [2.4 手动部署（全平台）](#24-手动部署全平台)
- [3. 连接 NapCat](#3-连接-napcat)
- [4. 首次启动与 WebUI](#4-首次启动与-webui)
- [5. 配置详解](#5-配置详解)
- [6. 运行模式](#6-运行模式)
- [7. 运维管理](#7-运维管理)
- [8. 故障排查](#8-故障排查)
- [9. 进阶文档](#9-进阶文档)

---

## 1. 环境准备

### 1.1 Linux（Ubuntu / Debian / CentOS）

> 一键安装脚本会自动处理以下依赖，如果你使用 `install.sh` 可以跳过本节。

**Ubuntu / Debian：**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm ffmpeg git curl
```

**CentOS / RHEL：**

```bash
sudo yum install -y python3 python3-pip nodejs npm ffmpeg git curl
# 如果 yum 没有 ffmpeg，使用 RPM Fusion：
# sudo yum install -y epel-release
# sudo yum install -y ffmpeg
```

验证版本：

```bash
python3 --version   # 需要 3.10+
node --version      # 需要 18+
ffmpeg -version
```

### 1.2 Windows

1. **Python 3.10+**
   - 下载：https://www.python.org/downloads/
   - 安装时务必勾选 **"Add Python to PATH"**
   - 验证：打开 PowerShell，运行 `python --version`

2. **Node.js 18+**
   - 下载：https://nodejs.org/ （选 LTS 版本）
   - 验证：`node --version` 和 `npm --version`

3. **ffmpeg**
   - 下载：https://www.gyan.dev/ffmpeg/builds/ （选 `ffmpeg-release-essentials.zip`）
   - 解压到任意目录（如 `C:\ffmpeg`）
   - 将 `C:\ffmpeg\bin` 添加到系统环境变量 PATH
   - 验证：重启 PowerShell，运行 `ffmpeg -version`

4. **Git**
   - 下载：https://git-scm.com/download/win
   - 验证：`git --version`

### 1.3 macOS

```bash
# 使用 Homebrew
brew install python@3.12 node ffmpeg git

# 验证
python3 --version
node --version
ffmpeg -version
```

---

## 2. 部署安装

### 2.1 Linux 一键部署（推荐）

**远程一键安装（无需手动 clone）：**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

**本地安装（已 clone 仓库）：**

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
bash install.sh
```

安装脚本会交互式询问：
- 监听地址（HOST）和端口（PORT）
- WebUI 管理令牌（WEBUI_TOKEN）
- 是否注册 systemd 服务
- 是否自动放行防火墙端口
- 是否安装 NapCat

脚本自动完成：
- 系统依赖安装（Python / Node.js / npm / ffmpeg）
- Python 虚拟环境创建与依赖安装
- WebUI 前端构建
- `.env` 配置写入
- systemd 服务注册（可选）
- `yukiko` CLI 工具安装到 `/usr/local/bin/`

**非交互模式（适合自动化）：**

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 8081 --service-name yukiko --open-firewall
```

### 2.2 Windows 部署

```powershell
# 克隆项目
git clone https://github.com/dwgx/YuKiKo.git
cd YuKiKo

# 复制环境变量文件
Copy-Item .env.example .env

# 用记事本或 VS Code 编辑 .env，至少填写：
#   ONEBOT_ACCESS_TOKEN=你的NapCat令牌
#   WEBUI_TOKEN=一个随机字符串

# 一键启动（自动创建虚拟环境 + 安装依赖 + 启动）
.\start.bat
```

> `start.bat` 会自动检测虚拟环境，如果不存在会自动创建并安装依赖。

**手动构建 WebUI（可选但推荐）：**

```bat
build-webui.bat
```

### 2.3 macOS 部署

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
cp .env.example .env
# 编辑 .env，填写 ONEBOT_ACCESS_TOKEN 和 WEBUI_TOKEN
bash start.sh
```

### 2.4 手动部署（全平台）

适合需要完全控制部署过程的用户：

```bash
# 1. 克隆
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows PowerShell
# .venv\Scripts\activate.bat     # Windows CMD

# 3. 安装 Python 依赖
pip install -r requirements.txt

# 4. 复制并编辑环境变量
cp .env.example .env
# 编辑 .env

# 5. 构建 WebUI（可选）
cd webui && npm install && npm run build && cd ..

# 6. 启动
python main.py
```

也可以使用部署脚本：

```bash
python scripts/deploy.py          # 仅安装依赖
python scripts/deploy.py --run    # 安装依赖后直接启动
```

---

## 3. 连接 NapCat

YuKiKo 通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 OneBot V11 协议接入 QQ。

### 安装 NapCat

Linux 安装脚本会自动检测并提示安装。手动安装：

```bash
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
```

Windows 用户请从 [NapCat Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载。

### 配置反向 WebSocket

在 NapCat 的 OneBot V11 设置页面：

| 配置项 | 值 |
|--------|-----|
| 连接模式 | 反向 WebSocket (Reverse WS) |
| WS 上报地址 | `ws://<YuKiKo地址>:<端口>/onebot/v11/ws` |
| Access Token | 与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 完全一致 |

**示例：**

```text
# 同机部署
ws://127.0.0.1:8081/onebot/v11/ws

# 跨机器（填 YuKiKo 所在机器的局域网 IP）
ws://192.168.1.50:8081/onebot/v11/ws
```

**注意事项：**
- Linux 和 Windows 配置方式完全一致，只是 IP 地址不同
- Docker / 云服务器部署时，确保端口已放行且路由可达
- WS 路径推荐 `/onebot/v11/ws`（也兼容 `/onebot/v11/`）

---

## 4. 首次启动与 WebUI

首次启动时，如果 `config/config.yml` 不存在：
- 如果 WebUI 已构建（`webui/dist` 存在），会提示访问 `/webui/setup` 完成配置
- 如果未构建，会自动进入 CLI 配置向导

**构建 WebUI：**

| 平台 | 命令 |
|------|------|
| Linux / macOS | `bash build-webui.sh` |
| Windows | `build-webui.bat` |
| 手动 | `cd webui && npm install && npm run build` |

**访问 WebUI：**

```
http://<HOST>:<PORT>/webui/login
```

使用 `.env` 中的 `WEBUI_TOKEN` 登录。WebUI 提供：
- 配置在线编辑（热更新）
- 插件管理
- 系统提示词编辑
- 在线聊天测试
- 实时日志查看
- 数据库导出/导入
- 系统状态与版本检查

---

## 5. 配置详解

YuKiKo 采用三层配置体系，优先级从高到低：

### 5.1 `.env` — 环境变量与密钥

基于 `.env.example`，包含运行时敏感信息：

| 字段 | 说明 | 必填 |
|------|------|------|
| `HOST` | 监听地址（默认 `127.0.0.1`） | 是 |
| `PORT` | 监听端口（默认 `8081`） | 是 |
| `ONEBOT_ACCESS_TOKEN` | NapCat 鉴权令牌 | 是 |
| `WEBUI_TOKEN` | WebUI 管理面板令牌 | 是 |
| `SKIAPI_KEY` | 默认 AI 模型 API Key | 按需 |
| `OPENAI_API_KEY` | OpenAI API Key | 按需 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 按需 |
| `ANTHROPIC_API_KEY` | Claude API Key | 按需 |
| `GEMINI_API_KEY` | Gemini API Key | 按需 |
| `ONEBOT_API_TIMEOUT` | OneBot API 超时秒数（大文件建议调高） | 否 |

> 生产环境切勿将真实密钥提交到 Git。

### 5.2 `config/config.yml` — 全局业务配置

首次启动时从 `config/templates/master.template.yml` 自动生成。支持通过 WebUI 在线编辑。

| 分组 | 说明 |
|------|------|
| `bot` | 机器人名称、昵称、回复形式、分段策略、短呼叫词 |
| `api` | 模型供应商、模型名、base_url、温度、max_tokens、超时 |
| `agent` | Agent 最大步数、超时、工具调用策略、高风险控制 |
| `routing` | 路由置信度阈值、路由模式（`ai_full` / `keyword` 等） |
| `self_check` | 本地自检阈值、防误接话控制 |
| `queue` | 并发数、智能中断、消息 TTL、取消策略 |
| `music` | 本地音源、解锁源、歌手一致性保护 |
| `search` | 网页抓取超时、视频解析、视觉分析 |
| `safety` | 内容安全等级 |
| `admin` | 超级管理员 QQ、白名单群 |

配置缺失字段会自动从模板补齐（自愈机制），升级后无需手动迁移。

### 5.3 `plugins/config/*.yml` — 插件独立配置

每个插件一份独立 YAML 文件，在 WebUI 插件页可视化编辑。

示例 `plugins/config/newapi.yml`：

```yaml
enabled: true
display_name: skiapi
response:
  force_plain_text: true
  strip_markdown_chars: true
payment:
  auto_prefer_methods:
    - alipay
```

---

## 6. 运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 正常启动 | `python main.py` | 标准运行模式 |
| 一键启动 | `start.bat` / `bash start.sh` | 自动检查环境，缺失则自动修复 |
| 首次配置 | 自动触发 | `config/config.yml` 不存在时进入 |
| 强制 CLI 配置 | `python main.py --setup` | 跳过 WebUI，直接 CLI 向导 |
| 仅部署 | `python scripts/deploy.py` | 只安装依赖，不启动 |
| 部署并启动 | `python scripts/deploy.py --run` | 安装依赖后直接运行 |

---

## 7. 运维管理

### Linux systemd 服务

安装脚本会注册 `yukiko` CLI 工具：

```bash
yukiko status              # 查看服务状态
yukiko logs --lines 200    # 查看最近日志
yukiko restart             # 重启服务
yukiko stop / start        # 停止 / 启动
yukiko update --check-only # 检查是否有新版本
yukiko update --restart    # 拉取更新并重启
yukiko register            # 注册 systemd 服务
yukiko unregister          # 注销 systemd 服务
yukiko uninstall           # 卸载（可加 --purge-runtime --purge-env）
```

### WebUI 运维功能

Dashboard 页面提供：
- GitHub 版本检查与一键代码拉取
- Python 依赖同步
- WebUI 重新构建
- 多会话 AI 并发状态监控

Database 页面提供：
- 数据库文件导出
- SQLite 文件导入（自动备份旧库到 `storage/backups/db/`）

---

## 8. 故障排查

| 问题 | 解决方案 |
|------|----------|
| `ModuleNotFoundError` | 运行 `python scripts/deploy.py` 重新安装依赖 |
| WebUI 503 / 打不开 | 前端未构建：`cd webui && npm install && npm run build` |
| NapCat 连不上 | 检查 `ONEBOT_ACCESS_TOKEN` 是否一致，WS 地址格式是否正确 |
| 插件配置不生效 | 检查 `plugins/config/<name>.yml` 是否存在，或在 WebUI 插件页重新保存 |
| ffmpeg 找不到 | Linux: `sudo apt install ffmpeg`；Windows: 下载后加入 PATH |
| pip 安装超时 | 使用国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| Node.js 版本过低 | 升级到 18+：Linux 可用 `nvm install 18`；Windows 重新下载 LTS |
| 端口被占用 | 修改 `.env` 中的 `PORT`，或查找占用进程：`lsof -i:<PORT>`（Linux）/ `netstat -ano | findstr <PORT>`（Windows） |
| 配置文件损坏 | 删除 `config/config.yml`，重启后会从模板自动重建 |
| 虚拟环境损坏 | 删除 `.venv` 目录，重新运行 `start.sh` / `start.bat` 或 `python scripts/deploy.py` |

---

## 9. 进阶文档

| 文档 | 说明 |
|------|------|
| [架构说明](ARCHITECTURE.md) | 消息链路、Router、Agent、Self-check 设计原理 |
| [深度总结](PROJECT_DEEP_SUMMARY.md) | 项目架构深度分析、Prompt 维护、代码维护策略（700+ 行） |
| [发布运维手册](RELEASE_PLAYBOOK.md) | 发布/升级/回滚/排障 SOP、400+ 项 Checklist |
| [插件开发指南](../PLUGIN_GUIDE.md) | 插件配置模板与开发规范 |
| [English Guide](../en/GUIDE.md) | English deployment & configuration guide |
