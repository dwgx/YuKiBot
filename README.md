<p align="center">
  <img src="webui/public/logo.svg" width="120" alt="YuKiKo Logo" />
</p>

<h1 align="center">YuKiKo Bot（雪子）</h1>

<p align="center">
  基于 NoneBot2 + NapCat 的 QQ 智能助手 · 多模型 Agent · 长期记忆 · 联网搜索 · 视频解析 · WebUI 管理
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python" />
  <img src="https://img.shields.io/badge/NoneBot2-OneBot_V11-green?logo=data:image/svg+xml;base64," />
  <img src="https://img.shields.io/badge/React-18.3-61DAFB?logo=react" />
  <img src="https://img.shields.io/badge/License-Private-red" />
</p>

---

## 概述

YuKiKo 是一个功能完整的 QQ 群聊智能体，不只是聊天机器人——它是一个具备多步推理、工具调用、长期记忆和知识管理能力的 AI Agent 平台。

核心引擎链：

```
消息 → Trigger(触发判定) → Router(意图路由) → Agent(多步推理+工具调用) → Safety(安全过滤) → Response
```

**42,000+ 行 Python** 后端 + **React TypeScript** WebUI，覆盖从消息接入到智能回复的完整链路。

---

## 核心能力

| 能力 | 说明 |
|------|------|
| **Agent 多步推理** | LLM 驱动的 think → act → observe 循环，127+ 可调用工具，智能意图过滤 |
| **多模型热切换** | SkiAPI / OpenAI / Anthropic / DeepSeek / Gemini，运行时无缝切换 |
| **联网搜索** | SearXNG / DuckDuckGo / Bing 多引擎聚合，知乎/百科/GitHub 专项搜索 |
| **ScrapyLLM 智能抓取** | 网页抓取 + LLM 结构化提取 / 摘要 / 链接跟踪 |
| **视频解析** | B站 / 抖音 / 快手 / AcFun 富元数据 + 关键帧 Vision 分析 |
| **长期记忆** | SQLite 向量检索 + 用户画像 + 每日快照 + 知识库 |
| **三级权限模型** | 超级管理员 > 群管理员 > 普通用户，精细化工具权限控制 |
| **插件系统** | 热加载插件，支持 Agent 工具注册 + Prompt 注入 + 动态上下文 |
| **WebUI 管理** | React 仪表盘：配置编辑 / 实时日志 / 数据库浏览 / Prompt 管理 |
| **消息队列** | 群级并发控制 / 顺序发送 / TTL / 过载保护 |

---

## 架构

```
NapCat (QQ协议)
  │ WebSocket
  ▼
NoneBot2 Adapter ──→ app.py (事件解析 · 去重 · 多模态预处理)
  │
  ▼
GroupQueueDispatcher (并发控制 · TTL · 过载保护)
  │
  ▼
YukikoEngine.handle_message()
  ├── TriggerEngine ─── 是否响应？(@/昵称/关键词/活跃度/插件命令)
  ├── RouterEngine ──── 什么意图？(reply/search/generate/analyze/music/...)
  ├── AgentLoop ─────── 多步推理 + 工具调用
  │   ├── _build_system_prompt() ← 智能工具过滤(按意图选工具子集)
  │   ├── LLM 决策 → {"tool": "web_search", "args": {...}}
  │   ├── AgentToolRegistry.call() ← 权限检查 + 参数校验 + 执行
  │   ├── 结果反馈 → LLM 继续推理
  │   └── final_answer → 用户回复
  ├── SafetyEngine ──── 内容风险分级 + 违规冷却
  └── Response 格式化 → 分段发送 + 速率控制
  │
  ▼
NapCat → QQ 群聊/私聊
```

---

## 模块一览

### 后端核心 (`core/`)

| 模块 | 行数 | 职责 |
|------|------|------|
| `engine.py` | 5,500+ | 主编排器，串联 Trigger → Router → Agent → Safety → Response |
| `agent.py` | 2,200+ | Agent 循环引擎：多步推理 + 工具调用 + 智能工具过滤 |
| `agent_tools.py` | 6,300+ | 工具注册表：127+ 工具 schema + handler + 三级权限 |
| `tools.py` | 7,800+ | 工具实现层：搜索/视频/图片/音乐/下载 |
| `router.py` | 870+ | AI 意图分类，输出 action + confidence + tool_args |
| `search.py` | 1,200+ | 多引擎搜索聚合 (SearXNG/DuckDuckGo/Bing/Baidu) |
| `memory.py` | 1,000+ | SQLite 向量记忆 + 用户画像 + 每日快照 |
| `knowledge.py` | 520+ | 知识库 (SQLite FTS5)：事实/热梗/百科/热搜/学习 |
| `video_analyzer.py` | 1,050+ | 多平台视频解析 + 关键帧 Vision 分析 |
| `crawlers.py` | 630+ | 专项爬虫：知乎/百科/微博/抖音/B站 |
| `queue.py` | 400+ | 消息队列调度：并发控制 + 顺序发送 + TTL |
| `trigger.py` | 630+ | 多策略触发判定 |
| `music.py` | 770+ | 网易云音乐搜索 + SILK 编码 |
| `sticker.py` | 1,050+ | 表情包系统：QQ表情 + 自定义 + 群内学习 |
| `qzone.py` | 500+ | QQ空间数据抓取 |
| `admin.py` | 1,020+ | 管理员命令系统 + 白名单管理 |
| `safety.py` | — | 内容风险四级分类 + 违规冷却 |
| `emotion.py` | — | 情绪识别与共情回复 |
| `personality.py` | — | 场景化人格指令 + 风格参数 |
| `webui.py` | 1,540+ | WebUI API 后端 (FastAPI) |
| `config_manager.py` | 560+ | 配置热重载 + 环境变量替换 + 加密存储 |

### 服务层 (`services/`)

| 模块 | 职责 |
|------|------|
| `model_client.py` | 统一模型客户端路由，自动 failover |
| `skiapi.py` | SkiAPI 聚合代理客户端 |
| `openai.py` | OpenAI 官方 API 客户端 |
| `anthropic.py` | Anthropic Claude 客户端 |
| `deepseek.py` | DeepSeek 客户端 |
| `gemini.py` | Google Gemini 客户端 |
| `openai_compatible.py` | OpenAI 兼容接口适配器 |

### 工具库 (`utils/`)

| 模块 | 职责 |
|------|------|
| `scrapy_llm.py` | ScrapyLLM：智能网页抓取 + LLM 结构化提取 |
| `media.py` | FFmpeg/Whisper 媒体处理基础设施 |
| `text.py` | 文本处理：分词/截断/规范化 |
| `filter.py` | 内容过滤 |
| `intent.py` | 意图检测辅助 |

### 前端 (`webui/`)

| 页面 | 功能 |
|------|------|
| Dashboard | 实时状态：在线时长 / 消息统计 / 活跃用户 / 队列深度 |
| Config | YAML 配置编辑器，分 Tab 管理各配置段 |
| Logs | 实时日志流 (WebSocket) |
| Database | 记忆/知识库/用户画像浏览器 |
| Prompts | 系统提示词管理 |
| Setup | 首次配置向导 |

---

## Agent 工具系统

YuKiKo 的 Agent 拥有 **127+ 可调用工具**，按语义分为 13 个组：

| 工具组 | 数量 | 示例工具 |
|--------|------|----------|
| **core** | 2 | `final_answer`, `think` |
| **messaging** | 9 | `send_group_message`, `send_forward_msg` |
| **group_query** | 16 | `get_group_info`, `get_group_member_list`, `get_essence_msg_list` |
| **group_manage** | 18 | `set_group_ban`, `set_group_kick`, `delete_message` |
| **social** | 17 | `group_poke`, `send_like`, `set_msg_emoji_like` |
| **search** | 12 | `web_search`, `scrape_extract`, `github_search`, `lookup_wiki` |
| **knowledge** | 2 | `search_knowledge`, `learn_knowledge` |
| **media** | 15 | `analyze_image`, `analyze_video`, `ocr_image`, `generate_image` |
| **file** | 5 | `upload_group_file`, `smart_download` |
| **sticker** | 9 | `send_face`, `learn_sticker`, `browse_sticker_categories` |
| **qzone** | 5 | `get_qzone_profile`, `get_qzone_moods`, `analyze_qzone` |
| **utility** | 14 | `translate_en2zh`, `music_play`, `get_mini_app_ark` |
| **admin** | 3 | `config_update`, `admin_command`, `cli_invoke` |

### 智能工具过滤

不是所有 127 个工具都塞进 system prompt——Agent 会根据用户消息意图，只展示相关工具子集：

```
用户: "帮我搜一下 Python 教程"  → 展示 search + knowledge 组 (~14 工具)
用户: "踢掉那个人"              → 展示 group_manage + group_query 组 (~34 工具)
用户: "看看这张图片是什么"       → 展示 media + search 组 (~27 工具)
用户: "今天天气怎么样"           → 无明确意图，展示全量工具 (兜底)
```

每次请求节省 3,000-5,000 tokens，减少 LLM 选择困难。

### 三级权限模型

```
超级管理员 (super_admin)
  └── 凌驾一切规则，可执行任何操作
群管理员 (group_admin)
  └── 加白群的群主/管理员，可执行群管理操作
普通用户 (user)
  └── 基础工具，不能执行管理操作
```

权限在 AgentLoop 和 AgentToolRegistry 双层校验，defense in depth。

### ScrapyLLM 智能抓取

Agent 内置的 LLM 增强网页抓取能力：

| 工具 | 功能 |
|------|------|
| `scrape_extract` | 抓取网页 + 按指令提取关键信息 |
| `scrape_summarize` | 抓取网页 + AI 智能摘要 |
| `scrape_structured` | 抓取网页 + 按 schema 提取 JSON |
| `scrape_follow_links` | 抓取 → AI 选链接 → 跟进提取 |

---

## 多模型与联网扩展支持

### 模型提供商

| 提供商 | 模型 | 说明 |
|--------|------|------|
| **SkiAPI** | 多模型聚合 | 默认代理，支持运行时切换 |
| **OpenAI** | GPT-4o / GPT-4o-mini | 官方 API |
| **Anthropic** | Claude Sonnet / Opus | 官方 API |
| **DeepSeek** | DeepSeek-V3 / Chat | 官方 API |
| **Gemini** | Gemini 2.0 Flash / Pro | 官方 API |
| **OpenAI Compatible（扩展）** | 任意兼容模型 | 通过自定义 `base_url` 接入第三方兼容网关/自建服务 |

### 联网能力扩展

| 能力 | 扩展方式 | 当前支持 |
|------|----------|----------|
| **搜索引擎** | 在 `search` 段切换或组合引擎 | SearXNG / DuckDuckGo / Bing / Baidu |
| **网页抓取** | 使用 ScrapyLLM 工具链 | `scrape_extract` / `scrape_summarize` / `scrape_structured` / `scrape_follow_links` |
| **视频解析** | 平台解析 + 直链回退 | B站 / 抖音 / 快手 / AcFun / 直链视频 |
| **专项搜索** | 工具级扩展 | 知乎 / 百科 / GitHub / 热搜聚合 |

通过 `config/config.yml` 的 `api` 段可做主模型、降级链和分提供商配置：

```yaml
api:
  provider: skiapi
  model: claude-sonnet-4-5-20250929
  fallback_providers:
    - openai
    - deepseek
  providers:
    openai:
      api_key: ${OPENAI_API_KEY}
      model: gpt-4o-mini
      base_url: https://api.openai.com
    deepseek:
      api_key: ${DEEPSEEK_API_KEY}
      model: deepseek-chat
      base_url: https://api.deepseek.com
```

如果要接入 OpenAI 兼容第三方网关，通常只需把对应 provider 的 `base_url` 和 `api_key` 换成你的网关参数即可。

---

## 快速启动

### 环境要求

- Python 3.11+
- ffmpeg（系统 PATH 中可用）
- NapCat（已登录 QQ 账号）
- Node.js 18+（仅 WebUI 开发需要）

### 安装

```bash
cd yukiko-bot
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

### 配置

1. 编辑 `.env`，填写必要密钥：

```env
ONEBOT_ACCESS_TOKEN=你的NapCat_token
SKIAPI_KEY=你的skiapi密钥
# 按需填写
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
GEMINI_API_KEY=
WEBUI_TOKEN=你的WebUI登录token
```

2. 编辑 `config/config.yml` 核心配置段：

| 配置段 | 说明 |
|--------|------|
| `bot` | 名称、昵称、语言、功能开关 |
| `api` | 模型提供方、地址、模型名、超时 |
| `agent` | Agent 开关、最大步数、高风险控制 |
| `memory` | 长期记忆、向量检索、隐私过滤 |
| `trigger` | 触发策略、活跃阈值、主动接话概率 |
| `search` | 搜索引擎配置、SearXNG 地址 |
| `queue` | 并发数、顺序发送、TTL |
| `safety` | 风险分级、违规冷却 |
| `admin` | 超级管理员QQ、白名单群 |

### NapCat 对接

1. 启动 NapCat 并完成 QQ 登录
2. OneBot11 网络配置中启用反向 WebSocket
3. 反向地址：`ws://127.0.0.1:8081/onebot/v11/ws`
4. token 与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 保持一致

### 启动

```bash
python main.py
```

日志出现 `Bot xxx connected` 即链路成功。WebUI 访问 `http://127.0.0.1:8080/webui/`。

---

## 触发机制

机器人在以下情况处理消息：

1. 被 `@`
2. 提到昵称（雪、yukiko、yuki 等）
3. 命中触发关键词
4. 命中插件命令
5. 会话仍在活跃状态（跟进回复窗口内）
6. 群聊高活跃且满足主动接话策略
7. 命中敏感词或冲突词

---

## 插件开发

最小插件结构：

```python
class Plugin:
    name = "demo"
    description = "示例插件"
    intent_examples = ["demo", "示例"]
    rules = ["返回示例文本"]
    args_schema = {"message": "string"}

    async def setup(self, config, context):
        # 可选: 注册 Agent 工具
        context.agent_tool_registry.register(tool_schema, handler)
        # 可选: 注入 Prompt
        context.agent_tool_registry.register_prompt_hint(hint)

    async def handle(self, message: str, context: dict) -> str:
        return "示例返回"
```

放入 `plugins/` 目录即可自动加载。配置优先级：`config/plugins.yml` > `plugins/config/<name>.yml` > `config.yml`。

---

## 目录结构

```
yukiko-bot/
├── main.py                     # 启动入口
├── app.py                      # NoneBot 事件接入 + 消息路由 (2,650 行)
├── requirements.txt            # Python 依赖
├── .env                        # 环境变量（密钥）
│
├── config/
│   ├── config.yml              # 主配置（热重载）
│   ├── prompts.yml             # 可编辑提示词
│   └── templates/
│       └── master.template.yml # 配置模板 + Agent Prompt 模板
│
├── core/                       # 核心引擎 (38,000+ 行)
│   ├── engine.py               # 主编排器
│   ├── agent.py                # Agent 循环引擎
│   ├── agent_tools.py          # 工具注册表 (127+ 工具)
│   ├── tools.py                # 工具实现层
│   ├── router.py               # AI 意图路由
│   ├── trigger.py              # 触发判定
│   ├── memory.py               # 向量记忆 + 用户画像
│   ├── knowledge.py            # 知识库 (FTS5)
│   ├── search.py               # 多引擎搜索
│   ├── video_analyzer.py       # 视频解析
│   ├── crawlers.py             # 专项爬虫
│   ├── sticker.py              # 表情包系统
│   ├── qzone.py                # QQ空间
│   ├── music.py                # 音乐引擎
│   ├── queue.py                # 消息队列
│   ├── safety.py               # 安全过滤
│   ├── emotion.py              # 情绪引擎
│   ├── personality.py          # 人格系统
│   ├── admin.py                # 管理员系统
│   ├── webui.py                # WebUI API
│   ├── config_manager.py       # 配置管理
│   ├── prompt_loader.py        # Prompt 加载器
│   ├── prompt_policy.py        # Prompt 策略
│   └── setup.py                # 首次配置向导
│
├── services/                   # 模型提供商适配
│   ├── model_client.py         # 统一客户端路由
│   ├── skiapi.py / openai.py / anthropic.py / deepseek.py / gemini.py
│   └── openai_compatible.py    # 兼容接口
│
├── utils/                      # 工具库
│   ├── scrapy_llm.py           # ScrapyLLM 智能抓取
│   ├── media.py                # FFmpeg/Whisper 媒体处理
│   ├── text.py                 # 文本处理
│   ├── filter.py               # 内容过滤
│   └── intent.py               # 意图检测
│
├── plugins/                    # 插件目录（自动加载）
│   ├── example_plugin.py       # 示例插件
│   └── connect_cli.py          # CLI 连接工具
│
├── webui/                      # React 管理界面
│   ├── src/
│   │   ├── pages/              # Dashboard / Config / Logs / Database / Prompts
│   │   ├── components/         # UI 组件
│   │   ├── api/client.ts       # API 客户端
│   │   └── App.tsx             # 根组件
│   ├── package.json            # React 18 + Vite 6 + HeroUI + Tailwind
│   └── dist/                   # 构建产物
│
├── storage/                    # 运行时数据
│   ├── logs/                   # 应用日志
│   ├── memory/                 # 向量记忆数据库
│   ├── knowledge/              # 知识库
│   ├── cache/                  # 媒体缓存
│   ├── emoji/                  # 表情包存储
│   └── tmp/                    # 临时文件
│
├── scripts/                    # 工具脚本
└── docs/                       # 文档
    ├── MEMORY_HANDOVER.md      # 交接记忆（系统上下文）
    ├── OPERATIONS_RUNBOOK.md   # 运维操作手册（启动/排障/回滚）
    ├── AGENT_MASTER_PLAN.md    # Agent 强化路线图
    ├── PROJECT_ANALYSIS.md     # 架构分析
    └── PROJECT_AUDIT.md        # 代码审计报告
```

---

## 技术栈

### 后端

| 技术 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 主语言 |
| NoneBot2 | latest | 异步 Bot 框架 |
| OneBot V11 | NapCat | QQ 协议适配 |
| FastAPI | (内置) | WebUI API |
| SQLite | WAL mode | 记忆 + 知识库 |
| httpx | 0.27+ | 异步 HTTP |
| yt-dlp | 2025.2+ | 视频下载 |
| bilibili-api-python | 17+ | B站 API |
| f2 | 0.0.1+ | 抖音解析 |
| pilk | 0.2+ | SILK 音频编码 |
| cryptography | 46+ | 配置加密 |

### 前端

| 技术 | 版本 | 用途 |
|------|------|------|
| React | 18.3 | UI 框架 |
| TypeScript | 5.7 | 类型安全 |
| Vite | 6.0 | 构建工具 |
| Tailwind CSS | 3.4 | 样式 |
| HeroUI | 2.7 | 组件库 |
| CodeMirror | latest | YAML 编辑器 |
| Framer Motion | latest | 动画 |

---

## 常见问题

**没有回复** — 检查触发条件（@/昵称/关键词），查看日志是否收到 `message.group.normal` 事件。

**一直走降级回复** — 检查 `.env` 中 `SKIAPI_KEY` 是否填写，`config.yml` 中 `api.base_url` 是否正确。

**反向 WebSocket 连接拒绝** — 先启动 `python main.py`，再启动 NapCat。连接失败会自动重连。

**token 鉴权失败** — 确认 NapCat 与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 完全一致。

**视频发送超时** — 大视频文件上传需要时间，`.env` 中 `ONEBOT_API_TIMEOUT` 默认 120 秒，可适当增大。

**WebUI 打不开** — 确认 `WEBUI_TOKEN` 已设置，访问 `http://127.0.0.1:8080/webui/`。

---

## 隐私说明

默认配置为长期记忆全量记录（`memory.privacy_filter: false`）。如需降低隐私风险，可改为 `true`，系统会自动脱敏联系方式与密钥片段。

---

## 参考文档

- [交接记忆（Memory Handover）](docs/MEMORY_HANDOVER.md)
- [运维操作手册（Operations Runbook）](docs/OPERATIONS_RUNBOOK.md)
- [NapCat 接口文档 (Apifox)](https://napcat.apifox.cn/)
- [NapCat OneBot 消息段说明](https://www.napcat.wiki/onebot/sement)
- [NoneBot2 文档](https://nonebot.dev/)
- [OneBot V11 协议](https://11.onebot.dev/)
