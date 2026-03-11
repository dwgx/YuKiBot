# YuKiKo Bot 原理說明（繁體中文）

本文件重點是系統設計與協作方式，不是部署教學。

## 1. 訊息主流程

1. OneBot 事件進入 `app.py`
2. 轉成 `EngineMessage`
3. `core/queue.py` 依會話策略排程
4. Trigger + Router 決定是否處理與動作類型
5. Self-check 做本地風控兜底
6. Agent/Tool 執行工具
7. Engine 輸出最終回覆（文字/圖片/語音/影片）

## 2. Self-check 的存在價值

Router 很快，但純靠模型容易在群聊誤接話。  
Self-check 的目標是把誤回覆降到可控範圍。

常見攔截：

- 非指向群聊、低置信度、沒有 listen_probe
- 只 @ 別人沒 @ 機器人
- 需要工具卻只回 `reply`

## 3. 配置模板設計

主模板：`config/templates/master.template.yml`  
載入合併：`core/config_templates.py`

特性：

- 缺欄位可自動補預設值
- 舊配置可在升級後被修復
- WebUI 與執行時共享同一份結構

## 4. 插件模板化配置

路徑：`plugins/config/*.yml`  
每個插件一份設定，WebUI 插件頁會讀 schema 呈現欄位。

優點：

- 可按插件拆分維護，不會變成超大設定頁
- 版本回滾更簡單
- 能對單一插件獨立調整風險策略

## 5. 音樂鏈路原則

推薦流程：

1. `music_search`
2. `music_play_by_id`
3. 失敗再走回退策略

穩定重點：

- 開 `artist_guard_enable`
- 控制 `unblock_sources` 品質
- 不把非解鎖來源混入解鎖 source 清單

## 6. 啟動模式分層

`main.py` 內建多模式切換：

- 正常模式：直接啟動 Bot
- 首次設定模式：缺 `config.yml` 時進 setup
- 強制 CLI setup：`--setup` / `setup`
- 前端未建置時自動回退 CLI setup

這樣可避免新環境卡在半初始化狀態。
