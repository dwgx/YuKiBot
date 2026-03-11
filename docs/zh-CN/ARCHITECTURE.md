# YuKiKo Bot 原理说明（简体中文）

这份文档专门讲“为什么这样设计”，不是部署步骤。

## 1. 核心链路

消息主链路：

1. OneBot 事件进入 `app.py`
2. 组装 `EngineMessage` 后交给 `YukikoEngine.handle_message`
3. 队列层按会话并发策略调度（`core/queue.py`）
4. Trigger + Router 判断“是否处理、怎么处理”
5. Self-check 做本地风控和一致性兜底
6. Agent/Tool 执行工具并返回结构化结果
7. Engine 统一输出文本/图片/语音/视频

## 2. 为什么要有 Self-check

Router 结果来自模型，速度快但有误接话风险。  
Self-check 是本地规则兜底，目标是“减少群聊乱回”。

典型拦截：

- 非指向群聊、低置信度、无 listen_probe 的插话
- @别人但没@机器人
- 工具型请求却只走 `reply`（避免“会说不会做”）

## 3. 配置模板机制

全局模板：`config/templates/master.template.yml`  
加载与合并：`core/config_templates.py`

设计目标：

- 任何缺失字段都有默认值
- 升级后旧配置可自愈补齐
- WebUI 与运行时共享同一配置结构

## 4. 插件模板化配置

插件配置路径：`plugins/config/*.yml`  
WebUI 插件页会读取插件 `config_schema`/`args_schema` 渲染字段。

这样做的好处：

- 每个插件一份小配置，不会堆成超大单页
- 可以按插件分权限、分责任维护
- 便于版本控制和回滚

## 5. 音乐链路原则

音乐流程建议：

1. 先 `music_search`
2. 再 `music_play_by_id`
3. 失败时按策略回退（例如 B 站音频提取）

稳定性关键：

- `artist_guard_enable=true`
- 谨慎维护 `unblock_sources`
- 避免把非解锁源混入解锁 source 列表

## 6. 运行模式设计

`main.py` 里做了分层启动：

- 正常模式：直接跑 Bot
- 首次配置模式：缺配置时进入 setup
- 强制 CLI setup：`--setup` / `setup`
- WebUI 未构建时自动回退 CLI setup

这样可以保证“新机器也能启动，不会卡死在半配置状态”。
