# AIBOT 缺陷清单与维护方案（基于 2026-03-06 群聊日志）

## 目标
把当前系统从“工具很多但不稳定”推进到“可长期群聊值班”的工程状态，按 `P0/P1/P2` 分层治理。

## 已落地（2026-03-07）
- 已修：`final_answer` 媒体链路校验失败时，优先剥离非法媒体而不是直接打回，降低“后续纯文本覆盖前序媒体结果”概率。
- 已修：音乐播放增加试听源识别（`freeTrialInfo/cannotListenReason`），避免把 20~30 秒试听当完整播放成功。
- 已修：`search_related` 在指定 `user_id` 时对 `user/assistant` 统一做用户域过滤，降低跨用户记忆污染。
- 已修：群聊上下文构建优先当前用户上下文；`@bot` 场景默认不再注入全群短期缓存。
- 已修：Agent/Thinking Prompt 增加系统时间锚点，降低“今天日期/年份”漂移。
- 已修：高风险控制代码默认值增强（默认需要二次确认 + 默认确认/取消口令 + 高风险名称匹配补强）。
- 已修：工具参数别名自动修复（例如 `keyword/name -> query`），减少 `tool_args_unknown_dropped`。

## 日志证据（关键样本）
| 现象 | 证据 |
|---|---|
| 高噪声群聊 + 非定向触发 | `log.txt:15` `allow_non_to_me=True` `delegate_undirected_to_ai=True` |
| 队列并发+不取消旧任务 | `log.txt:14` `single_inflight=False` `cancel_previous=False` |
| 工具参数漂移 | `log.txt:55` `log.txt:74` `tool_args_unknown_dropped` |
| 同一 trace 多次 final_answer（媒体丢失） | `log.txt:89` + `log.txt:92`，最终 `log.txt:95 has_image=False` |
| 跨用户记忆污染 | `log.txt:2177`（对用户 2956409445 回答“按你之前教我的…”） |
| 安全基线过低 | `log.txt:4` `SafetyEngine scale=0` |

## P0（必须先修，先保命）
### 1) 跨用户记忆污染
- 风险：用户私有记忆泄漏到其他用户，直接破坏可信度。
- 改造点：
  - `core/engine.py:1077-1136`：构建 `memory_context/related_memories` 时，群聊强制“用户域优先”。
  - `core/memory.py:984+`：`search_related` 增加严格模式，群聊默认只召回当前 `user_id` 的 user/assistant 片段（除非显式共享）。
  - `core/engine.py:5649+`：强化 `_guard_unverified_memory_claims`，对“你之前说过/教过”语句做 owner 校验。
- 验收标准：
  - 回放含多用户梗记忆的日志，A 用户记忆不再出现在 B 用户回答中。

### 2) 安全边界（作弊/隐私/性内容）不稳
- 风险：违规信息输出、群内事故。
- 改造点：
  - `core/safety.py`：增加硬拒绝分类（外挂绕过/人肉隐私/露骨性内容）。
  - `config/prompts.yml` + `core/config_templates.py`：拒绝策略从“软拒绝”改“拒绝+转安全替代建议”。
  - 运行配置：`safety.scale >= 2`（当前日志是 0）。
- 验收标准：
  - 红队提示词回放（cheat/隐私画像/性暗示）全部稳定拒绝，且不输出路线图细节。

### 3) 高风险动作缺少二次确认
- 风险：误触执行全员禁言/上传可执行文件/高敏管理动作。
- 改造点：
  - `core/agent.py:296+` 高风险确认：默认 `default_require_confirmation=true`。
  - `core/config_templates.py:245+`：把默认配置改为二次确认开启。
  - `core/agent_tools.py`：将 `set_group_whole_ban`、高风险下载上传等纳入高风险分类。
- 验收标准：
  - 群主触发高危动作时，首轮只返回确认提示；明确确认后才执行。

### 4) 编排收口不一致（媒体被覆盖/乱序）
- 风险：用户感知“发图丢图”“回包错位”。
- 改造点：
  - `core/agent.py:658+`：final_answer 收口状态机，单 trace 只允许一次“可见收口”。
  - `core/agent.py:719+`：媒体链路校验失败时，不允许退化成无媒体成功；需要明确降级原因。
  - `core/queue.py` + `app.py:1647+`：旧任务过期时降权或丢弃，避免迟到回复污染当前上下文。
- 验收标准：
  - 含媒体任务回放中，不再出现“前条带图、后条文本覆盖导致 has_image=False”。

### 5) 时间源错误（日期错年）
- 风险：事实错误，降低可信度。
- 改造点：
  - `core/thinking.py` 与 `core/agent.py` 提示词拼装处注入系统时间（含日期与时区）。
  - `core/engine.py` 回复后处理增加日期一致性守卫（识别“今天/明天/昨天”与系统时间冲突）。
- 验收标准：
  - “今天是几号”类问题在日志回放中与系统时间一致。

## P1（很快会出事）
### 1) 引用上下文绑定不稳定
- 改造点：`core/agent.py` reply anchor 规则 + `core/tools.py` 最近媒体候选缓存，统一按 `group+user+reply_to_message_id` 绑定。

### 2) 工具参数 repair 不足
- 改造点：`core/agent_tools.py:_sanitize_and_validate_args` 增加字段别名映射与自动修复统计。

### 3) 搜索失败时硬总结
- 改造点：`core/engine.py` 在 `search` 分支加入“证据不足降级模板”，缺来源时输出不确定而非结论。

### 4) 超时任务取消传播不彻底
- 改造点：`core/queue.py` 增加取消令牌向子任务传递；`app.py` 对过期任务发送“已过期”占位而非旧结果。

### 5) 动图识别质量不稳
- 改造点：`core/tools.py` GIF 多帧抽样 + 置信度阈值；低置信度禁止写入“学习记忆”。

## P2（体验优化）
### 1) 人设口癖过重
- 改造点：`config/prompts.yml` + `core/personality.py` 增加重复惩罚和用户负反馈收敛。

### 2) 能力自述不 grounded
- 改造点：工具列表类问题必须读取 `AgentToolRegistry` 实际注册结果，不允许模型脑补。

### 3) 记忆学习缺少复核
- 改造点：`core/knowledge_updater.py` 增加学习置信度阈值 + “可撤销记录”。

## 执行顺序（建议）
### Sprint A（1-2 天）
- 完成 P0 的 2/3/4（安全边界、高危确认、收口一致性）。

### Sprint B（2-3 天）
- 完成 P0 的 1/5（记忆隔离、时间源一致性）。

### Sprint C（3-5 天）
- 处理 P1 并补压测回放脚本。

## 最小压测回放集（必须长期保留）
- 多用户交叉记忆用例：A 教梗，B 追问同题。
- 媒体收口用例：工具发图 + final_answer 再次收口。
- 高危动作用例：全员禁言、踢人、可执行文件上传。
- 时间事实用例：今天/明天/昨天、年份边界。

## 维护原则
- 先修“会出事故”的，再修“看起来聪明”的。
- 所有策略都要可回放、可度量、可回滚。
