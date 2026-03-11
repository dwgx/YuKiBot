# YuKiKo Bot 使用指南（繁體中文）

本指南依照實際操作順序編排：先部署、再調參、最後看完整運行模式。  
目標是讓你快速上線，後續也好維護。

## 1. 部署前準備

### 1.1 環境需求

- Python 3.10+（建議 3.11 或 3.12）
- Node.js 18+（用於建置 WebUI）
- npm
- 可用的 OneBot V11 服務（例如 NapCat）

### 1.2 下載與進入專案

```bash
git clone <your-repo-url> yukiko-bot
cd yukiko-bot
```

### 1.3 建立環境變數檔

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

至少先調整：

- `ONEBOT_ACCESS_TOKEN`：必須和 OneBot 端一致
- `WEBUI_TOKEN`：WebUI API 保護 token，請改成隨機值
- `HOST` / `PORT`：依實際部署環境設定

## 2. 快速部署啟動（建議）

### 2.1 Linux 一鍵部署（1Panel 風格）

```bash
bash install.sh
```

GitHub 遠端腳本直裝（不用先手動 clone）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh)
```

非互動直裝範例：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiBot/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

你會逐步設定：

- `HOST`（監聽位址）
- `PORT`（自訂端口）
- `WEBUI_TOKEN`
- systemd 服務名稱
- 是否自動放行防火牆端口

腳本會自動完成：

- 系統依賴安裝（Python/Node.js/npm/ffmpeg 等）
- 虛擬環境與 Python 套件部署
- WebUI 建置
- 寫入 `.env` 的 `HOST/PORT`
- 可選建立並啟動 systemd 服務

非互動模式範例：

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

常用維運命令：

```bash
yukiko --help
yukiko update --check-only
yukiko update --restart
yukiko status
yukiko logs --lines 200
yukiko stop
yukiko start
yukiko register --service-name yukiko
yukiko unregister --service-name yukiko
yukiko uninstall --purge-runtime --purge-env
```

### 2.2 Windows 一鍵啟動

```bat
start.bat
```

### 2.3 Linux / macOS 一鍵啟動（輕量模式）

```bash
bash start.sh
```

以上腳本會先檢查 `.venv` 健康狀態。  
若虛擬環境缺失或壞掉，會自動走 `scripts/deploy.py --run` 重新部署。

### 2.4 手動部署

```bash
python scripts/deploy.py
python scripts/deploy.py --run
```

- `python scripts/deploy.py`：只做環境與依賴修復
- `python scripts/deploy.py --run`：修復完成後直接啟動

## 3. 首次啟動與 WebUI

若 `config/config.yml` 不存在：

- 系統會進入首次設定模式
- 若 `webui/dist` 存在，會導向 `/webui/setup`
- 若前端尚未建置，會回退到 CLI 設定精靈

建置 WebUI：

Windows:

```bat
build-webui.bat
```

Linux / macOS:

```bash
bash build-webui.sh
```

手動：

```bash
cd webui
npm install
npm run build
```

## 4. 參數設定（重點）

參數分三層：`.env`、`config/config.yml`、`plugins/config/*.yml`。

### 4.1 `.env`（執行環境與金鑰）

參考 `.env.example`。常用欄位：

- `HOST` / `PORT`
- `ONEBOT_API_TIMEOUT`
- `ONEBOT_ACCESS_TOKEN`
- `WEBUI_TOKEN`
- `SKIAPI_KEY` / `OPENAI_API_KEY` / `NEWAPI_API_KEY` 等

建議：

- 不要把真實金鑰提交到 Git
- 測試與正式環境使用不同 token

### 4.2 `config/config.yml`（全域業務設定）

模板來源：`config/templates/master.template.yml`，  
`core/config_templates.py` 會處理預設值與自我修復。

高頻設定區：

- `bot`：名稱、暱稱、回覆策略、短呼叫詞
- `api`：模型供應商、模型名、超時、token
- `agent`：步數、超時、高風險控制
- `routing`：路由置信度策略
- `self_check`：本地自檢閾值
- `queue`：併發與中斷策略
- `music`：本地音源與解鎖音源

### 4.3 `plugins/config/*.yml`（插件模板化設定）

這層就是你想要的「列表可控」配置方式：一個插件一個檔案。  
比起超大單頁配置，維護更直觀。

#### NewAPI 範例（`plugins/config/newapi.yml`）

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

#### ConnectCLI 範例（`plugins/config/connect_cli.yml`）

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

## 5. 音樂介面維護建議

關鍵在 `config/config.yml -> music`：

- `local_source_enable`
- `unblock_enable`
- `unblock_sources`
- `artist_guard_enable`

穩定策略：

- 先確保本地源可用，再開解鎖源
- `unblock_sources` 只放真正解鎖來源（如 `qq,kuwo,migu`）
- 不要把用途不同的 source 混進去，避免誤路由

## 6. 所有運行方式（完整）

### 6.1 標準運行

```bash
python main.py
```

或腳本：

- Windows: `start.bat`
- Linux/macOS: `bash start.sh`

### 6.2 首次設定 WebUI 模式（自動）

觸發：`config/config.yml` 不存在。  
入口：`/webui/setup`。

### 6.3 強制 CLI 設定模式

```bash
python main.py --setup
```

或：

```bash
python main.py setup
```

### 6.4 只部署不啟動

```bash
python scripts/deploy.py
```

### 6.5 部署後直接啟動

```bash
python scripts/deploy.py --run
```

### 6.6 只建置前端

```bash
bash build-webui.sh
```

Windows:

```bat
build-webui.bat
```

## 7. 快速排錯

- `ModuleNotFoundError`：先執行 `python scripts/deploy.py`
- WebUI 503：前端未建置，執行 `npm run build`
- OneBot 無法連線：確認 `ONEBOT_ACCESS_TOKEN` 與上游一致
- 插件配置無效：檢查 `plugins/config/<name>.yml` 與 WebUI 插件頁保存結果

## 8. 原理文件

- `docs/zh-TW/ARCHITECTURE.md`
