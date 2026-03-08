# YuKiKo 运维操作手册（Operations Runbook）

> 适用场景：日常值班、故障排查、版本切换、交接接班。
> 最后更新：2026-03-05

## 1. 启动前检查

1. NapCat 已登录目标 QQ 账号，OneBot V11 已开启反向 WS。
2. Python 虚拟环境存在：`.venv\Scripts\python.exe`。
3. `ffmpeg` 与 `ffprobe` 在 PATH 可调用。
4. `.env` 已配置：
- `ONEBOT_ACCESS_TOKEN`
- `WEBUI_TOKEN`
- 模型密钥（至少一个）
5. `config/config.yml` 存在。
- 若不存在，先跑 setup（见第 2 节）。

## 2. 启动与停止

Windows 推荐：

```powershell
cd D:\Project\YuKiKo\yukiko-bot
.\start.bat
```

或手动：

```powershell
cd D:\Project\YuKiKo\yukiko-bot
.\.venv\Scripts\Activate.ps1
python main.py
```

首次初始化（无 `config/config.yml`）：

```powershell
python main.py --setup
```

停止：
- 终端 `Ctrl + C`
- 若由 IDE 启动，直接停止运行进程

## 3. 启动成功判据

日志中至少出现：
- `NoneBot is initializing`
- `Running NoneBot...`
- `Bot <id> connected`
- `queue_init ...`
- `Uvicorn running on http://127.0.0.1:8081`

WebUI 可访问：
- `http://127.0.0.1:8081/webui/`

## 4. 值班最小回归（每次重启后）

1. 文本回复：`@bot 在吗`
2. 联网搜索：`@bot 全网最推荐什么 API 聚合平台`
3. 视频解析：`@bot <视频链接> 解析`
4. 媒体处理：
- `@bot 把这个视频分割成30秒`
- `@bot 导出这个视频音频`
- `@bot 提取这个视频封面`
5. 点歌：`@bot 点歌 <歌名>`

## 5. 核心配置位（接手最常改）

来自 `config/config.yml`：

- `api.provider/model/base_url/api_key`
- `agent.max_steps`
- `agent.tool_timeout_seconds`
- `agent.tool_timeout_seconds_media`
- `queue.group_concurrency`
- `queue.cancel_previous_on_new`
- `queue.group_isolate_by_user`
- `search.video_resolver.require_audio_for_send`

建议：
- 视频任务多时优先提高 `agent.tool_timeout_seconds_media` 与 queue 的视频超时配置。
- 不要直接把 `cancel_previous_on_new` 改为 `true`，除非你确认要“新消息打断旧任务”。

## 6. 故障处理手册（按症状）

### A. 401 Unauthorized / 未提供令牌

检查顺序：
1. `.env` 或 provider 配置是否为空
2. `base_url` 是否正确
3. provider 协议是否匹配（responses / chat-completions）
4. 网关是否需要额外 header

### B. stream disconnected before response.completed

处理顺序：
1. 切非流式
2. 降低模型负载（缩短输出、降低推理强度）
3. 检查网关超时和代理链路（反代、CDN、连接复用）

### C. `queue_final ... process_timeout`

处理顺序：
1. 看 `yukiko.agent` 的工具步骤是否过长
2. 减少一次请求中的多跳工具调用
3. 增大 `agent.tool_timeout_seconds_media`
4. 必要时增大 queue `process_timeout_seconds`

### D. 视频发送无声 / 失败

处理顺序：
1. 确认 `search.video_resolver.require_audio_for_send: true`
2. 查日志：`video_qq_compat_check`、`audio_missing`
3. 检查 ffmpeg 是否可用
4. 用 `split_video mode=audio` 验证源视频是否本身有音轨

### E. 下载失败 `Not Found`

处理顺序：
1. 直链是否过期
2. 先下载到本地，再上传
3. 对需 cookie 的站点，先修 cookie 再下载

## 7. 事故分级与动作

P0（完全不可用）：
- bot 无法连接 QQ / 无法回复
- 立即回滚到上一版可用配置
- 先保可用性，再定位根因

P1（核心能力退化）：
- 搜索/视频/下载大面积失败
- 限流降级（只保文本+搜索）
- 修复后分批恢复媒体能力

P2（体验问题）：
- 偶发超时、个别工具不稳定
- 不停机修复，发布小版本

## 8. 变更发布与回滚

发布前：
1. 备份 `config/config.yml`
2. 备份 `storage/knowledge/knowledge.db`
3. 执行第 4 节回归

发布后：
1. 观察 15 分钟关键日志
2. 关注 `queue_cancelled` / `agent_timeout` / `send_error`

回滚优先级：
1. 回滚配置
2. 回滚最近代码提交
3. 清理异常缓存（`storage/cache/videos`）

## 9. 交接验收标准

满足以下 6 条即可判定“可接手”：
1. 能独立启动与停止
2. 能完成最小回归 5 项
3. 能在 10 分钟内定位 401/超时/无声视频三类问题
4. 知道配置事实源和备份位置
5. 能执行一次无损回滚
6. 在交接记录中写清当前 provider、模型、超时策略

## 10. 交接记录模板

建议每次交接追加以下内容到内部记录：

```text
交接时间：
交接人：
接手人：

当前 provider/model：
关键超时参数：
队列参数：
媒体策略（是否要求音轨）：

当日变更：
已知风险：
回滚点：
```
