<div align="center">

# YuKiKo Bot

**基于 NoneBot2 + OneBot V11 的智能 QQ 机器人**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![NoneBot2](https://img.shields.io/badge/NoneBot2-FastAPI-EA5252)](https://nonebot.dev/)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=white)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/dwgx/YuKiKo?style=social)](https://github.com/dwgx/YuKiKo)

一个功能丰富的 AI 驱动 QQ 群聊/私聊机器人，支持多模型接入、Agent 工具调用、
音乐点播、图片生成、视频解析、Web 管理面板等。

[快速开始](#-快速开始) · [功能特性](#-功能特性) · [配置说明](#-配置说明) · [文档](#-文档) · [English](docs/en/GUIDE.md)

</div>

---

## ✨ 功能特性

| 类别 | 功能 |
|------|------|
| **AI 对话** | 多模型（OpenAI / Claude / DeepSeek / Gemini / 通义千问 / Moonshot 等），上下文记忆，知识库自动学习 |
| **Agent 系统** | 50+ 内置工具，自动推理与多步工具调用，置信度路由 |
| **音乐点播** | 多平台搜索播放，VIP 歌曲解锁，歌手一致性保护，B站音频回退 |
| **搜索引擎** | 联网搜索，网页智能摘要，结构化信息提取 |
| **视频解析** | B站 / 抖音 / 快手 / AcFun 视频下载与内容分析 |
| **图片生成** | DALL-E 等多模型，自动 NSFW 过滤 |
| **Web 管理面板** | React 前端，配置热更新，日志实时查看，数据库管理，在线聊天测试 |
| **插件系统** | 热插拔，独立配置文件，WebUI 可视化管理 |
| **安全机制** | 内容过滤，权限三级分层，密钥加密存储，Self-check 防误回复 |

## 📋 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| **Python** | 3.10+ | 推荐 3.11 / 3.12 |
| **Node.js** | 18+ | 构建 WebUI 用 |
| **ffmpeg** | 最新 | 音视频处理（Linux 安装脚本自动安装） |
| **OneBot V11 服务** | — | 推荐 [NapCat](https://github.com/NapNeko/NapCatQQ) |

> Windows 用户如果没有 Python / Node.js，请先安装：
> - Python: https://www.python.org/downloads/ （安装时勾选 "Add to PATH"）
> - Node.js: https://nodejs.org/ （LTS 版本即可）
> - ffmpeg: https://www.gyan.dev/ffmpeg/builds/ （下载后加入系统 PATH）

## 🚀 快速开始

### 方式一：Linux 一键部署（推荐）

无需手动 clone，一条命令搞定：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

脚本自动完成：系统依赖 → Python 虚拟环境 → WebUI 构建 → systemd 服务注册。

<details>
<summary>非交互模式（适合自动化 / CI）</summary>

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh) \
  -- --non-interactive --host 0.0.0.0 --port 8081 --service-name yukiko --open-firewall
```
</details>

<details>
<summary>已 clone 仓库？用本地安装脚本</summary>

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
bash install.sh
```
</details>

### 方式二：Windows 手动部署

```powershell
# 1. 克隆项目
git clone https://github.com/dwgx/YuKiKo.git
cd YuKiKo

# 2. 复制环境变量文件
Copy-Item .env.example .env

# 3. 编辑 .env，至少填写：
#    ONEBOT_ACCESS_TOKEN=你的NapCat令牌
#    WEBUI_TOKEN=随机字符串

# 4. 一键启动（自动创建虚拟环境 + 安装依赖）
.\start.bat
```

首次启动后访问 `http://127.0.0.1:8081/webui/setup` 完成初始配置。

### 方式三：macOS / Linux 手动部署

```bash
# 1. 克隆项目
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo

# 2. 复制环境变量文件并编辑
cp .env.example .env
# 编辑 .env，填写 ONEBOT_ACCESS_TOKEN 和 WEBUI_TOKEN

# 3. 一键启动
bash start.sh
```

### 方式四：手动完全控制

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 构建 WebUI（可选但推荐）
cd webui && npm install && npm run build && cd ..

# 启动
python main.py
```

## 🔗 连接 NapCat（QQ 适配器）

YuKiKo 通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 OneBot V11 协议接入 QQ。

**NapCat 侧配置：**

1. 连接模式选择 **反向 WebSocket (Reverse WS)**
2. WS 上报地址填写：`ws://<YuKiKo地址>:<端口>/onebot/v11/ws`
3. Access Token 与 `.env` 中的 `ONEBOT_ACCESS_TOKEN` **保持一致**

```text
# 同机部署示例
ws://127.0.0.1:8081/onebot/v11/ws

# 跨机器示例（填 YuKiKo 所在机器的 IP）
ws://192.168.1.50:8081/onebot/v11/ws
```

> Linux 安装脚本会自动检测并提示安装 NapCat。手动安装：
> ```bash
> curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && bash napcat.sh
> ```
> 进阶运维与本次更新说明：[`docs/zh-TW/NAPCAT_LINUX_WEBUI_UPDATE.md`](docs/zh-TW/NAPCAT_LINUX_WEBUI_UPDATE.md)

## ⚙️ 配置说明

YuKiKo 采用三层配置体系：

| 层级 | 文件 | 用途 |
|------|------|------|
| **环境变量** | `.env` | 端口、密钥、API Key 等敏感信息 |
| **全局配置** | `config/config.yml` | 机器人行为、模型参数、路由策略等 |
| **插件配置** | `plugins/config/*.yml` | 每个插件独立一份配置文件 |

### .env 关键字段

```env
HOST=0.0.0.0                              # 监听地址
PORT=8081                                  # 监听端口
ONEBOT_ACCESS_TOKEN=your_napcat_token      # NapCat 鉴权令牌
WEBUI_TOKEN=your_random_string             # WebUI 管理面板令牌
SKIAPI_KEY=                                # 默认 AI 模型 API Key
OPENAI_API_KEY=                            # OpenAI（按需）
DEEPSEEK_API_KEY=                          # DeepSeek（按需）
ANTHROPIC_API_KEY=                         # Claude（按需）
```

完整字段参考 [`.env.example`](.env.example)。

### config.yml 主要分组

首次启动时会从模板自动生成，也可通过 WebUI 在线编辑。

| 分组 | 说明 |
|------|------|
| `bot` | 机器人名称、昵称、回复形式、分段策略 |
| `api` | 模型供应商、模型名、base_url、温度、token 上限 |
| `agent` | Agent 步数上限、超时、工具调用策略 |
| `routing` | 路由置信度阈值、路由模式 |
| `queue` | 并发控制、智能中断、消息队列策略 |
| `music` | 音源配置、解锁源、歌手保护 |
| `search` | 网页抓取、视频解析、视觉分析 |
| `safety` | 内容安全等级 |

## 🖥️ WebUI 管理面板

启动后访问 `http://<HOST>:<PORT>/webui/login`，使用 `.env` 中的 `WEBUI_TOKEN` 登录。

| 页面 | 功能 |
|------|------|
| Dashboard | 系统状态、版本检查、一键更新、并发监控 |
| Config | YAML 在线编辑器，支持热更新 |
| Plugins | 插件列表、启用/禁用、独立配置编辑 |
| Prompts | 系统提示词编辑 |
| Chat | 在线聊天测试 |
| Memory | 记忆管理与搜索 |
| Database | 数据库导出/导入/浏览 |
| Logs | 实时日志查看 |

## 🔌 插件系统

插件放在 `plugins/` 目录下，配置文件在 `plugins/config/` 中。

内置插件：
- **NewAPI** — 支付/充值集成
- **Wayback** — Internet Archive 网页快照查询
- **ConnectCLI** — 外部 CLI 工具接入

开发自定义插件请参考 [`plugins/example_plugin.py`](plugins/example_plugin.py) 和 [插件开发指南](docs/PLUGIN_GUIDE.md)。

## 🛠️ 运维命令

安装脚本会注册 `yukiko` CLI 工具（仅 Linux）：

```bash
yukiko status              # 查看服务状态
yukiko logs --lines 200    # 查看最近日志
yukiko restart             # 重启服务
yukiko stop                # 停止服务
yukiko start               # 启动服务
yukiko update --check-only # 检查更新
yukiko update --restart    # 更新并重启
yukiko uninstall           # 卸载
```

## 🔍 常见问题

<details>
<summary><b>报 ModuleNotFoundError</b></summary>

虚拟环境依赖不完整，运行：
```bash
python scripts/deploy.py
```
</details>

<details>
<summary><b>WebUI 打不开 / 503</b></summary>

前端未构建。运行：
```bash
cd webui && npm install && npm run build
```
或使用脚本：`bash build-webui.sh`（Linux）/ `build-webui.bat`（Windows）
</details>

<details>
<summary><b>NapCat 连不上</b></summary>

1. 确认 `.env` 中 `ONEBOT_ACCESS_TOKEN` 与 NapCat 侧完全一致
2. 确认 NapCat 连接模式为"反向 WebSocket"
3. 确认 WS 地址格式：`ws://<IP>:<PORT>/onebot/v11/ws`
4. 跨机器部署时确认防火墙已放行端口
</details>

<details>
<summary><b>Windows 下 ffmpeg 找不到</b></summary>

1. 从 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg
2. 解压后将 `bin` 目录加入系统 PATH
3. 重启终端，运行 `ffmpeg -version` 验证
</details>

<details>
<summary><b>插件配置不生效</b></summary>

YuKiKo 支持两种插件配置位置：
- `plugins/config/<plugin>.yml`（推荐，独立管理）
- `config/plugins.yml`（统一管理）

在 WebUI 插件页保存后会自动写回正确位置。如果手动编辑，请确认文件路径正确。
</details>

## 📁 项目结构

```
YuKiKo/
├── main.py              # 入口
├── app.py               # OneBot 事件处理
├── core/                # 核心引擎（消息处理、路由、Agent、队列、WebUI 后端）
├── plugins/             # 插件 + 插件配置模板
│   └── config/          # 插件独立配置文件
├── config/              # 全局配置
│   └── templates/       # 配置模板（master.template.yml）
├── services/            # 模型客户端 / 外部服务封装
├── webui/               # React + Vite 管理面板前端
├── scripts/             # 部署 / 构建脚本
├── utils/               # 工具函数（文本处理、媒体、过滤器）
├── storage/             # 运行时数据（缓存、数据库）
├── deploy/              # systemd 服务模板
├── tests/               # 测试
├── install.sh           # Linux 交互式安装脚本
├── bootstrap.sh         # 远程一键部署脚本
├── start.sh / start.bat # 一键启动脚本
└── .env.example         # 环境变量模板
```

## 📖 文档

| 文档 | 说明 |
|------|------|
| [简体中文部署指南](docs/zh-CN/GUIDE.md) | 完整部署、配置、运行方式 |
| [简体中文架构说明](docs/zh-CN/ARCHITECTURE.md) | 内部设计原理 |
| [English Guide](docs/en/GUIDE.md) | Deployment & configuration |
| [English Architecture](docs/en/ARCHITECTURE.md) | Internal design notes |
| [繁體中文指南](docs/zh-TW/GUIDE.md) | 部署與設定 |
| [插件开发指南](docs/PLUGIN_GUIDE.md) | 插件配置与开发 |

## 📄 License

[MIT](LICENSE)
