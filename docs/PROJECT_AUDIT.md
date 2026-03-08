# YuKiKo 本地全量审计报告（代码级全量，P0 稳定性优先）

- 审计日期: 2026-03-02
- 审计范围: `core/*`, `app.py`, `main.py`, `services/*`, `plugins/*`, `webui/*`, `storage/logs/yukiko.log`
- 审计方式: 本地只读检查 + 三轮重复检测（不依赖真实平台登录态）

## 1. 结论摘要

当前代码并非“全部没问题”。核心能力完整，但稳定性链路仍有高风险历史行为与配置漂移问题，尤其集中在:

1. 群聊非 `@` 响应（历史日志可复现）
2. 同会话 inflight 堆积与 TTL 过期混杂
3. 队列状态语义在历史日志与当前源码间不一致
4. `get_user_info` 参数来源优先级错误导致错误 QQ 号
5. `knowledge_auto_update` 在异常会话中持续写入，存在污染风险

当前源码已经有部分修复（如 matcher 硬门禁、单会话锁、工具参数强校验），但仍需统一策略与补齐回归网。

## 2. 三轮重复检测结果

检测命令（每轮相同）:

- `python -m pytest -q`
- `python -m compileall -q core services plugins utils app.py main.py`
- `npm run build` (in `webui/`)

结果:

| 轮次 | pytest | compileall | webui build | 结论 |
|---|---|---|---|---|
| Round 1 | exit=5 (`no tests ran`) + `pytest_asyncio` 配置警告 | exit=0 | exit=0 + chunk > 500kB 告警 | 一致 |
| Round 2 | exit=5 (`no tests ran`) + `pytest_asyncio` 配置警告 | exit=0 | exit=0 + chunk > 500kB 告警 | 一致 |
| Round 3 | exit=5 (`no tests ran`) + `pytest_asyncio` 配置警告 | exit=0 | exit=0 + chunk > 500kB 告警 | 一致 |

稳定结论:

- 无自动化测试用例（0 tests）
- 代码可编译
- WebUI 构建可用但前端包体偏大

## 3. 功能全景盘点

### 3.1 架构主链路

- Matcher 接入: `app.py`
- 队列调度: `core/queue.py`
- 引擎编排: `core/engine.py`
- Agent 循环: `core/agent.py`
- 工具执行: `core/agent_tools.py` + `core/tools.py`
- 记忆与知识: `core/memory.py`, `core/knowledge.py`, `core/knowledge_updater.py`
- 安全/触发/路由: `core/safety.py`, `core/trigger.py`, `core/router.py`

### 3.2 能力规模（本地实例化统计）

- Agent 工具总数: `93`
- 工具分类:
  - `napcat`: 72
  - `search`: 11
  - `media`: 6
  - `admin`: 2
  - `general`: 2
- 插件加载: `connect_cli`, `example`
- 核心模块数: `core/*.py` 共 35 个

### 3.3 功能分组

- 对话与触发: @触发、昵称触发、follow-up、去重、分段拼接
- Agent/工具: 群管理、用户信息、搜索、图片/视频解析、音频、爬虫、插件桥接
- 多媒体: 图像识别、视频解析/分析、语音发送、媒体段记忆
- 管理与安全: 超管命令、安全分级、敏感词、限速与拥塞提示
- 配置与运维: Setup 向导、热重载、WebUI 管理面板

## 4. P0 缺陷清单（证据 + 根因 + 修复点）

### P0-1 群聊非 @ 触发仍出现（历史窗口可复现）

证据:

- 日志统计: `to_me=false` 后 30s 内同用户出现 `agent_done` 事件 `142` 次（历史窗口）
- 样例:
  - `2026-03-01 06:31:41 qq_recv ... to_me=false ... user=***REMOVED***`
  - `2026-03-01 06:31:46 agent_done ... 用户=***REMOVED***`

当前代码现状:

- Matcher 最外层硬门禁存在:
  - `app.py:423-429`
  - `app.py:461-463`
- 配置冲突告警存在:
  - `app.py:338-344` (`trigger_guard_override`)

根因判定:

- 历史阶段存在配置/版本漂移，触发链路曾允许未 @ 消息进入 Agent。
- 当前代码虽已加硬门禁，但仍需要“生效配置可观测性 + 回归测试”防止回退。

修复点（执行位）:

- `app.py/register_handlers`：启动时打印“有效触发策略快照”（`allow_non_to_me`、`undirected_policy`、`ai_listen_enable`）
- 增加针对非 @ 消息的回归用例，防止后续改动破坏硬门禁

---

### P0-2 同会话 inflight 堆积，触发 TTL 过期与回复排队

证据:

- `agent_inflight_wait`: `35` 次
- `queue_final pending>5`: `34` 次
- `queue_final status=expired`: `21` 次
- 样例:
  - `2026-03-02 07:27:45 queue_final ... pending=7`
  - `2026-03-02 07:28:35 queue_final ... status=expired ... reason=message_ttl_expired`

当前代码现状:

- 会话级锁:
  - `core/engine.py:1568-1582`
- 队列支持单 inflight + cancel previous:
  - `core/queue.py:49-56`
  - `core/queue.py:111-116`
  - `core/queue.py:165-205`

根因判定:

- 历史日志显示 `queue_init` 曾长期为 `group_concurrency=2 | policy=strict_order`，与当前实现不一致。
- 高并发输入 + 较长工具耗时导致 backlog，TTL 到期后出现过期和发送并存。

修复点（执行位）:

- 强制 `single_inflight_per_conversation=true` + `cancel_previous_on_new=true` 的配置守卫
- 增加 inflight 健康指标日志（等待时长、队列深度分位）
- 对会话积压达到阈值时启用早期拒绝或摘要回复策略

---

### P0-3 队列状态语义漂移（`sent/expired` vs `finished/cancelled`）

证据:

- 历史日志大量 `queue_final status=sent|expired`
- 当前源码状态机注释与实现:
  - `_QueueItem.state`: `pending/running/finished/cancelled` (`core/queue.py:28`)
  - `_run_item` 仅产生 `finished/cancelled` (`core/queue.py:220+`)
- `app.py` 会原样输出 `dispatch.status`:
  - `app.py:923-934`

根因判定:

- 代码与日志来自不同版本阶段，运维与排障语义不统一。

修复点（执行位）:

- 统一状态口径到 `finished/cancelled`
- 历史 `sent/expired` 仅做日志兼容映射，不允许进入业务判断

---

### P0-4 `get_user_info` 参数来源优先级错误（截断 ID）

证据:

- 日志命中:
  - `tool=get_user_info | args={"user_id": 13666641}`（短位 QQ）
- 统计: `get_user_info_short_id=4`

当前代码现状:

- Agent 参数兜底:
  - `core/agent.py:619-620`
- QQ 提取顺序为“正文数字优先”:
  - `core/agent.py:666-688`
- 工具层确有严格 QQ 校验:
  - `core/agent_tools.py:65`
  - `core/agent_tools.py:257-261`

根因判定:

- 错误不在“是否校验”，而在“校验前拿错了候选值”。
- 8 位 QQ 在当前校验正则下是合法值，无法靠长度规则单独拦截。

修复点（执行位）:

- `core/agent.py/_extract_candidate_qq_id`:
  - 改为 `@目标` > `reply目标` > 正文数字
  - 正文含多个数字时，优先离语义词最近的候选（如“QQ/号/用户”附近）
- `core/agent_tools.py`:
  - 增加“来源一致性校验”（`@目标` 与 `user_id` 冲突则拒绝执行）

---

### P0-5 知识库自动写入仍有污染风险

证据:

- `knowledge_auto_update` 日志总计: `16`
- 异常会话期间仍持续 `inserted=1`

当前代码现状:

- Updater 已有明确防线:
  - 显式事实门控: `core/knowledge_updater.py:80, 128-130`
  - 推测过滤: `core/knowledge_updater.py:81, 140-143`
  - 工具回显过滤: `core/knowledge_updater.py:82, 125-127`
- 触发点在 `_after_reply`:
  - `core/engine.py:3530-3541`

根因判定:

- 当前 `knowledge_learning=aggressive` 默认偏激进。
- `source_text` 来自 `user_text/message.text`，在复杂会话中仍可能把噪声当“显式事实”。

修复点（执行位）:

- `core/engine.py/_after_reply`: 增加只读写“用户原始明确声明句”的前置裁剪
- `core/knowledge_updater.py`: 收窄 `_EXPLICIT_FACT_CUES`，禁止“泛化 possessive 语句”直接入库
- 增加 `source_type=user_explicit_fact` 强约束与审计字段

## 5. P1 / P2 优化项

### P1

1. 自动化回归网缺失（`pytest` 0 tests）
2. 触发策略缺少端到端回归用例（非 @ 不触发、reply 场景、配置冲突）
3. 队列与 Agent 缺少统一健康指标（inflight wait、pending 分位、取消率）
4. 日志编码偶发异常字符（影响排障可读性）

### P2

1. WebUI 构建主 chunk 体积过大（已出现 Vite warning）
2. 告警噪音偏大（`qq_data_root_not_found`、`cookie_expired` 等）压制关键异常
3. 配置热重载行为缺少变更审计与差异快照

## 6. 回归验证矩阵（必须通过）

| 场景 | 输入 | 预期 |
|---|---|---|
| 非 @ 压测 | 群内连续 50 条非 @ 文本 | 0 条机器人回复 |
| 单会话并发 | 同会话 20 条密集消息 | 任一时刻仅 1 active inflight；旧任务被取消或排队，不并发跑 Agent |
| TTL 语义 | 人工构造超时消息 | 仅出现 `cancelled(message_ttl_expired)`，不再进入 send |
| 错误 ID 保护 | `13666641 + @***REMOVED***` 混合句 | `get_user_info` 取 `@` 目标；冲突时拒绝执行 |
| 知识库防污染 | 推测句/工具回显句 | 不触发入库 |
| 稳定性复检 | 重复三轮检查 | 结果一致，关键异常下降 |

建议执行命令（最小集）:

```powershell
python -m pytest -q
python -m compileall -q core services plugins utils app.py main.py
npm run build
```

## 7. 关键代码定位

- 触发硬门禁与冲突告警:
  - `app.py:334-344`
  - `app.py:423-463`
- 队列状态与取消策略:
  - `core/queue.py:28`
  - `core/queue.py:49-56`
  - `core/queue.py:111-116`
  - `core/queue.py:165-205`
- Agent 会话锁与 inflight 等待:
  - `core/engine.py:1568-1582`
- `knowledge_auto_update` 调用点:
  - `core/engine.py:3530-3541`
- 工具参数强校验:
  - `core/agent_tools.py:65-68`
  - `core/agent_tools.py:223-307`
- `get_user_info` 参数兜底来源:
  - `core/agent.py:619-620`
  - `core/agent.py:666-688`
- Setup 默认保守配置:
  - `core/setup.py:338-382`

## 8. 本轮假设与边界

- 仅做代码级全量审计，不做真实账号端到端联调。
- 历史日志用于复现证据，不能直接等同当前源码行为。
- 当前仓库存在未提交改动，本报告只做增量审计，不覆盖你已有变更。

## 9. 下一步执行顺序（建议）

1. 先做 P0-4（`get_user_info` 参数来源修复）和 P0-3（状态语义统一）
2. 再做 P0-2（inflight/backpressure 指标和阈值策略）
3. 最后做 P0-5（knowledge 写入收紧）并补齐回归测试

