# YuKiKo 项目全量分析（2026-03-09）

## 1. 分析范围与当前基线

本次分析覆盖以下目录：

- 入口与装配：`main.py`、`app.py`
- 核心引擎：`core/*.py`
- 模型服务适配：`services/*.py`
- 工具与辅助：`utils/*.py`
- 插件体系：`plugins/*.py`
- 前端管理台：`webui/src/*`
- 配置模板与提示词：`config/templates/*`、`config/prompts.yml`

当前工作区规模（不含 `.venv`、`webui/node_modules`、`webui/dist`）：

- Python 文件：`78` 个，约 `49,571` 行
- `core/`：`41` 个文件，约 `41,371` 行
- WebUI 源码：`14` 个文件，约 `3,686` 行

体量最大的核心文件：

1. `core/tools.py`（374KB）
2. `core/agent_tools.py`（322KB）
3. `core/engine.py`（269KB）
4. `core/agent.py`（134KB）

---

## 2. 真实运行链路（按当前代码）

### 2.1 启动链路

1. `main.py` 加载 `.env` / `.env.prod`
2. 若 `config/config.yml` 不存在：进入 `core/setup.py` 向导（WebUI 或 CLI）
3. `nonebot.init()` + 注册 OneBot V11 Adapter
4. `create_engine()` 构建 `YukikoEngine`
5. `register_handlers(engine)` 注册消息、通知、请求、元事件处理器
6. 挂载 `core/webui.py` 的 API 路由与前端静态资源

### 2.2 消息处理主链

`app.py` 的群消息处理主路径：

1. 解析并标准化消息段（文本/图片/语音/视频）
2. 去重（短窗口重复消息抑制）
3. 触发前硬门禁（@、别名、回复、配置策略）
4. 构建 `EngineMessage`
5. 进入 `GroupQueueDispatcher`（会话并发、超时、取消策略）
6. 调用 `YukikoEngine.handle_message()`
7. 发送层按类型下发文本/图片/视频/语音，并处理限速与失败回退

### 2.3 引擎内部编排

`YukikoEngine.handle_message()` 当前主顺序：

1. Trigger（是否该响应）
2. Router（意图路由、动作决策）
3. Agent（LLM 循环 + 工具调用）
4. Safety（输出安全与策略约束）
5. 后处理（缓存、记忆、风格与长度控制）

---

## 3. 模块分层理解

## 3.1 接入与编排层

- `main.py`：启动、Setup 模式切换、WebUI 挂载
- `app.py`：OneBot 事件入口、媒体预处理、发送控制、队列对接
- `core/queue.py`：会话级队列、TTL、取消、过载提示
- `core/engine.py`：全局编排中枢

## 3.2 Agent 与工具层

- `core/agent.py`：多步推理循环、工具调用协议、高风险确认
- `core/agent_tools.py`：工具注册中心、权限分层、大量工具 handler
- `core/tools.py`：实际工具实现（下载、搜索、媒体、文件等）
- `core/enhanced_tools.py`：增强工具注册（好感度、卡片、增强生图）

## 3.3 认知与内容层

- `core/router.py`：意图判定（AI 主导 + 回退策略）
- `core/trigger.py`：触发判定、旁听策略、会话窗口
- `core/memory.py`：长期记忆/用户画像
- `core/knowledge.py`：独立知识库（SQLite + FTS5）
- `core/knowledge_updater.py`：聊天转知识更新（LLM-first）
- `core/safety.py`：风险控制与输出约束

## 3.4 业务扩展层

- `core/video_analyzer.py` + `core/video_resolver_hybrid.py`：视频解析与混合下载策略
- `core/music.py` + `core/music_sources.py`：点歌与跨平台音源匹配
- `core/qzone.py`：QQ 空间数据抓取
- `core/sticker.py`：表情相关能力
- `core/affinity.py`：好感度/心情/打卡系统
- `core/image_gen.py`：增强生图引擎（含 NSFW 关键词拦截）

## 3.5 服务适配层

`services/model_client.py` 已支持多提供方统一接入与降级链，包括：

- `skiapi`、`openai`、`anthropic`、`gemini`、`deepseek`
- `newapi`、`openrouter`、`xai`、`qwen`
- `moonshot`、`mistral`、`zhipu`、`siliconflow`

## 3.6 插件与运维层

- `plugins/connect_cli.py`：Agent 内部调用外部 CLI（Windows）
- `plugins/wayback_plugin.py`：Wayback 历史快照抓取工具
- `core/webui.py` + `webui/src/*`：配置、日志、数据库、插件、Cookie、初始化向导

---

## 4. 本轮大改动后的关键变化

1. 架构已从“关键词规则优先”转向“LLM 主导路由 + 策略兜底”。
2. 工具能力进一步扩展，覆盖管理、搜索、媒体、下载、知识学习、卡片消息等。
3. 引入好感度/心情系统与增强生图能力，强化交互风格可塑性。
4. 模型供应商扩展到多家兼容通道，并具备 provider failover。
5. WebUI 覆盖范围扩大，已能完成启动期配置与运行期维护。

---

## 5. 数据目录与仓库卫生策略

`storage/` 下大部分目录属于运行时数据，不应进入 Git：

- 日志：`storage/logs/`
- 下载与临时文件：`storage/tmp/`
- 缓存：`storage/cache/`
- 记忆与知识数据库：`storage/memory/`、`storage/knowledge/*.db*`
- 运行态画像/素材：`storage/affinity/`、`storage/emoji/`、`storage/sticker/`
- 本地密钥与白名单状态：`storage/.secret_key`、`storage/whitelist_groups.json`

同理，以下也应排除：

- 本地环境变量：`.env*`（保留 `.env.example`）
- 本地配置：`config/config.yml`
- 前端构建缓存：`webui/node_modules/`、`webui/dist/`
- IDE 与临时文件：`.idea/`、`.vscode/`、`*.log`、`__pycache__/`

本次已按上述策略更新 `.gitignore`。

---

## 6. 主要风险与技术债

## 6.1 高耦合超大文件（高）

`tools.py`、`agent_tools.py`、`engine.py` 体量极大，任何跨模块修改都容易引入回归。

## 6.2 自动化测试薄弱（高）

目前主要依赖运行日志观察，缺少稳定的最小回归集（触发、路由、工具权限、发送链路）。

## 6.3 运行数据污染提交风险（中高）

在大改动阶段，若 `.gitignore` 不完整，极易把下载包、数据库、日志、个人状态文件带入仓库。

## 6.4 平台耦合风险（中）

部分功能依赖 Windows/本地二进制（如外部 CLI、ffmpeg/ffprobe 路径），跨平台部署需额外校验。

---

## 7. 建议执行顺序（务实版）

## P0（立即）

1. 固化提交前检查：敏感信息扫描 + 缓存目录检查 + 大文件检查。
2. 建立最小 smoke tests：`trigger/router/agent parse/tool permission` 四类。
3. 将下载与媒体相关高风险策略（来源可信、文件类型校验）配置化并集中。

## P1（近期）

1. 拆分超大模块：优先 `tools.py` 与 `engine.py`。
2. 统一策略层：合并分散在 trigger/router/engine 的重复判定。
3. 在 WebUI 中补充关键健康指标（超时率、工具失败率、队列取消率）。

## P2（中期）

1. 建立基于真实日志的场景回放测试集。
2. 对插件与 provider 接口做契约测试，减少适配器漂移。

---

## 8. 本次结论

项目已经完成大规模能力升级，当前重点不再是“功能补齐”，而是“工程收敛”：

- 保证仓库提交只包含源码与必要文档
- 避免运行态数据、缓存、密钥污染
- 用最小自动化测试守住迭代稳定性

只要持续执行这三点，后续大改会更可控。
