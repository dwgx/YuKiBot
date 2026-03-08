# YuKiKo 交接记忆（Memory Handover）

> 目标：让新接手同学在 30 分钟内理解系统、在 60 分钟内可独立排障与发布配置。
> 最后更新：2026-03-05

## 1. 项目定位

YuKiKo 是基于 `NoneBot2 + OneBot V11(NapCat)` 的 QQ 群聊 Agent 系统，核心链路：

`QQ消息 -> app.py -> queue -> engine -> router/agent -> tools -> send_final`

关键特性：
- 多模型路由（默认 `SkiAPI`，支持 `newapi/openai_compatible` 一类网关）
- 工具化 Agent（搜索、下载、视频解析、音乐、群管理）
- 记忆/知识库（SQLite + 日志快照）
- WebUI 配置和运行态管理

## 2. 关键入口与目录

- 进程入口：`main.py`
- 事件与发送主流程：`app.py`
- 统一编排：`core/engine.py`
- Agent 循环：`core/agent.py`
- 工具注册：`core/agent_tools.py`
- 工具实现：`core/tools.py`
- 队列并发：`core/queue.py`
- WebUI API：`core/webui.py`
- 提供商适配：`services/`
- 配置模板：`config/templates/master.template.yml`
- 运行数据：`storage/`

## 3. 配置事实源（非常重要）

当前仓库默认只有模板文件：`config/templates/master.template.yml`。  
首次运行时会生成 `config/config.yml`（通过 WebUI setup 或 CLI setup）。

环境变量来自：
- `.env`
- `.env.prod`
- 参考模板：`.env.example`

必须确认的变量：
- `ONEBOT_ACCESS_TOKEN`（必须与 NapCat 侧一致）
- `WEBUI_TOKEN`（WebUI API 鉴权）
- 模型密钥（`SKIAPI_KEY` / `NEWAPI_API_KEY` / `OPENAI_API_KEY` 等）
- `ONEBOT_API_TIMEOUT`（大视频发送时通常需要 >=120）

## 4. 当前默认行为（模板基线）

以下默认值来自 `master.template.yml`：

- `api.provider: skiapi`
- `api.timeout_seconds: 120`
- `agent.max_steps: 8`
- `agent.tool_timeout_seconds: 28`
- `agent.tool_timeout_seconds_media: 45`
- `search.video_resolver.require_audio_for_send: true`
- `queue.group_concurrency: 3`
- `queue.cancel_previous_on_new: false`
- `queue.group_isolate_by_user: true`

补充：`core/queue.py` 内部默认（当配置缺失时）是 `single_inflight_per_conversation=true`、`cancel_previous_on_new=true`，但模板已显式覆盖为更高并发策略。

## 5. 媒体能力现状（接手人常问）

视频相关不是只有 `parse_video`：
- `parse_video`：解析链接并可发送视频
- `analyze_video`：偏分析
- `split_video`：可做切片/导音频/封面/关键帧

`split_video` 支持模式：
- `mode=clip`：输出视频片段
- `mode=audio`：导出音频（mp3/wav）
- `mode=cover`：提取封面图
- `mode=frames`：提取关键帧组图

这意味着“分割视频、出音频、封面画面”能力已在工具层存在，可直接通过 Agent 调用。

## 6. 运行态观察重点日志

先看这些 logger 前缀：
- `yukiko.queue`：排队、取消、超时
- `yukiko.agent`：工具调用链、步骤耗时
- `yukiko.app`：发送成功/失败、视频兼容处理
- `yukiko.tools` / `yukiko.ytdlp`：下载与解析细节
- `nonebot` / `uvicorn`：连接状态

健康启动最小信号：
- `Running NoneBot...`
- `Bot <id> connected`
- `queue_init ...`
- `cli_provider_health_all_ok`（如启用 connect_cli）

## 7. 常见故障签名与定位

1. `401 Unauthorized / 未提供令牌`
- 先查 provider 的 token 是否传到实际请求头。
- 检查 base_url 与 wire_api 是否匹配（responses/chat-completions）。

2. `stream disconnected before response.completed`
- 多见于上游网关流式连接中断。
- 先降级：非流式、降低 reasoning effort、缩短输出。
- 再查：代理层超时、反代连接复用、网关稳定性。

3. `queue_final ... cancelled | reason=process_timeout`
- 工具链过长或单步太慢。
- 调整 `agent` 与 `queue` 超时参数，并控制多跳搜索次数。

4. 视频发出但无声 / 被 QQ 拒收
- 已有 `require_audio_for_send: true` 基线。
- 重点查 `video_qq_compat_check` / `audio_missing` / ffmpeg 转码日志。

5. `Not Found`（下载到 NapCat 上传阶段）
- 常见于直链失效或临时 URL 过期。
- 先落本地文件再上传，避免直接透传不稳定 URL。

## 8. 接手首日清单

1. 启动一次完整链路（bot + napcat + webui）。
2. 做 5 条回归：
- 普通文本回复
- 联网搜索
- 视频解析并发送
- `split_video mode=audio`
- 点歌（music_fast_path）
3. 验证日志留存与告警关键词。
4. 备份当前 `config/config.yml` 与 `storage/knowledge/knowledge.db`。
5. 在 `docs/OPERATIONS_RUNBOOK.md` 记录当日改动与回滚点。

## 9. 交接边界

本文件解决“知道系统是什么、哪里改、哪里看、哪里容易炸”。  
具体操作步骤与事故处理请配套查看：`docs/OPERATIONS_RUNBOOK.md`。

