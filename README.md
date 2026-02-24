# YuKiKo Bot（雪子）

基于 **NoneBot2 + NapCat (OneBot V11)** 的 QQ 智能助手机器人。

核心引擎链：`Trigger → Router → Thinking(LLM) → Tools → Response`

支持多模型切换、视频解析、联网搜索、图片生成、长期记忆、语音消息、插件系统。

---

## 架构总览

```
消息进入
  │
  ├─ Trigger（触发判定）
  │    @/昵称/关键词/活跃度/会话延续/插件命令
  │
  ├─ Router（AI 意图路由）
  │    LLM 分析 → action + confidence + tool_args
  │    支持: reply / search / generate_image / analyze_video / music / moderate / ignore
  │
  ├─ Tools（工具执行）
  │    搜索 / 视频解析 / 图片生成 / 音乐 / B站搜索 / 网页抓取
  │
  ├─ Thinking（回复生成）
  │    多模型 LLM → 人格化回复 → 场景适配 → 长度控制
  │
  └─ Queue（消息队列）
       并发控制 / 顺序发送 / TTL / 过载保护
```

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 引擎 | `core/engine.py` | 主编排器，串联所有模块 |
| 路由 | `core/router.py` | AI 意图分类，决定 action/tool/置信度 |
| 思考 | `core/thinking.py` | LLM 回复生成，场景/风格/长度自适应 |
| 工具 | `core/tools.py` | 搜索、视频、图片、音乐等工具执行 |
| 搜索 | `core/search.py` | SearXNG / DuckDuckGo / Bing 多引擎搜索 |
| 视频 | `core/video_analyzer.py` | B站/抖音/快手/AcFun 视频解析 + 关键帧分析 |
| 记忆 | `core/memory.py` | SQLite 向量检索 + 用户画像 + 日志快照 |
| 触发 | `core/trigger.py` | 多策略触发判定（@/昵称/关键词/活跃度） |
| 人格 | `core/personality.py` | 场景化人格指令 + 风格参数 |
| 提示词 | `core/system_prompts.py` | Router/Thinking/Vision 系统提示词 |
| 队列 | `core/queue.py` | 消息队列调度，并发+顺序+过载保护 |
| 安全 | `core/safety.py` | 内容风险分级 + 违规冷却 |
| 情绪 | `core/emotion.py` | 情绪识别与共情回复 |
| 管理 | `core/admin.py` | 管理员命令系统 |
| Cookie | `core/cookie_auth.py` | 多策略 Cookie 提取 + 自动刷新 |
| 音乐 | `core/music.py` | 网易云音乐搜索/播放（骨架） |
| 配置 | `core/config_manager.py` | 配置热重载 |
| 加密 | `core/crypto.py` | 敏感配置加密存储 |
| 引导 | `core/setup.py` | 首次启动交互式配置向导 |

## 多模型支持

| 提供商 | 服务 | 说明 |
|--------|------|------|
| SkiAPI | `skiapi.dev` | 聚合代理，支持多模型切换（默认） |
| OpenAI | 官方 API | GPT-4o / GPT-4o-mini |
| Anthropic | 官方 API | Claude 3.5 Sonnet / Opus |
| DeepSeek | 官方 API | DeepSeek-V3 / Chat |
| Gemini | 官方 API | Gemini 2.0 Flash / Pro |

通过 `config/config.yml` 的 `api` 段配置，支持运行时热切换。

## 功能清单

### 搜索
- SearXNG 元搜索引擎（聚合 Google/Bing/DuckDuckGo，需自部署）
- DuckDuckGo Instant API + HTML 抓取
- Bing 图片搜索
- B站视频搜索 API
- 网页正文抓取（Readability 提取）

### 视频解析
- **B站**：bilibili-api-python 富元数据（弹幕热词、热评、标签、分P）
- **抖音**：f2 库解析（aweme_id 提取、签名算法）
- **快手**：GraphQL API 自实现
- **AcFun**：yt-dlp 基础元数据
- **通用**：yt-dlp 下载 + ffmpeg 压缩 + 关键帧提取 + Vision API 多模态分析

### 图片
- AI 图片生成（通过 LLM 提供商）
- Bing 图片搜索

### 音乐（骨架）
- 网易云音乐搜索 API
- yt-dlp 音频提取
- SILK 编码（pilk）→ QQ 语音消息

### 记忆系统
- SQLite 向量检索（语义相似度匹配）
- 用户画像自动生成（兴趣、习惯、活跃时段）
- 每日对话快照
- 隐私过滤（可选脱敏）

### 消息队列
- 同群并发处理 + 严格顺序发送
- TTL 过期丢弃
- 积压过载提示
- 视频大文件超时宽限

### 安全与管理
- 内容风险四级分类（safe / low_risk / high_risk / illegal）
- 违规冷却机制
- 管理员白名单/黑名单
- 敏感词过滤

## 快速启动

### 环境要求
- Python 3.11+
- ffmpeg（系统 PATH 中可用）
- NapCat（已登录 QQ 账号）

### 安装

```bash
cd d:\Project\YuKiKo\yukiko-bot
python -m venv .venv

# Windows PowerShell
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
# 或使用官方密钥（按需）
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
GEMINI_API_KEY=
```

2. 编辑 `config/config.yml`，核心配置段：

| 配置段 | 说明 |
|--------|------|
| `bot` | 名称、昵称、语言、功能开关 |
| `api` | 模型提供方、地址、模型名、超时 |
| `memory` | 长期记忆、向量检索、隐私过滤 |
| `trigger` | 触发策略、活跃阈值、主动接话概率 |
| `search` | 搜索引擎配置、SearXNG 地址、返回条数 |
| `judge` | 全量判定开关、超时、最小置信度 |
| `queue` | 并发数、顺序发送、TTL、积压上限 |
| `safety` | 风险分级、违规冷却 |
| `image` | 生图开关与尺寸 |

### NapCat 对接

1. 启动 NapCat 并完成 QQ 登录
2. OneBot11 网络配置中启用反向 WebSocket
3. 反向地址：`ws://127.0.0.1:8080/onebot/v11/ws`
4. token 与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 保持一致
5. 启动机器人：

```bash
python main.py
```

日志出现 `Bot xxx connected` 即链路成功。

## 触发机制

机器人在以下情况处理消息：

1. 被 `@`
2. 提到昵称（雪、yukiko、yuki 等）
3. 命中触发关键词
4. 命中敏感词或冲突词
5. 命中插件命令或关键词
6. 会话仍在活跃状态
7. 群聊高活跃且满足主动接话策略

## 目录结构

```
yukiko-bot/
├── main.py                  # 启动入口
├── app.py                   # NoneBot 事件接入 + 消息路由 + 队列调度
├── config/
│   └── config.yml           # 主配置文件
├── core/
│   ├── engine.py            # 主编排引擎
│   ├── router.py            # AI 意图路由
│   ├── thinking.py          # LLM 回复生成
│   ├── tools.py             # 工具执行器（搜索/视频/图片/音乐）
│   ├── search.py            # 多引擎搜索
│   ├── video_analyzer.py    # 视频解析 + 关键帧分析
│   ├── memory.py            # 长期记忆 + 向量检索
│   ├── trigger.py           # 触发判定
│   ├── personality.py       # 人格系统
│   ├── system_prompts.py    # 系统提示词
│   ├── queue.py             # 消息队列
│   ├── safety.py            # 安全过滤
│   ├── emotion.py           # 情绪引擎
│   ├── admin.py             # 管理员系统
│   ├── cookie_auth.py       # Cookie 提取 + 自动刷新
│   ├── music.py             # 音乐引擎（骨架）
│   ├── config_manager.py    # 配置热重载
│   ├── crypto.py            # 加密存储
│   └── setup.py             # 首次配置向导
├── services/
│   ├── model_client.py      # 统一模型客户端接口
│   ├── skiapi.py            # SkiAPI 客户端
│   ├── openai.py            # OpenAI 客户端
│   ├── anthropic.py         # Anthropic 客户端
│   ├── deepseek.py          # DeepSeek 客户端
│   ├── gemini.py            # Gemini 客户端
│   ├── openai_compatible.py # OpenAI 兼容接口
│   ├── base_client.py       # 基础 HTTP 客户端
│   └── logger.py            # 日志服务
├── plugins/
│   └── example_plugin.py    # 示例插件
├── utils/
│   ├── text.py              # 文本处理工具
│   └── filter.py            # 内容过滤
├── scripts/
│   ├── cookie_selftest.py   # Cookie 提取自测
│   └── fix_encoding.py      # 编码修复工具
└── docs/                    # 文档
```

## 插件开发

最小插件结构：

```python
class Plugin:
    name = "demo"
    commands = ["/demo"]
    keywords = ["demo", "示例"]

    async def handle(self, message: str, context: dict) -> str:
        return "示例返回"
```

放入 `plugins/` 目录即可自动加载。

## 常见问题

**没有回复** — 检查触发条件（@/昵称/关键词），查看日志是否收到 `message.group.normal` 事件。

**一直走降级回复** — 检查 `.env` 中 `SKIAPI_KEY` 是否填写，`config.yml` 中 `api.base_url` 是否正确。

**反向 WebSocket 连接拒绝** — 先启动 `python main.py`，再启动 NapCat。连接失败会自动重连。

**token 鉴权失败** — 确认 NapCat 与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 完全一致。

**视频发送超时** — 大视频文件上传需要时间，`.env` 中 `ONEBOT_API_TIMEOUT` 默认 120 秒，可适当增大。

## 隐私说明

默认配置为长期记忆全量记录（`memory.privacy_filter=false`）。
如需降低隐私风险，可改为 `true`，系统会自动脱敏联系方式与密钥片段。

