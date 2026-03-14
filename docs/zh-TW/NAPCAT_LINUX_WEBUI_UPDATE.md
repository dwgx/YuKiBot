# NapCat / Linux / WebUI 更新說明

本次更新聚焦三件事：

1. Linux 下 NapCat 安裝檢測更完整
2. 卸載流程可自動清理 NapCat
3. WebUI 可直接管理 `.env`（含 `ONEBOT_ACCESS_TOKEN` / `WEBUI_TOKEN`）
4. 新增 `doctor / backup / restore` 運維命令
5. 部署與更新加速（`--fast`、按變更決定依賴同步）

## 1) Linux 管理腳本 (`scripts/yukiko_manager.sh`)

### 新增命令

```bash
yukiko napcat-status
yukiko napcat-status --method-only
yukiko napcat-status --quiet
```

用途：

- 檢查 NapCat 是否已安裝
- 回傳檢測來源（binary / shell path / systemd / docker / process）

### Update 熱更新流程強化

`yukiko update` 現在包含：

- `npm ci --no-audit --no-fund || npm install --no-audit --no-fund`
- 服務重啟後等待 `systemctl is-active`
- 輪詢 `http://<HOST>:<PORT>/api/webui/health`，確認服務真正恢復

### Uninstall 流程強化

`yukiko uninstall` 預設會嘗試清理 NapCat：

- 優先跑官方卸載：`bash install.sh --uninstall`
- 失敗時自動 fallback：
  - 停止/禁用 napcat 相關 systemd 單元
  - 清理 napcat docker container
  - 刪除常見 NapCat 路徑與執行檔

可選參數：

```bash
yukiko uninstall --keep-napcat
```

## 2) Linux 安裝腳本 (`install.sh`)

新增參數：

```bash
--onebot-access-token <token>
```

安裝時會寫入：

- `HOST`
- `PORT`
- `WEBUI_TOKEN`
- `ONEBOT_ACCESS_TOKEN`

並且在 NapCat 檢測中新增了常見 Linux 路徑與 systemd 搜索方式，減少「已安裝卻檢測不到」。

## 3) WebUI `.env` 管理

後端新增：

- `GET /api/webui/env`
- `PUT /api/webui/env`

前端 `Config` 頁新增「環境變數與 NapCat 連接」卡片，可直接編輯並保存允許的 `.env` 欄位。

行為規則：

- `WEBUI_TOKEN` 變更：回傳 `reauth_required=true`，前端會清除 token 並導回登入
- `HOST/PORT/DRIVER/ONEBOT_API_TIMEOUT/ONEBOT_ACCESS_TOKEN`：回傳 `restart_required=true`
- 其餘可熱更新項目：會嘗試即時重載

## 4) 回歸測試

新增測試：

- `tests/test_webui_env_regression.py`
- `tests/test_linux_scripts_regression.py`

並通過全量測試：

```text
133 passed
```

## 5) 新增運維命令

### `yukiko doctor`

```bash
yukiko doctor
yukiko doctor --service-name yukiko --timeout-seconds 12
```

檢查項目：

- `.env`、`PORT`、`WEBUI_TOKEN`、`ONEBOT_ACCESS_TOKEN`
- `ffmpeg`
- NapCat 本地檢測
- systemd 服務狀態
- `WebUI /api/webui/health`
- `WebUI /api/webui/napcat/status`（帶 token）

### `yukiko backup` / `yukiko restore`

```bash
yukiko backup
yukiko backup --output-dir ./backups --name prod_before_update
yukiko restore --file ./backups/yukiko_backup_20260101_120000.tar.gz --yes
```

備份內容：

- `.env`、`.env.prod`
- `config/`
- `plugins/config/`
- `storage/`

`restore` 會先做一份 `pre_restore` 安全備份，再執行還原；預設會嘗試重啟服務。

## 6) 部署加速

### 安裝加速（`install.sh`）

```bash
bash install.sh --fast --non-interactive
```

`--fast` 會：

- 跳過 WebUI build
- 跳過 NapCat 自動安裝
- 在 apt 系統略過 `apt-get update`

### 更新加速（`yukiko update`）

```bash
yukiko update --fast
```

新策略：

- 只有偵測到依賴變更才同步 Python 套件
- 只有偵測到 `webui/` 變更才執行前端 build
- 支援 `--force-python` / `--force-webui` 強制執行

### 更新失敗自動回滾

`yukiko update` 預設啟用 `auto-rollback`：

- 若更新後服務重啟或健康檢查失敗，會回滾到更新前 commit
- 可用 `--no-auto-rollback` 關閉
