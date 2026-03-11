# YuKiKo Bot 使用指南（简体中文）

本指南按实际使用顺序写：先部署，再配置参数，最后讲所有运行方式。  
目标是让你拿到仓库后，最快时间跑起来并稳定维护。

## 1. 部署前准备

### 1.1 环境要求

- Python 3.10+（建议 3.11 或 3.12）
- Node.js 18+（用于构建 WebUI）
- npm（随 Node.js 安装）
- 已可用的 OneBot V11 服务（例如 NapCat）

### 1.2 克隆与进入目录

```bash
git clone <your-repo-url> yukiko-bot
cd yukiko-bot
```

### 1.3 准备环境变量

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

至少先改这几项：

- `ONEBOT_ACCESS_TOKEN`：必须和 OneBot 服务保持一致
- `WEBUI_TOKEN`：WebUI API 访问 token，务必改成随机字符串
- `HOST` / `PORT`：按你本机和反向代理规划设置

## 2. 快速部署启动（推荐）

### 2.1 Linux 一键部署（1Panel 风格）

```bash
bash install.sh
```

你会按步骤填写：

- `HOST`（监听地址）
- `PORT`（自定义端口）
- `WEBUI_TOKEN`
- systemd 服务名
- 是否自动放行防火墙端口

脚本会自动完成：

- 系统依赖安装（Python/Node.js/npm/ffmpeg 等）
- 虚拟环境和 Python 依赖部署
- WebUI 构建
- 写入 `.env` 的 `HOST/PORT`
- 可选创建并启动 systemd 服务

非交互示例（适合自动化）：

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

常用运维命令：

```bash
yukiko --help
yukiko status
yukiko logs --lines 200
yukiko stop
yukiko start
yukiko register --service-name yukiko
yukiko unregister --service-name yukiko
yukiko uninstall --purge-runtime --purge-env
```

### 2.2 Windows 一键启动

```bat
start.bat
```

### 2.3 Linux / macOS 一键启动（轻量模式）

```bash
bash start.sh
```

这两个脚本会自动检查 `.venv` 是否可用。  
如果虚拟环境缺失或损坏，会自动调用 `scripts/deploy.py --run` 做安装并启动。

### 2.4 手动部署（可控模式）

```bash
python scripts/deploy.py
python scripts/deploy.py --run
```

- `python scripts/deploy.py`：只做环境修复和依赖安装
- `python scripts/deploy.py --run`：修复后直接运行 `main.py`

## 3. 首次启动与 WebUI

当 `config/config.yml` 不存在时：

- 系统会进入首次配置模式
- 如果 `webui/dist` 已构建，会提示打开 `/webui/setup`
- 如果未构建，会自动回退到 CLI 向导

构建 WebUI：

Windows:

```bat
build-webui.bat
```

Linux / macOS:

```bash
bash build-webui.sh
```

手动构建：

```bash
cd webui
npm install
npm run build
```

## 4. 参数设置（核心）

参数分三层：`.env`、`config/config.yml`、`plugins/config/*.yml`。

### 4.1 `.env`（运行环境与密钥）

参考文件：`.env.example`。

高频项：

- `HOST` / `PORT`：服务监听地址与端口
- `ONEBOT_API_TIMEOUT`：OneBot API 超时，发大文件建议调高
- `ONEBOT_ACCESS_TOKEN`：OneBot 鉴权
- `WEBUI_TOKEN`：WebUI API 鉴权
- `SKIAPI_KEY` / `OPENAI_API_KEY` / `NEWAPI_API_KEY` 等：上游模型密钥

建议：

- 生产环境不要把真实密钥提交到 Git
- 使用不同 token 区分测试环境和正式环境

### 4.2 `config/config.yml`（全局业务参数）

模板来源：`config/templates/master.template.yml`。  
`core/config_templates.py` 会负责默认值与自愈。

重点分组：

- `bot`：机器人名字、昵称、回复形式、短呼叫词等
- `api`：模型供应商、模型名、base_url、超时、token 上限
- `agent`：Agent 步数、超时、高风险控制
- `routing`：路由置信度与路由策略
- `self_check`：本地自检阈值与防误接话控制
- `queue`：并发、取消策略、中断策略
- `music`：本地音源/解锁音源参数
- `search`：网页抓取、视频解析、视觉分析参数

### 4.3 `plugins/config/*.yml`（插件级参数模板）

这层是你提出的“模板化好操控”核心实践。  
每个插件一个独立 yml，便于在 WebUI 插件页分插件管理。

#### NewAPI 模板示例（`plugins/config/newapi.yml`）

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

#### ConnectCLI 模板示例（`plugins/config/connect_cli.yml`）

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

## 5. 音乐接口维护建议（避免乱源）

核心参数在 `config/config.yml -> music`：

- `local_source_enable`：是否启用本地源
- `unblock_enable`：是否启用解锁源
- `unblock_sources`：解锁源列表（逗号分隔）
- `artist_guard_enable`：是否启用歌手一致性保护

稳定建议：

- 先开 `local_source_enable=true`，验证本地链路稳定
- `unblock_sources` 仅放真正解锁源（如 `qq,kuwo,migu`）
- 避免混入不稳定或用途不同的 source，减少错误路由
- 保持 `artist_guard_enable=true`，降低“播错歌”概率

## 6. 所有运行方式（完整清单）

### 6.1 正常运行模式

```bash
python main.py
```

或使用脚本：

- Windows: `start.bat`
- Linux/macOS: `bash start.sh`

### 6.2 首次配置 WebUI 模式（自动触发）

触发条件：`config/config.yml` 不存在。  
入口：`http://<HOST>:<PORT>/webui/setup`（需先构建前端）。

### 6.3 强制 CLI 配置模式

```bash
python main.py --setup
```

或：

```bash
python main.py setup
```

### 6.4 仅部署不启动

```bash
python scripts/deploy.py
```

### 6.5 部署后立即启动

```bash
python scripts/deploy.py --run
```

### 6.6 仅构建前端

```bash
bash build-webui.sh
```

或 Windows:

```bat
build-webui.bat
```

## 7. 故障排查速查

- 报 `ModuleNotFoundError`：先跑 `python scripts/deploy.py`
- WebUI 503：前端没构建，执行 `npm run build`
- OneBot 连不上：检查 `ONEBOT_ACCESS_TOKEN` 与上游是否一致
- 插件参数不生效：检查对应 `plugins/config/<name>.yml` 与 WebUI 插件页保存结果

## 8. 原理文档

- 架构与数据流：`docs/zh-CN/ARCHITECTURE.md`
- 里面包含 Agent、Router、自检、队列、插件配置模板的协作关系
