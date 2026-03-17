# YuKiKo 接管改进方案（安全加固 + Agent 架构演进）

更新时间：2026-03-17

## 1. 这次已经落地的改动

### 1.1 `self_learning` 已改成“安全默认值”

已完成：

1. 默认禁用插件，不再出现“配置缺失时自动启用”的风险。
2. 非交互环境下跳过首次配置向导，不再在无人值守启动时误打开危险功能。
3. `create_skill` / `test_in_sandbox` 改为：
   - 需要显式打开 `allow_code_execution`
   - 需要显式确认 `acknowledge_unsafe_execution`
   - 默认仅 `super_admin` 可用
4. 所谓 `isolated` 现在被明确标注为“最佳努力受限运行”，不是强隔离沙盒。
5. 代码测试前新增真实拦截：
   - AST 级别拦截危险导入
   - 拦截 `open/eval/exec/__import__/getattr/setattr` 等高风险调用
   - 拦截路径逃逸片段（如 `../`、`..\\`、系统路径片段）
6. 代码执行目录改为沙盒子目录临时运行，不再直接把脚本丢到普通临时路径。

### 1.2 WebUI 鉴权链路已收口

已完成：

1. 后端不再接受 URL query token 作为图片接口/日志 WS 鉴权方式。
2. `/auth` 登录成功后，服务端会下发同源 `HttpOnly` cookie。
3. WebUI 后续请求同时支持：
   - `Authorization: Bearer ...`
   - 同源 `HttpOnly cookie`
4. 新增 `/auth/logout`，用于显式清理会话 cookie。
5. `/status` 已改为必须鉴权，避免未登录泄露运行状态、模型、插件列表等信息。
6. 前端已移除：
   - 日志 WebSocket URL 上的 token query
   - 聊天图片代理 URL 上的 token query
7. 前端进入已登录区域时会先自动补 session cookie，再渲染页面，避免图片/WS 首屏失效。

### 1.3 安全尺度与 NSFW 规则已开始 WebUI 化

已完成：

1. `image_gen` 已补齐生成前提示词审查配置：
   - `prompt_review_enable`
   - `prompt_review_fail_closed`
   - `prompt_review_model`
   - `prompt_review_max_tokens`
2. 图片生成新增 WebUI 可配项：
   - `image_gen.custom_block_terms`
   - `image_gen.custom_allow_terms`
3. 通用安全新增 WebUI 可配项：
   - `safety.custom_block_terms`
   - `safety.custom_allow_terms`
   - `safety.group_profiles`
   - `safety.output_sensitive_words`
4. 输出敏感词替换不再只能写死在代码里，可以直接在配置里维护。

### 1.4 可复现性已先收住

已完成：

1. `requirements.txt` 已改为固定版本。
2. 固定版本基于当前通过测试的环境版本，优先保证“当前仓库 + 当前测试集”可复现。

### 1.5 回归验证已补齐

已新增/更新：

- `tests/test_self_learning_plugin.py`
- `tests/test_webui_auth_regression.py`
- `tests/test_image_nsfw_guard_regression.py`
- `tests/test_safety_profile_regression.py`
- `scripts/project_takeover_selfcheck.py`

当前验证结果：

- `python -m pytest -q` 通过
- `python scripts/project_takeover_selfcheck.py` 通过
- `npm run build` 通过

### 1.6 结构与执行边界又往前推进了一步

这轮继续落地：

1. `core/webui.py` 已开始从“全量内联路由”改成“装配层 + 子路由模块”：
   - `core/webui_route_context.py`
   - `core/webui_auth_routes.py`
   - `core/webui_log_routes.py`
   - `core/webui_cookie_routes.py`
   - `core/webui_setup_support.py`
2. 这意味着 `health/auth/status/logs/cookies` 这些最敏感、最容易膨胀的 WebUI 路由，已经有了清晰边界。
3. `setup` 模式也已从主文件中整体迁出，`core/webui.py` 从 `6007` 行降到 `4599` 行。
4. `self_learning` 已新增执行后端抽象：
   - `plugins/self_learning_runtime.py`
   - 配置项 `execution_backend`
5. 当前已实现的后端只有：
   - `disabled`
   - `local_subprocess`
6. 这一步的意义不是“把本地 subprocess 包装成真沙盒”，而是：
   - 先把运行时边界抽象出来
   - 后续再接 Docker / Remote Runtime / Sandbox Service 时，不必再次改穿插件主逻辑

---

## 2. 现在还没有彻底解决的问题

### 2.1 `self_learning` 仍然不是真沙盒

这次做的是“默认关闭 + 明确风险 + 受限执行 + 权限收口”，不是“发明了一个真正安全的跨平台 Python 沙盒”。

原因很直接：

1. 纯 Python 子进程 + 工作目录切换，不是安全边界。
2. Windows / macOS / Linux 三端要做到同等级强隔离，通常要依赖外部运行时：
   - Docker / 容器
   - 虚拟机
   - 远程代码执行 runtime
   - 专门的 sandbox service

所以后续正确方向不是继续“把当前 subprocess 包装得更像沙盒”，而是把它替换成独立执行运行时。

### 2.2 大文件/长函数问题还在

这次没有直接把 `core/engine.py`、`core/webui.py`、`core/agent_tools.py` 等超大文件一次性拆掉。

原因：

1. 这类重构属于中高风险工程，不适合和安全修复混在同一批次硬改。
2. 先把 P0/P1 风险关掉，再做模块边界拆分，回归面更稳。

所以“维护复杂度过高”这件事，这次是给出迁移路径与切分方案，不是假装一口气重写完。

---

## 3. 联网学习后的官方参考（推荐吸收方式）

下面这些链接已在 2026-03-17 重新核对过，优先只采用官方文档 / 官方仓库。

另外有一个很重要的新变化：

- LangChain 官方现在把“快速上手自带能力更全的 Agent”更多引导到 `LangChain / Deep Agents`
- 当你需要**状态图、强定制、持久化、HITL、确定性 + Agent 混编**时，仍然推荐直接上 `LangGraph`

这和你这个项目的现状非常贴合：群聊主回路需要轻量；复杂任务流、审批流、长期状态则需要图式编排。

下面只列官方文档/官方仓库，适合作为你后面重构 Agent 编排时的主参考：

### 3.1 LangGraph

官方资料：

- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts / Human-in-the-loop](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop)
- [LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph)
- [LangChain Overview（官方说明何时选 LangChain / LangGraph / Deep Agents）](https://docs.langchain.com/oss/python/langchain/overview)

值得学的点：

1. **State Graph**：把 Agent 回合拆成显式节点与边，不再全部塞进一个超长 `run()`。
2. **Persistence**：中途状态可持久化，崩了还能恢复，不必整轮重跑。
3. **Interrupt / HITL**：高风险动作可暂停，等人审核后再继续。
4. **Durable Execution**：比“函数递归套工具调用”更适合长链任务。

对 YuKiKo 的直接启发：

- `core/agent.py` 适合改造成“状态图 + checkpoint”的执行器。
- `agent.high_risk_control` 非常适合改造成 LangGraph 风格的 interrupt 点。

### 3.2 CrewAI

官方资料：

- [CrewAI Docs](https://docs.crewai.com/)
- [CrewAI Crews](https://docs.crewai.com/concepts/crews)
- [CrewAI Flows](https://docs.crewai.com/concepts/flows)
- [CrewAI Human-in-the-Loop](https://docs.crewai.com/en/learn/human-in-the-loop)
- [CrewAI GitHub](https://github.com/crewAIInc/crewAI)

值得学的点：

1. **Crew**：角色分工清晰，适合 Planner / Researcher / Executor / Reviewer 协作。
2. **Flow**：流程可控，适合生产型任务，不会像自由 Agent 一样无限漂移。
3. 官方文档已经明确把 **state / persist / resume / HITL** 作为 Flow 的一等能力来讲。
4. **“灵活 + 可控”并存**：很适合你现在这种既想保留 Agent 灵活性，又不想把整套系统写成不可维护黑盒的项目。

对 YuKiKo 的直接启发：

- 搜索、学习、改配置、代码生成、审查这类链路很适合拆成 Flow。
- 群聊即时回复则继续保留轻量 Agent，不必全量 Flow 化。

### 3.3 AutoGen

官方资料：

- [AutoGen 官方文档](https://microsoft.github.io/autogen/stable/)
- [AutoGen AgentChat](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/index.html)
- [AutoGen Core](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html)
- [AutoGen Runtime Architecture](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/architecture.html)
- [AutoGen GitHub](https://github.com/microsoft/autogen)

值得学的点：

1. **多 Agent 对话编排**成熟。
2. **Core / AgentChat 分层**很清楚：想要高抽象可用 AgentChat，要分布式/runtime 级控制就下沉到 Core。
3. 官方已经把 **standalone runtime / distributed runtime** 讲清楚了，这对你后面做跨进程运行时非常有参考价值。
4. 对“研究员-执行器-审查员”类模式支持自然。

对 YuKiKo 的直接启发：

- 可以借鉴它的“多角色 but 明确停止条件”思路，避免 Agent 自转。

### 3.4 OpenHands

官方资料：

- [OpenHands Docs](https://docs.openhands.dev/openhands/)
- [OpenHands Runtime Overview](https://docs.openhands.dev/openhands/usage/runtimes)
- [OpenHands Runtime Architecture](https://docs.openhands.dev/openhands/usage/architecture/runtime)
- [OpenHands GitHub](https://github.com/OpenHands/OpenHands)

值得学的点：

1. **把代码执行 runtime 当成独立层**，不是塞进主 Agent 进程里糊弄。
2. 官方已经明确区分：
   - Docker Runtime
   - Remote Runtime
   - Local Runtime
3. 运行时与 Agent 层分离后，安全、恢复、审计都更清楚。
4. 官方 runtime 文档本身就强调不同 runtime 的隔离级别差异，这对 `self_learning` 是非常重要的参考。

对 YuKiKo 的直接启发：

- 未来要做真正可用的“自学习/自修改”能力，应该把执行放到外部 runtime，不应该继续在主进程旁边 `subprocess` 假装沙盒。

### 3.5 Letta

官方资料：

- [Letta Docs](https://docs.letta.com/)
- [Stateful Agents](https://docs.letta.com/guides/core-concepts/stateful-agents/)
- [Memory Overview](https://docs.letta.com/guides/agents/memory)
- [Letta GitHub](https://github.com/letta-ai/letta)

值得学的点：

1. **Stateful agent / memory-first** 思路明确。
2. 官方把 agent 状态拆成：system prompt、memory blocks、messages、tools，这非常适合做 YuKiKo 的状态对象建模。
3. 更强调长期状态与记忆管理，而不是单轮 prompt 技巧。

### 3.6 现在最值得直接吸收的“共同模式”

看完这些官方资料后，最值得直接落到 YuKiKo 里的不是“照搬某个框架”，而是下面 5 件事：

1. **执行器与运行时分层**  
   参考 OpenHands / AutoGen Runtime，把代码执行从主 Agent 里剥离。
2. **状态对象先行**  
   参考 LangGraph / Letta，把当前散落在 `agent/engine/memory` 的状态合成显式状态对象。
3. **任务流显式节点化**  
   参考 LangGraph / AutoGen GraphFlow / CrewAI Flow，把高风险链路做成有开始、暂停、恢复、结束的流程。
4. **HITL 变成一等能力**  
   参考 LangGraph interrupt 与 CrewAI Flow HITL，把人工审核做成可恢复的暂停点，而不是零散 if-else。
5. **群聊 Agent 与生产 Flow 分层**  
   群聊继续轻量，复杂任务走持久化 Flow；不要强行让一个超级 Agent 兼顾一切。

对 YuKiKo 的直接启发：

- 你的项目已经有 memory / knowledge / affinity 体系，后续非常适合往“显式状态对象”而不是“零散 if-else”方向走。

---

## 4. 推荐的下一阶段重构路线

## Phase A：先把执行边界做对

目标：把“危险代码执行”从主项目里剥离。

建议：

1. 新建 `core/execution_runtime/` 抽象层。
2. 定义统一接口：
   - `submit_code_run()`
   - `get_run_status()`
   - `cancel_run()`
   - `collect_artifacts()`
3. 本地模式只保留 `trusted_local`，明确写进文档：仅开发自测。
4. 真正默认推荐：
   - Docker runtime
   - 远程 sandbox runtime

## Phase B：把 Agent 主回路状态图化

目标：降低 `core/agent.py` / `core/engine.py` 的超长函数风险。

建议节点：

1. `ingest_message`
2. `classify_context`
3. `resolve_permission`
4. `plan_or_reply`
5. `tool_execution`
6. `review_high_risk`
7. `finalize_output`
8. `persist_memory`

每个节点只做一类事，状态对象统一传递。

## Phase C：把“自由 Agent”与“生产 Flow”分层

建议分成两类：

1. **Chat Agent**  
   负责群聊即时响应，保持轻量、低延迟。
2. **Task Flow**  
   负责：
   - 搜索整理
   - 学习总结
   - 配置修改
   - 代码生成
   - 数据维护

这一步最适合借鉴 CrewAI Flow 思路。

## Phase D：把 HITL 做进 WebUI

建议在 WebUI 新增：

1. 待审核高风险动作列表
2. 运行中的 Agent 图/步骤流
3. checkpoint 恢复
4. 执行日志 / 工具轨迹

这一步最适合借鉴 LangGraph 的 interrupt / persistence 思路。

---

## 5. 建议的文件拆分方案

### 5.1 `core/webui.py`

建议拆成：

- `core/webui/auth.py`
- `core/webui/status.py`
- `core/webui/config.py`
- `core/webui/chat.py`
- `core/webui/logs_ws.py`
- `core/webui/database.py`
- `core/webui/system.py`
- `core/webui/cookies.py`

### 5.2 `core/engine.py`

建议拆成：

- `core/engine/plugin_registry.py`
- `core/engine/message_pipeline.py`
- `core/engine/runtime_state.py`
- `core/engine/config_reload.py`
- `core/engine/dispatch.py`

### 5.3 `core/agent.py`

建议拆成：

- `core/agent/state.py`
- `core/agent/planner.py`
- `core/agent/executor.py`
- `core/agent/tool_policy.py`
- `core/agent/render.py`
- `core/agent/checkpoint.py`

### 5.4 `core/agent_tools.py`

建议拆成：

- `core/agent_tools/admin.py`
- `core/agent_tools/media.py`
- `core/agent_tools/search.py`
- `core/agent_tools/memory.py`
- `core/agent_tools/runtime.py`

---

## 6. 对“更多高度自定义 AI 尺度 / AI 底线”的建议

你想要的不是单纯“放宽”或“更严”，而是**可配置、分层、可解释**。

推荐最终形成三层：

### 第 1 层：绝对红线（代码内置，不开放关闭）

例如：

- 未成年人色情
- 明确违法侵害
- 真实暴恐教程
- 高危自伤实施指导

这层不要开放关闭。

### 第 2 层：项目级策略（WebUI 可调）

例如：

- `safety.profile`
- `safety.scale`
- `safety.custom_block_terms`
- `safety.custom_allow_terms`
- `safety.output_sensitive_words`
- `image_gen.prompt_review_*`
- `image_gen.custom_block_terms`
- `image_gen.custom_allow_terms`

这层就是你要的“高度自定义 AI 尺度 / AI 底线”。

### 第 3 层：群级策略（WebUI 可覆盖）

例如：

- `safety.group_profiles`
- `output.group_overrides`
- `output.group_style_overrides`

这样不同群可以用不同尺度，而不是全局一刀切。

---

## 7. 结论

这次接管的策略是：

1. **先关掉真实风险**：任意 Python 执行、query token、未鉴权状态接口。
2. **再补可配能力**：NSFW / safety / 输出敏感词尽量 WebUI 化。
3. **最后给出长期路线**：往 LangGraph 的状态图 + 持久化 + HITL，和 CrewAI 的角色分工 + Flow 控制靠拢。

如果后面继续推进，我建议优先顺序是：

1. `self_learning` 外部 runtime 化
2. `core/webui.py` 模块拆分
3. `core/agent.py` 状态图化
4. WebUI 增加 HITL 审核面板
