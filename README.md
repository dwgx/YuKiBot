# YuKiKo Bot（雪子）

QQ 群聊 AI 助手，基于 NoneBot2 + NapCat + 多模型 Agent。

## 用户先读：5 分钟跑起来

### 1. 环境准备
- Python 3.11+
- 已安装并登录 NapCat
- `ffmpeg` 在系统 PATH 中可用
- （可选）Node.js 18+，用于本地构建 WebUI

### 2. 拉取并安装
```bash
cd yukiko-bot
python -m venv .venv

# Windows
.\\.venv\\Scripts\\Activate.ps1

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

### 3. 填 `.env`
至少填写：
```env
ONEBOT_ACCESS_TOKEN=你的NapCat_token
WEBUI_TOKEN=你自己的WebUI登录口令
SKIAPI_KEY=你的模型密钥
```

如果你用其他模型商，再按需填写：`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`。

### 4. 配 NapCat 反向 WS
- 地址：`ws://127.0.0.1:8081/onebot/v11/ws`
- Token：与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 完全一致

OneBot 官方：<https://onebot.dev/>

### 5. 启动
```bash
# 通用
python main.py

# Linux/macOS 推荐（会自动检查 venv 并兜底部署）
./start.sh
```

启动后访问：`http://127.0.0.1:8080/webui/`

## 常用入口（用户视角）
- 改模型/API：WebUI `Setup` / `Config`
- 改回复风格与 Agent 规则：WebUI `Prompts`
- 看实时日志：WebUI `Logs`
- 管理 Cookie：WebUI `Cookies`
- 看数据库（记忆/知识）：WebUI `Database`

## Linux / macOS 部署

### 快速部署（单机常驻）
```bash
cd yukiko-bot
chmod +x start.sh build-webui.sh
./start.sh
```

### 构建 WebUI 静态资源
```bash
./build-webui.sh
```

### 建议的生产做法
- 用 `systemd`（Linux）或 `launchd`（macOS）托管 `./start.sh`
- 反向代理 `/webui/`（Nginx/Caddy）并限制来源 IP
- 把 `.env` 与 `config/config.yml` 做备份

## Cookie 能力矩阵（当前实现）

说明：自动提取受浏览器版本、系统密钥链、登录态影响。Windows 成功率通常最高。

| 能力 | Windows | Linux | macOS | 说明 |
|---|---|---|---|---|
| B站二维码扫码登录 | ✅ | ✅ | ✅ | 依赖 `bilibili-api-python`；WebUI Setup/Cookies 都可发起 |
| B站浏览器 Cookie 提取 | ✅ | ⚠️ | ⚠️ | Linux/macOS 受 keyring/浏览器加密策略影响，可能失败 |
| Douyin browser scan-login + cookie extraction | Yes | Maybe | Maybe | Open Douyin's official login page in the browser, scan-login there, then extract cookies from the same browser profile |
| Kuaishou browser scan-login + cookie extraction | Yes | Maybe | Maybe | Open Kuaishou's official login page in the browser, scan-login there, then extract cookies from the same browser profile |
| QZone browser scan-login + cookie extraction | Yes | Maybe | Maybe | Open QZone's official login page in the browser, scan-login there, and make sure the browser reaches your own QZone home page before extraction |
| QQ native QR direct login | No | No | No | Native QQ QR auth is still unavailable; use the QZone browser scan-login + cookie extraction flow instead |

### Cookie 失败时的处理顺序
1. 先用 B站二维码登录（B站场景最稳）
2. 确认目标站点已在浏览器登录
3. 尝试切换浏览器（Windows 推荐 Edge/Chrome，Linux/macOS 可优先 Firefox）
4. 在 WebUI 勾选“自动关闭浏览器后重试”

## 常见问题
- 不回复：先看是否满足触发条件（@、私聊、会话跟进），再看 `Logs`
- WebUI 401：检查 `WEBUI_TOKEN`
- 连接 NapCat 失败：先启动 bot，再检查反向 WS 地址/Token
- 视频发送慢：调大超时与上传等待配置

---

## 开发者附录（放底部）

### 架构主链
`NapCat -> NoneBot Adapter -> Queue -> Trigger -> Router -> Agent -> Safety -> Response`

### 关键目录
```text
yukiko-bot/
├─ main.py
├─ app.py
├─ config/
│  ├─ config.yml
│  ├─ prompts.yml
│  └─ templates/master.template.yml
├─ core/
│  ├─ engine.py
│  ├─ router.py
│  ├─ agent.py
│  ├─ agent_tools.py
│  ├─ tools.py
│  └─ webui.py
├─ services/
├─ utils/
├─ plugins/
└─ webui/
```

### 本次路线相关说明
- 本地 cue/heuristic 路由路径已按“纯 AI 优先”方向清理
- `prompts.yml` 现在主要承担 AI 行为约束，不再承担本地关键词分支匹配

### 开发常用命令
```bash
python -m py_compile core/engine.py core/tools.py core/router.py core/agent.py core/config_templates.py utils/intent.py

cd webui
npm run build
```

### 插件最小示例
```python
class Plugin:
    name = "demo"
    description = "示例插件"

    async def handle(self, message: str, context: dict) -> str:
        return "ok"
```
