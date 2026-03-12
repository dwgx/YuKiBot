# YuKiKo Bot 部署與使用指南（繁體中文）

本指南涵蓋從零開始到穩定運行的完整流程，適用於 Linux / Windows / macOS。

---

## 目錄

- [1. 環境準備](#1-環境準備)
- [2. 部署安裝](#2-部署安裝)
- [3. 連接 NapCat](#3-連接-napcat)
- [4. 首次啟動與 WebUI](#4-首次啟動與-webui)
- [5. 設定詳解](#5-設定詳解)
- [6. 運行模式](#6-運行模式)
- [7. 維運管理](#7-維運管理)
- [8. 故障排查](#8-故障排查)

---

## 1. 環境準備

| 依賴 | 版本 | 說明 |
|------|------|------|
| Python | 3.10+ | 建議 3.11 / 3.12 |
| Node.js | 18+ | 建置 WebUI 用 |
| ffmpeg | 最新 | 音視頻處理 |
| OneBot V11 | — | 建議 [NapCat](https://github.com/NapNeko/NapCatQQ) |

### Linux（Ubuntu / Debian）

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm ffmpeg git curl
```

### Windows

1. **Python**: https://www.python.org/downloads/ （安裝時勾選 "Add to PATH"）
2. **Node.js**: https://nodejs.org/ （LTS 版本）
3. **ffmpeg**: https://www.gyan.dev/ffmpeg/builds/ （解壓後將 `bin` 加入 PATH）
4. **Git**: https://git-scm.com/download/win

### macOS

```bash
brew install python@3.12 node ffmpeg git
```

---

## 2. 部署安裝

### Linux 一鍵部署（建議）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

腳本自動完成：系統依賴 → Python 虛擬環境 → WebUI 建置 → systemd 服務。

### Windows

```powershell
git clone https://github.com/dwgx/YuKiKo.git
cd YuKiKo
Copy-Item .env.example .env
# 編輯 .env，填寫 ONEBOT_ACCESS_TOKEN 和 WEBUI_TOKEN
.\start.bat
```

### macOS

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
cp .env.example .env
# 編輯 .env
bash start.sh
```

### 手動部署

```bash
git clone https://github.com/dwgx/YuKiKo.git && cd YuKiKo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cd webui && npm install && npm run build && cd ..
python main.py
```

---

## 3. 連接 NapCat

在 NapCat 的 OneBot V11 設定頁：

| 設定項 | 值 |
|--------|-----|
| 連線模式 | 反向 WebSocket |
| WS 上報位址 | `ws://<YuKiKo地址>:<端口>/onebot/v11/ws` |
| Access Token | 與 `.env` 中 `ONEBOT_ACCESS_TOKEN` 一致 |

---

## 4. 首次啟動與 WebUI

首次啟動（`config/config.yml` 不存在時）會進入設定模式。

建置 WebUI：`bash build-webui.sh`（Linux）/ `build-webui.bat`（Windows）

存取 WebUI：`http://<HOST>:<PORT>/webui/login`

---

## 5. 設定詳解

三層設定體系：

| 層級 | 檔案 | 用途 |
|------|------|------|
| 環境變數 | `.env` | 端口、金鑰、API Key |
| 全域設定 | `config/config.yml` | 機器人行為、模型參數 |
| 插件設定 | `plugins/config/*.yml` | 每個插件獨立一份 |

---

## 6. 運行模式

| 模式 | 命令 |
|------|------|
| 標準啟動 | `python main.py` |
| 一鍵啟動 | `start.bat` / `bash start.sh` |
| 強制 CLI 設定 | `python main.py --setup` |
| 僅部署 | `python scripts/deploy.py` |
| 部署並啟動 | `python scripts/deploy.py --run` |

---

## 7. 維運管理

```bash
yukiko status / logs / restart / stop / start
yukiko update --check-only / --restart
yukiko uninstall
```

---

## 8. 故障排查

| 問題 | 解決 |
|------|------|
| `ModuleNotFoundError` | `python scripts/deploy.py` |
| WebUI 503 | `cd webui && npm install && npm run build` |
| NapCat 連不上 | 確認 token 一致、WS 位址正確 |
| ffmpeg 找不到 | 安裝後加入 PATH |

---

進階文件：[架構說明](ARCHITECTURE.md) · [簡體中文指南](../zh-CN/GUIDE.md) · [English Guide](../en/GUIDE.md)
