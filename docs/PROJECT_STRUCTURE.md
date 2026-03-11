# PROJECT_STRUCTURE

## Tree (core folders)

```text
yukiko-bot/
├─ main.py
├─ app.py
├─ core/                  # engine, queue, router, tools, config loader
├─ plugins/               # plugin implementations + plugin config templates
│  └─ config/
├─ config/
│  └─ templates/          # master.template.yml
├─ services/              # model clients / external service wrappers
├─ webui/                 # React + Vite admin frontend
├─ scripts/               # deploy/build helpers
└─ tests/                 # regression tests
```

## 简体中文说明

- `core/engine.py`：统一编排消息处理主链路
- `core/queue.py`：并发和会话队列
- `core/router.py`：路由决策
- `core/tools.py`：工具执行与回传
- `plugins/`：插件功能与插件模板配置
- `webui/`：管理面板前端

## 繁體中文說明

- `core/`：核心處理邏輯
- `plugins/`：插件與插件設定
- `config/templates/`：全域模板
- `webui/`：管理介面
- `tests/`：回歸測試

## English notes

- `core/` holds runtime orchestration
- `plugins/` holds plugin logic and plugin-level configs
- `config/templates/` is the canonical default template source
- `webui/` is the admin UI frontend
- `tests/` holds regression coverage
